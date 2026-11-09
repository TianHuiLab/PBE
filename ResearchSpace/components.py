"""按卫星配置装配能源部件，并保留测量边界与缺测信息。

配置中的 plugin 选择计算插件；更换卫星或启停部件无需修改整星评估器。
电流乘母线电压是母线等效功率，只有独立支路电压时才用 vi。
"""
import importlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd


CONFIG = Path(__file__).resolve().parent / 'config' / 'components.json'
ROLES = {'solar_array', 'solar_available', 'solar_pre_shunt_actual',
         'load', 'battery_charge', 'battery_discharge',
         'shunt', 'loss', 'context'}

@dataclass(frozen=True)
class ComponentPlugin:
    """单机功率模型；calculate(wide, bus_voltage_code, spec) 返回 W 序列。"""
    fields: tuple[str, ...]
    power_basis: str
    calculate: Callable
    context: bool = False
    needs_bus_voltage: bool = False
    requires_watt_unit: bool = False


PLUGINS = {}


def register_plugin(name, plugin):
    """受信任的本地模块在导入时注册额外模型，重名插件不能覆盖。"""
    if (not isinstance(name, str) or re.fullmatch(r'[a-z][a-z0-9_]*', name) is None
            or not isinstance(plugin, ComponentPlugin) or name in PLUGINS
            or not isinstance(plugin.fields, tuple)
            or any(not isinstance(field, str) or not field for field in plugin.fields)
            or not isinstance(plugin.power_basis, str) or not plugin.power_basis
            or not callable(plugin.calculate)):
        raise ValueError(f'无效或重复的部件插件: {name}')
    PLUGINS[name] = plugin


def _plugin_name(spec):
    name = spec.get('plugin', spec.get('model'))
    if 'plugin' in spec and 'model' in spec and spec['plugin'] != spec['model']:
        raise ValueError(f"{spec.get('id')} 的 plugin/model 不一致")
    return name


def _plugin(spec):
    return PLUGINS[_plugin_name(spec)]


def load_config(path=CONFIG, satellite_id=None):
    """每次调用重新读取配置，可在同一进程中切换部件组合。"""
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    return validate_config(config, satellite_id)


def validate_config(config, satellite_id=None):
    if not isinstance(config, dict) or not isinstance(config.get('components'), list):
        raise ValueError('部件配置必须包含 components 列表')
    if satellite_id is not None and config.get('satellite_id') != satellite_id:
        raise ValueError('部件配置卫星 ID 与当前遥测不一致')
    if not isinstance(config.get('balance_boundary_verified', False), bool):
        raise ValueError('balance_boundary_verified 必须为布尔值')
    modules = config.get('plugin_modules', [])
    if not isinstance(modules, list) or any(
            not isinstance(module, str) or
            re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', module) is None
            for module in modules):
        raise ValueError('plugin_modules 必须是本地 Python 模块名列表')
    for module in modules:
        importlib.import_module(module)
    seen = set()
    for spec in config['components']:
        if not isinstance(spec, dict):
            raise ValueError('部件定义必须是对象')
        name = spec.get('id')
        if not isinstance(name, str) or re.fullmatch(r'[a-z][a-z0-9_]*', name) is None:
            raise ValueError('部件 id 必须为小写字母开头的字母、数字或下划线')
        if name in seen:
            raise ValueError(f'重复部件 id: {name}')
        seen.add(name)
        plugin_name = _plugin_name(spec)
        if spec.get('role') not in ROLES or plugin_name not in PLUGINS:
            raise ValueError(f'{name} 的 role/plugin 不受支持')
        plugin = _plugin(spec)
        if not isinstance(spec.get('enabled'), bool):
            raise ValueError(f'{name} 必须显式指定 enabled 布尔值')
        for field in plugin.fields:
            if not isinstance(spec.get(field), str) or not spec[field]:
                raise ValueError(f'{name} 缺少 {field}')
        if plugin.requires_watt_unit and spec.get('unit') != 'W':
            raise ValueError(f'{name} 的直接功率遥测须明确 unit=W')
        scale = spec.get('scale', 1.0)
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not np.isfinite(scale) or scale <= 0:
            raise ValueError(f'{name} 的 scale 必须为有限正数')
        max_power = spec.get('max_power_W')
        if (max_power is not None
                and (isinstance(max_power, bool)
                     or not isinstance(max_power, (int, float))
                     or not np.isfinite(max_power)
                     or max_power <= 0)):
            raise ValueError(f'{name} 的 max_power_W 必须为有限正数')
        mode = spec.get('mode')
        if mode is not None and (not isinstance(mode, dict) or
                                 not isinstance(mode.get('code'), str) or
                                 not isinstance(mode.get('values'), dict)):
            raise ValueError(f'{name} 的 mode 必须包含 code 和 values 映射')
        features = spec.get('feature_codes', {})
        if (not isinstance(features, dict) or
            any(re.fullmatch(r'[a-z][a-z0-9_]*', key) is None or
                not isinstance(code, str) or not code
                for key, code in features.items())):
            raise ValueError(f'{name} 的 feature_codes 必须为名称到遥测代号的映射')
        if plugin.context and (spec['role'] != 'context' or
                               (mode is None and not features)):
            raise ValueError(f'{name} 的 state 模型需要 context 角色和状态或特征遥测')
    return [dict(spec) for spec in config['components'] if spec['enabled']]


