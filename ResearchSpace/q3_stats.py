#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深度分析真实电源遥测：关键参数统计（量级/波动/异常）"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ch_http as C

sat = 'SCS-01-01'
params = ['TMD003','TMD004','TMD005','TMD006','TMD007','TMD008',
          'TMD009','TMD010','TMD011','TMD012','TMD013','TMD014','TMD015',
          'TMD016','TMD017','TMD018','TMD019',
          'TMD023','TMD024','TMD025','TMD026','TMD027','TMD028','TMD029','TMD030',
          'TMD031','TMD032','TMD033','TMD034','TMD043','TMD044',
          'TMD045','TMD046','TMD047','TMD048',          # reaction wheels
          'TMD050','TMD052','TMD054','TMD056',          # TTC currents
          'TMD066','TMD067',
          'TMD079','TMD080','TMD081','TMD082','TMD083'   # temps
          ]
ph = ','.join(["'%s'" % p for p in params])
rows = C.query(
    "SELECT param_code, count(*) n, "
    "  round(avg(value),4) mean, round(min(value),4) mn, round(max(value),4) mx, "
    "  round(stddevSamp(value),4) sd "
    "FROM telemetry_raw_01 WHERE satellite_id='%s' AND param_code IN (%s) "
    "GROUP BY param_code ORDER BY param_code" % (sat, ph)
)
print("%-10s %9s %10s %10s %10s %10s" % ('param','n','mean','min','max','std'))
for r in rows:
    print("%-10s %9d %10s %10s %10s %10s" % (r['param_code'], r['n'], r['mean'], r['mn'], r['mx'], r['sd']))

# 电池电压/母线电压时变性
print("\n== 近期24h母线电压、电池电压、太阳阵总电流的分钟均值采样 ==")
rows2 = C.query(
    "SELECT toStartOfInterval(station_time, INTERVAL 30 MINUTE) t, "
    "  max(CASE WHEN param_code='TMD016' THEN value END) busV, "
    "  max(CASE WHEN param_code='TMD019' THEN value END) batV, "
    "  max(CASE WHEN param_code='TMD009' THEN value END) solA "
    "FROM telemetry_raw_01 WHERE satellite_id='%s' AND station_time > now()-INTERVAL 24 HOUR "
    "  AND param_code IN ('TMD016','TMD019','TMD009') GROUP BY t ORDER BY t" % sat
)
for r in rows2:
    print("%s busV=%s batV=%s solA=%s" % (r['t'], r['busV'], r['batV'], r['solA']))