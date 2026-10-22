#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断地影样本（v2.1 口径）：蓄电池放电行为、电压跌落、负载需求。

地影时太阳阵不发电：I_solar(TMD009)≈0、I_bus(TMD017)≈0，
负载由蓄电池经放电开关供电（放电流不经过 TMD017 测点），
故 v2.1 口径下地影段 P_excess = P_solar - P_load ≈ -P_load（全部计放电）。
"""
import pandas as pd, numpy as np, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from energy_balancer import EnergyBalanceEvaluator

raw = pd.read_csv(Path(__file__).resolve().parent / 'data_power_minute.csv')
raw['t'] = pd.to_datetime(raw['t'])
wide = raw.pivot_table(index='t', columns='param_code', values='avg').sort_index()
ev = EnergyBalanceEvaluator(); ev.load(wide)
df = ev.compute_power()

mask = (df['I_solar'] < 1.0)
print("地影样本数(按I_solar<1A):", int(mask.sum()))
ecl = df[mask]
if len(ecl):
    print("\n== 地影样本统计 ==")
    print(ecl[['V_bus', 'V_bat', 'I_solar', 'I_bus', 'I_load',
               'P_solar', 'P_load', 'P_excess']].describe().T[['mean', '50%', 'min', 'max']])

# 地影时段连续块
group_id = (mask != mask.shift()).cumsum()
blocks = mask[mask].groupby(group_id).size()
print("\n== 地影连续分钟数(块长) ==")
print(blocks.describe())
print("最长地影块 (minutes):", int(blocks.max()) if len(blocks) else 0)

# 放电深度估计：地影期间蓄电池压降推断
print("\n== 地影起始/末 V_bat 采样 ==")
batch = df.loc[mask]
if len(batch):
    print(batch[['V_bat', 'V_bus', 'I_load', 'P_load']].head(60).to_string())
