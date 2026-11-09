#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新口径数值核算（SCS-01-01 分钟级真实遥测）。

更新（用户 2026-11-02）：
  1) 太阳阵电压遥测 TMD003-TMD008 判为不可用 → 不参与任何计算；
  2) TMD017 = 太阳阵侧母线电流 → 光照 P_bus≈六板端原值功率和(诊断)，地影≈0；
  3) 光照太阳阵功率 = 太阳阵侧母线功率 + 蓄电池充电功率。
"""
import csv
import math
import os
import statistics as st

BASE = os.path.dirname(os.path.abspath(__file__))
DT = 60.0  # 秒，分钟聚合
PV = ["TMD003", "TMD004", "TMD005", "TMD006", "TMD007", "TMD008"]
PC = ["TMD010", "TMD011", "TMD012", "TMD013", "TMD014", "TMD015"]


def f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def load_wide():
    """分钟表 -> {t: {code: avg}}"""
    wide = {}
    with open(os.path.join(BASE, "data_power_minute.csv"), newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            wide.setdefault(row["t"], {})[row["param_code"]] = f(row["avg"])
    return wide


def load_phase():
    """t -> 'sun'/'ecl'/'na'"""
    ph = {}
    p = os.path.join(BASE, "out", "extra_minute.csv")
    if not os.path.isfile(p):
        return ph
    with open(p, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            s = (row.get("TMK5315_state") or "").strip()
            if s in ("1", "阳照区"):
                ph[row["t"]] = "sun"
            elif s in ("0", "阴影区"):
                ph[row["t"]] = "ecl"
    return ph


def main():
    wide = load_wide()
    ph = load_phase()
    sun, ecl = [], []
    for t, d in wide.items():
        p = ph.get(t)
        if p is None:
            continue
        vb = d.get("TMD016")
        ib = d.get("TMD017")
        il = d.get("TMD018")
        isolar = d.get("TMD009")
        if vb is None or ib is None:
            continue
        rec = {"vb": vb, "ib": ib, "il": il, "isolar": isolar}
        # 六板端原值功率和（诊断，电压遥测弃用，仅供参考）
        if all(all(c in d for c in grp) for grp in (PV, PC)):
            rec["panel_raw"] = sum(d[PV[i]] * d[PC[i]] for i in range(6))
        if p == "sun":
            sun.append(rec)
        else:
            ecl.append(rec)
    print("样本: 光照 %d / 地影 %d 分钟" % (len(sun), len(ecl)))

    def energy(field, recs):
        """Σ power * dt /3600/1000 -> kWh（分钟级）"""
        tot = 0.0
        for r in recs:
            v = r.get(field)
            if v is not None:
                tot += v * DT
        return tot / 3600.0 / 1000.0

    def med(xs):
        xs = [x for x in xs if x is not None]
        return st.median(xs) if xs else float("nan")

    # 地影：太阳阵侧母线电流应≈0
    ecl_ib = [r["ib"] for r in ecl]
    print("\n=== 地影期 TMD017（太阳阵侧母线电流）===")
    print("  均值 %.3f A  中位 %.3f A  |值|>1A 占比 %.1f%%" % (
        st.mean(ecl_ib), med(ecl_ib), 100.0 * sum(1 for x in ecl_ib if abs(x) > 1) / len(ecl_ib)))

    # 光照
    print("\n=== 光照期 ===")
    pbus = [r["vb"] * r["ib"] for r in sun]
    pload = [r["vb"] * r["il"] for r in sun if r["il"] is not None]
    pchg = [r["vb"] * (r["ib"] - r["il"]) for r in sun if r["il"] is not None]
    praw = [r["panel_raw"] for r in sun if r.get("panel_raw") is not None]
    pdi = [r["vb"] * (r["ib"] - r["il"]) for r in sun if r["il"] is not None]
    print("  母线功率 P=V×I_017  中位 %.1f W  能量 %.2f kWh" % (med(pbus), energy("dummy", [])))
    # 用积分
    e_pbus = sum(p * DT for p in pbus) / 3600.0 / 1000.0
    e_pload = sum(p * DT for p in pload) / 3600.0 / 1000.0
    e_pchg = sum(p * DT for p in pchg) / 3600.0 / 1000.0
    e_praw = sum(p * DT for p in praw) / 3600.0 / 1000.0
    print("  母线功率(入母线) 能量 %.2f kWh" % e_pbus)
    print("  负载功率 能量 %.2f kWh" % e_pload)
    print("  充电功率(=V×(I017-I018)) 能量 %.2f kWh" % e_pchg)
    print("  六板端原值功率和(诊断) 能量 %.2f kWh" % e_praw)
    print("  P_母线 / P_六板端 比值 = %.3f" % (e_pbus / e_praw if e_praw else float("nan")))
    print("  六板端中位=2×V_panel? 核验: 单板电压均值=%.3f V" % med([d[PV[0]] for d in sun if d.get(PV[0]) is not None]))
    # 新口径 3: 太阳阵功率 = 母线功率 + 充电功率
    e_array_new = e_pbus + e_pchg
    print("\n>>> 新口径3 太阳阵功率 = 母线功率 + 充电功率 = %.2f kWh" % e_array_new)
    print(">>> 对比: 帆板级(旧口径 I_solar×V_bus) = %.2f kWh" %
          (sum((r["vb"] * r["isolar"]) * DT for r in sun if r["isolar"] is not None) / 3600.0 / 1000.0))
    # 翻倍核查: P_bus 是否已含充电?
    print("\n  核查: I017 - I018 > 0 占比 = %.1f%% (正值=母线电流含充电支路)" % (
        100.0 * sum(1 for r in sun if r["il"] is not None and (r["ib"] - r["il"]) > 0) / max(1, sum(1 for r in sun if r["il"] is not None))))


if __name__ == "__main__":
    main()