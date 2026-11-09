#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云尖3D星：从 UTC 宽表流式取数、去重、分分钟评估并生成独立报告。"""
import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import ch_http
from gen_charts import line_chart, PALETTE

BASE = Path(__file__).resolve().parent
PROFILE = BASE / 'satellites' / 'YJ-3D'
BEIJING = ZoneInfo('Asia/Shanghai')
UTC = timezone.utc
FIELDS = ('bus_voltage', 'bus_current', 'battery_voltage',
          'battery_current', 'solar_current')
POWER_FIELDS = ('solar_bus_proxy_W', 'bus_measure_W',
                'battery_signed_trial_W', 'residual_diag_W')
PHASES = ('日照', '地影', '未判定')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_profile(root=PROFILE):
    root = Path(root)
    cfg = json.loads((root / 'config' / 'telemetry_params.json').read_text(encoding='utf-8'))
    design = json.loads((root / 'config' / 'assessment_params.json').read_text(encoding='utf-8'))
    if (cfg['source_database'], cfg['source_table'], cfg['source_time_column']) != (
            'chdb_3D', 'yc_history', 'timestamp'):
        raise ValueError('云尖3D来源必须为已核对的 chdb_3D.yc_history.timestamp')
    codes = [cfg['measurements'][name]['code'] for name in FIELDS]
    codes += [cfg['phase_state']['code'], cfg['discharge_switch']['code']]
    if len(set(codes)) != len(codes) or any(not code.isidentifier() for code in codes):
        raise ValueError('云尖3D测点代号重复或无效')
    return cfg, design


def utc_bounds(start_beijing, end_beijing):
    start = datetime.fromisoformat(start_beijing).replace(tzinfo=BEIJING)
    end = datetime.fromisoformat(end_beijing).replace(tzinfo=BEIJING)
    if start >= end:
        raise ValueError('评估窗口起点必须早于不含终点')
    return start.astimezone(UTC), end.astimezone(UTC)


def iter_source_rows(cfg, start_utc, end_utc, chunk_days=3):
    """仅查询给定 UTC 半开窗口内的 EPS A 路记录；列名来自已校验配置。"""
    if chunk_days < 1:
        raise ValueError('chunk_days 必须为正整数')
    codes = [cfg['measurements'][name]['code'] for name in FIELDS]
    codes += [cfg['phase_state']['code'], cfg['discharge_switch']['code']]
    select = ','.join(codes)
    sql = (f'SELECT timestamp,{select} FROM chdb_3D.yc_history '
           'PREWHERE timestamp >= toDateTime64({start:String},3,\'UTC\') '
           'AND timestamp < toDateTime64({end:String},3,\'UTC\') '
           f'WHERE {codes[0]} IS NOT NULL ORDER BY timestamp')
    cursor = start_utc
    while cursor < end_utc:
        stop = min(cursor + timedelta(days=chunk_days), end_utc)
        params = {'start': cursor.strftime('%Y-%m-%d %H:%M:%S'),
                  'end': stop.strftime('%Y-%m-%d %H:%M:%S')}
        yield from ch_http.query_iter(sql, database='chdb_3D', params=params,
                                      timeout=240, max_execution_time=240)
        cursor = stop


def deduplicate_sorted(rows, codes):
    """同 UTC 时间戳的相同记录只留一份；冲突字段置空并显式计数。"""
    previous = None
    merged = None
    conflicts = set()
    source_count = 0
    for row in rows:
        stamp = row['timestamp']
        if previous is not None and stamp < previous:
            raise ValueError('来源记录未按 UTC 时间排序')
        if stamp != previous:
            if merged is not None:
                merged['_source_count'] = source_count
                merged['_conflicts'] = len(conflicts)
                yield merged
            merged = {'timestamp': stamp, **{code: row.get(code) for code in codes}}
            previous, source_count, conflicts = stamp, 1, set()
            continue
        source_count += 1
        for code in codes:
            if code in conflicts:
                continue
            old, new = merged.get(code), row.get(code)
            if old in (None, ''):
                merged[code] = new
            elif new not in (None, '') and old != new:
                merged[code] = None
                conflicts.add(code)
    if merged is not None:
        merged['_source_count'] = source_count
        merged['_conflicts'] = len(conflicts)
        yield merged


def numeric(raw):
    try:
        number = float(raw)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