def required_codes(specs, bus_voltage_code=None):
    codes = []
    for spec in specs:
        plugin = _plugin(spec)
        codes.extend(spec[field] for field in plugin.fields)
        if spec.get('mode'):
            codes.append(spec['mode']['code'])
        codes.extend(spec.get('feature_codes', {}).values())
        if plugin.needs_bus_voltage and bus_voltage_code:
            codes.append(bus_voltage_code)
    return list(dict.fromkeys(codes))


def _numeric(wide, code):
    if code not in wide:
        return pd.Series(np.nan, index=wide.index, dtype=float)
    values = pd.to_numeric(wide[code], errors='coerce')
    return values.where(np.isfinite(values))


def _mode(wide, spec):
    config = spec.get('mode')
    if config is None:
        return pd.Series('未配置', index=wide.index)
    code = config['code']
    if code not in wide:
        return pd.Series('缺测', index=wide.index)
    values = wide[code]
    min_code, max_code = f'{code}__mn', f'{code}__mx'
    if min_code in wide and max_code in wide:
        low, high = _numeric(wide, min_code), _numeric(wide, max_code)
        values = values.where((low == high) & low.notna(), np.nan)
    labels = {str(key): value for key, value in config['values'].items()}
    def label(value):
        if pd.isna(value):
            return '缺测'
        if isinstance(value, (int, float, np.number)) and np.isfinite(value):
            key = str(int(value)) if float(value).is_integer() else str(value)
        else:
            key = str(value)
        return labels.get(key, '未映射')
    result = values.map(label)
    if min_code in wide and max_code in wide:
        low, high = _numeric(wide, min_code), _numeric(wide, max_code)
        result = result.mask(low.notna() & high.notna() & (low != high), '分钟内切换')
    state_code = f'{code}__state'
    if state_code in wide:
        allowed = set(labels.values())
        state = wide[state_code]
        text_labels = state.map(lambda value: '分钟内切换' if value == '__mixed__'
                                else value if value in allowed else '未映射')
        result = result.mask(state.notna(), text_labels)
    return result


def _vi_power(wide, bus_voltage_code, spec):
    voltage = _numeric(wide, spec['voltage_code'])
    current = _numeric(wide, spec['current_code'])
    return voltage.where(voltage >= 0) * current.where(current >= 0)


def _bus_current_power(wide, bus_voltage_code, spec):
    voltage = _numeric(wide, bus_voltage_code)
    current = _numeric(wide, spec['current_code'])
    return voltage.where(voltage >= 0) * current.where(current >= 0)


def _direct_power(wide, bus_voltage_code, spec):
    return _numeric(wide, spec['power_code']).where(lambda s: s >= 0)


def _state_power(wide, bus_voltage_code, spec):
    return pd.Series(np.nan, index=wide.index, dtype=float)


register_plugin('vi', ComponentPlugin(
    ('voltage_code', 'current_code'), 'minute_mean_voltage_x_mean_current_proxy',
    _vi_power))
register_plugin('bus_current', ComponentPlugin(
    ('current_code',), 'bus_voltage_x_branch_current_proxy',
    _bus_current_power, needs_bus_voltage=True))
register_plugin('power', ComponentPlugin(
    ('power_code',), 'direct_power', _direct_power, requires_watt_unit=True))
register_plugin('state', ComponentPlugin(
    (), 'state_only', _state_power, context=True))


def _component_power(wide, bus_voltage_code, spec):
    raw = _plugin(spec).calculate(wide, bus_voltage_code, spec)
    if not isinstance(raw, pd.Series) or not raw.index.equals(wide.index):
        raise ValueError(f"{spec['id']} 插件必须返回与输入时间轴相同的功率序列")
    return pd.to_numeric(raw, errors='coerce').where(lambda s: np.isfinite(s) & (s >= 0)) * spec.get('scale', 1.0)


def _complete_sum(detail, specs, role):
    names = [f"P_{spec['id']}_W" for spec in specs if spec['role'] == role]
    if not names:
        return pd.Series(np.nan, index=detail.index, dtype=float)
    return detail[names].sum(axis=1, min_count=len(names))


