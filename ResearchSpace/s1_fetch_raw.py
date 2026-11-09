#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按卫星配置从 ClickHouse 取星上时间对齐的原始功率遥测，供相邻采样积分。"""
import argparse
import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ch_http as C
from s1_fetch import (BASE, CONFIG, _minute, native_query_iter,
                      source_table, source_satellite_ids, wide_pairs)
from s3_run import sha256_file

SQL = (
    'SELECT onboard_time_bj AS t, param_code, avg(value) AS v '
    'FROM {source_table} '
    'WHERE satellite_id IN ({satellite_id:String}, {satellite_alias:String}) '
    'AND param_code IN ({codes}) '
    'AND onboard_time_bj >= toDateTime({start:String}, {tz:String}) '
    'AND onboard_time_bj < toDateTime({end:String}, {tz:String}) '
    'AND value IS NOT NULL '
    'GROUP BY t, param_code ORDER BY t, param_code'
)
SQL_WIDE = (
    'SELECT t, param_code, avg(value) AS v FROM ('
    'SELECT onboard_time_bj AS t, tupleElement(kv,1) AS param_code, '
    'toFloat64OrNull(tupleElement(kv,2)) AS value '
    'FROM {source_table} ARRAY JOIN [{pairs}] AS kv '
    'WHERE spacecraft_id={spacecraft_id:String} '
    'AND onboard_time_bj >= toDateTime({start:String}, {tz:String}) '
    'AND onboard_time_bj < toDateTime({end:String}, {tz:String})) '
    'WHERE value IS NOT NULL GROUP BY t, param_code ORDER BY t, param_code'
)


def fetch_raw(start, end, source_timezone, output=BASE / 'data_power_raw.csv',
              chunk_days=7, query_rows=None, profile_dir=CONFIG.parent,
              transport='native'):
    start, end = _minute(start), _minute(end)
    if start >= end or chunk_days < 1:
        raise ValueError('须提供 start < end 且 chunk_days >= 1')
    if source_timezone != 'Asia/Shanghai':
        raise ValueError('onboard_time_bj 的时区必须为 Asia/Shanghai')
    if transport not in ('native', 'http'):
        raise ValueError('transport 必须是 native 或 http')
    cfg = json.loads((Path(profile_dir) / CONFIG.name).read_text(encoding='utf-8'))
    satellite_id, satellite_alias = source_satellite_ids(profile_dir)
    codes = [item['code'] for item in cfg['columns'].values()]
    if not codes or any(re.fullmatch(r'[A-Z]{3}\d{3}(?:_b\d+(?:b\d+)?)?', code) is None
                        for code in codes):
        raise ValueError('原始取数参数代号格式无效')
    table = source_table(profile_dir)
    wide_source = table.startswith('master_satellite_')
    sql = ((SQL_WIDE.replace('{source_table}', table)
            .replace('{pairs}', wide_pairs(codes))) if wide_source else
           (SQL.replace('{source_table}', table)
            .replace('{codes}', ','.join("'" + code + "'" for code in codes))))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + '.tmp')
    manifest_path = output.with_name(output.stem + '.manifest.json')
    stream_query = query_rows or (C.query_iter if transport == 'http'
                                  else native_query_iter)
    nrows = 0
    try:
        with temp.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['t', 'param_code', 'value'])
            cursor = start
            while cursor < end:
                stop = min(cursor + timedelta(days=chunk_days), end)
                params = {'start': cursor.strftime('%Y-%m-%d %H:%M:%S'),
                          'end': stop.strftime('%Y-%m-%d %H:%M:%S'),
                          'tz': source_timezone}
                if wide_source:
                    params['spacecraft_id'] = satellite_id
                else:
                    params.update(satellite_id=satellite_id,
                                  satellite_alias=satellite_alias)
                for row in stream_query(sql, params=params, timeout=120,
                                        max_execution_time=120):
                    writer.writerow([row['t'], row['param_code'], row['v']])
                    nrows += 1
                cursor = stop
        manifest = {'satellite_id': satellite_id,
                    'source_satellite_ids': ([satellite_id] if wide_source else
                                             list(dict.fromkeys((satellite_id, satellite_alias)))),
                    'source_id_column': 'spacecraft_id' if wide_source else 'satellite_id',
                    'source': f'{C.DB}.{table}',
                    'window_start_inclusive': start.isoformat(),
                    'window_end_exclusive': end.isoformat(),
                    'timezone': source_timezone,
                    'time_column': 'onboard_time_bj',
                    'param_codes': codes, 'row_count': nrows,
                    'data_sha256': sha256_file(temp),
                    'fetched_at_utc': datetime.now(timezone.utc).isoformat()}
        manifest_temp = manifest_path.with_suffix('.json.tmp')
        manifest_temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding='utf-8')
        temp.replace(output)
        manifest_temp.replace(manifest_path)
        return manifest
    finally:
        temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--source-timezone', required=True)
    parser.add_argument('--chunk-days', type=int, default=7)
    parser.add_argument('--out', type=Path, default=BASE / 'data_power_raw.csv')
    parser.add_argument('--profile-dir', type=Path, default=CONFIG.parent)
    parser.add_argument('--transport', choices=('native', 'http'), default='native')
    args = parser.parse_args()
    manifest = fetch_raw(args.start, args.end, args.source_timezone,
                         args.out, args.chunk_days, profile_dir=args.profile_dir,
                         transport=args.transport)
    print(f"原始遥测 {manifest['row_count']} 行；时间基准 {manifest['time_column']}")


if __name__ == '__main__':
    main()
