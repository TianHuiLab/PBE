#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探索 chdb_3body 真实遥测数据——电源系统参数清单"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ch_http as C

sat = 'SCS-01-01'
rows = C.query(
    "SELECT param_code, max(param_name) AS pn, count(*) AS c "
    "FROM telemetry_raw_01 "
    "WHERE system_name='Power' AND satellite_id='%s' "
    "GROUP BY param_code ORDER BY param_code" % sat
)
print('# Power params for', sat, ':', len(rows))
for r in rows:
    print("%-18s %-40s n=%s" % (r['param_code'], (r['pn'] or '')[:40], r['c']))