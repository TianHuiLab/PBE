"""云尖3D同源配对账本的时间归属、差额及缺测边界。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yj3d_balance import summarize


def row(minute_utc, count=2, bus=100.0, branch=75.0, bcr=110.0, battery=-5.0):
    item = {'minute_utc': minute_utc, 'eps_source_rows': count,
            'valid_bus_rows': count, 'battery_BCR': battery * count}
    for key in ('A', 'B', 'AB', 'HEAT', 'ABH', 'BCR'):
        item[f'n_{key}'] = count
        item[f'bus_{key}'] = bus * count
        item[f'source_{key}'] = (bcr if key == 'BCR' else branch) * count
    return item


class YJ3DBalanceTests(unittest.TestCase):
    def test_same_minute_pair_and_beijing_phase(self):
        phases = {'2026-07-26 00:00:00': '日照',
                  '2026-07-26 00:01:00': '地影'}
        plotted = []
        data = summarize([
            row('2026-07-25 16:00:00'),
            row('2026-07-25 16:01:00', bus=130, branch=90, bcr=0, battery=132),
            row('2026-07-25 16:02:00')], phases, plotted.append)
        sun = data['日照']
        self.assertEqual(sun['clear_phase_minutes'], 1)
        self.assertEqual(sun['eps_source_rows'], 2)
        self.assertEqual(sun['comparisons']['ABH']['paired_minutes'], 1)
        self.assertAlmostEqual(sun['comparisons']['ABH']['bus_minus_source_W_p05_p50_p95'][1], 25)
        self.assertAlmostEqual(sun['comparisons']['BCR']['bcr_plus_battery_minus_bus_W_p05_p50_p95'][1], 5)
        eclipse = data['地影']['comparisons']['BCR']
        self.assertEqual(eclipse['paired_minutes'], 1)
        self.assertAlmostEqual(eclipse['bcr_plus_battery_minus_bus_W_p05_p50_p95'][1], 2)
        self.assertEqual(len(plotted), 2)
        self.assertEqual(plotted[0]['t_beijing'], '2026-07-26 00:00:00')
        self.assertEqual(plotted[0]['phase'], '日照')
        self.assertAlmostEqual(plotted[0]['bus_W'], 100)
        self.assertAlmostEqual(plotted[0]['pl_ab_heat_W'], 75)
        self.assertAlmostEqual(plotted[0]['bus_minus_pl_ab_heat_W'], 25)

    def test_zero_pairs_and_duplicate_minute(self):
        empty = row('2026-07-25 16:00:00', count=0)
        phases = {'2026-07-26 00:00:00': '日照'}
        result = summarize([empty], phases)
        self.assertEqual(result['日照']['comparisons']['BCR']['paired_minutes'], 0)
        self.assertIsNone(result['日照']['comparisons']['BCR']['source_W_p05_p50_p95'][1])
        with self.assertRaisesRegex(ValueError, '重复'):
            summarize([empty, empty], phases)


if __name__ == '__main__':
    unittest.main()