def evaluate_components(wide, solar_bus, load_total, bus_voltage_code, specs,
                        balance_boundary_verified=False):
    """返回逐样本部件功率和摘要；未知/缺测不按零功率参与差额。"""
    columns = {'P_solar_bus_W': solar_bus,
               'P_load_total_W': load_total,
               'P_battery_net_est_W': solar_bus - load_total}
    summary = {'components': {}, 'physical_balance': {'status': 'unavailable'}}
    for spec in specs:
        name = spec['id']
        plugin = _plugin(spec)
        needed = required_codes([spec], bus_voltage_code)
        power = _component_power(wide, bus_voltage_code, spec)
        if not plugin.context:
            columns[f'P_{name}_W'] = power
        if spec.get('mode'):
            columns[f'mode_{name}'] = _mode(wide, spec)
        for key, code in spec.get('feature_codes', {}).items():
            columns[f'feature_{name}_{key}'] = _numeric(wide, code)
        valid = power.dropna()
        missing_codes = [code for code in needed if code not in wide]
        context_observed = pd.Series(False, index=wide.index)
        if plugin.context:
            if spec.get('mode'):
                context_observed |= columns[f'mode_{name}'].ne('缺测')
            for key in spec.get('feature_codes', {}):
                context_observed |= columns[f'feature_{name}_{key}'].notna()
        status = ('missing_sensor' if missing_codes else
                  'observed_context' if plugin.context and context_observed.any() else
                  'no_valid_samples' if plugin.context else
                  'observed' if len(valid) else 'no_valid_samples')
        by_mode = {}
        if spec.get('mode') and len(valid):
            grouped = pd.DataFrame({'power': power,
                                    'mode': columns[f'mode_{name}']}).dropna(subset=['power'])
            for mode_name, group in grouped.groupby('mode', sort=True):
                by_mode[str(mode_name)] = {
                    'n_valid': int(len(group)),
                    'power_mean_W': float(group['power'].mean()),
                    'power_p95_W': float(group['power'].quantile(.95)),
                }
        summary['components'][name] = {
            'name': spec.get('name', name), 'role': spec['role'],
            'model': _plugin_name(spec), 'plugin': _plugin_name(spec),
            'context_only': plugin.context,
            'source_codes': needed, 'power_basis': plugin.power_basis,
            'status': status, 'missing_codes': missing_codes,
            'n_valid': int(len(valid)), 'n_total': int(len(wide)),
            'n_observed_context': int(context_observed.sum()) if plugin.context else None,
            'power_mean_W': float(valid.mean()) if len(valid) else None,
            'power_p95_W': float(valid.quantile(.95)) if len(valid) else None,
            'mode_counts': ({str(k): int(v) for k, v in
                             columns[f'mode_{name}'].value_counts(dropna=False).items()}
                            if spec.get('mode') else {}),
            'power_by_mode': by_mode,
        }
    detail = pd.DataFrame(columns, index=wide.index)
    detail['P_panel_terminal_sum_W'] = _complete_sum(detail, specs, 'solar_array')
    detail['P_sum_configured_branches_W'] = _complete_sum(detail, specs, 'load')
    detail['P_load_minus_branch_sum_W'] = load_total - detail['P_sum_configured_branches_W']
    detail['P_panel_terminal_to_bus_difference_W'] = detail['P_panel_terminal_sum_W'] - solar_bus
    summary['n_negative_load_minus_branch_sum'] = int(
        (detail['P_load_minus_branch_sum_W'] < 0).sum())
    summary['n_valid_load_branch_sum'] = int(detail['P_sum_configured_branches_W'].notna().sum())
    summary['load_breakdown_validation'] = 'not_reconciled_with_total_load'
    summary['n_valid_panel_terminal_sum'] = int(detail['P_panel_terminal_sum_W'].notna().sum())
    summary['n_valid_panel_terminal_to_bus_difference'] = int(
        detail['P_panel_terminal_to_bus_difference_W'].notna().sum())
    roles = {spec['role'] for spec in specs}
    required = {'solar_pre_shunt_actual', 'battery_charge', 'battery_discharge',
                'shunt', 'loss'}
    if balance_boundary_verified and required <= roles:
        # 两个独立测点在阵列/S3R 账本中合为一个边界总耗散项。
        array_s3r_loss_total = (_complete_sum(detail, specs, 'shunt')
                                 + _complete_sum(detail, specs, 'loss'))
        detail['P_array_S3R_loss_total_measured_W'] = array_s3r_loss_total
        residual = (_complete_sum(detail, specs, 'solar_pre_shunt_actual')
                    + _complete_sum(detail, specs, 'battery_discharge')
                    - load_total - _complete_sum(detail, specs, 'battery_charge')
                    - array_s3r_loss_total)
        detail['P_physical_residual_W'] = residual
        summary['physical_balance'] = {'status': 'measured_boundary',
                                       'n_valid': int(residual.notna().sum())}
    else:
        summary['physical_balance']['reason'] = '缺少分流前实际电输出、母线侧电池功率或阵列/S3R 边界总耗散的独立测量与边界核实'
    return detail, summary
