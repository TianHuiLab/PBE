#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：母线电流(TMD017) vs 负载电流(TMD018)；确认负载定义与分流行为"""
import pandas as pd, numpy as np, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from energy_balancer import EnergyBalanceEvaluator

raw = pd.read_csv(Path(__file__).resolve().parent / 'data_power_minute.csv')
raw['t'] = pd.to_datetime(raw['t'])
wide = raw.pivot_table(index='t', columns='param_code', values='avg').sort_index()

ev = EnergyBalanceEvaluator()
ev.load(wide)
df = ev.compute_power()

print("== 负载定义对比（光照样本） ==")
sun = df[df['P_solar']>100]
if len(sun) == 0:
    raise SystemExit("未找到 P_solar>100W 的光照样本，无法对比负载口径")
# P_bus  = I_bus(TMD017) × V_bus：含充电的总母线需求口径
# P_load = I_load(TMD018) × V_bus：主负载口径（v2 采用）
print("TMD017 母线电流 I_bus : mean %.2f A  (P_bus=%.0f W)" % (sun['I_bus'].mean(), sun['P_bus'].mean()))
print("TMD018 负载电流 I_load: mean %.2f A  (P_load=%.0f W)" % (sun['I_load'].mean(), sun['P_load'].mean()))
print("太阳阵总电流     I_solar: mean %.2f A" % sun['I_solar'].mean())

# 太阳阵总电流 vs 各帆板电流之和
panelI = wide[['TMD010','TMD011','TMD012','TMD013','TMD014','TMD015']].sum(axis=1)
both = pd.DataFrame({'I_solar_total': wide['TMD009'], 'sum_panel_I': panelI}).dropna()
print("\n== 太阳阵总电流 vs 帆板电流之和 ==")
print(both.describe().T[['mean','min','max']])
print("corr:", round(both.corr().iloc[0,1],3))

# 电池电压分布（SOC/浮充状态判断）
vb = df['V_bat'].dropna()
print("\n== 蓄电池电压分布 ==")
print("mean %.3f  p05=%.3f p50=%.3f p95=%.3f max=%.3f" % (
    vb.mean(), vb.quantile(.05), vb.quantile(.5), vb.quantile(.95), vb.max()))
print("母线低压样本量: %d 条 V_bus<27" % ((df['V_bus']<27).sum()))
print("母线低压样本量: %d 条 V_bus<26" % ((df['V_bus']<26).sum()))

# P_bus 与 P_load 的对比（谁更接近可信负载）
print("\n== 光照下 P_bus(TMD017) vs P_load(TMD018) 直方 ===")
print(pd.DataFrame({'P_bus': sun['P_bus'], 'P_load': sun['P_load']}).describe().T[['mean','50%','min','max']])