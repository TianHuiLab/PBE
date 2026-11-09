#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为双向电流测点生成「绝对值分钟均值」序列。

分钟表 data_power_minute.csv 的 avg 是有符号平均。磁力矩器这类线圈电流是
正负方波，分钟平均会把正负抵消（实测分钟中位 −0.004 A，折算功率接近 0），
不能用来算功耗。本脚本按 |I| 重新聚合，供 design_compare 的
measure.stat = "abs_current" 使用。

哪些代号需要绝对值守则，由 config/design_load_table.json 决定，不在代码里
写死：凡 measure.stat == "abs_current" 的 devices 都会进来。

输出：<workspace>/data_abs_current_minute.csv
      t, param_code, avg_abs, n
以及同名 .manifest.json 记录来源、时间窗、幅值上限与行数。
"""
import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import ch_http as C

BASE = Path(__file__).resolve().parent
DESIGN = BASE / 'config' / 'design_load_table.json'
SOURCE_TABLE = 'chdb_3body.telemetry_raw_01'
TIMEZONE = 'Asia/Shanghai'
# 剔除拼帧/标定异常（实测存在 946 A 与 1e36 量级跳点）；磁力矩器额定电流在 1 A 内
DEFAULT_MAX_ABS = 5.0

SQL = (
    'SELECT toStartOfInterval(onboard_time_bj, INTERVAL 1 MINUTE) AS t, '
    'param_code, round(avg(abs(value)), 6) AS avg_abs, count(*) AS n '
    f'FROM {SOURCE_TABLE} '
    'WHERE satellite_id = {satellite:String} '
    'AND param_code IN ({codes}) '
    "AND onboard_time_bj >= toDateTime({start:String}, {tz:String}) "
    "AND onboard_time_bj < toDateTime({end:String}, {tz:String}) "
    'AND value IS NOT NULL '
    'AND abs(value) <= {max_abs:Float64} '
    'GROUP BY t, param_code ORDER BY t, param_code'
)


def abs_current_codes(table=None):
    """从设计表配置取出需要绝对值守则的代号（去重保序）。"""
    table = table or json.loads(DESIGN.read_text(encoding='utf-8'))
    codes = []
    for row in table['rows']:
        measure = row.get('measure') or {}
        if measure.get('stat') != 'abs_current':
            continue
        for group in measure.get('devices') or []:
            for code in group:
                if code not in codes:
                    codes.append(code)
    return codes


def source_window(workspace):
    """沿用分钟表的时间窗，保证两份序列可逐分钟对齐。"""
    manifest = Path(workspace) / 'data_power_minute.manifest.json'
    if not manifest.exists():
        raise SystemExit(f'缺少 {manifest}，请先运行 s1_fetch.py 生成本地分钟表')
    meta = json.loads(manifest.read_text(encoding='utf-8'))
    return meta['window_start_inclusive'], meta['window_end_exclusive']


def fetch(workspace, start, end, chunk_days=30, max_abs=DEFAULT_MAX_ABS):
    codes = abs_current_codes()
    if not codes:
        raise SystemExit('配置里没有 stat="abs_current" 的测点，无需生成')
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    sql = (SQL.replace('{codes}', ','.join(f"'{code}'" for code in codes))
              .replace('{max_abs:Float64}', str(float(max_abs))))

    workspace = Path(workspace)
    output = workspace / 'data_abs_current_minute.csv'
    temp = output.with_suffix(output.suffix + '.tmp')
    manifest_path = output.with_name(output.stem + '.manifest.json')
    rows = 0
    seen = set()
    cursor = start_dt
    with temp.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['t', 'param_code', 'avg_abs', 'n'])
        while cursor < end_dt:
            stop = min(cursor + timedelta(days=chunk_days), end_dt)
            params = {'satellite': 'SCS-01-01', 'tz': TIMEZONE,
                      'start': cursor.strftime('%Y-%m-%d %H:%M:%S'),
                      'end': stop.strftime('%Y-%m-%d %H:%M:%S')}
            for row in C.query_iter(sql, params=params, timeout=180,
                                    max_execution_time=180):
                writer.writerow([row['t'], row['param_code'],
                                 row['avg_abs'], row['n']])
                seen.add(row['param_code'])
                rows += 1
            cursor = stop
    temp.replace(output)
    manifest = {
        'satellite_id': 'SCS-01-01',
        'source': SOURCE_TABLE,
        'time_column': 'onboard_time_bj',
        'timezone': TIMEZONE,
        'window_start_inclusive': start,
        'window_end_exclusive': end,
        'aggregation_seconds': 60,
        'statistic': 'mean(abs(value))',
        'max_abs_filter': max_abs,
        'param_codes': codes,
        'codes_with_data': sorted(seen),
        'row_count': rows,
        'fetched_at_utc': datetime.utcnow().isoformat() + '+00:00',
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding='utf-8')
    print(f'[ok] {output.name} · {rows} 行 · {len(seen)}/{len(codes)} 个代号有数据')
    missing = [code for code in codes if code not in seen]
    if missing:
        print(f'[warn] 无数据：{", ".join(missing)}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace-dir', default=str(BASE))
    parser.add_argument('--start', default=None, help='默认取分钟表清单的时间窗')
    parser.add_argument('--end', default=None)
    parser.add_argument('--chunk-days', type=int, default=30)
    parser.add_argument('--max-abs', type=float, default=DEFAULT_MAX_ABS)
    args = parser.parse_args()
    start, end = args.start, args.end
    if not start or not end:
        start, end = source_window(args.workspace_dir)
    fetch(args.workspace_dir, start, end, args.chunk_days, args.max_abs)


if __name__ == '__main__':
    main()
