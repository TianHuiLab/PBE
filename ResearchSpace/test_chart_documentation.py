"""Offline rendering and document integration regression checks."""
import importlib.util
import csv
from html.parser import HTMLParser
from datetime import datetime
import re
from pathlib import Path
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent
SCRIPT = BASE / 'gen_charts.py'
PAGE = BASE / 'framework_documentation.html'
SVG = '{http://www.w3.org/2000/svg}'
RESULT_CSV = BASE / 'out' / 'energy_v2_result.csv'
MINUTE_CSV = BASE / 'data_power_minute.csv'

# 数据文件由 s1_fetch.py + s3_run.py 生成（大文件不入库）；缺失时跳过数据相关用例
require_data = unittest.skipUnless(
    RESULT_CSV.is_file() and MINUTE_CSV.is_file(),
    '需要 out/energy_v2_result.csv 与 data_power_minute.csv（运行 s1_fetch.py + s3_run.py 生成）')


class ChartElementReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.svgs = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == 'svg':
            self.svgs.append(dict(attrs))
        if tag == 'img':
            self.images.append(dict(attrs))


class ChartDocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('energy_charts', SCRIPT)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_line_chart_uses_shared_calendar_axis_and_breaks_large_gaps(self):
        chart = self.module.line_chart('验证', {
            '甲': (['2026-01-01', '2026-01-02', '2026-04-01'], [2.0, 4.0, 6.0], '#123456'),
            '乙': (['2026-01-01', '2026-03-01', '2026-04-01'], [3.0, 5.0, 7.0], '#654321'),
        }, gap_seconds=3 * 86400)
        root = ET.fromstring(chart)
        paths = root.findall(f'.//{SVG}path')
        self.assertEqual(len(paths), 2)
        # Paths must break when the next timestamp lies outside a data-covered interval.
        self.assertTrue(all(path.attrib['d'].count('M') >= 2 for path in paths))
        # Both series use the same real-time x-domain: Jan 1 is at the same pixel.
        self.assertEqual(paths[0].attrib['d'].split('M')[1].split(',')[0],
                         paths[1].attrib['d'].split('M')[1].split(',')[0])
        self.assertIn('2026-01-01', chart)
        self.assertIn('2026-04-01', chart)

    def test_nan_produces_disconnected_segments_not_invalid_svg(self):
        chart = self.module.line_chart('缺测', {'电流': (
            ['2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04'],
            [1.0, 2.0, None, 4.0], '#123456')}, gap_seconds=3 * 86400)
        ET.fromstring(chart)
        self.assertNotIn('nan', chart.lower())
        self.assertGreaterEqual(chart.count('M'), 2)

    @require_data
    def test_representative_day_is_most_observed_day_with_both_phases(self):
        with (BASE / 'out' / 'energy_v2_result.csv').open(encoding='utf-8', newline='') as stream:
            records = list(csv.DictReader(stream))
        count = {}
        for row in records:
            day = row['t'][:10]
            if day not in count:
                count[day] = [0, set()]
            count[day][0] += 1
            count[day][1].add(row['phase'])
        expected = min((day for day in count if count[day][0] == max(
            n for n, phases in count.values() if len(phases) == 2)), default=None)
        self.assertEqual(self.module.select_representative_day(records), expected)
        # 动态性质断言：期望日真实存在、样本最多且同时含日照/地影（不硬编码具体日期）
        self.assertIn(expected, count)
        self.assertEqual(len(count[expected][1]), 2)
        self.assertEqual(count[expected][0],
                         max(n for n, _ in count.values()))

    @require_data
    def test_window_selection_prefers_longest_segment_containing_both_phases(self):
        with (BASE / 'out' / 'energy_v2_result.csv').open(encoding='utf-8', newline='') as stream:
            data = list(csv.DictReader(stream))
        segment = self.module.select_observation_window(data)
        # 独立重算期望窗口（与实现相互印证，不硬编码日期与长度）
        chunks, current, previous = [], [], None
        for row in sorted(data, key=lambda item: item['t']):
            instant = datetime.fromisoformat(row['t'])
            if previous is not None and (instant - previous).total_seconds() > 180:
                chunks.append(current)
                current = []
            current.append(row)
            previous = instant
        if current:
            chunks.append(current)
        eligible = [chunk for chunk in chunks
                    if {'日照', '地影'} <= {r['phase'] for r in chunk}]
        best = min(eligible, key=lambda chunk: (-len(chunk), chunk[0]['t']))
        self.assertEqual((segment[0]['t'], segment[-1]['t'], len(segment)),
                         (best[0]['t'], best[-1]['t'], len(best)))
        self.assertEqual({row['phase'] for row in segment}, {'日照', '地影'})
        for left, right in zip(segment, segment[1:]):
            self.assertLessEqual((datetime.fromisoformat(right['t']) -
                                  datetime.fromisoformat(left['t'])).total_seconds(), 180)

    def test_scatter_chart_draws_one_circle_per_observed_value_no_connected_path(self):
        chart = self.module.line_chart('窗口', {'电压': (
            ['2025-12-30 11:03:00', '2025-12-30 11:04:00', '2025-12-30 11:05:00'],
            [28.1, None, 28.3], '#2563eb')}, scatter=True,
            window_start='2025-12-30 11:03:00', window_end='2025-12-30 11:05:00')
        root = ET.fromstring(chart)
        self.assertEqual(len(root.findall(f'.//{SVG}circle')), 2)
        self.assertEqual(root.findall(f'.//{SVG}path'), [])
        self.assertEqual(root.get('data-window-start'), '2025-12-30 11:03:00')
        self.assertEqual(root.get('data-window-end'), '2025-12-30 11:05:00')

    @require_data
    def test_generated_svg_axes_show_selected_window_bounds(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=BASE,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with (BASE / 'out' / 'energy_v2_result.csv').open(encoding='utf-8', newline='') as stream:
            selected = self.module.select_observation_window(list(csv.DictReader(stream)))
        start, end = selected[0]['t'], selected[-1]['t']
        html = PAGE.read_text(encoding='utf-8')
        self.assertIn(start[:16], html)
        self.assertIn(end[11:16], html)
        for chart in ('chart_voltages', 'chart_solar_panels', 'chart_power_flow',
                      'chart_daily_power', 'chart_energy_daily'):
            path = BASE / 'charts' / (chart + '.svg')
            root = ET.parse(path).getroot()
            # Every chart has the same observed-window time domain.
            self.assertEqual(root.get('data-window-start'), start, chart)
            self.assertEqual(root.get('data-window-end'), end, chart)
            ticks = [node.text for node in root.findall(f'.//{SVG}text')]
            self.assertEqual(root.findall(f'.//{SVG}path'), [], chart)
            self.assertTrue(root.findall(f'.//{SVG}circle'), chart)
            self.assertFalse(any(re.fullmatch(r'20\\d{2}-\\d{2}-\\d{2}', t or '') for t in ticks))
        # No captions refer to a multi-day trend while the plots show one date.
        self.assertNotIn('00:00–24:00', html[html.index('id="charts"'):])

    @require_data
    def test_document_contains_offline_embedded_charts_from_real_csv(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=BASE,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        html = PAGE.read_text(encoding='utf-8')
        self.assertIn('id="charts"', html)
        self.assertIn('真实遥测', html)
        self.assertIn('观测窗口', html)
        for chart in ('chart_daily_power', 'chart_voltages', 'chart_currents',
                      'chart_solar_panels', 'chart_power_flow', 'chart_soc_dod',
                      'chart_daily_shunt', 'chart_energy_daily'):
            path = BASE / 'charts' / (chart + '.svg')
            self.assertTrue(path.is_file(), chart)
            root = ET.parse(path).getroot()
            self.assertEqual(root.tag, SVG + 'svg')
            self.assertTrue(root.findall(f'.//{SVG}circle'), chart)
        parser = ChartElementReader()
        parser.feed(html)
        self.assertEqual(len(parser.svgs), 11)
        self.assertEqual(parser.images, [])
        with (BASE / 'out' / 'energy_v2_result.csv').open(encoding='utf-8', newline='') as stream:
            selected = self.module.select_observation_window(list(csv.DictReader(stream)))
        window_start, window_end = selected[0]['t'], selected[-1]['t']
        self.assertTrue(all(svg.get('data-window-start') == window_start and
                            svg.get('data-window-end') == window_end
                            for svg in parser.svgs))
        self.assertIn('data-inline-chart="chart_daily_power"', html)
        self.assertNotIn('src="http', html)
        self.assertNotIn('src="//', html)

    @require_data
    def test_charts_dir_has_no_orphan_svgs(self):
        """charts/ 中不允许存在清单之外的陈旧 SVG（往期迭代产物应被清理）。"""
        proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=BASE,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        manifest = set(self.module.load_chart_manifest())
        on_disk = {path.stem for path in (BASE / 'charts').glob('*.svg')}
        self.assertEqual(on_disk, manifest)

    @require_data
    def test_regenerating_charts_refreshes_html_without_duplicate_svgs(self):
        first = subprocess.run([sys.executable, str(SCRIPT)], cwd=BASE,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        old_html = PAGE.read_text(encoding='utf-8')
        second = subprocess.run([sys.executable, str(SCRIPT)], cwd=BASE,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 0, second.stderr)
        new_html = PAGE.read_text(encoding='utf-8')
        self.assertEqual(new_html, old_html)
        parser = ChartElementReader()
        parser.feed(new_html)
        self.assertEqual(len(parser.svgs), 11)
        self.assertEqual(parser.images, [])


if __name__ == '__main__':
    unittest.main()
