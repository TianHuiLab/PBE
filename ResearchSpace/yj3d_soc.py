#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云尖3D星 7.84 A 放电标定曲线与观测分钟的条件电压映射。"""
import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from yj3d_assess import BASE, PROFILE, sha256


def load_calibration(root=PROFILE):
    root = Path(root)
    config_path = root / 'config' / 'battery_curve.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    source = BASE.parent / config['source_csv']
    with source.open(encoding='utf-8-sig', newline='') as stream:
        rows = [row for row in csv.DictReader(stream)
                if any(value and value.strip() for value in row.values())]
    if len(rows) < 2:
        raise ValueError('云尖3D电池标定曲线至少需要两个数据点')
    current = np.array([float(row['放电电流/A'].strip()) for row in rows])
    voltage = np.array([float(row['电压/V'].strip()) for row in rows])
    capacity = np.array([float(row['容量/Ah'].strip()) for row in rows])
    dod = np.array([float(row['放电DOD'].strip().rstrip('%')) for row in rows])
    soc = np.array([float(row['SoC'].strip().rstrip('%')) for row in rows])
    if (not all(np.isfinite(part).all() for part in (current, voltage, capacity, dod, soc))
            or not np.allclose(current, current[0], atol=1e-6)
            or not np.all(np.diff(voltage) > 0)
            or not np.all(np.diff(capacity) > 0)
            or not np.all(np.diff(soc) > 0)
            or not np.allclose(dod + soc, 100, atol=0.01)
            or not 0 <= soc[0] < soc[-1] <= 100
            or not math.isclose(float(current[0]), config['calibration_discharge_current_A'], abs_tol=1e-6)):
        raise ValueError('云尖3D电池标定曲线的电流、单调性或 SOC/DoD 不一致')
    if config['current_match_tolerance_A'] <= 0:
        raise ValueError('电流匹配容差必须为正值')
    return {'config': config, 'source': source,
            'source_sha256': sha256(source), 'config_sha256': sha256(config_path),
            'current_A': float(current[0]), 'capacity_Ah': float(capacity[-1]),
            'voltage_V': voltage, 'soc_pct': soc,
            'points': [{'voltage_V': float(v), 'capacity_Ah': float(c),
                        'dod_pct': float(d), 'soc_pct': float(s)}
                       for v, c, d, s in zip(voltage, capacity, dod, soc)]}


def analyze_minutes(frame, calibration):
    """只映射明确地影、平均电流近标定值且平均电压在曲线内的分钟。"""
    current = calibration['current_A']
    tolerance = calibration['config']['current_match_tolerance_A']
    voltage = calibration['voltage_V']
    eclipse = frame['phase'].eq('地影')
    clear_phase = frame['phase_source'].eq('TMZS003') & frame['phase_conflict'].eq(0)
    paired = (frame['battery_current_n'].gt(0) & frame['battery_voltage_n'].gt(0)
              & frame['battery_current_mean'].between(0, 10)
              & frame['battery_voltage_mean'].between(21, 29.2))
    current_match = frame['battery_current_mean'].between(current-tolerance, current+tolerance)
    voltage_match = frame['battery_voltage_mean'].between(voltage[0], voltage[-1])
    observed_voltage = frame['battery_voltage_n'].gt(0) & np.isfinite(frame['battery_voltage_mean'])
    eligible = eclipse & clear_phase & paired & current_match & voltage_match
    selected = frame.loc[eligible, ['t', 'n_unique_samples', 'battery_current_mean',
                                    'battery_voltage_mean']].copy()
    selected['soc_7p84A_pct'] = np.interp(selected['battery_voltage_mean'],
                                         voltage, calibration['soc_pct'])
    selected['dod_7p84A_pct'] = 100 - selected['soc_7p84A_pct']
    selected.columns = ['t_beijing', 'unique_samples_in_minute',
                        'battery_current_mean_A', 'battery_voltage_mean_V',
                        'soc_7p84A_pct', 'dod_7p84A_pct']
    values = selected['soc_7p84A_pct']
    summary = {
        'method': 'minute-mean voltage piecewise-linear interpolation; no extrapolation or boundary clamping',
        'source_curve': calibration['config']['source_csv'],
        'source_curve_sha256': calibration['source_sha256'],
        'calibration_config_sha256': calibration['config_sha256'],
        'calibration_discharge_current_A': current,
        'calibration_capacity_Ah': calibration['capacity_Ah'],
        'current_match_tolerance_A': tolerance,
        'curve_voltage_range_V': [float(voltage[0]), float(voltage[-1])],
        'observed_minutes_voltage_within_curve': int((observed_voltage & voltage_match).sum()),
        'observed_minutes_voltage_above_curve': int((observed_voltage &
                                                     frame['battery_voltage_mean'].gt(voltage[-1])).sum()),
        'observed_minutes_voltage_below_curve': int((observed_voltage &
                                                     frame['battery_voltage_mean'].lt(voltage[0])).sum()),
        'observed_minutes_voltage_unavailable': int((~observed_voltage).sum()),
        'observed_eclipse_minutes': int(eclipse.sum()),
        'eclipse_minutes_above_curve_voltage': int((eclipse & frame['battery_voltage_mean'].gt(voltage[-1])).sum()),
        'matched_minutes': len(selected),
        'first_matched_beijing': str(selected['t_beijing'].iloc[0]) if len(selected) else None,
        'last_matched_beijing': str(selected['t_beijing'].iloc[-1]) if len(selected) else None,
        'soc_pct_min': float(values.min()) if len(values) else None,
        'soc_pct_median': float(values.median()) if len(values) else None,
        'soc_pct_p5': float(values.quantile(.05)) if len(values) else None,
        'soc_pct_p95': float(values.quantile(.95)) if len(values) else None,
        'dod_pct_max': float(selected['dod_7p84A_pct'].max()) if len(values) else None,
        'interpretation': ('Only selected observed minutes under an approximate 7.84 A discharge condition; '
                           'minute means do not guarantee sample-level current/voltage pairing. '
                           'Not a whole-period SOC, battery capacity measurement, or SOH estimate.')}
    return summary, selected
