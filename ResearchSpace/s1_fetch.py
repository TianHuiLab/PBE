#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按明确的半开时间窗分批取分钟遥测，并保存可校验的来源清单。"""
import argparse
import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ch_http as C
from s3_run import sha256_file
from components import CONFIG as COMPONENT_CONFIG, load_config as load_components
from components import required_codes as component_codes

BASE = Path(__file__).resolve().parent
CONFIG = BASE / 'config' / 'telemetry_params.json'
SQL = (
    'SELECT toStartOfInterval(onboard_time_bj, INTERVAL 1 MINUTE) AS t, '
    'param_code, count(*) n, round(avg(value),4) avg, '
    'round(min(value),4) mn, round(max(value),4) mx, '
    'round(stddevSamp(value),4) sd, '
    "multiIf(uniqExactIf(value_str, value_str != '') = 0, '', "
    "uniqExactIf(value_str, value_str != '') = 1, anyIf(value_str, value_str != ''), "
    "'__mixed__') AS state_text "
    'FROM {source_table} '
    'WHERE satellite_id IN ({satellite_id:String}, {satellite_alias:String}) '
    'AND param_code IN ({codes}) '
    'AND onboard_time_bj >= toDateTime({start:String}, {tz:String}) '
    'AND onboard_time_bj < toDateTime({end:String}, {tz:String}) '
    "AND (value IS NOT NULL OR value_str != '') "
    'GROUP BY t, param_code ORDER BY t, param_code'
)
SQL_WIDE = (
    'SELECT t, param_code, count(*) n, round(avg(value),4) avg, '
    'round(min(value),4) mn, round(max(value),4) mx, '
    'round(stddevSamp(value),4) sd, '
    "multiIf(uniqExactIf(raw_state, raw_state != '') = 0, '', "
    "uniqExactIf(raw_state, raw_state != '') = 1, anyIf(raw_state, raw_state != ''), "
    "'__mixed__') AS state_text "
    'FROM (SELECT toStartOfInterval(onboard_time_bj, INTERVAL 1 MINUTE) AS t, '
    'tupleElement(kv,1) AS param_code, '
    'toFloat64OrNull(tupleElement(kv,2)) AS value, '
    "if(isNull(toFloat64OrNull(tupleElement(kv,2))), "
    "ifNull(tupleElement(kv,2), ''), '') AS raw_state "
    'FROM {source_table} ARRAY JOIN [{pairs}] AS kv '
    'WHERE spacecraft_id={spacecraft_id:String} '
    'AND onboard_time_bj >= toDateTime({start:String}, {tz:String}) '
    'AND onboard_time_bj < toDateTime({end:String}, {tz:String})) '
    "WHERE value IS NOT NULL OR raw_state != '' "
    'GROUP BY t, param_code ORDER BY t, param_code'
)


def native_query_iter(sql, params, timeout=120, max_execution_time=120):
    """通过 ClickHouse 原生端口逐行读取；连接只在生成器迭代期间保持。"""
    secure = os.environ.get('CH_NATIVE_SECURE', '').lower() in ('1', 'true', 'yes')
    allow_plaintext = os.environ.get('CH_ALLOW_PLAINTEXT', '').lower() in ('1', 'true', 'yes')
    if C.PASSWORD and not secure and not allow_plaintext:
        raise C.ClickHouseError('携带口令的原生明文连接已禁用；需显式允许或使用加密隧道')
    try:
        from clickhouse_driver import Client
    except ImportError as error:
        raise RuntimeError('原生 9000 取数需要安装 clickhouse-driver') from error
    client = Client(
        host=C.HOST, port=int(os.environ.get('CH_NATIVE_PORT', '9000')),
        user=C.USER, password=C.PASSWORD, database=C.DB, secure=secure,
        connect_timeout=min(timeout, 10), send_receive_timeout=timeout,
        settings={'server_side_params': True, 'max_execution_time': max_execution_time},
    )
    try:
        stream = client.execute_iter(sql, params=params, with_column_types=True)
        names = [name for name, _ in next(stream)]
        for values in stream:
            yield dict(zip(names, values))
    finally:
        client.disconnect()


def _settings(profile_dir=CONFIG.parent):
    profile_dir = Path(profile_dir)
    cfg = json.loads((profile_dir / CONFIG.name).read_text(encoding='utf-8'))
    components = load_components(profile_dir / COMPONENT_CONFIG.name,
                                 cfg['satellite_id'])
    codes = ([item['code'] for item in cfg['columns'].values()] +
             cfg['panel_voltage_codes'] + cfg['panel_current_codes'] +
             cfg['fetch_extra_codes'] +
             component_codes(components, cfg['columns']['bus_V']['code']))
    phase_state = cfg.get('phase_state')
    if phase_state:
        codes.append(phase_state['code'])
    battery_direction = cfg.get('battery_direction')
    if battery_direction:
        codes.extend([battery_direction['current_code'], battery_direction['switch_code']])
    codes = list(dict.fromkeys(codes))
    if not codes or any(re.fullmatch(r'[A-Z]{3}\d{3,4}(?:_b\d+(?:b\d+)?)?', code) is None
                        for code in codes):
        raise ValueError('取数参数代号格式无效')
    names = {item['code']: item['name'] for item in cfg['columns'].values()}
    if phase_state:
        names[phase_state['code']] = phase_state['name']
    if battery_direction:
        names[battery_direction['current_code']] = '蓄电池组母线电流检测'
        names[battery_direction['switch_code']] = '蓄电池组放电开关状态'
    names.update(cfg.get('other_params', {}))
    for spec in components:
        for code in component_codes([spec], cfg['columns']['bus_V']['code']):
            names.setdefault(code, spec.get('name', spec['id']))
    return cfg['satellite_id'], codes, names


