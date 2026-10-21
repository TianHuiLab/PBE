#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 3: 在真实遥测上运行 v2 能源平衡评估，生成报告并保存结果。"""
import pandas as pd, os, json, sys
from pathlib import Path
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from energy_balancer import EnergyBalanceEvaluator

raw = pd.read_csv(BASE / 'data_power_minute.csv')
raw['t'] = pd.to_datetime(raw['t'])
wide = raw.pivot_table(index='t', columns='param_code', values='avg').sort_index()

ev = EnergyBalanceEvaluator()
ev.load(wide)
df, m, h, phase = ev.run_all()

print("=" * 72)
print("SCS-01-01 整星能源平衡定量评估 (v2, 真实遥测)")
print("=" * 72)
for k, v in m.items():
    print(f"  {k:22s}: {v}")
print(f"\n  健康度: {h['hs']:.1f} 分 ({h['level']})   分项: { {k: round(v,1) for k,v in h['subscores'].items()} }")

print("\n-- 日照段日均功率 --")
for k, v in phase['sun'].items():
    if k.endswith('mean') or k.endswith('p5') or k.endswith('p50') or k.endswith('p95'):
        print("   %-18s %.2f" % (k, v))
print("\n-- 地影段日均功率 --")
for k, v in phase['ecl'].items():
    if k.endswith('mean') or k.endswith('p5') or k.endswith('p50') or k.endswith('p95'):
        print("   %-18s %.2f" % (k, v))

print("\n-- 能量统计 (kWh) --")
for name, e in [('日照', phase['e_sun']), ('地影', phase['e_ecl'])]:
    print("  %s: 发电=%.2f 负载=%.2f 母线需求=%.2f 电池净=%.2f 分流=%.2f" %
          (name, e['e_solar_kWh'], e['e_load_kWh'], e['e_bus_kWh'], e['e_batnet_kWh'], e['e_shunt_kWh']))

print("\n-- 闭合校验 --")
print("  注: e_residual(发电-负载-电池净-分流) 由构造恒等于 0，无校验能力;")
print("      有效校验是母线独立通道残差 e_bus - e_load - e_charge:")
for name, e in [('日照', phase['e_sun']), ('地影', phase['e_ecl'])]:
    denom = e['e_bus_kWh'] or float('nan')
    print("  %s: e_bus_residual = %+.2f kWh (占母线需求 %+.1f%%)" %
          (name, e['e_bus_residual_kWh'],
           100.0 * e['e_bus_residual_kWh'] / denom if denom else float('nan')))
print("  该残差需结合 TMD017 测量点拓扑解释（充电/分流电流是否流经测点），")
print("  与 09 号文档『TMD017 含充电』叙事的偏差列为待查项（SOC 标定后复核）。")

outdir = BASE / 'out'
os.makedirs(outdir, exist_ok=True)
df[['V_bus','V_bat','I_solar','P_solar','I_load','P_load','P_excess','P_shunt','SOC','DoD','phase']].to_csv(
    os.path.join(outdir, 'energy_v2_result.csv'))
# daily
dd = df.copy(); dd['date'] = dd.index.date
daily = dd.groupby('date').agg(P_solar=('P_solar','mean'), P_load=('P_load','mean'),
                               P_excess=('P_excess','mean'), SOC=('SOC','mean'),
                               P_shunt=('P_shunt','mean'), n_ecl=('phase', lambda s:(s=='地影').sum()))
daily.to_csv(os.path.join(outdir, 'energy_v2_daily.csv'))
print("\ndaily tail:\n", daily.tail(8).round(1).to_string())