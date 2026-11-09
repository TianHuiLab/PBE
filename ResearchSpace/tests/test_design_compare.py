"""设计功耗对照（config/design_load_table.json + design_compare.py）的离线单测。

全部用合成分钟表，不依赖 ClickHouse 或本地大 CSV。
"""
import copy
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import design_compare as dc

BASE = Path(__file__).resolve().parent.parent
NS = '{http://www.w3.org/2000/svg}'
SVG = NS + 'svg'

RAILS = {
    'bus': {'name': '一次母线 23.1–29.2 V', 'voltage_code': 'TMD016', 'nominal_V': None},
    'rail_5v': {'name': '5 V 二次电源', 'voltage_code': 'TMD059', 'nominal_V': 5.0},
}

TABLE = {
    '_meta': {'working_state_ratio': 0.1, 'design_rows': 2},
    'satellite_id': 'TEST-01',
    'rails': RAILS,
    'rows': [
        {
            'no': '1', 'subsystem': '载荷系统', 'name': '母线负载',
            'rail': 'bus', 'routes': 2, 'design_W': 56.0, 'switch': '开关控制',
            'measure': {'mode': 'rail_current', 'devices': [['TMD023', 'TMD024']]},
            'match': 'exact', 'note': '',
        },
        {
            'no': '2', 'subsystem': '姿轨控系统', 'name': '5 V 负载',
            'rail': 'rail_5v', 'routes': 1, 'design_W': 2.2, 'switch': '无',
            'measure': {'mode': 'rail_current', 'devices': [['TMD067']]},
            'match': 'exact', 'note': '',
        },
    ],
}


def minute(t, code, value):
    return {'t': t, 'param_code': code, 'avg': value}


def build_minutes(bus_current='1.0', negative='-0.02'):
    rows = []
    for index in range(10):
        t = f'2026-01-01 00:{index:02d}:00'
        rows.append(minute(t, 'TMD016', '28.0'))
        rows.append(minute(t, 'TMD059', '5.0'))
        rows.append(minute(t, 'TMD023', bus_current))
        rows.append(minute(t, 'TMD024', negative))
        rows.append(minute(t, 'TMD067', '0.6'))
    return rows


