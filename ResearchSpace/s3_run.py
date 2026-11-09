#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在分钟遥测上运行能源评估，并保存带来源信息的结果。"""
import argparse
import hashlib
import importlib
import json
from pathlib import Path

import pandas as pd

from energy_balancer import (EnergyBalanceEvaluator, CONFIG_DIR, TELEMETRY_COLS,
                             _load_design, _load_cols, _load_v_soc_table)
from interval_energy import integrate_raw
from components import CONFIG as COMPONENT_CONFIG, load_config as load_components
from components import evaluate_components

BASE = Path(__file__).resolve().parent
REQUIRED_FIELDS = {'t', 'param_code', 'avg', 'mn', 'mx', 'count'}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_minute_data(path, satellite_id=None, mode_codes=(), solar_code=None,
                     core_codes=None):
    """校验分钟长表并转换为评估宽表，保留工况状态的分钟内极值。"""
    path = Path(path)
    raw = pd.read_csv(path)
    missing = REQUIRED_FIELDS - set(raw.columns)
    if missing:
        raise ValueError(f'分钟遥测缺少列: {sorted(missing)}')
    raw['t'] = pd.to_datetime(raw['t'], errors='raise')
    if raw['t'].isna().any() or raw.duplicated(['t', 'param_code']).any():
        raise ValueError('分钟遥测含无效时间或重复的 t/param_code')
    if raw.empty:
        raise ValueError('分钟遥测为空')
    anchor_code = solar_code or TELEMETRY_COLS['solar_I']
    core_codes = set(core_codes or TELEMETRY_COLS.values())
    anchor_times = raw.loc[raw['param_code'].isin(core_codes), 't'].drop_duplicates()
    if anchor_times.empty:
        raise ValueError('分钟遥测缺少整星核心测点，无法建立主时间轴')
    wide = raw.pivot(index='t', columns='param_code', values='avg').sort_index()
    # 单机状态可在其他分钟到达；不能据此制造整星能源观测分钟。
    wide = wide.loc[anchor_times.sort_values().to_numpy()]
    auxiliary = {}
    for code in dict.fromkeys([anchor_code, *mode_codes]):
        matching = raw.loc[raw['param_code'] == code].set_index('t')
        auxiliary[f'{code}__mn'] = pd.to_numeric(matching['mn'], errors='coerce')
        auxiliary[f'{code}__mx'] = pd.to_numeric(matching['mx'], errors='coerce')
        if code in mode_codes and 'state_text' in matching:
            auxiliary[f'{code}__state'] = matching['state_text'].replace('', pd.NA)
    wide = pd.concat([wide, pd.DataFrame(auxiliary, index=wide.index)], axis=1)

    digest = sha256_file(path)
    manifest_path = path.with_name(path.stem + '.manifest.json')
    provenance = {'status': 'unverified_legacy_input', 'data_sha256': digest,
                  'timezone': 'source_timestamp_unspecified'}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('data_sha256') != digest:
            raise ValueError('分钟遥测哈希与取数清单不一致')
        if satellite_id is not None and manifest.get('satellite_id') != satellite_id:
            raise ValueError('分钟遥测卫星 ID 与当前配置不一致')
        if manifest.get('time_column') != 'onboard_time_bj':
            raise ValueError('分钟遥测取数清单时间基准不是 onboard_time_bj')
        provenance = {'status': 'verified_fetch_manifest', **manifest}
    return wide, provenance


def _write_csv_atomic(frame, path, **kwargs):
    temp = path.with_suffix(path.suffix + '.tmp')
    frame.to_csv(temp, **kwargs)
    temp.replace(path)


