#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断（v2.1 口径）：母线电流(TMD017) vs 负载电流(TMD018) vs 帆板总电流(TMD009)。

S3R 拓扑实证：TMD017 是"太阳阵经 S3R 送入母线的单向电流"，
  - 日照: I_bus(TMD017) = I_load(TMD018) + I_charge（母线闭合）
  - 地影: I_bus ≈ 0（太阳阵不发电，电池放电不经过该测点）
  - I_solar(TMD009) - I_bus(TMD017) = 被 S3R 分流/PCU 损耗的部分（诊断量）
"""
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

sun = df[df['I_solar'] >= 1.0]
if len(sun) == 0:
    raise SystemExit("未找到光照样本（I_solar>=1A），无法对比负载口径")
ecl = df[df['I_solar'] < 1.0]

print("== 日照样本（I_solar>=1A，%d 条）三口径对比 ==" % len(sun))
print("帆板总电流 I_solar(TMD009): mean %.2f A" % sun['I_solar'].mean())
print("母线电流   I_bus  (TMD017): mean %.2f A  → P_solar(v2.1 发电口径)=%.0f W" % (
    sun['I_bus'].mean(), sun['P_solar'].mean()))
print("负载电流   I_load (TMD018): mean %.2f A  → P_load=%.0f W" % (
    sun['I_load'].mean(), sun['P_load'].mean()))
print("应有充电电流 I_bus-I_load : mean %.2f A（应>=0；S3R 分流不流经母线）" % (
    (sun['I_bus'] - sun['I_load']).mean()))
print("分流诊断 I_solar-I_bus    : mean %.2f A（帆板级-母线级差额）" % (
    (sun['I_solar'] - sun['I_bus']).mean()))

# 太阳阵总电流 vs 各帆板电流之和
panelI = wide[['TMD010', 'TMD011', 'TMD012', 'TMD013', 'TMD014', 'TMD015']].sum(axis=1)
both = pd.DataFrame({'I_solar_total': wide['TMD009'], 'sum_panel_I': panelI}).dropna()
print("\n== 太阳阵总电流 vs 帆板电流之和 ==")
print(both.describe().T[['mean', 'min', 'max']])
print("corr:", round(both.corr().iloc[0, 1], 3))

# 电池电压分布（SOC/浮充状态判断）
vb = df['V_bat'].dropna()
print("\n== 蓄电池电压分布 ==")
print("mean %.3f  p05=%.3f p50=%.3f p95=%.3f max=%.3f" % (
    vb.mean(), vb.quantile(.05), vb.quantile(.5), vb.quantile(.95), vb.max()))
print("母线低压样本量: %d 条 V_bus<27" % ((df['V_bus'] < 27).sum()))
print("母线低压样本量: %d 条 V_bus<26" % ((df['V_bus'] < 26).sum()))

print("\n== 地影样本（%d 条）母线电流行为（应≈0，电池放电不经 TMD017）==" % len(ecl))
if len(ecl):
    print("I_bus(TMD017) mean %.2f A（max %.2f）; I_load(TMD018) mean %.2f A" % (
        ecl['I_bus'].mean(), ecl['I_bus'].max(), ecl['I_load'].mean()))
