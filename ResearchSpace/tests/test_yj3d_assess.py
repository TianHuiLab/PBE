"""云尖3D UTC 宽表去重、原值保留与短间隔积分检查。"""
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yj3d_assess as yj
import yj3d_boundary
import yj3d_report
import yj3d_soc
import yj3d_unit_power
import yj3d_units
import pandas as pd

try:
    from ._data import require_local_data
except ImportError:
    from _data import require_local_data


BASE = Path(__file__).resolve().parents[1]


class YJ3DAssessmentTests(unittest.TestCase):
    @require_local_data(
        BASE.parent / "Data" / "0010-云尖3D星" /
        "volt_capacity_3D_v0.csv")
    def test_dedicated_battery_curve_parses_and_validates(self):
        curve = yj3d_soc.load_calibration()
        self.assertEqual(len(curve['points']), 10)
        self.assertAlmostEqual(curve['current_A'], 7.84)
        self.assertAlmostEqual(curve['capacity_Ah'], 37.69)
        self.assertEqual(curve['points'][0]['soc_pct'], 10)
        self.assertEqual(curve['points'][-1]['soc_pct'], 100)

    @require_local_data(
        BASE.parent / "Data" / "0010-云尖3D星" /
        "volt_capacity_3D_v0.csv")
    def test_soc_interpolation_requires_matching_discharge_and_curve_range(self):
        curve = yj3d_soc.load_calibration()
        base = {'phase': '地影', 'phase_source': 'TMZS003', 'phase_conflict': 0,
                'battery_current_n': 4, 'battery_voltage_n': 4,
                'battery_current_mean': 7.84, 'battery_voltage_mean': 27.11,
                'n_unique_samples': 4}
        rows = [dict(base, t='2026-07-24 10:00:00'),
                dict(base, t='2026-07-24 10:01:00', battery_voltage_mean=28.19),
                dict(base, t='2026-07-24 10:02:00', battery_current_mean=6.5),
                dict(base, t='2026-07-24 10:03:00', phase='日照'),
                dict(base, t='2026-07-24 10:04:00', battery_voltage_n=0),
                dict(base, t='2026-07-24 10:05:00', phase_conflict=1)]
        summary, selected = yj3d_soc.analyze_minutes(pd.DataFrame(rows), curve)
        self.assertEqual(summary['matched_minutes'], 1)
        self.assertEqual(summary['observed_minutes_voltage_within_curve'], 4)
        self.assertEqual(summary['observed_minutes_voltage_above_curve'], 1)
        self.assertEqual(summary['observed_minutes_voltage_unavailable'], 1)
        self.assertEqual(summary['eclipse_minutes_above_curve_voltage'], 1)
        self.assertAlmostEqual(selected['soc_7p84A_pct'].iloc[0], 80)
        self.assertAlmostEqual(selected['dod_7p84A_pct'].iloc[0], 20)

    def test_utc_duplicate_range_and_phase_boundaries(self):
        cfg, design = yj.load_profile()
        codes = {key: cfg['measurements'][key]['code'] for key in yj.FIELDS}

        def record(t, state, voltage=28.0, solar=0.4):
            row = {'timestamp': t, cfg['phase_state']['code']: state,
                   cfg['discharge_switch']['code']: '开'}
            row.update({codes['bus_voltage']: str(voltage),
                        codes['bus_current']: '4',
                        codes['battery_voltage']: '28.2',
                        codes['battery_current']: '3.6',
                        codes['solar_current']: str(solar)})
            return row

        t0 = '2026-07-24 01:00:00.000'
        rows = [record(t0, '阴影区'), record(t0, '阴影区'),
                record('2026-07-24 01:00:01.000', '阴影区'),
                record('2026-07-24 01:00:02.000', '阴影区', voltage=103),
                record('2026-07-24 01:01:00.000', '光照区', solar=4)]
        minute_rows = []
        result = yj.assess_rows(rows, cfg, design, minute_rows.append)
        self.assertEqual(result['source_rows'], 5)
        self.assertEqual(result['unique_timestamps'], 4)
        self.assertEqual(result['duplicate_rows'], 1)
        self.assertEqual(result['observed_minutes'], 2)
        self.assertEqual(minute_rows[0]['t'], '2026-07-24 09:00:00')
        self.assertEqual(minute_rows[0]['bus_voltage_max'], 103)
        self.assertEqual(minute_rows[0]['solar_bus_proxy_W_n'], 2)
        self.assertEqual(result['numeric_and_range_counts']['bus_voltage']
                         ['outside_workbook_range'], 1)
        self.assertEqual(result['interval_energy_kWh']['地影']['intervals'], 1)
        self.assertEqual(result['interval_energy_kWh']['日照']['intervals'], 0)

    def test_conflicting_duplicate_field_is_not_silently_selected(self):
        cfg, design = yj.load_profile()
        base = {'timestamp': '2026-07-24 01:00:00.000',
                cfg['phase_state']['code']: '光照区',
                cfg['discharge_switch']['code']: '开'}
        for key in yj.FIELDS:
            base[cfg['measurements'][key]['code']] = '4'
        other = dict(base)
        other[cfg['measurements']['solar_current']['code']] = '7'
        minutes = []
        result = yj.assess_rows([base, other], cfg, design, minutes.append)
        self.assertEqual(result['conflicted_timestamps'], 1)
        self.assertEqual(minutes[0]['solar_current_n'], 0)

    def test_out_of_range_solar_is_plotted_but_not_used_for_phase_fallback(self):
        cfg, design = yj.load_profile()
        row = {'timestamp': '2026-07-24 01:00:00.000',
               cfg['phase_state']['code']: None,
               cfg['discharge_switch']['code']: '开'}
        for key in yj.FIELDS:
            row[cfg['measurements'][key]['code']] = '4'
        row[cfg['measurements']['solar_current']['code']] = '-24.8'
        minutes = []
        yj.assess_rows([row], cfg, design, minutes.append)
        self.assertEqual(minutes[0]['solar_current_min'], -24.8)
        self.assertEqual(minutes[0]['phase'], '未判定')

    def test_report_window_requires_consistent_sun_and_eclipse(self):
        first = pd.date_range('2026-07-25 18:00', periods=120, freq='min')
        second = pd.date_range('2026-07-26 03:00', periods=120, freq='min')
        times = first.append(second)
        phases = ['地影'] * 20 + ['日照'] * 100
        frame = pd.DataFrame({
            't': times,
            'phase': phases + phases,
            'phase_conflict': [1] * 120 + [0] * 120,
            'solar_current_mean': ([0.4] * 120 + [0.4] * 20 + [4.5] * 100),
        })
        selected, focus = yj3d_report.choose_window(frame)
        self.assertEqual(focus, second[20])
        self.assertEqual(selected['phase_conflict'].sum(), 0)

    def test_boundary_diagnosis_keeps_sun_switch_and_eclipse_gaps_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            channels = [dict(name=f'PV{i}', input_voltage=f'IN{i}',
                             output_voltage=f'OUT{i}', output_current=f'I{i}')
                        for i in range(8)]
            (root / 'config' / 'boundary_candidates.json').write_text(
                json.dumps({'pv_channels': channels}), encoding='utf-8')
            (root / 'data_power_minute.csv').write_text('sample', encoding='utf-8')
            base = dict(phase_source='TMZS003', phase_conflict=0,
                        battery_current_n=1, battery_voltage_n=1,
                        battery_voltage_mean=28, bus_current_mean=4,
                        bus_voltage_mean=28, solar_current_mean=0.2,
                        discharge_switch='开')
            frame = pd.DataFrame([
                dict(base, t='2026-07-24 10:00:00', phase='地影', battery_current_mean=4),
                dict(base, t='2026-07-24 10:01:00', phase='地影', battery_current_mean=4),
                dict(base, t='2026-07-24 10:03:00', phase='地影', battery_current_mean=-0.4),
                dict(base, t='2026-07-24 10:04:00', phase='日照', battery_current_mean=-1),
            ])
            frame['t'] = pd.to_datetime(frame['t'])
            result = yj3d_boundary.analyze(frame, root)
            self.assertEqual(result['eclipse_clear_valid_minutes'], 3)
            self.assertEqual(result['eclipse_battery_current_positive_over_0p2A_minutes'], 2)
            self.assertEqual(result['longest_contiguous_observed_eclipse_minutes'], 2)
            self.assertEqual(result['sun_discharge_switch_open_minutes'], 1)
            self.assertEqual(result['sun_battery_current_negative_below_minus_0p2A_minutes'], 1)
            self.assertEqual(result['bcr_output_voltage_current_pair_count'], 8)

    def test_unit_voltage_range_uses_working_upper_bound(self):
        self.assertEqual(yj3d_unit_power.upper_limit('21V~29.2V或0V~0.5V', 'V'), 29.2)
        self.assertEqual(yj3d_unit_power.voltage_intervals('21V~29.2V或0V~0.5V'),
                         [(21.0, 29.2), (0.0, 0.5)])
        self.assertEqual(yj3d_unit_power.upper_limit('0A~3A', 'A'), 3)
        self.assertIsNone(yj3d_unit_power.upper_limit('1 == 1', 'V'))

    @require_local_data(
        BASE.parent / "Data" / "0010-云尖3D星" /
        "云尖3D星_遥测大表0813.xlsx")
    def test_unit_inventory_keeps_partial_feeds_and_bcr_gnss_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = yj3d_units.build_inventory(folder)
        self.assertEqual(len(inventory['units']), 24)
        units = {unit['pl_channel']: unit for unit in inventory['units']}
        self.assertEqual(units[25]['measurement_status'], 'voltage_only')
        self.assertEqual(units[29]['measurement_status'], 'voltage_only')
        self.assertEqual(units[22]['banks']['A']['voltage']['code'], 'TMPA046')
        self.assertEqual(units[22]['banks']['A']['current']['code'], 'TMPA045')
        self.assertEqual(units[33]['banks']['A']['voltage']['sheet'], '0303-BCR_A')

    def test_cross_minute_phase_change_energy_is_undetermined(self):
        cfg, design = yj.load_profile()
        rows = []
        for t, state, solar in (
                ('2026-07-24 01:00:59.000', '阴影区', '0.4'),
                ('2026-07-24 01:01:00.000', '光照区', '4.0')):
            row = {'timestamp': t, cfg['phase_state']['code']: state,
                   cfg['discharge_switch']['code']: '开'}
            values = {'bus_voltage': '28', 'bus_current': '4',
                      'battery_voltage': '28.2', 'battery_current': '3.6',
                      'solar_current': solar}
            row.update({cfg['measurements'][key]['code']: value
                        for key, value in values.items()})
            rows.append(row)
        minutes = []
        result = yj.assess_rows(rows, cfg, design, minutes.append)
        self.assertEqual(result['interval_energy_kWh']['未判定']['intervals'], 1)
        self.assertEqual(result['interval_energy_kWh']['日照']['intervals'], 0)
        self.assertEqual(result['interval_energy_kWh']['地影']['intervals'], 0)


if __name__ == '__main__':
    unittest.main()