def run_pipeline(data_path=BASE / 'data_power_minute.csv', outdir=BASE / 'out',
                 component_config_path=None, profile_dir=CONFIG_DIR,
                 raw_data_path=None):
    data_path, outdir = Path(data_path), Path(outdir)
    profile_dir = Path(profile_dir)
    telemetry_profile = json.loads((profile_dir / 'telemetry_params.json').read_text(
        encoding='utf-8'))
    satellite_id = telemetry_profile['satellite_id']
    phase_spec = telemetry_profile.get('phase_state') or {}
    phase_code = phase_spec.get('code')
    battery_direction = telemetry_profile.get('battery_direction') or {}
    cols, design = _load_cols(profile_dir), _load_design(profile_dir)
    evaluator = EnergyBalanceEvaluator(design=design, cols=cols,
                                       v_soc_table=_load_v_soc_table(profile_dir),
                                       phase_code=phase_code,
                                       phase_labels=(phase_spec.get('sun', '阳照区'),
                                                     phase_spec.get('eclipse', '阴影区')),
                                       battery_direction=battery_direction)
    component_config_path = (Path(component_config_path) if component_config_path
                             else profile_dir / COMPONENT_CONFIG.name)
    component_specs = load_components(component_config_path, satellite_id)
    component_profile = json.loads(component_config_path.read_text(encoding='utf-8'))
    balance_boundary_verified = component_profile.get('balance_boundary_verified', False)
    mode_codes = [spec['mode']['code'] for spec in component_specs if spec.get('mode')]
    if phase_code:
        mode_codes.append(phase_code)
    if battery_direction:
        mode_codes.extend([battery_direction['current_code'],
                           battery_direction['switch_code']])
    wide, provenance = load_minute_data(data_path, satellite_id, mode_codes,
                                        cols['solar_I'], cols.values())
    raw_stem = (data_path.stem[:-7] + '_raw'
                if data_path.stem.endswith('_minute') else data_path.stem + '_raw')
    raw_path = (Path(raw_data_path) if raw_data_path is not None
                else data_path.with_name(raw_stem + '.csv'))
    raw_manifest_path = raw_path.with_name(raw_path.stem + '.manifest.json')
    interval_energy = None
    raw_provenance = None
    if raw_path.exists() or raw_manifest_path.exists():
        if not raw_path.is_file() or not raw_manifest_path.is_file():
            raise ValueError('原始采样数据或取数清单缺失')
        raw_manifest = json.loads(raw_manifest_path.read_text(encoding='utf-8'))
        for key in ('satellite_id', 'source_satellite_ids', 'source_id_column',
                    'source', 'window_start_inclusive',
                    'window_end_exclusive', 'timezone', 'time_column'):
            if raw_manifest.get(key) != provenance.get(key):
                raise ValueError(f'原始采样与分钟遥测取数清单不一致: {key}')
        if raw_manifest.get('data_sha256') != sha256_file(raw_path):
            raise ValueError('原始采样哈希与取数清单不一致')
        raw_provenance = raw_manifest
    evaluator.load(wide)
    df, metrics, health, phase = evaluator.run_all()
    if raw_provenance:
        interval_energy = integrate_raw(raw_path, design['interval_max_gap_seconds'],
                                        evaluator=evaluator, minute_phase=df['phase'])
    health['data_provenance'] = provenance['status']
    component_detail, component_summary = evaluate_components(
        wide, df['P_solar'], df['P_load'], cols['bus_V'], component_specs,
        balance_boundary_verified=balance_boundary_verified)
    component_summary['time_basis'] = 'observed_minute_average_samples'
    component_summary['energy_integration'] = 'not_available_for_components_in_current_raw_file'
    panel_bus_difference = component_detail['P_panel_terminal_to_bus_difference_W'].dropna()
    daylight_difference = component_detail.loc[
        df['phase'].eq('日照'), 'P_panel_terminal_to_bus_difference_W'].dropna()
    component_summary['panel_to_bus_difference'] = {
        'status': 'diagnostic_only_unverified_boundary',
        'n_valid': int(len(panel_bus_difference)),
        'n_daylight_valid': int(len(daylight_difference)),
        'n_negative': int((panel_bus_difference < 0).sum()),
        'daylight_median_W': (float(daylight_difference.median())
                              if len(daylight_difference) else None),
        'basis': 'sum_of_panel_minute_mean_V_times_I_minus_bus_minute_mean_V_times_I',
    }

    outdir.mkdir(parents=True, exist_ok=True)
    result = df[['V_bus', 'V_bat', 'I_solar', 'I_bus', 'P_solar',
                 'P_shunt_diag', 'I_load', 'P_load', 'P_excess',
                 'SOC', 'DoD', 'SOC_observed', 'phase', 'phase_source', 'phase_conflict',
                 'battery_switch_state', 'battery_direction_rule',
                 'I_battery_switch_signed_A', 'P_battery_terminal_switch_W',
                 'battery_direction_conflict',
                 'P_charge', 'P_disch']].copy()
    result = result.rename(columns={'P_charge': 'P_charge_est',
                                    'P_disch': 'P_disch_est'})
    _write_csv_atomic(result, outdir / 'energy_v2_result.csv')
    _write_csv_atomic(component_detail, outdir / 'energy_v2_components.csv')
    day = df.assign(date=df.index.date).groupby('date').agg(
        P_solar=('P_solar', 'mean'), P_load=('P_load', 'mean'),
        P_excess=('P_excess', 'mean'), SOC=('SOC', 'mean'),
        P_shunt_diag=('P_shunt_diag', 'mean'),
        n_ecl=('phase', lambda s: int((s == '地影').sum())),
        n_unknown=('phase', lambda s: int((s == '未判定').sum())))
    _write_csv_atomic(day, outdir / 'energy_v2_daily.csv')

    config_hashes = {p.name: sha256_file(p) for p in sorted(profile_dir.glob('*.json'))}
    config_hashes['active_components.json'] = sha256_file(component_config_path)
    plugin_module_hashes = {}
    for module_name in component_profile.get('plugin_modules', []):
        module_file = getattr(importlib.import_module(module_name), '__file__', None)
        if not module_file or not Path(module_file).is_file():
            raise ValueError(f'插件模块源码不可用: {module_name}')
        plugin_module_hashes[module_name] = sha256_file(module_file)
    phase_interpretation = (f'光照/地影优先按 {phase_code} 状态，缺测由 '
                            f'{cols["solar_I"]} 回退；' if phase_code else
                            f'光照/地影按 {cols["solar_I"]} 电流判定；')
    battery_interpretation = (f'{battery_direction["switch_code"]} 为“'
                              f'{battery_direction["discharge_state"]}”时，按用户规则给 '
                              f'{battery_direction["current_code"]} 幅值赋放电正号；'
                              '开关断开不自动判为充电。' if battery_direction else '')
    summary = {'satellite_id': satellite_id,
               'spacecraft_id': telemetry_profile.get('spacecraft_id', satellite_id),
               'soc_curve_status': telemetry_profile.get('soc_curve_status',
                                                         'confirmed_for_satellite'),
               'metrics': metrics, 'health': health,
               'components': component_summary,
               'phase': {name: {k: v for k, v in block.items() if k != 'df'}
                         for name, block in phase.items() if name != 'df'},
               'provenance': provenance, 'config_sha256': config_hashes,
               'plugin_module_sha256': plugin_module_hashes,
               'raw_provenance': raw_provenance,
               'interval_energy': interval_energy,
               'code_sha256': {p.name: sha256_file(p) for p in
                               (BASE / 's1_fetch.py', BASE / 's1_fetch_raw.py',
                                BASE / 'energy_balancer.py', BASE / 'interval_energy.py',
                                BASE / 'components.py', BASE / 's3_run.py')},
               'interpretation': (
                   'phase 中能量是每分钟名义 60 秒等效值；interval_energy 只对相邻原始采样短间隔作梯形积分。'
                   '设计方案为不调节母线 + S³R 分流，蓄电池电压钳位母线；光照期先供负载、再充电、最后分流，'
                   '阴影期由电池供电，日照短时不足时联合供电。' + phase_interpretation +
                   'P_solar 历史字段仅为 TMD016×TMD017 测点折算功率；'
                   'TMD017 是否覆盖 PCDU 充电支路尚需连接图确认。P_excess、P_charge、P_disch 及对应能量字段'
                   '均来自 TMD017 与负载测点的代数差额，不能当作独立电池充放电测量。'
                   '帆板端与 TMD017 测点差额可能包含测点之前分出的充电支路，不能直接解释为 S³R 总耗散。'
                   + battery_interpretation +
                   '电池电流的原生符号和测点位置尚未核实；母线损耗和整星物理闭合仍不可计算。'
                   '单机支路功率仅对已有观测分钟有效。')}
    summary_path = outdir / 'energy_v2_summary.json'
    temp = summary_path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                               allow_nan=False), encoding='utf-8')
    temp.replace(summary_path)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=BASE / 'data_power_minute.csv')
    parser.add_argument('--out', type=Path, default=BASE / 'out')
    parser.add_argument('--profile-dir', type=Path, default=CONFIG_DIR)
    parser.add_argument('--raw-data', type=Path,
                        help='原始采样 CSV；默认按分钟文件名推导')
    args = parser.parse_args()
    summary = run_pipeline(args.data, args.out, profile_dir=args.profile_dir,
                           raw_data_path=args.raw_data)
    m, h = summary['metrics'], summary['health']
    print(f"{summary['satellite_id']} 能源评估：{m['n_samples']} 个观测分钟，"
          f"明确工况 {m['n_phase_classified']}，未判定 {m['n_phase_unknown']}")
    print(f"日历分钟覆盖率 {m['calendar_minute_coverage']:.2%}；"
          f"来源 {summary['provenance']['status']}")
    score = '无法评分' if h['hs'] is None else f"{h['hs']:.1f} 分（{h['level']}）"
    print(f"观测样本评分：{score}；整段时期结论：未评定")
    for name, key in [('日照', 'e_sun'), ('地影', 'e_ecl'), ('未判定', 'e_unknown')]:
        e = (summary['interval_energy']['phase'][key] if summary['interval_energy']
             else summary['phase'][key])
        method = '原始采样间隔积分' if summary['interval_energy'] else '名义 60 秒等效'
        print(f"{name}（{method}）: TMD017 测点折算 {e['e_solar_kWh']:.3f} kWh，"
              f"负载 {e['e_load_kWh']:.3f} kWh，"
              f"两测点差额（非电池电量）{e['e_batnet_kWh']:.3f} kWh")


if __name__ == '__main__':
    main()
