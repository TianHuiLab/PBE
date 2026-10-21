#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断地影样本：蓄电池放电行为、电压跌落、负载需求"""
import pandas as pd, numpy as np, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from energy_balancer import EnergyBalanceEvaluator

raw = pd.read_csv(Path(__file__).resolve().parent / 'data_power_minute.csv')
raw['t'] = pd.to_datetime(raw['t'])
wide = raw.pivot_table(index='t', columns='param_code', values='avg').sort_index()
ev = EnergyBalanceEvaluator(); ev.load(wide)
df = ev.compute_power()

print("地影样本数(按I_solar<1A):", int((df['I_solar']<1).sum()))
ecl = df[df['I_solar']<1]
print("\n== 地影样本统计 ==")
print(ecl[['V_bus','V_bat','I_bus','I_load','P_solar','P_load','P_bus','P_excess']].describe().T[['mean','50%','min','max']])

# 地影时段连续块
mask = (df['I_solar']<1)
group_id = (mask != mask.shift()).cumsum()
blocks = mask[mask].groupby(group_id).size()
print("\n== 地影连续分钟数(块长) ==")
print(blocks.describe())
print("最长地影块 (minutes):", int(blocks.max()) if len(blocks) else 0)

# 放电深度估计：地影期间蓄电池压降推断
print("\n== 地影起始/末V_bat 采样 ==")
batch = df.loc[mask]
if len(batch):
    print(batch[['V_bat','V_bus','P_load']].head(60).to_string())