#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 2: 基于真实遥测计算瞬时电源物理量 + 能量平衡核心指标（数据探索版）"""
import pandas as pd, numpy as np, os
from pathlib import Path

BASE = Path(__file__).resolve().parent
df = pd.read_csv(BASE / 'data_power_minute.csv')
df['t'] = pd.to_datetime(df['t'])
print("columns:", list(df.columns))
print("time range:", df['t'].min(), '->', df['t'].max())
print("n raw rows:", len(df), "unique minutes:", df['t'].nunique())
print("\nper param minutes:")
print(df.groupby('param_code').agg(n=('t','count'), first=('t','min'), last=('t','max')))

# 透视成宽表：每个分钟一线
wide = df.pivot_table(index='t', columns='param_code', values='avg')
print("\nwide shape:", wide.shape)
print(wide.head())

# 物理量计算
P = pd.DataFrame(index=wide.index)
V_bus = wide['TMD016']
P['P_solar_total'] = wide['TMD009'] * V_bus           # 太阳阵总电流×母线电压
P['P_load_bus']    = wide['TMD017'] * V_bus           # 母线电流×母线电压
P['P_battery']     = P['P_solar_total'] - P['P_load_bus']  # 电池功率（+充/-放）
P['V_bus'] = V_bus
P['V_bat'] = wide['TMD019']
print("\nPhysical metric stats (real data):")
print(P.describe().T[['mean','min','max']])

os.makedirs(BASE / 'out', exist_ok=True)
P.to_csv(BASE / 'out' / 'power_physical.csv')
print("\nanalysis rows:", len(P))