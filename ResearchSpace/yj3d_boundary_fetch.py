#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读核验云尖3D星 BCR 与电池诊断候选宽表列。"""
import json
from datetime import datetime, timezone
from pathlib import Path

from s1_fetch import native_query_iter
from yj3d_assess import PROFILE, load_profile, sha256, utc_bounds


def inspect(root=PROFILE, limit=12):
    root = Path(root)
    limit = int(limit)
    if not 1 <= limit <= 100:
        raise ValueError('样本行数应在 1 至 100 之间')
    cfg, design = load_profile(root)
    candidates = json.loads((root / 'config' / 'boundary_candidates.json').read_text(encoding='utf-8'))
    codes = [item[field] for item in candidates['pv_channels']
             for field in ('input_voltage', 'output_voltage', 'output_current')]
    codes += [item['code'] for item in candidates['other_diagnostic_candidates']]
    if len(codes) != len(set(codes)) or not all(code.isidentifier() for code in codes):
        raise ValueError('候选代号重复或无效')
    schema = list(native_query_iter(
        'SELECT name,type FROM system.columns WHERE database={db:String} AND table={table:String}',
        {'db': cfg['source_database'], 'table': cfg['source_table']},
        timeout=30, max_execution_time=30))
    types = {row['name']: row['type'] for row in schema}
    missing = sorted(set(codes) - set(types))
    if missing:
        raise ValueError(f'候选代号未找到宽表列: {missing}')
    start, end = utc_bounds(design['assessment_start_beijing'],
                            design['assessment_end_exclusive_beijing'])
    params = {'start': start.strftime('%Y-%m-%d %H:%M:%S'),
              'end': end.strftime('%Y-%m-%d %H:%M:%S')}
    table = f"{cfg['source_database']}.{cfg['source_table']}"
    if not cfg['source_database'].isidentifier() or not cfg['source_table'].isidentifier():
        raise ValueError('源表名称无效')
    selected = ['timestamp'] + codes
    sql = ('SELECT ' + ','.join(selected) + ' FROM ' + table +
           " PREWHERE timestamp>=toDateTime64({start:String},3,'UTC')"
           " AND timestamp<toDateTime64({end:String},3,'UTC')"
           f' WHERE TMBA019 IS NOT NULL ORDER BY timestamp LIMIT {limit}')
    rows = list(native_query_iter(sql, params, timeout=60, max_execution_time=60))
    return {'window_start_utc': start.isoformat(), 'window_end_exclusive_utc': end.isoformat(),
            'source': table, 'types': {code: types[code] for code in codes},
            'sample_rows': [{key: str(value) if value is not None else None
                             for key, value in row.items()} for row in rows]}