class MinuteAccumulator:
    def __init__(self, instant):
        self.instant = instant
        self.n_unique = self.n_source = self.n_conflict = 0
        self.values = {key: [] for key in (*FIELDS, *POWER_FIELDS)}
        self.solar_for_phase = []
        self.states = Counter()
        self.switches = Counter()
        self.within_seconds = 0.0
        self.within_intervals = 0
        self.within_energy = {key: 0.0 for key in POWER_FIELDS}
        self.cross_from_previous = None

    def add(self, values, powers, state, switch, source_count, conflicts,
            valid_solar_current):
        self.n_unique += 1
        self.n_source += source_count
        self.n_conflict += bool(conflicts)
        for key, number in values.items():
            if number is not None:
                self.values[key].append(number)
        for key, number in powers.items():
            if number is not None:
                self.values[key].append(number)
        if valid_solar_current is not None:
            self.solar_for_phase.append(valid_solar_current)
        if state not in (None, ''):
            self.states[str(state)] += 1
        if switch not in (None, ''):
            self.switches[str(switch)] += 1

    def finish(self, cfg, threshold):
        sun, eclipse = cfg['phase_state']['sun'], cfg['phase_state']['eclipse']
        states = set(self.states)
        if states:
            if states == {sun}:
                phase, source = '日照', cfg['phase_state']['code']
            elif states == {eclipse}:
                phase, source = '地影', cfg['phase_state']['code']
            else:
                phase, source = '未判定', '状态切换或异常'
        else:
            solar = self.solar_for_phase
            if solar and min(solar) >= threshold:
                phase, source = '日照', '太阳阵电流回退'
            elif solar and max(solar) < threshold:
                phase, source = '地影', '太阳阵电流回退'
            else:
                phase, source = '未判定', '无明确判据'
        solar = self.solar_for_phase
        conflict = bool(solar and ((phase == '日照' and max(solar) < threshold) or
                                   (phase == '地影' and min(solar) >= threshold)))
        row = {'t': self.instant.strftime('%Y-%m-%d %H:%M:%S'),
               'n_unique_samples': self.n_unique, 'n_source_rows': self.n_source,
               'n_conflicted_timestamps': self.n_conflict,
               'phase': phase, 'phase_source': source, 'phase_conflict': int(conflict),
               'phase_state': next(iter(states)) if len(states) == 1 else
                              '__mixed__' if states else '',
               'discharge_switch': (next(iter(self.switches))
                                    if len(self.switches) == 1 else
                                    '__mixed__' if self.switches else '')}
        for key, numbers in self.values.items():
            row[f'{key}_n'] = len(numbers)
            row[f'{key}_mean'] = sum(numbers) / len(numbers) if numbers else ''
            row[f'{key}_min'] = min(numbers) if numbers else ''
            row[f'{key}_max'] = max(numbers) if numbers else ''
        return row


def minute_columns():
    columns = ['t', 'n_unique_samples', 'n_source_rows',
               'n_conflicted_timestamps', 'phase', 'phase_source',
               'phase_conflict', 'phase_state', 'discharge_switch']
    for key in (*FIELDS, *POWER_FIELDS):
        columns += [f'{key}_{suffix}' for suffix in ('n', 'mean', 'min', 'max')]
    return columns


