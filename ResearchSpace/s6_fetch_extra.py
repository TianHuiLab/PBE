#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按现有分钟数据相同口径，从 telemetry_raw_01 补拉 TMK5315(光照/地影标志) 与
TMH025(蓄电池组母线电流原始值)，保存为长表 CSV 以便与主分钟数据按 t 合并。

用法: CH_ALLOW_PLAINTEXT=1 python s6_fetch_extra.py
"""
import csv
from pathlib import Path

import ch_http as C

BASE = Path(__file__).resolve().parent
OUT = BASE / "out" / "extra_minute.csv"
START = "2025-12-27 01:52:00"
END = "2026-08-10 10:43:00"
SAT = "SCS-01-01"


def fetch(code, select_expr):
    sql = f"""
    SELECT toStartOfInterval(onboard_time_bj, INTERVAL 1 MINUTE) AS t,
           {select_expr}
    FROM telemetry_raw_01
    WHERE satellite_id = '{SAT}'
      AND param_code = '{code}'
      AND onboard_time_bj >= toDateTime('{START}')
      AND onboard_time_bj <  toDateTime('{END}')
    GROUP BY t
    ORDER BY t
    """
    return list(C.query_iter(sql, max_execution_time=300))


def main():
    # TMK5315: 字符串状态（1=阳照,0=阴影）
    k5 = fetch("TMK5315",
               "multiIf(uniqExactIf(value_str, value_str != '') = 0, '', "
               "uniqExactIf(value_str, value_str != '') = 1, "
               "anyIf(value_str, value_str != ''), '__mixed__') state_text")
    # TMH025: 原始值均值/最小/最大
    h25 = fetch("TMH025",
                "round(avg(value),4) val_avg, round(min(value),4) mn, "
                "round(max(value),4) mx")
    k5_map = {str(r["t"]): r for r in k5}
    h25_map = {str(r["t"]): r for r in h25}
    ts = sorted(set(k5_map) | set(h25_map))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t", "TMK5315_state",
                    "TMH025_avg", "TMH025_min", "TMH025_max"])
        for t in ts:
            a = k5_map.get(t, {})
            b = h25_map.get(t, {})
            w.writerow([
                t,
                a.get("state_text", ""),
                b.get("val_avg", ""),
                b.get("mn", ""),
                b.get("mx", ""),
            ])
    print(f"written {len(ts)} rows -> {OUT}")


if __name__ == "__main__":
    main()
