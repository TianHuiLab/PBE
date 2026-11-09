"""原始采样时间积分的可观测行为测试。"""
import csv
import sys
import tempfile
import unittest
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interval_energy import integrate_raw


class IntervalEnergyTests(unittest.TestCase):
    def test_minute_phase_drives_raw_energy_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'raw.csv'
            self._write(path)
            phase = pd.Series(['地影', '未判定'],
                              index=pd.to_datetime(['2026-01-01 00:00:00',
                                                    '2026-01-01 00:01:00']))
            result = integrate_raw(path, 10, minute_phase=phase)
            self.assertEqual(result['phase']['e_sun']['observed_seconds'], 0)
            self.assertEqual(result['phase']['e_ecl']['observed_seconds'], 6)
            self.assertEqual(result['phase']['e_unknown']['observed_seconds'], 3)

    def _write(self, path):
        with path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['t', 'param_code', 'value'])
            for instant, sun in [('2026-01-01 00:00:00', True),
                                 ('2026-01-01 00:00:03', True),
                                 ('2026-01-01 00:00:06', True),
                                 ('2026-01-01 00:01:00', False),
                                 ('2026-01-01 00:01:03', False)]:
                values = {'TMD009': 20 if sun else 0, 'TMD016': 28,
                          'TMD017': 15 if sun else 0, 'TMD018': 10,
                          'TMD019': 27}
                for code, value in values.items():
                    writer.writerow([instant, code, value])

    def test_short_intervals_integrate_and_long_gap_is_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'raw.csv'
            self._write(path)
            result = integrate_raw(path, 10)
            self.assertEqual(result['n_gaps_over_limit'], 1)
            self.assertEqual(result['phase']['e_sun']['observed_seconds'], 6)
            self.assertEqual(result['phase']['e_ecl']['observed_seconds'], 3)
            self.assertAlmostEqual(result['phase']['e_sun']['e_solar_kWh'],
                                   420 * 6 / 3_600_000)
            self.assertAlmostEqual(result['phase']['e_ecl']['e_load_kWh'],
                                   280 * 3 / 3_600_000)

    def test_duplicate_timestamp_and_code_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'raw.csv'
            self._write(path)
            with path.open('a', encoding='utf-8') as stream:
                stream.write('2026-01-01 00:00:00,TMD009,20\n')
            with self.assertRaisesRegex(ValueError, '重复'):
                integrate_raw(path)

    def test_mixed_millisecond_timestamps_missing_power_and_phase_transition(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'raw.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['t', 'param_code', 'value'])
                points = [('2026-01-01 00:00:00', 20, 15),
                          ('2026-01-01 00:00:03.500', 20, 15),
                          ('2026-01-01 00:00:06', 0, 0),
                          ('2026-01-01 00:00:09.500', 0, None),
                          ('2026-01-01 00:00:13', 0, 0)]
                for instant, solar_i, bus_i in points:
                    for code, value in {'TMD009': solar_i, 'TMD016': 28,
                                        'TMD017': bus_i, 'TMD018': 10,
                                        'TMD019': 27}.items():
                        if value is not None:
                            writer.writerow([instant, code, value])
            result = integrate_raw(path, 10)
            self.assertEqual(result['n_intervals_missing_power'], 2)
            self.assertEqual(result['phase']['e_sun']['observed_seconds'], 3.5)
            self.assertEqual(result['phase']['e_unknown']['observed_seconds'], 2.5)
            self.assertEqual(result['phase']['e_ecl']['observed_seconds'], 0)
            self.assertAlmostEqual(result['phase']['e_unknown']['e_solar_kWh'],
                                   210 * 2.5 / 3_600_000)


if __name__ == '__main__':
    unittest.main()
