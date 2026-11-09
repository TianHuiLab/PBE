#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云尖3D宽表同来源行、同分钟的分边界功率对照；不把差额命名为损耗。"""
import argparse
import csv
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from s1_fetch import native_query_iter
from yj3d_assess import PROFILE, load_profile, sha256, utc_bounds
from yj3d_unit_power import upper_limit, voltage_intervals


def finite(alias):
    return f'(isNotNull({alias}) AND isFinite(ifNull({alias},0)))'


def pl_bank_expression(inventory, bank):
    """沿用单机功率统计的工作簿筛选规则，只合计 21 条有电流的 PL 支路。"""
    aliases, predicates, terms = [], [], []
    for index, unit in enumerate(inventory['units']):
        if unit['pl_channel'] == 33:
            continue  # GNSS 备份位于 BCR 包，不能混入 PL A/B 路和。
        spec = unit['banks'][bank]
        if 'current' not in spec:
            continue
        voltage, current = spec['voltage'], spec['current']
        v, i = f'v_{bank}_{index}', f'i_{bank}_{index}'
        aliases.extend((f'toFloat64OrNull({voltage["code"]}) AS {v}',
                        f'toFloat64OrNull({current["code"]}) AS {i}'))
        valid = f'({finite(v)} AND {finite(i)} AND {v}>=0 AND {i}>=0'
        intervals = voltage_intervals(voltage['workbook_range'])
        if intervals:
            valid += ' AND (' + ' OR '.join(
                f'({v}>={low} AND {v}<={high})' for low, high in intervals) + ')'
        current_limit = upper_limit(current['workbook_range'], 'A')
        if current_limit is not None:
            valid += f' AND {i}<={current_limit}'
        valid += ')'
        predicates.append(valid)
        terms.append(f'if({valid},{v}*{i},0)')
    if len(terms) != 21:
        raise ValueError(f'PL {bank} 路可计电流支路数量异常: {len(terms)}')
    return aliases, '(' + ' AND '.join(predicates) + ')', '(' + '+'.join(terms) + ')'


def query_sql(inventory, bcr_config, component_config, database, table):
    if not database.isidentifier() or not table.isidentifier():
        raise ValueError('宽表名称无效')
    aliases = [
        'toFloat64OrNull(TMEA003) AS bv',
        'toFloat64OrNull(TMEA004) AS bi',
        'toFloat64OrNull(TMEA005) AS batv',
        'toFloat64OrNull(TMEA006) AS bati',
        'toFloat64OrNull(TMEA007) AS si',
    ]
    bus_ok = f'({finite("bv")} AND {finite("bi")} AND bv>=21 AND bv<=29.2 AND bi>=0 AND bi<=10)'
    battery_ok = (f'({finite("batv")} AND {finite("bati")} AND '
                  'batv>=21 AND batv<=29.2 AND bati>=-10 AND bati<=10)')
    aliases.extend((f'{bus_ok} AS ok_bus', 'bv*bi AS p_bus',
                    'batv*bati AS p_battery'))
    pair_conditions = {}
    for bank in ('A', 'B'):
        bank_aliases, valid, power = pl_bank_expression(inventory, bank)
        aliases.extend(bank_aliases)
        aliases.extend((f'{valid} AS ok_pl_{bank}', f'{power} AS p_pl_{bank}'))
        pair_conditions[bank] = f'(ok_bus AND ok_pl_{bank})'
    pair_conditions['AB'] = '(ok_bus AND ok_pl_A AND ok_pl_B)'
    heater_valid, heater_terms = [], []
    for index, component in enumerate(component_config['components']):
        code = component['current_code']
        if not component['enabled'] or not code.isidentifier() or not code.startswith('TMEA'):
            raise ValueError('加热支路配置无效')
        current = f'heat_i_{index}'
        aliases.append(f'toFloat64OrNull({code}) AS {current}')
        heater_valid.append(f'({finite(current)} AND {current}>=0 AND {current}<=1)')
        heater_terms.append(current)
    if len(heater_terms) != 10:
        raise ValueError('加热电流支路数量异常')
    aliases.extend((f'({" AND ".join(heater_valid)}) AS ok_heat',
                    f'bv*({"+".join(heater_terms)}) AS p_heat'))
    pair_conditions['HEAT'] = '(ok_bus AND ok_heat)'
    pair_conditions['ABH'] = '(ok_bus AND ok_pl_A AND ok_pl_B AND ok_heat)'
    bcr_valid, bcr_terms = [], []
    for index, channel in enumerate(bcr_config['pv_channels']):
        v, i = f'bvout_{index}', f'biout_{index}'
        for code in (channel['output_voltage'], channel['output_current']):
            if not code.isidentifier():
                raise ValueError('BCR 代号无效')
        aliases.extend((f'toFloat64OrNull({channel["output_voltage"]}) AS {v}',
                        f'toFloat64OrNull({channel["output_current"]}) AS {i}'))
        bcr_valid.extend((finite(v), finite(i)))
        bcr_terms.append(f'{v}*{i}')
    aliases.extend((f'({" AND ".join(bcr_valid)}) AS ok_bcr',
                    f'({"+".join(bcr_terms)}) AS p_bcr'))
    pair_conditions['BCR'] = f'(ok_bus AND {battery_ok} AND ok_bcr)'
    cols = ["formatDateTime(toStartOfMinute(timestamp),'%Y-%m-%d %H:%i:00','UTC') AS minute_utc",
            'count() AS eps_source_rows', 'countIf(ok_bus) AS valid_bus_rows']
    for key, pair in pair_conditions.items():
        source_expression = {'A': 'p_pl_A', 'B': 'p_pl_B',
                             'AB': 'p_pl_A+p_pl_B', 'HEAT': 'p_heat',
                             'ABH': 'p_pl_A+p_pl_B+p_heat',
                             'BCR': 'p_bcr'}[key]
        cols.extend((f'countIf({pair}) AS n_{key}',
                     f'sumIf(p_bus,{pair}) AS bus_{key}',
                     f'sumIf({source_expression},{pair}) AS source_{key}'))
        if key == 'BCR':
            cols.append(f'sumIf(p_battery,{pair}) AS battery_BCR')
    return ('WITH ' + ','.join(aliases) + ' SELECT ' + ','.join(cols) +
            f' FROM {database}.{table} '
            "PREWHERE timestamp>=toDateTime64({start:String},3,'UTC') "
            "AND timestamp<toDateTime64({end:String},3,'UTC') "
            'WHERE TMEA003 IS NOT NULL GROUP BY minute_utc ORDER BY minute_utc')


