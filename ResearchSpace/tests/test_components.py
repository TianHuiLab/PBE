"""部件配置、细分负载与功率边界的回归测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from components import (ComponentPlugin, evaluate_components, register_plugin,
                        validate_config)
from energy_balancer import EnergyBalanceEvaluator


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.wide = pd.DataFrame({
            'V': [28.0, 28.0, 28.0], 'SA_V': [30.0, 30.0, np.nan],
            'SA_I': [10.0, 8.0, 7.0], 'PAY_I': [2.0, np.nan, 1.0],
            'MODE': [0, 1, 9],
        }, index=pd.date_range('2026-01-01', periods=3, freq='min'))
        self.config = {
            'satellite_id': 'test-sat',
            'components': [
                {'id': 'array_a', 'role': 'solar_array', 'model': 'vi',
                 'voltage_code': 'SA_V', 'current_code': 'SA_I', 'enabled': True},
                {'id': 'payload', 'role': 'load', 'model': 'bus_current',
                 'current_code': 'PAY_I', 'enabled': True,
                 'mode': {'code': 'MODE', 'values': {'0': '待机', '1': '工作'}}},
                {'id': 'spare', 'role': 'load', 'model': 'bus_current',
                 'current_code': 'ABSENT', 'enabled': False},
            ],
        }

    def test_enabled_components_and_modes_keep_missing_unknown(self):
        specs = validate_config(self.config, 'test-sat')
        detail, summary = evaluate_components(self.wide, self.wide['V'] * 3,
                                              self.wide['V'] * 7, 'V', specs)
        self.assertEqual(list(summary['components']), ['array_a', 'payload'])
        self.assertEqual(detail['P_array_a_W'].iloc[0], 300)
        self.assertEqual(detail['P_payload_W'].iloc[0], 56)
        self.assertEqual(detail['mode_payload'].tolist(), ['待机', '工作', '未映射'])
        self.assertEqual(summary['components']['payload']['power_by_mode']['待机']['power_mean_W'], 56)
        self.assertTrue(np.isnan(detail['P_payload_W'].iloc[1]))
        self.assertTrue(np.isnan(detail['P_load_minus_branch_sum_W'].iloc[1]))
        self.assertTrue(np.isnan(detail['P_panel_terminal_sum_W'].iloc[2]))
        self.assertEqual(detail['P_panel_terminal_to_bus_difference_W'].iloc[0], 216)
        self.assertNotIn('P_shunt_W', detail.columns)
        self.assertEqual(summary['physical_balance']['status'], 'unavailable')

    def test_rejects_duplicate_ids_and_unverified_config(self):
        bad = dict(self.config)
        bad['components'] = [self.config['components'][0]] * 2
        with self.assertRaisesRegex(ValueError, '重复'):
            validate_config(bad, 'test-sat')
        with self.assertRaisesRegex(ValueError, '卫星'):
            validate_config(self.config, 'other-sat')
        with self.assertRaisesRegex(ValueError, 'unit=W'):
            validate_config({'satellite_id': 'test-sat', 'components': [
                {'id': 'direct', 'role': 'shunt', 'model': 'power',
                 'power_code': 'P', 'enabled': True}]}, 'test-sat')
        with self.assertRaisesRegex(ValueError, 'max_power_W'):
            validate_config({'satellite_id': 'test-sat', 'components': [
                {'id': 'bad_limit', 'role': 'load', 'model': 'bus_current',
                 'current_code': 'I', 'max_power_W': 0, 'enabled': True}]},
                           'test-sat')

    def test_scs01_load_rails_and_screening_limits_are_explicit(self):
        path = (Path(__file__).resolve().parents[1] / "satellites" /
                "SCS-01-01" / "config" / "components.json")
        config = json.loads(path.read_text(encoding="utf-8"))
        specs = validate_config(config, "SCS-01-01")
        by_id = {item["id"]: item for item in specs}

        self.assertEqual(by_id["fiber_gyro"]["plugin"], "vi")
        self.assertEqual(by_id["fiber_gyro"]["voltage_code"], "TMD059")
        self.assertEqual(by_id["uplink"]["voltage_code"], "TMD062")
        for spec in specs:
            if spec["role"] == "load":
                self.assertGreater(spec.get("max_power_W", 0), 0,
                                   spec["id"])

    def test_missing_enabled_sensor_is_reported_not_zero(self):
        specs = validate_config(self.config, 'test-sat')
        detail, summary = evaluate_components(self.wide.drop(columns='PAY_I'),
                                              self.wide['V'] * 3,
                                              self.wide['V'] * 7, 'V', specs)
        self.assertTrue(detail['P_payload_W'].isna().all())
        self.assertTrue(detail['P_load_minus_branch_sum_W'].isna().all())
        self.assertEqual(summary['components']['payload']['status'], 'missing_sensor')

    def test_minute_mode_switch_and_context_do_not_add_load_power(self):
        self.wide['MODE__mn'] = [0, 0, 9]
        self.wide['MODE__mx'] = [0, 1, 9]
        config = dict(self.config)
        config['components'] = self.config['components'] + [
            {'id': 'laser_mode', 'role': 'context', 'model': 'state',
             'enabled': True, 'mode': {'code': 'MODE', 'values': {'0': '休眠'}},
             'feature_codes': {'utilization': 'SA_I'}}]
        detail, summary = evaluate_components(self.wide, self.wide['V'] * 3,
                                              self.wide['V'] * 7, 'V',
                                              validate_config(config, 'test-sat'))
        self.assertEqual(detail['mode_payload'].tolist(),
                         ['待机', '分钟内切换', '未映射'])
        self.assertNotIn('P_laser_mode_W', detail)
        self.assertEqual(detail['feature_laser_mode_utilization'].iloc[0], 10)
        self.assertEqual(summary['components']['laser_mode']['n_observed_context'], 3)

    def test_battery_curve_can_be_swapped_with_satellite_profile(self):
        evaluator = EnergyBalanceEvaluator(v_soc_table=np.array([(20.0, .2),
                                                                 (30.0, .8)]))
        frame = pd.DataFrame({'V_bat': [25.0], 'P_excess': [10.0]})
        self.assertAlmostEqual(evaluator.estimate_battery(frame)['SOC'].iloc[0], 50.0)

    def test_text_state_from_database_is_used_without_numeric_average(self):
        self.wide['MODE'] = np.nan
        self.wide['MODE__state'] = ['待机', '__mixed__', '错误状态']
        detail, _ = evaluate_components(self.wide, self.wide['V'] * 3,
                                        self.wide['V'] * 7, 'V',
                                        validate_config(self.config, 'test-sat'))
        self.assertEqual(detail['mode_payload'].tolist(),
                         ['待机', '分钟内切换', '未映射'])

    def test_independent_physical_balance_can_have_nonzero_residual(self):
        wide = pd.DataFrame({'SA': [100.0], 'DIS': [10.0], 'CH': [20.0],
                             'SHUNT': [30.0], 'LOSS': [5.0], 'V': [28.0]})
        specs = validate_config({'satellite_id': 'test-sat', 'components': [
            {'id': name, 'role': role, 'model': 'power',
             'power_code': code, 'unit': 'W', 'enabled': True}
            for name, role, code in [
                ('actual', 'solar_pre_shunt_actual', 'SA'),
                ('discharge', 'battery_discharge', 'DIS'),
                ('charge', 'battery_charge', 'CH'),
                ('shunt', 'shunt', 'SHUNT'), ('loss', 'loss', 'LOSS')]]}, 'test-sat')
        detail, summary = evaluate_components(wide, pd.Series([70.0]),
                                              pd.Series([50.0]), 'V', specs,
                                              balance_boundary_verified=True)
        self.assertEqual(detail['P_array_S3R_loss_total_measured_W'].iloc[0], 35.0)
        self.assertEqual(detail['P_physical_residual_W'].iloc[0], 5.0)
        self.assertEqual(summary['physical_balance']['status'], 'measured_boundary')

        legacy = [dict(spec) for spec in specs]
        legacy[0]['role'] = 'solar_available'
        legacy_detail, legacy_summary = evaluate_components(
            wide, pd.Series([70.0]), pd.Series([50.0]), 'V', legacy,
            balance_boundary_verified=True)
        self.assertNotIn('P_physical_residual_W', legacy_detail)
        self.assertEqual(legacy_summary['physical_balance']['status'], 'unavailable')

    def test_custom_plugin_can_be_selected_per_satellite(self):
        register_plugin('test_calibrated_current', ComponentPlugin(
            ('current_code',), 'calibrated_bus_current',
            lambda wide, bus, spec: wide[bus] * wide[spec['current_code']] * 0.5,
            needs_bus_voltage=True))
        config = {'satellite_id': 'test-sat', 'components': [
            {'id': 'custom_load', 'role': 'load', 'plugin': 'test_calibrated_current',
             'current_code': 'PAY_I', 'enabled': True}]}
        specs = validate_config(config, 'test-sat')
        detail, summary = evaluate_components(self.wide, self.wide['V'] * 3,
                                              self.wide['V'] * 7, 'V', specs)
        self.assertEqual(detail['P_custom_load_W'].iloc[0], 28)
        self.assertEqual(summary['components']['custom_load']['plugin'],
                         'test_calibrated_current')
        config['components'][0]['enabled'] = False
        self.assertEqual(validate_config(config, 'test-sat'), [])

    def test_satellite_profile_imports_local_plugin_module(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / 'test_satellite_power_plugin.py'
            module.write_text(
                'from components import ComponentPlugin, register_plugin\n'
                "register_plugin('test_module_power', ComponentPlugin(\n"
                "    ('power_code',), 'module_power',\n"
                "    lambda wide, bus, spec: wide[spec['power_code']]))\n",
                encoding='utf-8')
            sys.path.insert(0, directory)
            try:
                config = {'satellite_id': 'test-sat',
                          'plugin_modules': ['test_satellite_power_plugin'],
                          'components': [
                              {'id': 'module_load', 'role': 'load',
                               'plugin': 'test_module_power',
                               'power_code': 'PAY_I', 'enabled': True}]}
                specs = validate_config(config, 'test-sat')
                detail, _ = evaluate_components(self.wide, self.wide['V'] * 3,
                                                self.wide['V'] * 7, 'V', specs)
                self.assertEqual(detail['P_module_load_W'].iloc[0], 2)
            finally:
                sys.path.remove(directory)


if __name__ == '__main__':
    unittest.main()
