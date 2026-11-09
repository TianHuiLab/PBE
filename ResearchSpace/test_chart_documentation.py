"""Offline rendering and document integration regression checks."""
import importlib.util
import csv
import json
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
PAGE = BASE / 'framework_documentation_current.html'
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
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
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

    def test_tmh025_direction_is_applied_to_current_magnitude(self):
        signed = self.module.signed_battery_current
        self.assertEqual(signed(26.5, '通'), 26.5)
        self.assertIsNone(signed(26.5, '断'))
        self.assertIsNone(signed(26.5, '__mixed__'))
        self.assertIsNone(signed(None, '通'))

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

    def test_hour_buckets_keep_observations_after_midnight(self):
        result = [
            {'t': '2026-01-01 23:59:00', 'P_solar': '60', 'P_load': '30',
             'P_excess': '30', 'P_shunt_diag': '10', 'SOC': '80'},
            {'t': '2026-01-02 00:00:00', 'P_solar': '120', 'P_load': '30',
             'P_excess': '90', 'P_shunt_diag': '20', 'SOC': '81'},
        ]
        hours, avg, energy = self.module.hour_buckets(result)
        self.assertEqual(len(hours), 2)
        self.assertEqual(avg['SOC'], [80.0, 81.0])
        self.assertEqual(energy['P_solar'], [0.001, 0.002])

    def test_window_falls_back_when_only_one_phase_is_observed(self):
        records = [{'t': f'2026-01-01 00:0{i}:00', 'phase': '未判定'}
                   for i in range(3)]
        self.assertEqual(len(self.module.select_observation_window(records)), 3)

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
        eligible = [day for day, (_, phases) in count.items()
                    if {'日照', '地影'} <= phases]
        expected = min(eligible, key=lambda day: (-count[day][0], day))
        self.assertEqual(self.module.select_representative_day(records), expected)
        # 动态性质断言：期望日真实存在、样本最多且同时含日照/地影（不硬编码具体日期）
        self.assertIn(expected, count)
        self.assertTrue({'日照', '地影'} <= count[expected][1])
        self.assertEqual(count[expected][0],
                         max(count[day][0] for day in eligible))

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
        self.assertTrue({'日照', '地影'} <= {row['phase'] for row in segment})
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
    def test_each_load_has_a_selectable_curve_or_empty_state(self):
        detail_path = BASE / 'out' / 'energy_v2_components.csv'
        self.assertTrue(detail_path.is_file())
        proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=BASE,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads((BASE / 'out' / 'energy_v2_summary.json').read_text(
            encoding='utf-8'))
        loads = {key for key, item in summary['components']['components'].items()
                 if item['role'] == 'load'}
        html = PAGE.read_text(encoding='utf-8')
        parser = ChartElementReader()
        parser.feed(html)
        curves = {svg['data-component-id']: svg for svg in parser.svgs
                  if 'data-component-id' in svg}
        selectors = [attrs for tag, attrs in parser.tags
                     if tag == 'select' and attrs.get('id') == 'component-select']
        self.assertEqual(len(selectors), 1)
        options = {attrs['value']: attrs for tag, attrs in parser.tags
                   if tag == 'option' and attrs.get('data-option-group') == 'component'}
        panels = {attrs['data-component-panel']: attrs for tag, attrs in parser.tags
                  if 'data-component-panel' in attrs}
        self.assertEqual(set(options), loads)
        self.assertEqual(set(panels), loads)
        with RESULT_CSV.open(encoding='utf-8', newline='') as stream:
            selected = self.module.select_observation_window(list(csv.DictReader(stream)))
        with detail_path.open(encoding='utf-8', newline='') as stream:
            detail = {row['t']: row for row in csv.DictReader(stream)}
        valid_counts = {
            name: sum(self.module.value(detail[row['t']].get(f'P_{name}_W')) is not None
                      for row in selected)
            for name in loads}
        self.assertEqual(set(curves), {name for name in loads if valid_counts[name]})
        expected_initial = ('router_main' if valid_counts.get('router_main', 0) else
                            next((name for name in summary['components']['components']
                                  if name in loads and valid_counts[name]),
                                 next(name for name in summary['components']['components']
                                      if name in loads)))
        self.assertEqual([name for name, attrs in panels.items() if 'hidden' not in attrs],
                         [expected_initial])
        self.assertIn('selected', options[expected_initial])
        for name, attrs in curves.items():
            self.assertEqual(attrs['data-window-start'], selected[0]['t'])
            self.assertEqual(attrs['data-window-end'], selected[-1]['t'])
            self.assertEqual(int(attrs['data-valid-minutes']), valid_counts[name])
            self.assertEqual(int(attrs['data-total-minutes']), len(selected))
        self.assertNotIn('data-component-id="heater_', html)
        sparse = next((name for name, attrs in curves.items()
                       if 0 < int(attrs['data-valid-minutes']) < 500), None)
        if sparse:
            match = re.search(r'<svg data-component-id="' + re.escape(sparse) +
                              r'"[\s\S]*?</svg>', html)
            self.assertIsNotNone(match)
            root = ET.fromstring(match.group())
            self.assertEqual(len(root.findall(f'.//{SVG}circle')),
                             int(curves[sparse]['data-valid-minutes']))

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
        # chart_array_s3r_candidate 依赖太阳阵电压遥测 TMD003–008，
        # 2026-11-02 起该电压判为不可用并已移出图表清单，故不再有该 SVG 可校验。
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
        self.assertEqual(len([svg for svg in parser.svgs
                              if 'data-inline-chart' in svg]),
                         len(self.module.load_chart_manifest()))
        chart_names = set(self.module.load_chart_manifest())
        selectors = [attrs for tag, attrs in parser.tags
                     if tag == 'select' and attrs.get('id') == 'telemetry-select']
        self.assertEqual(len(selectors), 1)
        options = {attrs['value']: attrs for tag, attrs in parser.tags
                   if tag == 'option' and attrs.get('data-option-group') == 'telemetry'}
        panels = {attrs['data-chart-panel']: attrs for tag, attrs in parser.tags
                  if 'data-chart-panel' in attrs}
        self.assertEqual(set(options), chart_names)
        self.assertEqual(set(panels), chart_names)
        self.assertEqual([name for name, attrs in panels.items() if 'hidden' not in attrs],
                         ['chart_power_flow'])
        self.assertIn('selected', options['chart_power_flow'])
        self.assertEqual(parser.images, [])
        with (BASE / 'out' / 'energy_v2_result.csv').open(encoding='utf-8', newline='') as stream:
            selected = self.module.select_observation_window(list(csv.DictReader(stream)))
        window_start, window_end = selected[0]['t'], selected[-1]['t']
        self.assertTrue(all(svg.get('data-window-start') == window_start and
                            svg.get('data-window-end') == window_end
                            for svg in parser.svgs
                            if 'data-inline-chart' in svg))
        self.assertIn('data-inline-chart="chart_daily_power"', html)
        self.assertNotIn('src="http', html)
        self.assertNotIn('src="//', html)

    @unittest.skipUnless((BASE / 'out' / 'battery_current_2025-12-29_0545_0620.csv').is_file(),
                         '需要 06:03 附近的 TMH025 分钟来源数据')
    @require_data
    def test_battery_and_array_power_near_0603_have_four_dashed_curves_and_formulas(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=BASE,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        html = PAGE.read_text(encoding='utf-8')
        self.assertIn('不调节母线', html)
        self.assertIn('母线供负载 → 蓄电池充电 → S³R 分流', html)
        self.assertIn('光照太阳阵功率（2026-11-03 校正）', html)
        self.assertIn('电池端有符号功率（开关判向试算）', html)
        self.assertIn('I_load(TMD018) − I_bus(TMD017)', html)
        self.assertIn('TMD074_b1=通 ⇒ 放电正号', html)
        self.assertIn('TMH025_signed_switch', html)
        self.assertIn('P_bat_signed_trial', html)
        self.assertIn('日照短时供电不足', html)
        match = re.search(r'<svg data-battery-power-window="true"[\s\S]*?</svg>', html)
        self.assertIsNotNone(match)
        root = ET.fromstring(match.group())
        self.assertEqual(root.get('data-window-start'), '2025-12-29 05:45:00')
        self.assertEqual(root.get('data-window-end'), '2025-12-29 06:20:00')
        paths = root.findall(f'.//{SVG}path')
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(path.get('stroke-dasharray') == '7 5' for path in paths))
        current_match = re.search(r'<svg data-battery-current-window="true"[\s\S]*?</svg>', html)
        self.assertIsNotNone(current_match)
        current_chart = ET.fromstring(current_match.group())
        self.assertEqual(current_chart.get('data-window-start'), '2025-12-29 05:45:00')
        self.assertEqual(current_chart.get('data-window-end'), '2025-12-29 06:20:00')
        self.assertEqual(len(current_chart.findall(f'.//{SVG}path')), 1)
        self.assertIn('TMH025_有符号', current_match.group())
        self.assertIn('D 组 9.5065 V', html)
        self.assertIn('434.9 +', html)
        self.assertIn('956.5 W', html)
        for formula in ('U_bus(TMD016) × I_bus(TMD017)',
                        'U_bus(TMD016) × I_load(TMD018)',
                        'TMH025_signed_switch = +|TMH025.value|（开关通）',
                        'U_bat(TMD019) × TMH025_signed_switch'):
            self.assertIn(formula, html)
        self.assertIn('TMD003–008', html)
        self.assertIn('TMD010–015', html)
        self.assertIn('负电压原值', html)
        with RESULT_CSV.open(encoding='utf-8', newline='') as stream:
            sample = next(row for row in csv.DictReader(stream)
                          if row['t'] == '2025-12-29 06:03:00')
        with (BASE / 'out' / 'battery_current_2025-12-29_0545_0620.csv').open(
                encoding='utf-8', newline='') as stream:
            current = next(row for row in csv.DictReader(stream)
                           if row['t'] == sample['t'])
        solar = float(sample['V_bus']) * float(sample['I_bus'])
        load = float(sample['V_bus']) * float(sample['I_load'])
        self.assertEqual(sample['battery_switch_state'], '通')
        signed_current = float(current['avg_A_claimed'])
        battery = float(sample['V_bat']) * signed_current
        self.assertIn(f'{signed_current:.4f} A', html)
        for number in (solar, load, battery):
            self.assertIn(f'{number:.1f} W', html)
        with MINUTE_CSV.open(encoding='utf-8-sig', newline='') as stream:
            panels = {row['param_code']: float(row['avg']) for row in csv.DictReader(stream)
                      if row['t'] == sample['t'] and row['param_code'] in
                      {f'TMD{i:03d}' for i in (*range(3, 9), *range(10, 16))}}
        self.assertEqual(len(panels), 12)
        array_raw = sum(panels[f'TMD{i:03d}'] * panels[f'TMD{i+7:03d}']
                        for i in range(3, 9))
        self.assertIn(f'{array_raw:.1f} W', html)

    @unittest.skipUnless((BASE / 'out' / 'battery_current_2025-12-29_0504_0537.csv').is_file(),
                         '需要 05:21 附近的 TMH025 分钟来源数据')
    @require_data
    def test_eclipse_power_window_and_boundary_formulas(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=BASE,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        html = PAGE.read_text(encoding='utf-8')
        match = re.search(r'<svg data-eclipse-power-window="true"[\s\S]*?</svg>', html)
        self.assertIsNotNone(match)
        chart = ET.fromstring(match.group())
        self.assertEqual(chart.get('data-window-start'), '2025-12-29 05:04:00')
        self.assertEqual(chart.get('data-window-end'), '2025-12-29 05:37:00')
        paths = chart.findall(f'.//{SVG}path')
        # 2026-11-03 起地影组去掉「六板端原值功率和」，只剩 TMD017/负载/电池三条虚线
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(path.get('stroke-dasharray') == '7 5' for path in paths))
        with RESULT_CSV.open(encoding='utf-8', newline='') as stream:
            sample = next(row for row in csv.DictReader(stream)
                          if row['t'] == '2025-12-29 05:21:00')
        with (BASE / 'out' / 'battery_current_2025-12-29_0504_0537.csv').open(
                encoding='utf-8', newline='') as stream:
            current = next(row for row in csv.DictReader(stream)
                           if row['t'] == sample['t'])
        self.assertEqual(sample['phase'], '地影')
        for power in (float(sample['V_bus']) * float(sample['I_bus']),
                      float(sample['V_bus']) * float(sample['I_load']),
                      float(sample['V_bat']) * float(current['avg_A_claimed'])):
            self.assertIn(f'{power:.1f} W', html)
        self.assertIn('TMD017 的值不再用于 TMH025 赋号', html)
        self.assertIn('地影区账本', html)
        self.assertIn('P_电池放电,母线侧 = P_全部负载 + P_其他 + dE_母线/dt', html)
        self.assertIn('E_电池放电,母线侧 = E_全部负载 + E_其他 + ΔE_母线', html)
        self.assertFalse('<code>P_B_dis_bus = P_load_all + P_bus_loss + dE_bus/dt</code>' in html)
        self.assertIn('光照区账本', html)

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
        self.assertEqual(len([svg for svg in parser.svgs
                              if 'data-inline-chart' in svg]),
                         len(self.module.load_chart_manifest()))
        self.assertEqual(parser.images, [])

    def test_phase2_chart_builders_emit_valid_offline_svg(self):
        bars = self.module.phase2_horizontal_bars(
            '模型比较', [
                {'label': '基线', 'value': 10.0, 'color': '#8b98aa'},
                {'label': '采用', 'value': 6.0, 'color': '#007e80',
                 'selected': True, 'detail': '分组留出'},
            ], 'W', 'unit-bars', subtitle='越低越好')
        bar_root = ET.fromstring(bars)
        self.assertEqual(bar_root.get('data-phase2-chart'), 'unit-bars')
        self.assertGreaterEqual(len(bar_root.findall(f'.//{SVG}rect')), 3)
        self.assertIn('10.00 W', bars)
        self.assertIn('6.00 W', bars)

        bounded = self.module.phase2_horizontal_bars(
            '占比', [{'label': 'A/B', 'value': 97.4, 'color': '#2554ad'}],
            '%', 'bounded-bars', xmax=100)
        self.assertIn('>100.00<', bounded)
        self.assertNotIn('>118.00<', bounded)

        diverging = self.module.phase2_diverging_bars(
            '误差', [
                {'label': '偏低', 'value': -2.5},
                {'label': '偏高', 'value': 1.0},
            ], 'pp', 'unit-diverging')
        diverging_root = ET.fromstring(diverging)
        self.assertEqual(diverging_root.get('data-phase2-chart'),
                         'unit-diverging')
        self.assertIn('-2.50 pp', diverging)
        self.assertIn('+1.00 pp', diverging)

        scatter = self.module.phase2_soc_scatter(
            'SOC', [
                {'x': 90.0, 'y': 91.0, 'group': 'DJF', 'label': 'W01'},
                {'x': 92.0, 'y': 92.5, 'group': 'JJA', 'label': 'W02'},
            ], 'unit-scatter')
        scatter_root = ET.fromstring(scatter)
        self.assertEqual(scatter_root.get('data-phase2-chart'), 'unit-scatter')
        self.assertEqual(len(scatter_root.findall(f'.//{SVG}circle')), 5)

    @require_data
    def test_phase2_chapters_are_source_bound_and_link_resolvable(self):
        scs_root = BASE / 'satellites' / 'SCS-01-01'
        yj_root = BASE / 'satellites' / 'YJ-3D'
        required = [
            scs_root / 'power_model/batch_validation_summary.json',
            scs_root / 'power_model/solar_power_model.json',
            yj_root / 'power_model/load_aware_comparison_summary.json',
            yj_root / 'power_model/unit_task_validation_summary.json',
        ]
        if not all(path.is_file() for path in required):
            self.skipTest('需要两星第二阶段机器可读摘要')
        scs_html = self.module.phase2_scs_section(scs_root)
        yj_html = self.module.phase2_yj_section(yj_root)
        self.assertIn('id="phase2-scs-01-01"', scs_html)
        self.assertNotIn('id="phase2-yj-3d"', scs_html)
        self.assertIn('id="phase2-yj-3d"', yj_html)
        self.assertNotIn('id="phase2-scs-01-01"', yj_html)
        self.assertIn('25 个冻结快照窗口', scs_html)
        self.assertIn('历史逐分钟背景回放', scs_html)
        self.assertIn(
            'href="stage2_testset/evaluation_summary.json"', scs_html)
        self.assertIn(
            'href="power_model/extended_data_coverage.json"', scs_html)
        yj_evaluation = self.module.phase2_json(
            yj_root / 'stage2_testset/evaluation_summary.json')
        expected_task_total = sum(
            int(case.get('task_count', 0))
            for case in yj_evaluation['cases'])
        self.assertIn(
            f'{expected_task_total} 条物理单机历史任务', yj_html)
        self.assertIn('条件 SOC / 任务规划级', scs_html)
        self.assertIn('本页指标不能用于两星“谁更准”的排名', yj_html)
        self.assertNotIn('飞行级 SOC/SOH 已验证', scs_html + yj_html)

        expected = {
            'scs': ({'scs-solar-cv', 'scs-soc-scatter'}, scs_html, scs_root),
            'yj': ({'yj-solar-cv', 'yj-target-soc-ab',
                    'yj-soc-errors', 'yj-solar-errors'}, yj_html, yj_root),
        }
        for name, (chart_ids, html, page_root) in expected.items():
            charts = re.findall(
                r'<svg[^>]*data-phase2-chart="([^"]+)"[\s\S]*?</svg>', html)
            self.assertEqual(set(charts), chart_ids, name)
            for svg in re.findall(
                    r'<svg[^>]*data-phase2-chart="[^"]+"[\s\S]*?</svg>', html):
                ET.fromstring(svg)
            for href in set(re.findall(r'href="([^"]+)"', html)):
                if href.startswith('#'):
                    continue
                self.assertTrue((page_root / href).is_file(), (name, href))

        # 根目录报告保持第一阶段总报告；星别报告各自只含自己的第二阶段。
        root_html = PAGE.read_text(encoding='utf-8')
        self.assertNotIn('id="phase2-scs-01-01"', root_html)
        self.assertNotIn('id="phase2-yj-3d"', root_html)
        scs_page = scs_root / 'framework_documentation_SCS-01-01.html'
        yj_page = yj_root / 'framework_documentation_YJ-3D.html'
        if scs_page.is_file():
            page_html = scs_page.read_text(encoding='utf-8')
            self.assertIn('href="#phase2-scs-01-01"', page_html)
            self.assertIn('id="phase2-scs-01-01"', page_html)
            self.assertNotIn('id="phase2-yj-3d"', page_html)
            nav_hrefs = re.findall(
                r'href="#([^"]+)"',
                re.search(r'<nav class="report-nav"[\s\S]*?</nav>',
                          page_html).group())
            self.assertEqual(nav_hrefs[-1], 'phase2-scs-01-01')
            section_ids = re.findall(
                r'<section[^>]*id="([^"]+)"', page_html)
            self.assertEqual(section_ids[-1], 'phase2-scs-01-01')
            self.assertLess(page_html.rfind('id="phase2-scs-01-01"'),
                            page_html.find('<footer'))
        if yj_page.is_file():
            page_html = yj_page.read_text(encoding='utf-8')
            self.assertIn('href="#phase2-yj-3d"', page_html)
            self.assertIn('id="phase2-yj-3d"', page_html)
            self.assertNotIn('id="phase2-scs-01-01"', page_html)
            nav_hrefs = re.findall(
                r'href="#([^"]+)"',
                re.search(r'<nav class="report-nav"[\s\S]*?</nav>',
                          page_html).group())
            self.assertEqual(nav_hrefs[-1], 'phase2-yj-3d')
            section_ids = re.findall(
                r'<section[^>]*id="([^"]+)"', page_html)
            self.assertEqual(section_ids[-1], 'phase2-yj-3d')
            self.assertLess(page_html.rfind('id="phase2-yj-3d"'),
                            page_html.find('<footer'))

    def test_stage2_reports_show_audit_metrics_and_manifest_links(self):
        cases = {
            'SCS': (
                BASE / 'satellites/SCS-01-01/'
                'framework_documentation_SCS-01-01.html',
                BASE / 'satellites/SCS-01-01/power_model'),
            'YJ': (
                BASE / 'satellites/YJ-3D/'
                'framework_documentation_YJ-3D.html',
                BASE / 'satellites/YJ-3D/power_model'),
        }
        for satellite, (page, model_dir) in cases.items():
            if not page.is_file():
                self.skipTest(f'{satellite} 星别报告不存在')
            self.assertTrue((model_dir / 'stage2_audit.json').is_file())
            self.assertTrue((model_dir / 'stage2_manifest.json').is_file())
            html = page.read_text(encoding='utf-8')
            for selector in ('id="stage2-audit"',
                             'id="stage2-audit-overview"',
                             'id="stage2-audit-metrics"',
                             'href="power_model/stage2_manifest.json"',
                             'href="power_model/stage2_audit.json"',
                             'href="stage2_testset/manifest.json"',
                             'href="unit_training/manifest.json"',
                             'href="unit_training/README.md"'):
                self.assertTrue(selector in html, f'{satellite}: {selector}')
            if satellite == 'YJ':
                for selector in ('id="stage2-audit-saturation"',
                                 'id="stage2-audit-background"',
                                 'id="stage2-audit-ab"',
                                 'id="stage2-audit-solar-structure"',
                                 'id="stage2-audit-bcr"'):
                    self.assertTrue(selector in html, selector)
            else:
                self.assertNotIn('id="stage2-audit-saturation"', html)
                for selector in (
                    'active/high 与运行截断',
                    'power_level: "high"',
                    '二阶候选因支撑区间非单调被拒绝',
                    'high 层通道',
                    'id="scs-unit-model-audit"',
                    '筛选上限覆盖',
                    '所有分档样本数现覆盖全部有效样本',
                ):
                    self.assertTrue(selector in html, f'{satellite}: {selector}')

    def test_stage2_reports_show_representative_input_output_and_soc_curve(self):
        cases = (
            (BASE / 'satellites/SCS-01-01/'
             'framework_documentation_SCS-01-01.html', 'SCS'),
            (BASE / 'satellites/YJ-3D/'
             'framework_documentation_YJ-3D.html', 'YJ'),
        )
        for page, label in cases:
            if not page.is_file():
                self.skipTest(f'{label} 星别报告不存在')
            html = page.read_text(encoding='utf-8')
            for selector in (
                    'id="stage2-case"',
                    'id="stage2-case-input"',
                    'id="stage2-case-output"',
                    'data-stage2-case-chart="soc-predicted-vs-observed"'):
                self.assertTrue(selector in html, f'{label}: {selector}')
            self.assertIn('预测 SOC', html)
            self.assertIn('实际条件 SOC', html)
            self.assertIn('模型输入', html)
            self.assertIn('模型输出', html)
            if label == 'YJ':
                self.assertTrue(
                    '使用历史提取的15条任务' in html, 'YJ task-aware case')
                self.assertTrue('TMEA006' in html, 'YJ current integration')
                self.assertTrue('V-SOC 端点' in html, 'YJ V-SOC crosscheck')
                self.assertIn('红色虚线 = V-SOC 端点 72.737%', html)
                chart_match = re.search(
                    r'<svg[^>]*data-stage2-case-chart='
                    r'"soc-predicted-vs-observed"[\s\S]*?</svg>', html)
                self.assertIsNotNone(chart_match)
                chart_root = ET.fromstring(chart_match.group())
                self.assertEqual(
                    len(chart_root.findall(f'.//{SVG}path')), 2)
                self.assertTrue(any(
                    line.get('stroke') == '#dc4667'
                    for line in chart_root.findall(f'.//{SVG}line')))
            else:
                self.assertTrue(
                    '2条历史提取任务' in html, 'SCS task-aware case')
                self.assertTrue(
                    '不提供太阳阵功率' in html, 'SCS simplified contract')
                self.assertTrue('phase_segments' in html, 'SCS phase input')


if __name__ == '__main__':
    unittest.main()
