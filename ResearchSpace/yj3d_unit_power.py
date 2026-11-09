#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读汇总云尖3D星 PL A/B 单机供电通道同来源行功率。"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from s1_fetch import native_query_iter
from yj3d_assess import PROFILE, load_profile, sha256, utc_bounds


def upper_limit(text, unit):
    # 工作簿常写作“21V~29.2V或0V~0.5V”，取首段工作上界。
    match = re.search(r'~([0-9.]+)' + re.escape(unit), text)
    return float(match.group(1)) if match else None


def voltage_intervals(text):
    return [(float(low), float(high)) for low, high in
            re.findall(r'([0-9.]+)V~([0-9.]+)V', text)]


def assess(root=PROFILE):
    root = Path(root)
    inventory_path = root / 'unit_inventory.json'
    inventory = json.loads(inventory_path.read_text(encoding='utf-8'))
    if sha256(Path(inventory['source_workbook'])) != inventory['source_workbook_sha256']:
        raise ValueError('单机清单所用工作簿已更改')
    cfg, design = load_profile(root)
    start, end = utc_bounds(design['assessment_start_beijing'],
                            design['assessment_end_exclusive_beijing'])
    if not cfg['source_database'].isidentifier() or not cfg['source_table'].isidentifier():
        raise ValueError('源表名称无效')
    codes = {detail['code'] for unit in inventory['units'] for bank in unit['banks'].values()
             for detail in bank.values()}
    if not all(code.isidentifier() for code in codes):
        raise ValueError('单机代号无效')
    schema = list(native_query_iter(
        'SELECT name,type FROM system.columns WHERE database={db:String} AND table={table:String}',
        {'db': cfg['source_database'], 'table': cfg['source_table']},
        timeout=30, max_execution_time=30))
    types = {item['name']: item['type'] for item in schema}
    if codes - types.keys():
        raise ValueError(f'宽表缺少单机代号: {sorted(codes-types.keys())}')
    table = f"{cfg['source_database']}.{cfg['source_table']}"
    params = {'start': start.strftime('%Y-%m-%d %H:%M:%S'),
              'end': end.strftime('%Y-%m-%d %H:%M:%S')}
    result = {
        'source': table, 'window_start_utc': start.isoformat(),
        'window_end_exclusive_utc': end.isoformat(),
        'inventory_sha256': sha256(inventory_path),
        'workbook_sha256': inventory['source_workbook_sha256'],
        'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
        'basis': ('Each PL bank is summarized separately. Power is same-source-row channel voltage '
                  'times current, filtered by workbook voltage working/off intervals and current limits where given; '
                  'source-row duplicates may affect quantiles. A/B physical relation is unverified, '
                  'so bank powers and medians are not summed into a bus energy balance.'),
        'banks': {},
    }
    for bank in ('A', 'B'):
        aliases = []
        columns = ['count() AS source_rows']
        pl_power_terms = []
        pl_valid_terms = []
        for index, unit in enumerate(inventory['units']):
            spec = unit['banks'][bank]
            vcode = spec['voltage']['code']
            vname = f'v{index}'
            aliases.append(f'toFloat64OrNull({vcode}) AS {vname}')
            numeric_v = f'(isNotNull({vname}) AND isFinite(ifNull({vname},0)))'
            columns.append(f'countIf({numeric_v}) AS c{index}_voltage_numeric_rows')
            if 'current' not in spec:
                columns.append(f'quantilesTDigestIf(0.5,0.95)({vname},{numeric_v}) AS c{index}_voltage_pcts_V')
                continue
            icode = spec['current']['code']
            iname = f'i{index}'
            aliases.append(f'toFloat64OrNull({icode}) AS {iname}')
            numeric_i = f'(isNotNull({iname}) AND isFinite(ifNull({iname},0)))'
            valid = f'({numeric_v} AND {numeric_i} AND {vname}>=0 AND {iname}>=0'
            vlimit = upper_limit(spec['voltage']['workbook_range'], 'V')
            ilimit = upper_limit(spec['current']['workbook_range'], 'A')
            intervals = voltage_intervals(spec['voltage']['workbook_range'])
            if intervals:
                voltage_valid = ' OR '.join(f'({vname}>={low} AND {vname}<={high})'
                                            for low, high in intervals)
                valid += f' AND ({voltage_valid})'
            if ilimit is not None:
                valid += f' AND {iname}<={ilimit}'
            valid += ')'
            if unit['pl_channel'] != 33:
                pl_power_terms.append(f'if({valid},{vname}*{iname},0)')
                pl_valid_terms.append(valid)
            columns.extend((
                f'countIf({numeric_v} AND {numeric_i}) AS c{index}_numeric_paired_rows',
                f'countIf({valid}) AS c{index}_valid_paired_rows',
                f'quantilesTDigestIf(0.5,0.95)({vname}*{iname},{valid}) AS c{index}_power_pcts_W'))
        complete = '(' + ' AND '.join(pl_valid_terms) + ')'
        pl_sum = '(' + '+'.join(pl_power_terms) + ')'
        columns.extend((
            f'countIf({complete}) AS pl_complete_source_rows',
            f'quantilesTDigestIf(0.05,0.5,0.95)({pl_sum},{complete}) AS pl_complete_sum_pcts_W'))
        sql = ('WITH ' + ','.join(aliases) + ' SELECT ' + ','.join(columns) + ' FROM ' + table +
               " PREWHERE timestamp>=toDateTime64({start:String},3,'UTC')"
               " AND timestamp<toDateTime64({end:String},3,'UTC')")
        row = list(native_query_iter(sql, params, timeout=180, max_execution_time=180))[0]
        channel_results = []
        for index, unit in enumerate(inventory['units']):
            spec = unit['banks'][bank]
            entry = {'pl_channel': unit['pl_channel'], 'name': unit['name'],
                     'voltage_code': spec['voltage']['code'],
                     'voltage_numeric_source_rows': int(row[f'c{index}_voltage_numeric_rows']),
                     'voltage_type': types[spec['voltage']['code']]}
            if 'current' in spec:
                entry.update({
                    'current_code': spec['current']['code'],
                    'current_type': types[spec['current']['code']],
                    'numeric_paired_source_rows': int(row[f'c{index}_numeric_paired_rows']),
                    'valid_paired_source_rows': int(row[f'c{index}_valid_paired_rows']),
                    'power_p50_p95_W': (row[f'c{index}_power_pcts_W']
                                      if row[f'c{index}_valid_paired_rows'] else None),
                    'workbook_voltage_upper_V': upper_limit(spec['voltage']['workbook_range'], 'V'),
                    'workbook_current_upper_A': upper_limit(spec['current']['workbook_range'], 'A'),
                })
            else:
                entry['voltage_p50_p95_V'] = (row[f'c{index}_voltage_pcts_V']
                                               if row[f'c{index}_voltage_numeric_rows'] else None)
            channel_results.append(entry)
        result['banks'][bank] = {'source_rows': int(row['source_rows']),
                                 'pl_paired_channels_in_sum': len(pl_power_terms),
                                 'pl_complete_source_rows': int(row['pl_complete_source_rows']),
                                 'pl_complete_sum_p05_p50_p95_W': (
                                     row['pl_complete_sum_pcts_W']
                                     if row['pl_complete_source_rows'] else None),
                                 'channels': channel_results}
    (root / 'unit_power_summary.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


if __name__ == '__main__':
    result = assess()
    print(json.dumps({bank: sum(item.get('valid_paired_source_rows', 0) > 0
                                for item in result['banks'][bank]['channels'])
                      for bank in ('A', 'B')}, ensure_ascii=False))
