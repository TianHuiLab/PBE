#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""完整48h的TMD009太阳阵总电流与TMD017母线电流逐样本分布，识别地影

观测窗口取环境变量 Q5_FROM / Q5_TO（'YYYY-MM-DD'），默认 2026-08-05 ~ 08-07；
输出行数上限取 Q5_LIMIT，默认 250（防止一次刷屏）。
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ch_http as C

sat = 'SCS-01-01'
day_from = os.environ.get('Q5_FROM', '2026-08-05')
day_to = os.environ.get('Q5_TO', '2026-08-07')
limit = int(os.environ.get('Q5_LIMIT', '250'))
rows = C.query(
    "SELECT station_time, param_code, max(value) v "
    "FROM telemetry_raw_01 WHERE satellite_id='%s' "
    "AND station_time>='%s 00:00:00' AND station_time<'%s 00:00:00' "
    "AND param_code IN ('TMD009','TMD019','TMD016','TMD017') "
    "GROUP BY station_time, param_code "
    "ORDER BY station_time LIMIT %d" % (sat, day_from, day_to, limit)
)
for r in rows:
    print("%s %-7s %s" % (r['station_time'][:19], r['param_code'], r['v']))
print(f"-- 共 {len(rows)} 行（LIMIT={limit}，可用 Q5_LIMIT 调整）")
