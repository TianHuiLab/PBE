#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 1: 拉取真实电源遥测，分钟级聚合落地为CSV，供评估框架迭代。"""
import sys, csv, time
from pathlib import Path
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import ch_http as C

sat = 'SCS-01-01'
# 电源核心遥测参数
params = {
 'TMD003':'太阳能阵A电压','TMD004':'太阳能阵B电压','TMD005':'太阳能阵C电压',
 'TMD006':'太阳能阵D电压','TMD007':'太阳能阵E电压','TMD008':'太阳能阵F电压',
 'TMD009':'太阳阵总电流',
 'TMD010':'太阳能阵A电流','TMD011':'B','TMD012':'C','TMD013':'D','TMD014':'E','TMD015':'F',
 'TMD016':'母线电压','TMD017':'母线电流','TMD018':'负载电流',
 'TMD019':'蓄电池电压','TMD023':'电推进A','TMD024':'电推进B','TMD066':'CXPD载荷','TMD067':'光纤陀螺',
}
ph = ','.join(["'%s'" % p for p in params])
print("Pulling minute-aggregated telemetry for", sat)
t0 = time.time()
rows = C.query(
    "SELECT toStartOfInterval(station_time, INTERVAL 1 MINUTE) AS t, "
    "  param_code, count(*) n, round(avg(value),4) avg, round(min(value),4) mn, round(max(value),4) mx, "
    "  round(stddevSamp(value),4) sd "
    "FROM telemetry_raw_01 "
    "WHERE satellite_id='%s' AND param_code IN (%s) AND value IS NOT NULL "
    "GROUP BY t, param_code ORDER BY t" % (sat, ph)
)
print("  rows:", len(rows), "elapsed %.1fs" % (time.time()-t0))
import csv
out = BASE / 'data_power_minute.csv'
with open(out, 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['t','param_code','param_name','count','avg','mn','mx','sd'])
    for r in rows:
        w.writerow([r['t'], r['param_code'], params.get(r['param_code'],''), r['n'], r['avg'], r['mn'], r['mx'], r['sd']])
print("saved", out)