def source_table(profile_dir=CONFIG.parent):
    """只允许已核对的同型长表；表名不能作为查询参数绑定。"""
    cfg = json.loads((Path(profile_dir) / CONFIG.name).read_text(encoding='utf-8'))
    table = cfg.get('source_table', 'master_satellite_01')
    if table not in ('master_satellite_01', 'master_satellite_02',
                     'telemetry_raw_01', 'telemetry_raw_02'):
        raise ValueError(f'不支持的遥测来源表: {table}')
    return table


def source_satellite_ids(profile_dir=CONFIG.parent):
    cfg = json.loads((Path(profile_dir) / CONFIG.name).read_text(encoding='utf-8'))
    primary = cfg['satellite_id']
    alias = cfg.get('source_satellite_alias', primary)
    if not isinstance(alias, str) or not alias:
        raise ValueError('来源卫星别名必须为非空字符串')
    return primary, alias


def wide_pairs(codes):
    """代号已由 _settings 校验，宽表列名只从该白名单构造。"""
    return ','.join(f"tuple('{code}', {code})" for code in codes)


def _minute(text):
    value = datetime.fromisoformat(text)
    if value.tzinfo is not None or value.second or value.microsecond:
        raise ValueError('时间边界须是无时区、整分钟格式，由 --source-timezone 指定时区')
    return value


def fetch(start, end, source_timezone, output=BASE / 'data_power_minute.csv',
          chunk_days=7, query_rows=None, profile_dir=CONFIG.parent,
          transport='native'):
    """写入成功后才替换目标文件；失败时保留上次数据与清单。"""
    start, end = _minute(start), _minute(end)
    if start >= end or chunk_days < 1:
        raise ValueError('须提供 start < end 且 chunk_days >= 1')
    ZoneInfo(source_timezone)
    if source_timezone != 'Asia/Shanghai':
        raise ValueError('onboard_time_bj 按北京时间存储，--source-timezone 必须为 Asia/Shanghai')
    if transport not in ('native', 'http'):
        raise ValueError('transport 必须是 native 或 http')
    satellite_id, codes, names = _settings(profile_dir)
    _, satellite_alias = source_satellite_ids(profile_dir)
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
            writer.writerow(['t', 'param_code', 'param_name', 'count',
                             'avg', 'mn', 'mx', 'sd', 'state_text'])
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
                    writer.writerow([row['t'], row['param_code'],
                                     names.get(row['param_code'], ''), row['n'],
                                     row['avg'], row['mn'], row['mx'], row['sd'],
                                     row.get('state_text', '')])
                    nrows += 1
                cursor = stop
        digest = sha256_file(temp)
        manifest = {
            'satellite_id': satellite_id,
            'source_satellite_ids': ([satellite_id] if wide_source else
                                     list(dict.fromkeys((satellite_id, satellite_alias)))),
            'source_id_column': 'spacecraft_id' if wide_source else 'satellite_id',
            'source': f'{C.DB}.{table}',
            'window_start_inclusive': start.isoformat(),
            'window_end_exclusive': end.isoformat(),
            'timezone': source_timezone,
            'time_column': 'onboard_time_bj',
            'aggregation_seconds': 60,
            'param_codes': codes,
            'row_count': nrows,
            'data_sha256': digest,
            'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
        }
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
    parser.add_argument('--start', required=True,
                        help='含起点，格式 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM')
    parser.add_argument('--end', required=True,
                        help='不含终点，格式 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM')
    parser.add_argument('--source-timezone', required=True,
                        help='onboard_time_bj 的时区，固定为 Asia/Shanghai')
    parser.add_argument('--chunk-days', type=int, default=7)
    parser.add_argument('--out', type=Path, default=BASE / 'data_power_minute.csv')
    parser.add_argument('--profile-dir', type=Path, default=CONFIG.parent)
    parser.add_argument('--transport', choices=('native', 'http'), default='native')
    args = parser.parse_args()
    manifest = fetch(args.start, args.end, args.source_timezone,
                     args.out, args.chunk_days, profile_dir=args.profile_dir,
                     transport=args.transport)
    print(f"已保存 {manifest['row_count']} 行；卫星 {manifest['satellite_id']}；"
          f"时间窗 [{manifest['window_start_inclusive']}, "
          f"{manifest['window_end_exclusive']}) {manifest['timezone']}")


if __name__ == '__main__':
    main()
