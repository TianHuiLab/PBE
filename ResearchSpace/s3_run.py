#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 3: 在真实遥测上运行 v2.1 能源平衡评估，生成报告并保存结果。

v2.1 口径：发电 = 母线电压 × 母线电流（TMD016×TMD017，S3R 送入母线的
有效发电）；分流不建模，改为诊断量 P_shunt_diag（帆板级-母线级差额）。
"""
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
print("SCS-01-01 整星能源平衡定量评估 (v2.1, 真实遥测)")
print("  口径: 发电 = V_bus(TMD016) × I_bus(TMD017, S3R送入母线); 负载 = V_bus × I_load(TMD018)")
print("=" * 72)
for k, v in m.items():
    print(f"  {k:22s}: {v}")
print(f"\n  健康度: {h['hs']:.1f} 分 ({h['level']})   分项: { {k: round(v,1) for k,v in h['subscores'].items()} }")

print("\n-- 日照段功率统计 --")
for k, v in phase['sun'].items():
    if k.endswith('mean') or k.endswith('p5') or k.endswith('p50') or k.endswith('p95'):
        print("   %-22s %.2f" % (k, v))
print("\n-- 地影段功率统计 --")
for k, v in phase['ecl'].items():
    if k.endswith('mean') or k.endswith('p5') or k.endswith('p50') or k.endswith('p95'):
        print("   %-22s %.2f" % (k, v))

print("\n-- 能量统计 (kWh) --")
for name, e in [('日照', phase['e_sun']), ('地影', phase['e_ecl'])]:
    print("  %s: 发电(入母线)=%.2f 负载=%.2f 电池净=%.2f 充电=%.2f 放电=%.2f" %
          (name, e['e_solar_kWh'], e['e_load_kWh'], e['e_batnet_kWh'],
           e['e_charge_kWh'], e['e_disch_kWh']))

print("\n-- 闭合校验 --")
print("  注: e_residual(发电-负载-电池净-分流诊断) 由构造恒等于 0, 无校验能力;")
print("      有效校验是帆板级-母线级独立测点差额 e_shunt_diag(应>=0):")
for name, e in [('日照', phase['e_sun']), ('地影', phase['e_ecl'])]:
    print("  %s: e_shunt_diag = %+.2f kWh (帆板级发电 %.2f - 入母线发电 %.2f)" %
          (name, e['e_shunt_diag_kWh'], e['e_solar_panel_kWh'], e['e_solar_kWh']))
print("  该差额 = 被 S3R 分流/PCU 变换损耗的等效能量; 持续为负说明电流测点间")
print("  存在系统偏差(需查测点拓扑), 见 13 号审查报告 TC2。")

outdir = BASE / 'out'
os.makedirs(outdir, exist_ok=True)
df[['V_bus', 'V_bat', 'I_solar', 'I_bus', 'P_solar', 'P_shunt_diag',
    'I_load', 'P_load', 'P_excess', 'SOC', 'DoD', 'phase']].to_csv(
    os.path.join(outdir, 'energy_v2_result.csv'))
# daily
dd = df.copy(); dd['date'] = dd.index.date
daily = dd.groupby('date').agg(P_solar=('P_solar', 'mean'), P_load=('P_load', 'mean'),
                               P_excess=('P_excess', 'mean'), SOC=('SOC', 'mean'),
                               P_shunt_diag=('P_shunt_diag', 'mean'),
                               n_ecl=('phase', lambda s: (s == '地影').sum()))
daily.to_csv(os.path.join(outdir, 'energy_v2_daily.csv'))
print("\ndaily tail:\n", daily.tail(8).round(1).to_string())