def assess(root=PROFILE, start_override=None, end_override=None, write=True):
    """只按来源行做 BCR 侧分位诊断，不跨测点拼装阵列输入功率。"""
    root = Path(root)
    cfg, design = load_profile(root)
    path = root / 'config' / 'boundary_candidates.json'
    candidates = json.loads(path.read_text(encoding='utf-8'))
    channels = candidates['pv_channels']
    if len(channels) != 8:
        raise ValueError('BCR PV 通道配置必须为 8 路')
    codes = [item[field] for item in channels
             for field in ('input_voltage', 'output_voltage', 'output_current')]
    if len(codes) != len(set(codes)) or not all(code.isidentifier() for code in codes):
        raise ValueError('BCR 测点代号重复或无效')
    if not cfg['source_database'].isidentifier() or not cfg['source_table'].isidentifier():
        raise ValueError('源表名称无效')
    start, end = utc_bounds(start_override or design['assessment_start_beijing'],
                            end_override or design['assessment_end_exclusive_beijing'])
    if not start < end:
        raise ValueError('查询时间窗口无效')
    schema = list(native_query_iter(
        'SELECT name,type FROM system.columns WHERE database={db:String} AND table={table:String}',
        {'db': cfg['source_database'], 'table': cfg['source_table']},
        timeout=30, max_execution_time=30))
    types = {row['name']: row['type'] for row in schema}
    missing = sorted(set(codes) - set(types))
    if missing:
        raise ValueError(f'宽表缺少 BCR 列: {missing}')
    aliases = []
    outputs = ['count() AS source_rows']
    output_power_terms = []
    output_valid_terms = []
    for index, item in enumerate(channels):
        vin, vout, iout = f'vin_{index}', f'vout_{index}', f'iout_{index}'
        aliases.extend((f'toFloat64OrNull({item["input_voltage"]}) AS {vin}',
                        f'toFloat64OrNull({item["output_voltage"]}) AS {vout}',
                        f'toFloat64OrNull({item["output_current"]}) AS {iout}'))
        valid_in = f'(isNotNull({vin}) AND isFinite(ifNull({vin},0)))'
        valid_out = (f'(isNotNull({vout}) AND isFinite(ifNull({vout},0)) AND '
                     f'isNotNull({iout}) AND isFinite(ifNull({iout},0)))')
        output_power_terms.append(f'({vout}*{iout})')
        output_valid_terms.append(valid_out)
        outputs.extend((
            f'countIf({valid_in}) AS c{index}_input_numeric_rows',
            f'countIf({valid_out}) AS c{index}_output_paired_rows',
            f'minIf({vin},{valid_in}) AS c{index}_input_min_V',
            f'maxIf({vin},{valid_in}) AS c{index}_input_max_V',
            f'quantilesTDigestIf(0.05,0.5,0.95)({vin},{valid_in}) AS c{index}_input_voltage_pcts_V',
            f'quantilesTDigestIf(0.05,0.5,0.95)({vout}*{iout},{valid_out}) AS c{index}_output_power_pcts_W',
            f'countIf({valid_out} AND {vout}*{iout}<0) AS c{index}_negative_output_power_rows'))
    all_output_valid = '(' + ' AND '.join(output_valid_terms) + ')'
    output_sum = '(' + '+'.join(output_power_terms) + ')'
    outputs.extend((
        f'countIf({all_output_valid}) AS all_bcr_output_paired_rows',
        f'quantilesTDigestIf(0.05,0.5,0.95)({output_sum},{all_output_valid}) AS all_bcr_output_sum_pcts_W'))
    aliases.extend(('toFloat64OrNull(TMEA010) AS temp_main',
                    'toFloat64OrNull(TMEB010) AS temp_backup',
                    'toFloat64OrNull(TMUA021) AS ttc_current',
                    'toFloat64OrNull(TMEA006) AS eps_current'))
    valid_current_pair = '(ttc_current>=-10 AND ttc_current<=10 AND eps_current>=-10 AND eps_current<=10)'
    nonzero_current_pair = f'({valid_current_pair} AND abs(ttc_current)>0.2 AND abs(eps_current)>0.2)'
    outputs.extend((
        'countIf(isNotNull(temp_main) AND isFinite(ifNull(temp_main,0))) AS temp_main_numeric_rows',
        'countIf(temp_main>=0 AND temp_main<=30) AS temp_main_workbook_range_rows',
        'quantilesTDigestIf(0.05,0.5,0.95)(temp_main,isNotNull(temp_main) AND isFinite(ifNull(temp_main,0))) AS temp_main_pcts_C',
        'countIf(isNotNull(temp_backup) AND isFinite(ifNull(temp_backup,0))) AS temp_backup_numeric_rows',
        'quantilesTDigestIf(0.05,0.5,0.95)(temp_backup,isNotNull(temp_backup) AND isFinite(ifNull(temp_backup,0))) AS temp_backup_pcts_C',
        'countIf(TMUA020 IS NOT NULL) AS ttc_switch_source_rows',
        'groupUniqArray(10)(TMUA020) AS ttc_switch_values',
        'countIf(isNotNull(ttc_current) AND isFinite(ifNull(ttc_current,0))) AS ttc_current_numeric_rows',
        'countIf(isNotNull(ttc_current) AND isNotNull(eps_current)) AS ttc_eps_same_source_row_pairs',
        'quantilesTDigestIf(0.05,0.5,0.95)(ttc_current-eps_current,isNotNull(ttc_current) AND isNotNull(eps_current)) AS ttc_minus_eps_current_pcts_A',
        f'countIf({valid_current_pair}) AS ttc_eps_both_within_10A_rows',
        f'quantilesTDigestIf(0.05,0.5,0.95)(ttc_current-eps_current,{valid_current_pair}) AS ttc_minus_eps_within_10A_pcts_A',
        f'countIf({nonzero_current_pair}) AS ttc_eps_nonzero_within_10A_rows',
        f'countIf({nonzero_current_pair} AND sign(ttc_current)=sign(eps_current)) AS ttc_eps_same_sign_rows'))
    table = f"{cfg['source_database']}.{cfg['source_table']}"
    sql = ('WITH ' + ','.join(aliases) + ' SELECT ' + ','.join(outputs) + ' FROM ' + table +
           " PREWHERE timestamp>=toDateTime64({start:String},3,'UTC')"
           " AND timestamp<toDateTime64({end:String},3,'UTC')")
    params = {'start': start.strftime('%Y-%m-%d %H:%M:%S'),
              'end': end.strftime('%Y-%m-%d %H:%M:%S')}
    row = list(native_query_iter(sql, params, timeout=180, max_execution_time=180))[0]
    result = {
        'source': table, 'source_time_column': cfg['source_time_column'],
        'window_start_utc': start.isoformat(), 'window_end_exclusive_utc': end.isoformat(),
        'source_rows': int(row['source_rows']),
        'basis': ('BCR source rows; input-voltage and same-channel output-voltage/current are separate '
                  'sides. Quantiles use numeric finite values without invented engineering limits. '
                  'Duplicate source rows may affect quantiles. Output power is not array terminal power.'),
        'candidate_config_sha256': sha256(path),
        'workbook_sha256': sha256(Path(candidates['source_workbook'])),
        'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
        'all_eight_output_paired_source_rows': int(row['all_bcr_output_paired_rows']),
        'all_eight_output_sum_p05_p50_p95_W': (
            row['all_bcr_output_sum_pcts_W'] if row['all_bcr_output_paired_rows'] else None),
        'other_diagnostics': {
            'battery_temperature_main_numeric_source_rows': int(row['temp_main_numeric_rows']),
            'battery_temperature_main_workbook_range_source_rows': int(row['temp_main_workbook_range_rows']),
            'battery_temperature_main_p05_p50_p95_C': row['temp_main_pcts_C'],
            'battery_temperature_backup_numeric_source_rows': int(row['temp_backup_numeric_rows']),
            'battery_temperature_backup_p05_p50_p95_C': row['temp_backup_pcts_C'],
            'ttc_discharge_switch_source_rows': int(row['ttc_switch_source_rows']),
            'ttc_discharge_switch_values': row['ttc_switch_values'],
            'ttc_current_numeric_source_rows': int(row['ttc_current_numeric_rows']),
            'ttc_eps_current_same_source_row_pairs': int(row['ttc_eps_same_source_row_pairs']),
            'ttc_minus_eps_current_p05_p50_p95_A': (
                row['ttc_minus_eps_current_pcts_A']
                if row['ttc_eps_same_source_row_pairs'] else None),
            'ttc_eps_both_within_10A_source_rows': int(row['ttc_eps_both_within_10A_rows']),
            'ttc_minus_eps_within_10A_p05_p50_p95_A': (
                row['ttc_minus_eps_within_10A_pcts_A']
                if row['ttc_eps_both_within_10A_rows'] else None),
            'ttc_eps_nonzero_within_10A_source_rows': int(row['ttc_eps_nonzero_within_10A_rows']),
            'ttc_eps_same_sign_source_rows': int(row['ttc_eps_same_sign_rows']),
        },
        'channels': []}
    for index, item in enumerate(channels):
        result['channels'].append({
            'name': item['name'], 'input_voltage_code': item['input_voltage'],
            'output_voltage_code': item['output_voltage'],
            'output_current_code': item['output_current'],
            'source_types': {code: types[code] for code in
                             (item['input_voltage'], item['output_voltage'], item['output_current'])},
            'input_numeric_source_rows': int(row[f'c{index}_input_numeric_rows']),
            'output_paired_source_rows': int(row[f'c{index}_output_paired_rows']),
            'input_voltage_raw_min_V': row[f'c{index}_input_min_V'],
            'input_voltage_raw_max_V': row[f'c{index}_input_max_V'],
            'input_voltage_p05_p50_p95_V': row[f'c{index}_input_voltage_pcts_V'],
            'output_power_p05_p50_p95_W': row[f'c{index}_output_power_pcts_W'],
            'negative_output_power_source_rows': int(row[f'c{index}_negative_output_power_rows']),
        })
    if write:
        (root / 'bcr_boundary_summary.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


if __name__ == '__main__':
    print(json.dumps(assess(), ensure_ascii=False, indent=2))
