#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证真实遥测数据的数值合理性、采样密度与时间覆盖"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ch_http as C
from collections import Counter

sat = 'SCS-01-01'
params = ['TMD003','TMD004','TMD005','TMD006','TMD007','TMD008',
          'TMD009','TMD010','TMD011','TMD012','TMD013','TMD014','TMD015',
          'TMD016','TMD017','TMD018','TMD019','TMD079','TMD080','TMD081',
          'TMD082','TMD083','TMD066','TMD067','TMD050','TMD056','TMU227','TMU228']
print("== value sample (SCS-01-01) ==")
rows = C.query(
    "SELECT station_time, param_code, value FROM telemetry_raw_01 "
    "WHERE satellite_id='%s' AND param_code IN ('TMD016','TMD017','TMD019','TMD003','TMD009') "
    "ORDER BY station_time LIMIT 20" % sat
)
for r in rows:
    print("%s %-8s %r" % (r['station_time'], r['param_code'], r['value']))

print("\n== per-param count & time range (TMD016) ==")
r = C.query("SELECT count(*) c, toDateTime(min(station_time)) mi, toDateTime(max(station_time)) ma "
            "FROM telemetry_raw_01 WHERE satellite_id='%s' AND param_code='TMD016'" % sat)[0]
print(r)

print("\n== station_time unique per hour for TMD016 (sampling freq) ==")
rows = C.query(
    "SELECT toStartOfInterval(station_time, INTERVAL 1 MINUTE) m, count(*) c "
    "FROM telemetry_raw_01 WHERE satellite_id='%s' AND param_code='TMD016' "
    "AND station_time > now() - INTERVAL 3 DAY "
    "GROUP BY m ORDER BY m DESC LIMIT 15" % sat, fmt='JSON')
for r in rows: print(r['m'], r['c'])