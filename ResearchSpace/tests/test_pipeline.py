"""取数边界、分钟宽表与运行清单的离线集成检查。"""
import csv
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import s1_fetch
import s1_fetch_raw
import s3_run
import gen_charts


class PipelineTests(unittest.TestCase):
    def test_tmh025_raw_plots_keep_outliers_with_conditional_product(self):
        times = ['2026-02-02 14:20:00', '2026-02-02 14:21:00']
        result = [dict(t=t, phase='日照', P_solar='730', P_load='240', V_bat='28',
                       P_battery_terminal_switch_W='', battery_switch_state='通')
                  for t in times]
        minute = []
        for t in times:
            for code in (*range(3, 9), *range(10, 16)):
                minute.append(dict(t=t, param_code=f'TMD{code:03d}',
                                   avg='28' if code < 10 else '4', count='2'))
            minute.append(dict(t=t, param_code='TMH025', avg='950',
                               mn='949', mx='65510' if t == times[1] else '951'))
        nearby = gen_charts.satellite_02_power_nearby(
            result, minute, '日照', times[0], times[1], times[0])
        overview = gen_charts.tmh025_raw_overview(minute)
        self.assertEqual(nearby['raw_current_focus'], 950)
        self.assertEqual(nearby['svg'].count('<path '), 3)
        self.assertEqual(nearby['battery_raw_product_focus'], 26600)
        self.assertIn('data-satellite02-sun-battery-raw-product', nearby['battery_svg'])
        self.assertEqual(nearby['battery_svg'].count('<path '), 1)
        self.assertEqual(overview['maximum'], 65510)
        self.assertEqual(overview['n_above_100'], 2)
        self.assertIn('data-tmh025-raw-overview', overview['svg'])

    def test_satellite_profiles_fetch_from_their_own_wide_tables(self):
        profiles = [
            ('SCS-01-01', s3_run.BASE / 'satellites' / 'SCS-01-01' / 'config'),
            ('SCS-01-02', s3_run.BASE / 'satellites' / 'SCS-01-02' / 'config'),
        ]
        with tempfile.TemporaryDirectory() as folder:
            for satellite_id, profile in profiles:
                calls = []

                def fake_query(sql, **kwargs):
                    calls.append((sql, kwargs['params']))
                    if ' AS state_text ' in sql:
                        yield {'t': kwargs['params']['start'], 'param_code': 'TMD016',
                               'n': 1, 'avg': 28.0, 'mn': 28.0, 'mx': 28.0,
                               'sd': None, 'state_text': ''}
                    else:
                        yield {'t': kwargs['params']['start'],
                               'param_code': 'TMD016', 'v': 28.0}

                minute = Path(folder) / f'{satellite_id}_minute.csv'
                raw = Path(folder) / f'{satellite_id}_raw.csv'
                minute_manifest = s1_fetch.fetch(
                    '2026-01-01', '2026-01-02', 'Asia/Shanghai', minute,
                    profile_dir=profile, query_rows=fake_query)
                raw_manifest = s1_fetch_raw.fetch_raw(
                    '2026-01-01', '2026-01-02', 'Asia/Shanghai', raw,
                    profile_dir=profile, query_rows=fake_query)
                expected_table = 'master_satellite_' + satellite_id[-2:]
                self.assertEqual(minute_manifest['source'],
                                 f'chdb_3body.{expected_table}')
                self.assertEqual(raw_manifest['source'], minute_manifest['source'])
                self.assertEqual(minute_manifest['source_id_column'], 'spacecraft_id')
                self.assertEqual(raw_manifest['source_id_column'], 'spacecraft_id')
                self.assertEqual(minute_manifest['satellite_id'], satellite_id)
                self.assertEqual(len(calls), 2)
                for sql, params in calls:
                    self.assertIn(f'FROM {expected_table} ARRAY JOIN', sql)
                    self.assertEqual(params['spacecraft_id'], satellite_id)
                    self.assertNotIn('satellite_id', params)

    def test_synthetic_end_to_end_including_chart_document(self):
        """CI 无真实数据时仍验证 CSV→工况→SOC→图表→HTML 契约。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            shutil.copy2(s3_run.BASE / 'config' / 'charts.json',
                         root / 'config' / 'charts.json')
            shutil.copy2(gen_charts.BASE / 'report_style.css', root / 'report_style.css')
            data = root / 'data_power_minute.csv'
            times = ['2026-01-01 23:58:00', '2026-01-01 23:59:00',
                     '2026-01-02 00:00:00', '2026-01-02 00:01:00']
            with data.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['t', 'param_code', 'param_name', 'count',
                                 'avg', 'mn', 'mx', 'sd'])
                for i, instant in enumerate(times):
                    solar = 0 if i < 2 else 20
                    values = {'TMD009': solar, 'TMD016': 28,
                              'TMD017': 0 if i < 2 else 15,
                              'TMD018': 10, 'TMD019': 27 if i < 2 else 28}
                    values.update({f'TMD{code:03d}': 28 for code in range(3, 9)})
                    values.update({f'TMD{code:03d}': solar / 6
                                   for code in range(10, 16)})
                    for code, value in values.items():
                        writer.writerow([instant, code, code, 2,
                                         value, value, value, 0])
            raw = root / 'data_power_raw.csv'
            with raw.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['t', 'param_code', 'value'])
                for instant, sun in [('2026-01-01 23:59:00', False),
                                     ('2026-01-01 23:59:03', False),
                                     ('2026-01-02 00:00:00', True),
                                     ('2026-01-02 00:00:03', True)]:
                    for code, value in {'TMD009': 20 if sun else 0,
                                        'TMD016': 28, 'TMD017': 15 if sun else 0,
                                        'TMD018': 10, 'TMD019': 27}.items():
                        writer.writerow([instant, code, value])
            common = {'satellite_id': 'SCS-01-01',
                      'source': 'chdb_3body.telemetry_raw_01',
                      'window_start_inclusive': '2026-01-01T00:00:00',
                      'window_end_exclusive': '2026-01-03T00:00:00',
                      'timezone': 'Asia/Shanghai',
                      'time_column': 'onboard_time_bj'}
            for path in (data, raw):
                path.with_name(path.stem + '.manifest.json').write_text(
                    json.dumps({**common, 'data_sha256': s3_run.sha256_file(path)}),
                    encoding='utf-8')
            summary = s3_run.run_pipeline(data, root / 'out')
            self.assertEqual(summary['metrics']['n_phase_unknown'], 0)
            self.assertIn('components', summary)
            self.assertEqual(summary['components']['components']['cxpd_payload']['status'],
                             'missing_sensor')
            self.assertEqual(summary['components']['physical_balance']['status'], 'unavailable')
            self.assertEqual(summary['components']['panel_to_bus_difference']['n_daylight_valid'], 2)
            self.assertEqual(summary['components']['panel_to_bus_difference']['status'],
                             'diagnostic_only_unverified_boundary')
            self.assertTrue((root / 'out' / 'energy_v2_components.csv').is_file())
            self.assertEqual(summary['phase']['e_ecl']['n_energy_samples'], 2)
            self.assertEqual(summary['provenance']['status'], 'verified_fetch_manifest')
            self.assertEqual(summary['interval_energy']['n_gaps_over_limit'], 1)
            self.assertEqual(summary['interval_energy']['phase']['e_sun']['observed_seconds'], 3)
            self.assertEqual(summary['interval_energy']['phase']['e_ecl']['observed_seconds'], 3)
            separate_raw = root / 'separate_raw.csv'
            shutil.copy2(raw, separate_raw)
            shutil.copy2(raw.with_name(raw.stem + '.manifest.json'),
                         separate_raw.with_name(separate_raw.stem + '.manifest.json'))
            separate_summary = s3_run.run_pipeline(data, root / 'out_separate',
                                                   raw_data_path=separate_raw)
            self.assertEqual(separate_summary['interval_energy']['n_timestamps'], 4)
            with mock.patch.object(gen_charts, 'BASE', root), \
                 mock.patch.object(gen_charts, 'OUT', root / 'charts'):
                gen_charts.main()
            html = (root / 'framework_documentation_current.html').read_text(encoding='utf-8')
            self.assertIn('data-inline-chart="chart_soc_dod"', html)
            # chart_array_s3r_candidate 依赖太阳阵电压遥测 TMD003–008，
            # 2026-11-02 起该电压判为不可用，图表已从 config/charts.json 移除。
            self.assertNotIn('data-inline-chart="chart_array_s3r_candidate"', html)
            self.assertIn('2026-01-02', html)
            self.assertIn('原始采样间隔积分', html)
            self.assertIn('单机与支路功率', html)
            self.assertIn('部件明细 CSV', html)
            # 帆板端差额数字随 TMD003–008 电压弃用而撤下，正文只保留说明
            self.assertIn('帆板端 ΣV×I − TMD017 差额（已弃用）', html)

            raw_manifest_path = raw.with_name(raw.stem + '.manifest.json')
            raw_manifest = json.loads(raw_manifest_path.read_text(encoding='utf-8'))
            raw_manifest['window_end_exclusive'] = '2026-01-04T00:00:00'
            raw_manifest_path.write_text(json.dumps(raw_manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '取数清单不一致'):
                s3_run.run_pipeline(data, root / 'out')
            raw_manifest['window_end_exclusive'] = common['window_end_exclusive']
            raw_manifest_path.write_text(json.dumps(raw_manifest), encoding='utf-8')

            with raw.open('a', encoding='utf-8') as stream:
                stream.write('2026-01-02 00:00:06,TMD016,28\n')
            with self.assertRaisesRegex(ValueError, '原始采样哈希'):
                s3_run.run_pipeline(data, root / 'out')

    def test_fetch_uses_bounded_windows_and_writes_verifiable_manifest(self):
        calls = []

        def fake_query(sql, **kwargs):
            calls.append((sql, kwargs['params']))
            yield {'t': kwargs['params']['start'], 'param_code': 'TMD009',
                   'n': 2, 'avg': 10.0, 'mn': 0.0, 'mx': 20.0, 'sd': 14.14}

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'minute.csv'
            manifest = s1_fetch.fetch('2026-01-01', '2026-01-03',
                                      'Asia/Shanghai', path, chunk_days=1,
                                      query_rows=fake_query)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1]['end'], calls[1][1]['start'])
            self.assertIn('onboard_time_bj <', calls[0][0])
            self.assertEqual(manifest['row_count'], 2)
            self.assertEqual(manifest['time_column'], 'onboard_time_bj')
            self.assertEqual(manifest['data_sha256'], s3_run.sha256_file(path))
            self.assertIn('TMD025', manifest['param_codes'])
            self.assertIn('TMK5315', manifest['param_codes'])
            self.assertIn('TMD074_b1', manifest['param_codes'])
            self.assertIn('TMH025', manifest['param_codes'])
            self.assertIn('TMG065', manifest['param_codes'])
            saved = json.loads((Path(folder) / 'minute.manifest.json').read_text(
                encoding='utf-8'))
            self.assertEqual(saved['satellite_id'], manifest['satellite_id'])

    def test_s3_loads_minute_extrema_and_rejects_mismatched_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'minute.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['t', 'param_code', 'count', 'avg', 'mn', 'mx', 'sd'])
                for code, avg, mn, mx in [
                    ('TMD009', 10, 0, 20), ('TMD016', 28, 28, 28),
                    ('TMD017', 15, 15, 15), ('TMD018', 10, 10, 10),
                    ('TMD019', 27, 27, 27)]:
                    writer.writerow(['2026-01-01 00:00:00', code, 2,
                                     avg, mn, mx, 0])
            wide, provenance = s3_run.load_minute_data(path, 'SCS-01-01')
            self.assertEqual(wide['TMD009__mn'].iloc[0], 0)
            self.assertEqual(wide['TMD009__mx'].iloc[0], 20)
            self.assertEqual(provenance['status'], 'unverified_legacy_input')
            manifest_path = Path(folder) / 'minute.manifest.json'
            manifest_path.write_text(json.dumps({'satellite_id': 'SCS-01-01',
                                                 'data_sha256': 'bad'}),
                                     encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '哈希'):
                s3_run.load_minute_data(path, 'SCS-01-01')
            manifest_path.write_text(json.dumps({'satellite_id': 'SCS-01-01',
                                                 'data_sha256': s3_run.sha256_file(path),
                                                 'time_column': 'station_time'}),
                                     encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '时间基准'):
                s3_run.load_minute_data(path, 'SCS-01-01')

    def test_state_only_minute_does_not_expand_energy_timeline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'minute.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['t', 'param_code', 'count', 'avg', 'mn', 'mx',
                                 'state_text'])
                writer.writerow(['2026-01-01 00:00:00', 'TMD009', 1, 10, 10, 10, ''])
                writer.writerow(['2026-01-01 00:00:00', 'TMG065', 1, '', '', '', '通信'])
                writer.writerow(['2026-01-01 00:01:00', 'TMG065', 1, '', '', '', '休眠'])
                writer.writerow(['2026-01-01 00:02:00', 'TMD016', 1, 28, 28, 28, ''])
            wide, _ = s3_run.load_minute_data(path, mode_codes=['TMG065'])
            self.assertEqual(len(wide), 2)
            self.assertEqual(wide['TMG065__state'].iloc[0], '通信')
            self.assertTrue(pd.isna(wide['TMD009'].iloc[1]))

    def test_illumination_state_survives_minute_pivot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'minute.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['t', 'param_code', 'count', 'avg', 'mn', 'mx', 'state_text'])
                writer.writerow(['2026-01-01 00:00:00', 'TMD009', 1, 0.3, 0.3, 0.3, ''])
                writer.writerow(['2026-01-01 00:00:00', 'TMK5315', 2, '', '', '', '阳照区'])
                writer.writerow(['2026-01-01 00:01:00', 'TMK5315', 2, '', '', '', '阴影区'])
            wide, _ = s3_run.load_minute_data(path, mode_codes=['TMK5315'])
            self.assertEqual(len(wide), 1)
            self.assertEqual(wide['TMK5315__state'].iloc[0], '阳照区')

    def test_battery_switch_and_current_extrema_survive_minute_pivot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'minute.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['t', 'param_code', 'count', 'avg', 'mn', 'mx', 'state_text'])
                writer.writerow(['2026-01-01 00:00:00', 'TMD009', 1, 20, 20, 20, ''])
                writer.writerow(['2026-01-01 00:00:00', 'TMH025', 2, 26, 24, 28, ''])
                writer.writerow(['2026-01-01 00:00:00', 'TMD074_b1', 2, '', '', '', '通'])
            wide, _ = s3_run.load_minute_data(path, mode_codes=['TMH025', 'TMD074_b1'])
            self.assertEqual(wide['TMH025__mx'].iloc[0], 28)
            self.assertEqual(wide['TMD074_b1__state'].iloc[0], '通')

    def test_profile_switches_satellite_components_and_soc_curve(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / 'profile'
            profile.mkdir()
            for name in ('telemetry_params.json', 'design_params.json',
                         'v_soc_curve.json'):
                shutil.copy2(s3_run.CONFIG_DIR / name, profile / name)
            telemetry = json.loads((profile / 'telemetry_params.json').read_text(encoding='utf-8'))
            telemetry['satellite_id'] = 'SCS-OTHER'
            (profile / 'telemetry_params.json').write_text(json.dumps(telemetry), encoding='utf-8')
            curve = json.loads((profile / 'v_soc_curve.json').read_text(encoding='utf-8'))
            curve['points'] = [[20, .2], [30, .8]]
            (profile / 'v_soc_curve.json').write_text(json.dumps(curve), encoding='utf-8')
            (profile / 'components.json').write_text(json.dumps({
                'satellite_id': 'SCS-OTHER',
                'components': [{'id': 'payload', 'role': 'load', 'model': 'bus_current',
                                'current_code': 'TMD066', 'enabled': True}]}), encoding='utf-8')
            data = root / 'minute.csv'
            with data.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['t', 'param_code', 'count', 'avg', 'mn', 'mx'])
                for code, value in [('TMD009', 10), ('TMD016', 28),
                                    ('TMD017', 10), ('TMD018', 5),
                                    ('TMD019', 25), ('TMD066', 1)]:
                    writer.writerow(['2026-01-01 00:00:00', code, 1, value, value, value])
            summary = s3_run.run_pipeline(data, root / 'result', profile_dir=profile)
            self.assertEqual(summary['satellite_id'], 'SCS-OTHER')
            self.assertEqual(list(summary['components']['components']), ['payload'])
            result = pd.read_csv(root / 'result' / 'energy_v2_result.csv')
            self.assertAlmostEqual(result['SOC'].iloc[0], 50)

    def test_fetch_rejects_wrong_timezone_for_onboard_beijing_time(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'Asia/Shanghai'):
                s1_fetch.fetch('2026-01-01', '2026-01-02', 'UTC',
                               Path(folder) / 'minute.csv', query_rows=lambda *_: iter(()))

    def test_fetch_failure_keeps_previous_output(self):
        def broken_query(sql, **kwargs):
            yield {'t': '2026-01-01 00:00:00', 'param_code': 'TMD009',
                   'n': 1, 'avg': 1, 'mn': 1, 'mx': 1, 'sd': 0}
            raise RuntimeError('interrupted')

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'minute.csv'
            path.write_text('previous', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                s1_fetch.fetch('2026-01-01', '2026-01-02',
                               'Asia/Shanghai', path, query_rows=broken_query)
            self.assertEqual(path.read_text(encoding='utf-8'), 'previous')
            self.assertFalse(path.with_suffix('.csv.tmp').exists())

    def test_raw_fetch_preserves_timestamp_and_manifest(self):
        def fake_query(sql, **kwargs):
            self.assertIn('onboard_time_bj AS t', sql)
            yield {'t': '2026-01-01 00:00:03.123', 'param_code': 'TMD016',
                   'v': 28.0}

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'raw.csv'
            manifest = s1_fetch_raw.fetch_raw('2026-01-01', '2026-01-02',
                                              'Asia/Shanghai', path,
                                              query_rows=fake_query)
            self.assertEqual(manifest['time_column'], 'onboard_time_bj')
            self.assertEqual(manifest['data_sha256'], s3_run.sha256_file(path))
            self.assertIn('00:00:03.123', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
