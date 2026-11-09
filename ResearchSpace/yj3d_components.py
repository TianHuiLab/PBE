#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云尖3D星可配置主路加热支路：有界查询各支路母线电压折算功率。"""
import json
from datetime import datetime, timezone

import ch_http
from components import load_config
from yj3d_assess import PROFILE, load_profile, sha256, utc_bounds


def require_nonempty_component_rows(components):
    """拒绝整批空查询，避免用全零摘要覆盖已有有效结果。"""
    valid_rows = sum(int(item['valid_source_rows']) for item in components.values())
    if not components or valid_rows <= 0:
        raise ValueError('云尖3D支路查询无有效行；拒绝用全零摘要覆盖已有结果')


def assess_components(root=PROFILE):
    cfg, design = load_profile(root)
    path = root / 'config' / 'components.json'
    specs = load_config(path, cfg['satellite_id'])
    if any(spec['plugin'] != 'bus_current' or spec['role'] != 'load'
           for spec in specs):
        raise ValueError('云尖3D当前支路查询只支持母线电流代理负载')
    start, end = utc_bounds(design['assessment_start_beijing'],
                            design['assessment_end_exclusive_beijing'])
    columns = []
    for spec in specs:
        code = spec['current_code']
        if not code.isidentifier() or not code.startswith('TMEA'):
            raise ValueError(f'无效 EPS A 路支路代号: {code}')
        ident = spec['id']
        current = f'toFloat64OrNull({code})'
        voltage = 'toFloat64OrNull(TMEA003)'
        valid = f'({current} >= 0 AND {current} <= 1 AND {voltage} >= 21 AND {voltage} <= 29.2)'
        columns.extend([
            f'countIf({code} IS NOT NULL) AS {ident}_source_rows',
            f'countIf({valid}) AS {ident}_valid_rows',
            f'min({current}) AS {ident}_raw_min_A',
            f'max({current}) AS {ident}_raw_max_A',
            f'avgIf({current}*{voltage},{valid}) AS {ident}_mean_W',
            f'quantileTDigestIf(0.95)({current}*{voltage},{valid}) AS {ident}_p95_W',
        ])
    sql = ('SELECT ' + ','.join(columns) + ' FROM chdb_3D.yc_history '
           "PREWHERE timestamp>=toDateTime64({start:String},3,'UTC') "
           "AND timestamp<toDateTime64({end:String},3,'UTC')")
    params = {'start': start.strftime('%Y-%m-%d %H:%M:%S'),
              'end': end.strftime('%Y-%m-%d %H:%M:%S')}
    row = ch_http.query(sql, database='chdb_3D', params=params,
                        timeout=90, max_execution_time=90)[0]
    components = {}
    for spec in specs:
        ident = spec['id']
        n = int(row[f'{ident}_valid_rows'])
        components[ident] = {
            'name': spec['name'], 'code': spec['current_code'],
            'source_rows': int(row[f'{ident}_source_rows']),
            'valid_source_rows': n,
            'raw_min_A': row[f'{ident}_raw_min_A'],
            'raw_max_A': row[f'{ident}_raw_max_A'],
            'mean_bus_voltage_proxy_W': row[f'{ident}_mean_W'] if n else None,
            'p95_bus_voltage_proxy_W': row[f'{ident}_p95_W'] if n else None,
        }
    require_nonempty_component_rows(components)
    result = {
        'source': 'chdb_3D.yc_history', 'source_time_column': 'timestamp',
        'window_start_utc': start.isoformat(), 'window_end_exclusive_utc': end.isoformat(),
        'basis': 'TMEA003 × branch current, paired source rows in workbook physical ranges; duplicate rows may affect means and p95',
        'balance_boundary_verified': False,
        'configuration_sha256': sha256(path),
        'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
        'components': components,
    }
    (root / 'component_summary.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


if __name__ == '__main__':
    result = assess_components()
    print(json.dumps({key: value['valid_source_rows'] for key, value in
                      result['components'].items()}, ensure_ascii=False))