class DesignCompareTests(unittest.TestCase):
    def test_bus_and_secondary_rails_are_applied_separately(self):
        summary = dc.summarize(build_minutes(), TABLE)
        bus_item, five_volt_item = summary['items']
        self.assertAlmostEqual(bus_item['median'], 28.0, places=6)
        # 5 V × 0.6 A；若误用母线电压会得到 16.8 W
        self.assertAlmostEqual(five_volt_item['median'], 3.0, places=6)

    def test_negative_branch_current_is_clamped_to_zero(self):
        summary = dc.summarize(build_minutes(negative='-2.0'), TABLE)
        bus_item = summary['items'][0]
        self.assertAlmostEqual(bus_item['median'], 28.0, places=6)
        self.assertAlmostEqual(bus_item['max'], 28.0, places=6)

    def test_working_state_needs_ten_percent_of_design(self):
        item = dc.summarize(build_minutes(), TABLE)['items'][0]
        self.assertEqual(item['n'], 10)
        self.assertEqual(item['work_n'], 10)
        self.assertAlmostEqual(item['work_median'], 28.0, places=6)

    def test_working_state_excludes_idle_minutes(self):
        rows = []
        for index in range(10):
            t = f'2026-01-01 01:{index:02d}:00'
            rows.append(minute(t, 'TMD016', '28.0'))
            rows.append(minute(t, 'TMD059', '5.0'))
            rows.append(minute(t, 'TMD023', '1.0' if index == 0 else '0.01'))
            rows.append(minute(t, 'TMD024', '0.0'))
            rows.append(minute(t, 'TMD067', '0.6'))
        item = dc.summarize(rows, TABLE)['items'][0]
        self.assertEqual(item['work_n'], 1)
        self.assertAlmostEqual(item['work_median'], 28.0, places=6)

    def test_modes_are_not_added_and_nearest_is_used(self):
        table = copy.deepcopy(TABLE)
        table['rows'][0]['modes'] = [{'label': '高性能模式', 'W': 56.0},
                                     {'label': '常规模式', 'W': 30.0}]
        item = dc.summarize(build_minutes(), table)['items'][0]
        # 实测 28 W 更接近 30 W 的常规模式，比值对 30 W 计，而不是 56+30
        self.assertEqual(item['mode_label'], '常规模式')
        self.assertAlmostEqual(item['mode_W'], 30.0, places=6)
        self.assertAlmostEqual(item['ratio'], 28.0 / 30.0, places=6)
        # 两个设计值都要画进单机曲线：比较模式用 6 4 长虚线，另一模式用 3 4 短虚线
        svg = dc.unit_series_chart(item)
        self.assertIn('stroke-dasharray="6 4"', svg)
        self.assertIn('stroke-dasharray="3 4"', svg)
        self.assertIn('设计 30 W', svg)
        self.assertIn('设计 56 W', svg)

    def test_multiple_devices_compare_per_device_not_sum(self):
        table = copy.deepcopy(TABLE)
        table['rows'][0]['measure'] = {'mode': 'rail_current',
                                       'devices': [['TMD023'], ['TMD024']]}
        rows = build_minutes()
        # TMD024 为 −0.02 A（零电流），两台里只有 TMD023 在工作
        item = dc.summarize(rows, table)['items'][0]
        self.assertEqual(item['n_devices'], 2)
        self.assertAlmostEqual(item['work_p95'], 28.0, places=6)
        self.assertAlmostEqual(item['ratio'], 28.0 / 56.0, places=6)
        # 两台合计只作参考：另一台为零电流，合计仍等于 28 W
        self.assertAlmostEqual(item['total']['p95'], 28.0, places=6)

    def test_vi_pair_uses_its_own_voltage(self):
        table = copy.deepcopy(TABLE)
        table['rows'][1]['measure'] = {'mode': 'vi_pair',
                                       'voltage': 'TMD053', 'current': 'TMD054'}
        rows = build_minutes()
        for index in range(10):
            t = f'2026-01-01 00:{index:02d}:00'
            rows.append(minute(t, 'TMD053', '26.0'))
            rows.append(minute(t, 'TMD054', '0.5'))
        item = dc.summarize(rows, table)['items'][1]
        self.assertAlmostEqual(item['median'], 13.0, places=6)

    def test_missing_branch_series_yields_no_samples(self):
        rows = [row for row in build_minutes() if row['param_code'] != 'TMD067']
        item = dc.summarize(rows, TABLE)['items'][1]
        self.assertEqual(item['n'], 0)
        self.assertIsNone(item['median'])
        self.assertIsNone(item['ratio'])


    def test_summarize_keeps_full_series_by_default(self):
        summary = dc.summarize(build_minutes(), TABLE)
        self.assertEqual(len(summary['items'][0]['series']), 10)
        self.assertIsNone(dc.summarize(build_minutes(), TABLE,
                                       with_series=False)['items'][0]['series'])

    def test_select_series_window_centres_on_peak(self):
        rows = [minute('2026-01-01 00:00:00', 'TMD016', '28.0'),
                minute('2026-01-01 01:00:00', 'TMD016', '28.0'),
                minute('2026-01-01 02:00:00', 'TMD016', '28.0'),
                minute('2026-01-01 03:00:00', 'TMD016', '28.0'),
                minute('2026-01-01 04:00:00', 'TMD016', '28.0')]
        for stamp, current in (('00:00:00', '0.2'), ('01:00:00', '0.5'),
                               ('02:00:00', '1.0'), ('03:00:00', '0.5'),
                               ('04:00:00', '0.2')):
            rows.append(minute(f'2026-01-01 {stamp}', 'TMD023', current))
            rows.append(minute(f'2026-01-01 {stamp}', 'TMD024', '0.0'))
            rows.append(minute(f'2026-01-01 {stamp}', 'TMD067', '0.6'))
            rows.append(minute(f'2026-01-01 {stamp}', 'TMD059', '5.0'))
        item = dc.summarize(rows, TABLE)['items'][0]
        segment = dc.select_series_window(item)
        # 峰值 02:00 前后 1 小时只有 3 个点（<8），扩到前后 3 小时取得 5 个点
        self.assertEqual(segment['half_minutes'], 180)
        self.assertEqual(len(segment['points']), 5)
        self.assertEqual(segment['start'], '2026-01-01 00:00:00')
        self.assertEqual(segment['end'], '2026-01-01 04:00:00')
        # 28 W ≥ 设计 56 W 的 10%，5 个分钟都算工作态，窗口取其最密集处
        self.assertEqual(segment['active'], 5)

    def test_unit_series_chart_draws_curve_and_design_dashed_line(self):
        summary = dc.summarize(build_minutes(), TABLE)
        item = summary['items'][0]
        svg = dc.unit_series_chart(item)
        self.assertTrue(svg.startswith('<svg '))
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, SVG)
        paths = root.findall(f'.//{NS}path')
        self.assertEqual(len(paths), 1)
        self.assertIsNone(paths[0].get('stroke-dasharray'))
        dashed = [line for line in root.findall(f'.//{NS}line')
                  if line.get('stroke-dasharray') == '6 4']
        # 图例样例 + 贯穿绘图区的设计值虚线
        self.assertEqual(len(dashed), 2)
        self.assertTrue(all(line.get('stroke') == '#dc4667' for line in dashed))
        self.assertTrue(any(line.get('x1') == '70' and line.get('x2') == '952'
                            for line in dashed))
        texts = [node.text or '' for node in root.findall(f'.//{NS}text')]
        self.assertTrue(any('设计 56 W' in text for text in texts))
        self.assertTrue(any('实测功率' in text for text in texts))

    def test_html_section_offers_unit_series_view(self):
        summary = dc.summarize(build_minutes(), TABLE)
        html = '\n'.join(dc.html_section(summary, dc.ratio_chart(summary),
                                          dc.power_chart(summary)))
        self.assertIn('id="design-view-select"', html)
        self.assertIn('value="unit-series"', html)
        self.assertIn('id="design-unit-select"', html)
        self.assertIn('data-design-view="overview"', html)
        self.assertIn('data-design-view="unit-series" hidden', html)
        self.assertIn('data-design-unit="unit-0"', html)
        self.assertIn('曲线窗口', html)

    def test_html_section_lists_each_route_without_leaking_series(self):
        summary = dc.summarize(build_minutes(), TABLE)
        html = '\n'.join(dc.html_section(summary, dc.ratio_chart(summary),
                                         dc.power_chart(summary)))
        # 两个支路各给一个面板；路数列仍是“供电路数”，不能把支路明细写进表格
        self.assertIn('data-design-unit="route-0-0"', html)
        self.assertIn('data-design-unit="route-0-1"', html)
        self.assertNotIn("'code':", html)
        self.assertIn('载荷系统 · 2 路', html)

    def test_blocked_row_renders_reason_not_just_missing(self):
        table = copy.deepcopy(TABLE)
        table['rows'][1]['match'] = 'none'
        table['rows'][1]['measure'] = {'mode': 'rail_current', 'devices': []}
        table['rows'][1]['blocked'] = {
            'category': 'voltage_only', 'detail': '只有 TMD065 工作电压',
            'seen': ['TMD065 MEMS 陀螺工作电压（仅电压）'], 'fix': '5 V 分路增设电流遥测'}
        summary = dc.summarize(build_minutes(), table)
        self.assertEqual(len(summary['blocked']), 1)
        self.assertEqual(summary['blocked'][0]['match_label'], '不可对照')
        html = '\n'.join(dc.html_section(summary, dc.ratio_chart(summary),
                                          dc.power_chart(summary)))
        self.assertIn('不可对照逐行核查', html)
        self.assertIn('仅工作电压、无电流', html)
        self.assertIn('TMD065 MEMS 陀螺工作电压（仅电压）', html)
        self.assertIn('补：5 V 分路增设电流遥测', html)

    def test_unmatched_telemetry_is_quantified(self):
        table = copy.deepcopy(TABLE)
        table['_meta']['unmatched_telemetry'] = [
            {'code': 'TMD023', 'name': '测试设备', 'voltage_code': 'TMD016',
             'note': '设计表无该行'}]
        summary = dc.summarize(build_minutes(), table)
        entry = summary['unmatched'][0]
        self.assertEqual(entry['n'], 10)
        # 1.0 A × 28 V
        self.assertAlmostEqual(entry['median'], 28.0, places=6)
        html = '\n'.join(dc.html_section(summary, dc.ratio_chart(summary),
                                          dc.power_chart(summary)))
        self.assertIn('反向缺口', html)
        self.assertIn('测试设备', html)

    def test_every_real_blocked_row_has_a_reason(self):
        table = dc.load_table()
        blocked = [row for row in table['rows'] if row['match'] == 'none']
        self.assertEqual(len(blocked), 10)
        for row in blocked:
            info = row.get('blocked')
            self.assertIsNotNone(info, f'第 {row["no"]} 行缺少不可对照原因')
            self.assertIn(info['category'], dc.BLOCK_CATEGORY)
            self.assertTrue(info['detail'].strip())
            self.assertTrue(info['seen'], f'第 {row["no"]} 行没写现有遥测')
            self.assertTrue(info['fix'].strip())
        # 反向缺口：有实测、设计表没有对应行的设备
        unmatched = table['_meta'].get('unmatched_telemetry') or []
        self.assertGreaterEqual(len(unmatched), 4)
        for entry in unmatched:
            self.assertTrue(entry['code'].startswith('TM'))
            self.assertTrue(entry['voltage_code'].startswith('TM'))

    def test_abs_current_uses_mean_abs_series(self):
        """双向电流必须用 mean(|I|)：有符号分钟均值会被正负抵消。"""
        table = copy.deepcopy(TABLE)
        table['rows'][0]['rail'] = 'rail_5v'
        table['rows'][0]['design_W'] = 2.5
        table['rows'][0]['measure'] = {'mode': 'rail_current', 'stat': 'abs_current',
                                       'devices': [['TMK6821']]}
        rows = build_minutes()
        abs_series = {'TMK6821': {f'2026-01-01 00:{index:02d}:00': 0.5
                                  for index in range(10)}}
        summary = dc.summarize(rows, table, abs_series=abs_series)
        item = summary['items'][0]
        # 5 V × 0.5 A = 2.5 W；若误用有符号均值，该代号在分钟表里根本没有数据
        self.assertAlmostEqual(item['median'], 2.5, places=6)
        self.assertAlmostEqual(item['work_p95'], 2.5, places=6)
        # 逐路拆解同样走绝对值序列
        self.assertAlmostEqual(item['route_list'][0]['median'], 2.5, places=6)

    def test_compact_codes_folds_consecutive_numbers(self):
        self.assertEqual(dc._compact_codes(['TMD031', 'TMD032', 'TMD033', 'TMD034']), 'TMD031–034')
        self.assertEqual(dc._compact_codes(['TMD025', 'TMD028']), 'TMD025、TMD028')
        self.assertEqual(dc._compact_codes([]), '—')

    def test_charts_are_wellformed_svg_without_nan(self):
        summary = dc.summarize(build_minutes(), TABLE)
        for builder in (dc.ratio_chart, dc.power_chart):
            svg = builder(summary)
            self.assertTrue(svg.startswith('<svg '))
            self.assertNotIn('nan', svg.lower())
            root = ET.fromstring(svg)
            self.assertEqual(root.tag, SVG)
            self.assertGreater(len(root.findall(f'.//{NS}rect')), 0)

    def test_html_section_lists_every_design_row(self):
        summary = dc.summarize(build_minutes(), TABLE)
        html = '\n'.join(dc.html_section(summary, dc.ratio_chart(summary),
                                         dc.power_chart(summary)))
        self.assertIn('id="design-compare"', html)
        self.assertIn('设计功耗对照', html)
        for item in summary['items']:
            self.assertIn(item['name'], html)
        self.assertEqual(html.count('已对照'), 2)
        self.assertIn('逐行说明', html)

    def test_real_config_is_internally_consistent(self):
        table = dc.load_table(BASE / 'config' / 'design_load_table.json')
        self.assertEqual(table['satellite_id'], 'SCS-01-01')
        self.assertEqual(table['_meta']['design_rows'], 22)
        self.assertEqual(len(table['rows']), 21)  # 序号 1、2 合并为一行
        measured = 0
        for row in table['rows']:
            self.assertGreater(row['design_W'], 0)
            self.assertIn(row['rail'], table['rails'])
            self.assertIn(row['match'], dc.MATCH_LABEL)
            measure = row['measure']
            if row['match'] == 'none':
                self.assertFalse(measure.get('devices'))
            else:
                measured += 1
                self.assertTrue(measure.get('devices') or measure.get('current'))
        self.assertEqual(measured, 11)
        # 智算机两行是同一台单机的两种模式，不叠加
        # 磁力矩器是双向线圈电流，必须走 mean(|I|) 口径
        torquer = next(row for row in table['rows'] if row['no'] == '19')
        self.assertEqual(torquer['match'], 'exact')
        self.assertEqual(torquer['measure']['stat'], 'abs_current')
        self.assertEqual(torquer['measure']['devices'],
                         [['TMK6821'], ['TMK6822'], ['TMK6823'], ['TMK6824']])
        self.assertEqual(dc.abs_current_codes(table),
                         ['TMK6821', 'TMK6822', 'TMK6823', 'TMK6824'])
        # 测传一体机按用户指定用分系统侧 TMC121/TMC122
        comm = next(row for row in table['rows'] if row['no'] == '13')
        self.assertEqual(comm['match'], 'exact')
        # 用户 2026-11-04 确认该机由 12 V 二次电源供电；设计表该行电压列写母线，待勘误
        self.assertEqual(comm['rail'], 'rail_12v')
        self.assertEqual(comm['measure']['voltage'], 'TMC121')
        self.assertEqual(comm['measure']['current'], 'TMC122')
        nx1 = next(row for row in table['rows'] if row['no'] == '1–2')
        self.assertEqual([mode['W'] for mode in nx1['modes']], [93.5, 49.0])
        self.assertEqual(nx1['measure']['devices'], [['TMD025', 'TMD028']])


if __name__ == '__main__':
    unittest.main()
