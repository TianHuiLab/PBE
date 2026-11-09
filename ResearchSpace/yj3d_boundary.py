#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已核验的云尖3D分钟宽表生成方向与覆盖的证据诊断。"""
import json
from pathlib import Path

import pandas as pd

from yj3d_assess import PROFILE, sha256


def analyze(frame, root=PROFILE):
    root = Path(root)
    path = root / 'config' / 'boundary_candidates.json'
    candidates = json.loads(path.read_text(encoding='utf-8'))
    if len(candidates['pv_channels']) != 8:
        raise ValueError('BCR PV 通道配置必须为 8 路')
    codes = [entry[field] for entry in candidates['pv_channels']
             for field in ('input_voltage', 'output_voltage', 'output_current')]
    if len(codes) != len(set(codes)):
        raise ValueError('BCR PV 通道代号重复')
    clear = frame['phase_source'].eq('TMZS003') & frame['phase_conflict'].eq(0)
    valid = (clear & frame['battery_current_n'].gt(0)
             & frame['battery_voltage_n'].gt(0)
             & frame['battery_current_mean'].between(-10, 10)
             & frame['battery_voltage_mean'].between(21, 29.2))
    eclipse = frame.loc[valid & frame['phase'].eq('地影')]
    sun = frame.loc[valid & frame['phase'].eq('日照')]
    paired = eclipse.loc[eclipse['bus_current_mean'].between(0, 10)
                         & eclipse['bus_voltage_mean'].between(21, 29.2)]
    delta = paired['bus_current_mean'] - paired['battery_current_mean']
    all_eclipse = frame.loc[frame['phase'].eq('地影'), 't']
    runs = (all_eclipse.diff().dt.total_seconds().fillna(60).ne(60)).cumsum()
    run_lengths = all_eclipse.groupby(runs).size()
    exceptions = eclipse.loc[eclipse['battery_current_mean'].le(.2),
                             ['t', 'battery_current_mean', 'solar_current_mean']]
    return {
        'source_minute_sha256': sha256(root / 'data_power_minute.csv'),
        'candidate_config_sha256': sha256(path),
        'eclipse_clear_valid_minutes': len(eclipse),
        'eclipse_discharge_switch_open_minutes': int(eclipse['discharge_switch'].eq('开').sum()),
        'eclipse_battery_current_positive_over_0p2A_minutes': int(eclipse['battery_current_mean'].gt(.2).sum()),
        'eclipse_solar_current_below_1A_minutes': int(eclipse['solar_current_mean'].lt(1).sum()),
        'eclipse_solar_current_outside_workbook_range_minutes': int(
            (~eclipse['solar_current_mean'].between(0, 14)).sum()),
        'sun_clear_valid_minutes': len(sun),
        'sun_discharge_switch_open_minutes': int(sun['discharge_switch'].eq('开').sum()),
        'sun_battery_current_negative_below_minus_0p2A_minutes': int(sun['battery_current_mean'].lt(-.2).sum()),
        'sun_battery_current_positive_over_0p2A_minutes': int(sun['battery_current_mean'].gt(.2).sum()),
        'eclipse_battery_current_nonpositive_examples': [
            {'t_beijing': str(row.t), 'battery_current_mean_A': float(row.battery_current_mean),
             'solar_current_mean_A': float(row.solar_current_mean)}
            for row in exceptions.itertuples(index=False)],
        'eclipse_bus_battery_paired_minutes': len(paired),
        'eclipse_bus_minus_battery_current_median_A': float(delta.median()) if len(delta) else None,
        'eclipse_bus_minus_battery_current_p05_A': float(delta.quantile(.05)) if len(delta) else None,
        'eclipse_bus_minus_battery_current_p95_A': float(delta.quantile(.95)) if len(delta) else None,
        'observed_eclipse_runs': len(run_lengths),
        'longest_contiguous_observed_eclipse_minutes': int(run_lengths.max()) if len(run_lengths) else 0,
        'bcr_pv_input_voltage_channel_count': len(candidates['pv_channels']),
        'bcr_output_voltage_current_pair_count': len(candidates['pv_channels']),
        'bcr_pv_input_current_channel_count': 0,
        'interpretation': ('Telemetry is consistent with positive TMEA006 representing discharge in eclipse, '
                           'but the switch is also open in sun and does not alone identify current direction. '
                           'Electrical direction and bus-load coverage require the circuit topology. '
                           'The observed eclipse runs and single-rate curve do not identify in-orbit capacity or SOH.')}
