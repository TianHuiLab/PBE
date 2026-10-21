#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""近期轨道剖面分析：光照/地影周期、母线电压、太阳能电流

观测起始日期取环境变量 Q4_DAY（'YYYY-MM-DD'），默认 2026-08-06。
用法: Q4_DAY=2026-08-10 python q4_orbit.py
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ch_http as C

sat = 'SCS-01-01'
day = os.environ.get('Q4_DAY', '2026-08-06')
# 自 day 起的数据，每 5 分钟聚合关键量
rows = C.query(
    "SELECT toStartOfInterval(station_time, INTERVAL 5 MINUTE) AS t, "
    "max(CASE WHEN param_code='TMD016' THEN value END) AS busV, "
    "max(CASE WHEN param_code='TMD019' THEN value END) AS batV, "
    "avg(CASE WHEN param_code='TMD009' THEN value END) AS solI, "
    "avg(CASE WHEN param_code='TMD017' THEN value END) AS busI, "
    "avg(CASE WHEN param_code='TMD018' THEN value END) AS loadI "
    "FROM telemetry_raw_01 "
    "WHERE satellite_id='%s' AND station_time > '%s 00:00:00' "
    "  AND param_code IN ('TMD016','TMD019','TMD009','TMD017','TMD018','TMD003') "
    "GROUP BY t ORDER BY t" % (sat, day)
)
hdr = "%-20s %8s %8s %8s %8s %8s" % ('time','busV','batV','solI','busI','loadI')
print(hdr)
for r in rows:
    print("%-20s %8s %8s %8s %8s %8s" % (
        r['t'].replace('T',' ')[:16], round(r['busV'],2) if r['busV'] else '',
        round(r['batV'],2) if r['batV'] else '',
        round(r['solI'],2) if r['solI'] else '',
        round(r['busI'],2) if r['busI'] else '',
        round(r['loadI'],2) if r['loadI'] else ''))