def assess_rows(rows, cfg, design, on_minute):
    """处理已按 UTC 排序的宽表记录；返回采样与短间隔积分证据。"""
    codes = [cfg['measurements'][name]['code'] for name in FIELDS]
    codes += [cfg['phase_state']['code'], cfg['discharge_switch']['code']]
    deduped = deduplicate_sorted(rows, codes)
    sums = {phase: {'seconds': 0.0, 'intervals': 0,
                    **{key: 0.0 for key in POWER_FIELDS}} for phase in PHASES}
    stats = Counter()
    phase_minutes = Counter()
    source_minutes = Counter()
    first = last = previous = None
    minute = None
    previous_minute_phase = None
    threshold = design['solar_phase_threshold_A']
    max_gap = design['maximum_integration_gap_seconds']

    def add_to_group(phase, seconds, intervals, energies):
        group = sums[phase]
        group['seconds'] += seconds
        group['intervals'] += intervals
        for key in POWER_FIELDS:
            group[key] += energies[key]

    def flush(current, prior_phase):
        record = current.finish(cfg, threshold)
        if current.within_intervals:
            add_to_group(record['phase'], current.within_seconds,
                         current.within_intervals, current.within_energy)
        if current.cross_from_previous is not None and prior_phase is not None:
            seconds, energies = current.cross_from_previous
            phase = record['phase'] if record['phase'] == prior_phase else '未判定'
            add_to_group(phase, seconds, 1, energies)
        on_minute(record)
        phase_minutes[record['phase']] += 1
        source_minutes[record['phase_source']] += 1
        stats['phase_conflict_minutes'] += record['phase_conflict']
        return record['phase']

    for item in deduped:
        instant_utc = datetime.fromisoformat(item['timestamp']).replace(tzinfo=UTC)
        instant = instant_utc.astimezone(BEIJING)
        minute_time = instant.replace(second=0, microsecond=0)
        if minute is None or minute.instant != minute_time:
            if minute is not None:
                previous_minute_phase = flush(minute, previous_minute_phase)
            minute = MinuteAccumulator(minute_time)
        stats['source_rows'] += item['_source_count']
        stats['unique_timestamps'] += 1
        stats['conflicted_timestamps'] += bool(item['_conflicts'])
        first = instant if first is None else first
        last = instant
        values = {}
        valid = {}
        for key in FIELDS:
            raw = numeric(item.get(cfg['measurements'][key]['code']))
            values[key] = raw
            bounds = cfg['measurements'][key]['range']
            valid[key] = raw if raw is not None and bounds[0] <= raw <= bounds[1] else None
            stats[f'{key}_numeric'] += raw is not None
            stats[f'{key}_outside_range'] += raw is not None and valid[key] is None
        state = item.get(cfg['phase_state']['code'])
        switch = item.get(cfg['discharge_switch']['code'])
        bv, bi, batv, bati, si = (valid[key] for key in FIELDS)
        solar = bv * si if bv is not None and si is not None else None
        bus = bv * bi if bv is not None and bi is not None else None
        battery = batv * bati if batv is not None and bati is not None else None
        powers = {'solar_bus_proxy_W': solar, 'bus_measure_W': bus,
                  'battery_signed_trial_W': battery,
                  'residual_diag_W': (solar + battery - bus
                                      if None not in (solar, bus, battery) else None)}
        minute.add(values, powers, state, switch,
                   item['_source_count'], item['_conflicts'], valid['solar_current'])
        if previous is not None:
            gap = (instant_utc - previous[0]).total_seconds()
            if gap > max_gap:
                stats['gaps_over_limit'] += 1
            if 0 < gap <= max_gap and all(
                    powers[key] is not None and previous[1][key] is not None
                    for key in POWER_FIELDS):
                energies = {key: ((powers[key] + previous[1][key]) / 2 * gap /
                                  3_600_000) for key in POWER_FIELDS}
                if previous[2] == minute_time:
                    minute.within_seconds += gap
                    minute.within_intervals += 1
                    for key in POWER_FIELDS:
                        minute.within_energy[key] += energies[key]
                else:
                    minute.cross_from_previous = (gap, energies)
        previous = (instant_utc, powers, minute_time)
    if minute is not None:
        flush(minute, previous_minute_phase)
    if first is None:
        raise ValueError('指定时间窗没有 EPS A 路母线电压记录')
    span_minutes = int((last.replace(second=0, microsecond=0) -
                        first.replace(second=0, microsecond=0)).total_seconds() / 60) + 1
    return {'source_rows': stats['source_rows'],
            'unique_timestamps': stats['unique_timestamps'],
            'duplicate_rows': stats['source_rows'] - stats['unique_timestamps'],
            'conflicted_timestamps': stats['conflicted_timestamps'],
            'first_sample_beijing': first.isoformat(),
            'last_sample_beijing': last.isoformat(),
            'observed_minutes': sum(phase_minutes.values()),
            'span_calendar_minutes': span_minutes,
            'calendar_minute_coverage': sum(phase_minutes.values()) / span_minutes,
            'phase_minutes': dict(phase_minutes),
            'phase_source_minutes': dict(source_minutes),
            'phase_conflict_minutes': stats['phase_conflict_minutes'],
            'numeric_and_range_counts': {key: {'numeric': stats[f'{key}_numeric'],
                                               'outside_workbook_range': stats[f'{key}_outside_range']}
                                         for key in FIELDS},
            'gaps_over_limit': stats['gaps_over_limit'],
            'interval_energy_kWh': sums}


def fetch_and_assess(root=PROFILE, chunk_days=3):
    root = Path(root)
    cfg, design = load_profile(root)
    start_text = design['assessment_start_beijing']
    end_text = design['assessment_end_exclusive_beijing']
    start_utc, end_utc = utc_bounds(start_text, end_text)
    output = root / 'data_power_minute.csv'
    temp = output.with_suffix('.csv.tmp')
    root.mkdir(parents=True, exist_ok=True)
    try:
        with temp.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=minute_columns())
            writer.writeheader()
            result = assess_rows(iter_source_rows(cfg, start_utc, end_utc, chunk_days),
                                 cfg, design, writer.writerow)
        workbook = BASE.parent / cfg['workbook']
        result['provenance'] = {
            'source': 'chdb_3D.yc_history', 'source_time_column': 'timestamp',
            'source_timezone': 'UTC', 'report_timezone': 'Asia/Shanghai',
            'window_start_inclusive_beijing': start_text,
            'window_end_exclusive_beijing': end_text,
            'window_start_inclusive_utc': start_utc.isoformat(),
            'window_end_exclusive_utc': end_utc.isoformat(),
            'selection': 'TMEA003 IS NOT NULL; identical UTC timestamps merged',
            'workbook': cfg['workbook'], 'workbook_sha256': sha256(workbook),
            'telemetry_config_sha256': sha256(root / 'config' / 'telemetry_params.json'),
            'assessment_config_sha256': sha256(root / 'config' / 'assessment_params.json'),
            'script_sha256': sha256(__file__), 'minute_csv_sha256': sha256(temp),
            'fetched_at_utc': datetime.now(UTC).isoformat()}
        summary_temp = root / 'energy_summary.json.tmp'
        summary_temp.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                          allow_nan=False), encoding='utf-8')
        temp.replace(output)
        summary_temp.replace(root / 'energy_summary.json')
        return result
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chunk-days', type=int, default=3)
    args = parser.parse_args()
    summary = fetch_and_assess(chunk_days=args.chunk_days)
    print(json.dumps({key: summary[key] for key in
                      ('source_rows', 'unique_timestamps', 'observed_minutes',
                       'phase_minutes', 'calendar_minute_coverage')}, ensure_ascii=False))
