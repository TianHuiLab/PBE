#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只在相邻星上采样间隔内积分，超过阈值的缺口不外推。

沿用历史键名：e_solar 为 TMD017 测点折算量；e_batnet 为它与负载测点
的代数差，充电支路位置未核实，不能解释为实测电池净电量。传入分钟
工况时，原始采样积分沿用该工况，避免与分钟报告使用不同判据。
"""
import numpy as np
import pandas as pd

from energy_balancer import EnergyBalanceEvaluator


def integrate_raw(path, max_gap_seconds=10.0, evaluator=None, minute_phase=None):
    if max_gap_seconds <= 0:
        raise ValueError('最大积分间隔必须为正数')
    raw = pd.read_csv(path)
    required = {'t', 'param_code', 'value'}
    if required - set(raw.columns):
        raise ValueError('原始遥测缺少 t/param_code/value 列')
    raw['t'] = pd.to_datetime(raw['t'], format='mixed', errors='raise')
    if raw.empty or raw['t'].isna().any() or raw.duplicated(['t', 'param_code']).any():
        raise ValueError('原始遥测为空、时间无效或 t/param_code 重复')
    wide = raw.pivot(index='t', columns='param_code', values='value').sort_index()
    evaluator = evaluator or EnergyBalanceEvaluator()
    evaluator.load(wide)
    d = evaluator.compute_power()
    seconds = np.diff(wide.index.values) / np.timedelta64(1, 's')
    gap_ok = (seconds > 0) & (seconds <= max_gap_seconds)
    solar = d['P_solar'].to_numpy(dtype=float)
    load = d['P_load'].to_numpy(dtype=float)
    shunt = d['P_shunt_diag'].to_numpy(dtype=float)
    current = d['I_solar'].to_numpy(dtype=float)
    valid = gap_ok & np.isfinite(solar[:-1]) & np.isfinite(solar[1:])
    valid &= np.isfinite(load[:-1]) & np.isfinite(load[1:])
    if minute_phase is None:
        phase = np.where(np.isfinite(current),
                         np.where(current >= evaluator.design['sun_I_threshold_A'], '日照', '地影'),
                         '未判定')
    else:
        if not minute_phase.index.is_unique:
            raise ValueError('分钟工况时间轴存在重复时间')
        mapped = minute_phase.reindex(wide.index.floor('min'))
        phase = mapped.fillna('未判定').to_numpy()
    interval_phase = np.where(phase[:-1] == phase[1:], phase[:-1], '未判定')
    factor = seconds / 3_600_000.0
    p_solar = (solar[:-1] + solar[1:]) / 2.0 * factor
    p_load = (load[:-1] + load[1:]) / 2.0 * factor
    p_shunt = (shunt[:-1] + shunt[1:]) / 2.0 * factor
    groups = {}
    for label, key in [('日照', 'e_sun'), ('地影', 'e_ecl'), ('未判定', 'e_unknown')]:
        mask = valid & (interval_phase == label)
        shunt_mask = mask & np.isfinite(p_shunt)
        es = float(p_solar[mask].sum())
        el = float(p_load[mask].sum())
        groups[key] = {'observed_seconds': float(seconds[mask].sum()),
                       'n_intervals': int(mask.sum()),
                       'e_solar_kWh': es, 'e_load_kWh': el,
                       'e_batnet_kWh': es - el,
                       'e_shunt_diag_kWh': float(p_shunt[shunt_mask].sum())}
    return {'method': 'adjacent_sample_trapezoid',
            'max_gap_seconds': float(max_gap_seconds),
            'n_timestamps': int(len(wide)),
            'n_gaps_over_limit': int((seconds > max_gap_seconds).sum()),
            'n_intervals_missing_power': int((gap_ok & ~valid).sum()),
            'phase': groups}