def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(rows, phases, on_pair_minute=None):
    """仅保留 TMZS003 明确且无冲突的分钟；每分钟先按配对来源行求均值。"""
    data = {phase: {key: [] for key in ('A', 'B', 'AB', 'HEAT', 'ABH', 'BCR')}
            for phase in ('日照', '地影')}
    source_rows = {phase: {key: 0 for key in ('A', 'B', 'AB', 'HEAT', 'ABH', 'BCR')}
                   for phase in data}
    eps_rows = {phase: 0 for phase in data}
    valid_bus_rows = {phase: 0 for phase in data}
    valid_bus_minutes = {phase: 0 for phase in data}
    seen = set()
    for row in rows:
        minute = datetime.fromisoformat(row['minute_utc']).replace(tzinfo=timezone.utc)
        minute_bj = (minute + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
        if minute_bj in seen:
            raise ValueError('查询分钟重复，请核对分批边界')
        seen.add(minute_bj)
        phase = phases.get(minute_bj)
        if phase not in data:
            continue
        eps_rows[phase] += int(row['eps_source_rows'])
        valid_bus_rows[phase] += int(row['valid_bus_rows'])
        valid_bus_minutes[phase] += int(row['valid_bus_rows']) > 0
        for key in data[phase]:
            count = int(row[f'n_{key}'])
            if count <= 0:
                continue
            bus = float(row[f'bus_{key}']) / count
            source = float(row[f'source_{key}']) / count
            battery = float(row['battery_BCR']) / count if key == 'BCR' else None
            if not all(math.isfinite(v) for v in (bus, source) + ((battery,) if battery is not None else ())):
                continue
            data[phase][key].append((bus, source, battery))
            source_rows[phase][key] += count
            if key == 'ABH' and on_pair_minute is not None:
                on_pair_minute({'t_beijing': minute_bj, 'phase': phase,
                                'paired_source_rows': count, 'bus_W': bus,
                                'pl_ab_heat_W': source,
                                'bus_minus_pl_ab_heat_W': bus - source})
    result = {}
    phase_counts = {phase: sum(value == phase for value in phases.values()) for phase in data}
    for phase, groups in data.items():
        result[phase] = {'clear_phase_minutes': phase_counts[phase],
                         'eps_source_rows': eps_rows[phase],
                         'valid_bus_source_rows': valid_bus_rows[phase],
                         'valid_bus_minutes': valid_bus_minutes[phase],
                         'comparisons': {}}
        for key, values in groups.items():
            bus = [v[0] for v in values]
            source = [v[1] for v in values]
            gap = [v[0] - v[1] for v in values]
            item = {
                'paired_minutes': len(values), 'paired_source_rows': source_rows[phase][key],
                'coverage_of_clear_phase_minutes': len(values) / phase_counts[phase] if phase_counts[phase] else 0,
                'bus_W_p05_p50_p95': [percentile(bus, p) for p in (0.05, 0.5, 0.95)],
                'source_W_p05_p50_p95': [percentile(source, p) for p in (0.05, 0.5, 0.95)],
                'bus_minus_source_W_p05_p50_p95': [percentile(gap, p) for p in (0.05, 0.5, 0.95)],
                'source_over_bus_ratio_p50': percentile([s/b for b, s in zip(bus, source) if b > 1], 0.5),
            }
            if key == 'BCR':
                battery = [v[2] for v in values]
                signed_gap = [s + bat - b for b, s, bat in values]
                item['battery_terminal_signed_W_p05_p50_p95'] = [percentile(battery, p) for p in (0.05, 0.5, 0.95)]
                item['bcr_plus_battery_minus_bus_W_p05_p50_p95'] = [percentile(signed_gap, p) for p in (0.05, 0.5, 0.95)]
            result[phase]['comparisons'][key] = item
    return result


def assess(root=PROFILE, start_override=None, end_override=None, write=True, chunk_days=3):
    root = Path(root)
    cfg, design = load_profile(root)
    energy = json.loads((root / 'energy_summary.json').read_text(encoding='utf-8'))
    inventory_path = root / 'unit_inventory.json'
    inventory = json.loads(inventory_path.read_text(encoding='utf-8'))
    bcr_path = root / 'config' / 'boundary_candidates.json'
    bcr_config = json.loads(bcr_path.read_text(encoding='utf-8'))
    component_path = root / 'config' / 'components.json'
    component_config = json.loads(component_path.read_text(encoding='utf-8'))
    minute_path = root / 'data_power_minute.csv'
    if sha256(minute_path) != energy['provenance']['minute_csv_sha256']:
        raise ValueError('分钟数据 SHA-256 与能源摘要不一致')
    if inventory['source_workbook_sha256'] != energy['provenance']['workbook_sha256']:
        raise ValueError('单机清单与能源摘要的工作簿不一致')
    if sha256(Path(bcr_config['source_workbook'])) != energy['provenance']['workbook_sha256']:
        raise ValueError('BCR 配置与能源摘要的工作簿不一致')
    start_text = start_override or design['assessment_start_beijing']
    end_text = end_override or design['assessment_end_exclusive_beijing']
    start, end = utc_bounds(start_text, end_text)
    start_bj = (start + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    end_bj = (end + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
    if chunk_days < 1:
        raise ValueError('分批天数必须为正')
    phases = {}
    with minute_path.open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            if (start_bj <= row['t'] < end_bj and
                    row['phase'] in ('日照', '地影') and
                    row['phase_source'] == cfg['phase_state']['code'] and
                    row['phase_conflict'] == '0'):
                phases[row['t']] = row['phase']
    sql = query_sql(inventory, bcr_config, component_config,
                    cfg['source_database'], cfg['source_table'])

    def rows():
        cursor = start
        while cursor < end:
            stop = min(cursor + timedelta(days=chunk_days), end)
            params = {'start': cursor.strftime('%Y-%m-%d %H:%M:%S'),
                      'end': stop.strftime('%Y-%m-%d %H:%M:%S')}
            yield from native_query_iter(sql, params, timeout=180, max_execution_time=180)
            cursor = stop

    pair_path = root / 'data_balance_pair_minute.csv'
    if write:
        temp = pair_path.with_suffix('.csv.tmp')
        fields = ('t_beijing', 'phase', 'paired_source_rows', 'bus_W',
                  'pl_ab_heat_W', 'bus_minus_pl_ab_heat_W')
        try:
            with temp.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                comparisons = summarize(rows(), phases, writer.writerow)
            temp.replace(pair_path)
        finally:
            temp.unlink(missing_ok=True)
    else:
        comparisons = summarize(rows(), phases)
    result = {
        'source': f'{cfg["source_database"]}.{cfg["source_table"]}',
        'window_start_inclusive_beijing': start_text,
        'window_end_exclusive_beijing': end_text,
        'time_contract': 'source UTC timestamp, Beijing report minute, half-open window',
        'basis': ('Same-source-row bus versus 21 measured PL feeds per bank, ten heater currents, '
                  'their conditional sums, or eight BCR output channels; '
                  'source rows are averaged within each minute, then minutes are compared equally. '
                  'Clear TMZS003 phase and no solar-current conflict only. Duplicated source rows may '
                  'weight a minute mean. PL_25/29 lack current; PL_33 is outside the PL bank sum. '
                  'A/B and heater topology and BCR/battery/bus boundary alignment are unverified. '
                  'Power gaps are diagnostics, not verified losses or energy integrals.'),
        'minute_csv_sha256': sha256(minute_path),
        'inventory_sha256': sha256(inventory_path),
        'bcr_config_sha256': sha256(bcr_path),
        'component_config_sha256': sha256(component_path),
        'comparisons_by_phase': comparisons,
    }
    if write:
        result['paired_minute_csv_sha256'] = sha256(pair_path)
        result['paired_minute_csv_rows'] = sum(
            comparisons[phase]['comparisons']['ABH']['paired_minutes']
            for phase in ('日照', '地影'))
        (root / 'balance_ledger_summary.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--chunk-days', type=int, default=3)
    parser.add_argument('--no-write', action='store_true')
    arguments = parser.parse_args()
    result = assess(start_override=arguments.start, end_override=arguments.end,
                    write=not arguments.no_write, chunk_days=arguments.chunk_days)
    print(json.dumps(result['comparisons_by_phase'], ensure_ascii=False))
