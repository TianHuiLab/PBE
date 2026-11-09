#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已落地的真实分钟遥测和 v2 输出生成离线 SVG 图。"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
import csv
import hashlib
import math
import re
import statistics

import design_compare
from stage2_audit import ensure_stage2_outputs

BASE = Path(__file__).resolve().parent
OUT = BASE / 'charts'
REPORT_NAME = 'framework_documentation_current.html'
WIDTH, HEIGHT = 960, 310
LEFT, RIGHT, TOP, BOTTOM = 68, 22, 48, 48
PW, PH = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
PALETTE = ('#2563eb', '#059669', '#e58914', '#dc4667', '#7c3aed', '#0891b2')
BATTERY_WINDOW_CSV = 'out/battery_current_2025-12-29_0545_0620.csv'
BATTERY_FOCUS = '2025-12-29 06:03:00'
ECLIPSE_WINDOW_CSV = 'out/battery_current_2025-12-29_0504_0537.csv'
ECLIPSE_FOCUS = '2025-12-29 05:21:00'
SATELLITE_02_POWER_WINDOWS = (
    ('日照', '2026-02-02 14:17:00', '2026-02-02 14:50:00', '2026-02-02 14:20:00'),
    ('地影', '2026-02-02 15:18:00', '2026-02-02 15:50:00', '2026-02-02 15:34:00'),
)

# 图表清单（单一事实源：config/charts.json）；缺失时回退内置列表
FALLBACK_CHART_NAMES = [
    'chart_voltages', 'chart_currents', 'chart_solar_panels', 'chart_panel_voltages',
    'chart_power_flow', 'chart_shunt', 'chart_array_s3r_candidate',
    'chart_soc_dod', 'chart_daily_power',
    'chart_daily_soc', 'chart_daily_shunt', 'chart_energy_daily',
]


def load_chart_manifest():
    """返回文档内嵌图表的有序名称清单。"""
    path = BASE / 'config' / 'charts.json'
    if path.is_file():
        with path.open(encoding='utf-8') as f:
            cfg = json.load(f)
        names = [c['name'] for c in cfg.get('charts', [])]
        if names:
            return names
    print('[gen_charts] 未找到 config/charts.json，使用内置图表清单', file=sys.stderr)
    return list(FALLBACK_CHART_NAMES)


def clean_orphan_svgs():
    """删除 charts/ 中不再属于清单的陈旧 SVG（往期迭代产物）。"""
    expected = set(load_chart_manifest())
    removed = []
    for path in sorted(OUT.glob('*.svg')):
        if path.stem not in expected:
            path.unlink()
            removed.append(path.name)
    for name in removed:
        print(f'removed orphan charts/{name}')
    return removed


def rows(path):
    with (BASE / path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def value(raw):
    try:
        number = float(raw)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def tmh025_raw_overview(minute=None):
    """保留本次取数窗口全部分钟原值及极值；不应用电流有效范围筛选。"""
    if minute is None:
        with (BASE / 'data_power_minute.csv').open(encoding='utf-8-sig', newline='') as stream:
            items = [row for row in csv.DictReader(stream)
                     if row['param_code'] == 'TMH025']
    else:
        items = [row for row in minute if row['param_code'] == 'TMH025']
    items.sort(key=lambda row: row['t'])
    if not items:
        return None
    times = [row['t'] for row in items]
    low = [value(row.get('mn')) for row in items]
    avg = [value(row.get('avg')) for row in items]
    high = [value(row.get('mx')) for row in items]
    observed_avg = [number for number in avg if number is not None]
    observed_high = [number for number in high if number is not None]
    if not observed_avg or not observed_high:
        return None
    svg = line_chart(
        'TMH025 全量数据库原值 · 分钟最小/均值/最大',
        {'分钟最小': (times, low, PALETTE[0]),
         '分钟均值': (times, avg, PALETTE[1]),
         '分钟最大': (times, high, PALETTE[3])},
        xname='星上北京时间 · 观测分钟', ylabel='原值',
        gap_seconds=180, window_start=times[0], window_end=times[-1])
    return {'svg': svg.replace('<svg ', '<svg data-tmh025-raw-overview="true" ', 1),
            'n': len(items), 'n_avg': len(observed_avg),
            'n_above_100': sum(number > 100 for number in observed_high),
            'median_avg': statistics.median(observed_avg),
            'maximum': max(observed_high), 'start': times[0], 'end': times[-1]}


def stamp(raw):
    if isinstance(raw, datetime):
        return raw.timestamp()
    if isinstance(raw, (int, float)):
        return float(raw)
    return datetime.fromisoformat(str(raw)).timestamp()


def fmt(number):
    return f'{number:.1f}' if abs(number) < 100 else f'{number:.0f}'


def phase2_json(relative_path):
    path = Path(relative_path)
    if not path.is_absolute():
        path = BASE / path
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)


def phase2_csv(relative_path):
    path = Path(relative_path)
    if not path.is_absolute():
        path = BASE / path
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def phase2_horizontal_bars(title, items, unit, chart_id, *,
                           subtitle='', value_digits=2, xmax=None):
    """绘制非负横向条形图；每个 item 含 label/value/color。"""
    if not items:
        return ''
    width, left, right, top, row_height = 960, 260, 78, 92, 43
    height = top + len(items) * row_height + 46
    maximum = max(float(item['value']) for item in items)
    scale_max = (float(xmax) if xmax is not None else
                 maximum * 1.18 if maximum > 0 else 1.0)
    if scale_max <= 0:
        raise ValueError("xmax 必须为正数")
    plot_width = width - left - right
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'data-phase2-chart="{escape(chart_id, quote=True)}" role="img" '
        f'aria-label="{escape(title, quote=True)}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="24" y="30" fill="#17243a" font-size="17" font-weight="700">'
        f'{escape(title)}</text>',
    ]
    if subtitle:
        parts.append(f'<text x="24" y="53" fill="#65748a" font-size="11">'
                     f'{escape(subtitle)}</text>')
    for index in range(5):
        tick = scale_max * index / 4
        x = left + plot_width * index / 4
        parts.extend([
            f'<line x1="{x:.1f}" y1="{top - 12}" x2="{x:.1f}" '
            f'y2="{top + len(items) * row_height - 8}" '
            'stroke="#e4ebf4" stroke-width="1"/>',
            f'<text x="{x:.1f}" y="{top + len(items) * row_height + 14}" '
            'text-anchor="middle" fill="#65748a" font-size="10">'
            f'{tick:.2f}</text>',
        ])
    for index, item in enumerate(items):
        center_y = top + index * row_height + row_height / 2
        bar_y = center_y - 10
        number = float(item['value'])
        bar_width = min(max(number / scale_max * plot_width, 0), plot_width)
        color = item.get('color', PALETTE[index % len(PALETTE)])
        stroke = (' stroke="#173d78" stroke-width="2"' if item.get('selected') else '')
        parts.extend([
            f'<text x="24" y="{center_y - 1}" fill="#263f62" font-size="12" '
            f'font-weight="600">{escape(str(item["label"]))}</text>',
        ])
        if item.get('detail'):
            parts.append(
                f'<text x="24" y="{center_y + 13}" fill="#7b8798" font-size="9.5">'
                f'{escape(str(item["detail"]))}</text>')
        parts.extend([
            f'<rect x="{left}" y="{bar_y}" width="{bar_width:.1f}" '
            f'height="20" rx="4" fill="{color}"{stroke}/>',
            f'<text x="{left + bar_width + 8:.1f}" y="{center_y + 4}" '
            f'fill="#40536c" font-size="11" font-weight="650">'
            f'{number:.{value_digits}f} {escape(unit)}</text>',
        ])
    parts.extend([
        f'<text x="{left + plot_width / 2:.1f}" y="{height - 8}" text-anchor="middle" '
        f'fill="#52627a" font-size="10">{escape(unit)}</text>',
        '</svg>',
    ])
    return ''.join(parts)


def phase2_diverging_bars(title, items, unit, chart_id, *, subtitle='',
                          value_digits=2):
    """以零线为中心绘制有符号误差；item 含 label/value/detail。"""
    if not items:
        return ''
    width, left, right, top, row_height = 960, 225, 54, 96, 38
    height = top + len(items) * row_height + 46
    maximum = max(abs(float(item['value'])) for item in items)
    scale_max = maximum * 1.12 if maximum > 0 else 1.0
    plot_width = width - left - right
    center_x = left + plot_width / 2
    half_width = plot_width / 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'data-phase2-chart="{escape(chart_id, quote=True)}" role="img" '
        f'aria-label="{escape(title, quote=True)}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="24" y="30" fill="#17243a" font-size="17" font-weight="700">'
        f'{escape(title)}</text>',
    ]
    if subtitle:
        parts.append(f'<text x="24" y="53" fill="#65748a" font-size="11">'
                     f'{escape(subtitle)}</text>')
    ticks = (-scale_max, -scale_max / 2, 0.0, scale_max / 2, scale_max)
    for tick in ticks:
        x = center_x + tick / scale_max * half_width
        stroke = '#8795a9' if tick == 0 else '#e4ebf4'
        parts.extend([
            f'<line x1="{x:.1f}" y1="{top - 12}" x2="{x:.1f}" '
            f'y2="{top + len(items) * row_height - 8}" '
            f'stroke="{stroke}" stroke-width="{1.2 if tick == 0 else 1}"/>',
            f'<text x="{x:.1f}" y="{top + len(items) * row_height + 14}" '
            f'text-anchor="middle" fill="#65748a" font-size="10">'
            f'{tick:.2f}</text>',
        ])
    for index, item in enumerate(items):
        center_y = top + index * row_height + row_height / 2
        number = float(item['value'])
        end_x = center_x + number / scale_max * half_width
        x = min(center_x, end_x)
        bar_width = max(abs(end_x - center_x), 1.5)
        color = item.get('color') or ('#2554ad' if number >= 0 else '#b66b18')
        parts.extend([
            f'<text x="24" y="{center_y - 1}" fill="#263f62" font-size="11.5" '
            f'font-weight="600">{escape(str(item["label"]))}</text>',
        ])
        if item.get('detail'):
            parts.append(
                f'<text x="24" y="{center_y + 12}" fill="#7b8798" font-size="9">'
                f'{escape(str(item["detail"]))}</text>')
        parts.append(
            f'<rect x="{x:.1f}" y="{center_y - 8}" width="{bar_width:.1f}" '
            f'height="16" rx="3" fill="{color}"/>')
        value_x = end_x + 6 if number >= 0 else end_x - 6
        anchor = 'start' if number >= 0 else 'end'
        parts.append(
            f'<text x="{value_x:.1f}" y="{center_y + 4}" text-anchor="{anchor}" '
            f'fill="#40536c" font-size="10.5" font-weight="650">'
            f'{number:+.{value_digits}f} {escape(unit)}</text>')
    parts.extend([
        f'<text x="{center_x:.1f}" y="{height - 8}" text-anchor="middle" '
        f'fill="#52627a" font-size="10">{escape(unit)} · 零线为无误差</text>',
        '</svg>',
    ])
    return ''.join(parts)


def phase2_soc_scatter(title, points, chart_id, *, xlabel='观测目标 SOC (%)',
                       ylabel='预测目标 SOC (%)'):
    """绘制逐留出窗口的预测—观测散点及 1:1 线。"""
    if not points:
        return ''
    width, height = 960, 430
    left, right, top, bottom = 76, 34, 74, 58
    plot_width, plot_height = width - left - right, height - top - bottom
    values = [number for point in points for number in (point['x'], point['y'])]
    low = math.floor(min(values) - 1)
    high = math.ceil(max(values) + 1)
    if high <= low:
        high = low + 1
    X = lambda number: left + (number - low) / (high - low) * plot_width
    Y = lambda number: top + (high - number) / (high - low) * plot_height
    colors = {'DJF': '#2554ad', 'MAM': '#007e80',
              'JJA': '#d17a0b', 'SON': '#7c3aed'}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'data-phase2-chart="{escape(chart_id, quote=True)}" role="img" '
        f'aria-label="{escape(title, quote=True)}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="24" y="30" fill="#17243a" font-size="17" font-weight="700">'
        f'{escape(title)}</text>',
        f'<text x="24" y="53" fill="#65748a" font-size="11">'
        f'共 {len(points)} 个分组留出窗口；越接近对角虚线，预测与观测越一致。</text>',
    ]
    for index in range(5):
        value = low + (high - low) * index / 4
        x = X(value)
        y = Y(value)
        parts.extend([
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" '
            f'y2="{top + plot_height}" stroke="#e4ebf4"/>',
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" '
            f'y2="{y:.1f}" stroke="#e4ebf4"/>',
            f'<text x="{x:.1f}" y="{top + plot_height + 20}" text-anchor="middle" '
            f'fill="#65748a" font-size="10">{value:.1f}</text>',
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'fill="#65748a" font-size="10">{value:.1f}</text>',
        ])
    parts.extend([
        f'<line x1="{X(low):.1f}" y1="{Y(low):.1f}" '
        f'x2="{X(high):.1f}" y2="{Y(high):.1f}" '
        'stroke="#8795a9" stroke-width="1.5" stroke-dasharray="6 5"/>',
        f'<text x="{left + plot_width / 2:.1f}" y="{height - 12}" '
        f'text-anchor="middle" fill="#52627a" font-size="11">{escape(xlabel)}</text>',
        f'<text x="18" y="{top + plot_height / 2:.1f}" text-anchor="middle" '
        f'fill="#52627a" font-size="11" transform="rotate(-90 18 '
        f'{top + plot_height / 2:.1f})">{escape(ylabel)}</text>',
    ])
    for point in points:
        color = colors.get(point.get('group'), PALETTE[0])
        parts.append(
            f'<circle cx="{X(point["x"]):.1f}" cy="{Y(point["y"]):.1f}" '
            f'r="5.5" fill="{color}" fill-opacity=".88" stroke="#ffffff" '
            f'stroke-width="1.5"><title>{escape(str(point["label"]))}；'
            f'观测 {point["x"]:.2f}%，预测 {point["y"]:.2f}%</title></circle>')
    for index, group in enumerate(('DJF', 'MAM', 'JJA')):
        x = left + index * 82
        y = 65
        parts.extend([
            f'<circle cx="{x}" cy="{y}" r="4.5" fill="{colors[group]}"/>',
            f'<text x="{x + 8}" y="{y + 4}" fill="#52627a" font-size="10">'
            f'{group}</text>',
        ])
    parts.append('</svg>')
    return ''.join(parts)


def phase2_flow(steps):
    fragments = ['<div class="phase2-flow">']
    for index, (title_text, detail) in enumerate(steps, 1):
        fragments.append(
            '<div class="phase2-flow-node">'
            f'<small>STEP {index:02d}</small>'
            f'<strong>{escape(title_text)}</strong>'
            f'<span>{escape(detail)}</span>'
            '</div>')
    fragments.append('</div>')
    return ''.join(fragments)


def _audit_value(value, digits=2, suffix=''):
    if value is None:
        return '—'
    return f'{float(value):.{digits}f}{suffix}'


def stage2_audit_html(profile_root):
    """Render common and satellite-specific audit metrics for one report."""
    profile_root = Path(profile_root).resolve()
    audit_path, manifest_path = ensure_stage2_outputs(profile_root)
    with audit_path.open(encoding='utf-8') as stream:
        audit = json.load(stream)
    with manifest_path.open(encoding='utf-8') as stream:
        manifest = json.load(stream)
    metric_labels = {
        'soc_abs_error_pp': '条件目标 SOC 绝对误差',
        'solar_energy_abs_relative_error_pct': '太阳能量绝对相对误差',
        'load_energy_abs_relative_error_pct': '负载能量绝对相对误差',
    }
    metric_units = {
        'soc_abs_error_pp': 'pp',
        'solar_energy_abs_relative_error_pct': '%',
        'load_energy_abs_relative_error_pct': '%',
    }
    metric_rows = []
    for key, label in metric_labels.items():
        item = audit['metrics'][key]
        equal = item['window_equal']
        lower, upper = equal['bootstrap_95_ci']
        metric_rows.append(
            f'<tr><td>{label}</td><td>{item["n_windows"]}</td>'
            f'<td>{item["effective_dates"]}</td>'
            f'<td>{_audit_value(equal["mean"], 3)}</td>'
            f'<td>{_audit_value(lower, 3)}–{_audit_value(upper, 3)}</td>'
            f'<td>{_audit_value(item["duration_weighted_mean"], 3)}</td>'
            f'<td>{_audit_value(item["energy_weighted_mean"], 3)}</td>'
            f'<td>{metric_units[key]}</td></tr>')
    case = audit.get('representative_case')
    if case:
        case_times = case['series']['times']
        is_yj_case = case['satellite_id'] == 'YJ-3D'
        observed_label = (
            '实际条件 SOC（TMEA006积分）'
            if is_yj_case else '实际条件 SOC（V-SOC曲线）')
        case_svg = line_chart(
            '预测 SOC 与实际条件 SOC',
            {
                '预测 SOC': (
                    case_times, case['series']['predicted_soc_pct'], '#2554ad'),
                observed_label: (
                    case_times, case['series']['observed_soc_pct'], '#007e80'),
            },
            xname='北京时间',
            ylabel='SOC (%)',
            gap_seconds=180,
            reference=(case['outputs'].get('v_soc_endpoint_soc_pct')
                       if is_yj_case else None),
            window_start=case['window']['start'],
            window_end=case['window']['end'])
        case_svg = case_svg.replace(
            '<svg ',
            '<svg data-stage2-case-chart="soc-predicted-vs-observed" ', 1)
        request_json = json.dumps(
            case['input_request'], ensure_ascii=False, indent=2)
        outputs = case['outputs']
        input_cards = ''.join(
            f'<div class="case-input"><b>{escape(label)}</b>'
            f'<span>{escape(value)}</span></div>'
            for label, value in (
                ('初始 SOC / %',
                 f'{case["derived_inputs"]["initial_soc_pct"]:.4f}'),
                ('电池电压 / V',
                 f'{case["derived_inputs"]["battery_voltage_V"]:.3f}'),
                ('背景负载 / W',
                 f'{case["derived_inputs"]["background_load_W"]:.3f}'),
                ('相位', str(case['derived_inputs']['phase'])),
                ('任务数', str(case['derived_inputs']['task_count'])),
            ))
        output_rows = (
            f'<tr><td>目标时刻 SOC</td>'
            f'<td>{outputs["predicted_target_soc_pct"]:.4f}%</td>'
            f'<td>{outputs["observed_target_soc_pct"]:.4f}%</td>'
            f'<td>{outputs["soc_signed_error_pp"]:+.4f} pp</td></tr>'
            f'<tr><td>目标时刻剩余容量</td>'
            f'<td>{outputs["predicted_remaining_Ah"]:.4f} Ah</td>'
            f'<td>{outputs["observed_remaining_Ah"]:.4f} Ah</td>'
            f'<td>{outputs["remaining_Ah_error"]:+.4f} Ah</td></tr>'
            f'<tr><td>太阳能量</td>'
            f'<td>{outputs["predicted_solar_energy_kWh"]:.6f} kWh</td>'
            f'<td>{outputs["observed_solar_proxy_energy_kWh"]:.6f} kWh</td>'
            f'<td>{outputs["solar_energy_relative_error_pct"]:+.2f}%</td></tr>'
            f'<tr><td>负载能量</td>'
            f'<td>{outputs["predicted_load_energy_kWh"]:.6f} kWh</td>'
            f'<td>{outputs["observed_load_energy_kWh"]:.6f} kWh</td>'
            f'<td>{outputs["load_energy_relative_error_pct"]:+.2f}%</td></tr>'
        )
        if is_yj_case:
            output_rows += (
                f'<tr><td>V-SOC 端点交叉参考</td><td>—</td>'
                f'<td>{outputs["v_soc_endpoint_soc_pct"]:.4f}%</td>'
                f'<td>{outputs["v_soc_minus_current_integrated_pp"]:+.4f} pp'
                '（相对电流积分）</td></tr>')
        source_items = [
            ('输入请求', case['sources']['input']),
            ('预测输出', case['sources']['output']),
            ('预测时间线', case['sources']['predicted_timeline']),
            ('实际条件 SOC 来源', case['sources']['observed_timeline']),
            ('验证摘要', case['sources'].get('summary')),
        ]
        if is_yj_case:
            source_items.extend([
                ('历史提取任务', case['sources']['task_plan']),
                ('电流积分 SOC 曲线', case['sources']['observed_soc']),
            ])
        else:
            source_items.extend([
                ('历史提取任务', case['sources']['task_plan']),
                ('实际条件 SOC 曲线', case['sources']['observed_soc']),
                ('太阳阵留出模型',
                 'power_model/batch_validation_holdout_solar_model.json'),
            ])
        source_links = ''.join(
            f'<a href="{escape(path, quote=True)}">{escape(label)}</a>'
            for label, path in source_items if path)
        reference_note = ''
        if is_yj_case:
            reference_note = (
                '<p class="phase2-warn"><strong>云尖SOC参考说明：</strong>'
                '实际条件曲线采用“初始V-SOC锚点 + TMEA006梯形电流积分”，'
                '终点为'
                f'{outputs["observed_target_soc_pct"]:.4f}%。'
                f'V-SOC端点值为{outputs["v_soc_endpoint_soc_pct"]:.4f}%，'
                f'两者相差{outputs["v_soc_minus_current_integrated_pp"]:+.4f} pp。'
                '在电池参考未闭合前，两者都不是独立硬件SOC真值。</p>')
        else:
            reference_note = (
                '<p class="phase2-warn"><strong>输入契约：</strong>'
                '请求只提供时间、相位分段和2条历史提取任务，'
                '不提供太阳阵功率或功率分段。阳照功率由留出三次多项式在'
                '预测器内部按 phase_segments 生成。</p>')
        case_html = (
            '<div class="panel method-panel" id="stage2-case">'
            '<h3>代表性测试用例：模型输入、输出与 SOC 曲线</h3>'
            f'<p><strong>{escape(case["title"])}</strong>　'
            f'{escape(case["window"]["start"])}—'
            f'{escape(case["window"]["end"])}，'
            f'{case["window"]["minutes"]} 分钟、'
            f'{escape(str(case["window"]["phase"]))}。</p>'
            '<h3 id="stage2-case-input">模型输入</h3>'
            f'<div class="case-inputs">{input_cards}</div>'
            f'<pre class="case-code"><code>{escape(request_json)}</code></pre>'
            '<h3 id="stage2-case-output">模型输出与实际条件值</h3>'
            f'<div class="phase2-kpis">'
            f'<div class="phase2-kpi"><span class="phase2-kpi-label">预测终点 SOC</span>'
            f'<strong class="phase2-kpi-value">'
            f'{outputs["predicted_target_soc_pct"]:.3f}%</strong>'
            f'<span class="phase2-kpi-foot">剩余 '
            f'{outputs["predicted_remaining_Ah"]:.3f} Ah</span></div>'
            f'<div class="phase2-kpi"><span class="phase2-kpi-label">实际条件终点 SOC</span>'
            f'<strong class="phase2-kpi-value">'
            f'{outputs["observed_target_soc_pct"]:.3f}%</strong>'
            f'<span class="phase2-kpi-foot">剩余 '
            f'{outputs["observed_remaining_Ah"]:.3f} Ah</span></div>'
            f'<div class="phase2-kpi"><span class="phase2-kpi-label">SOC 偏差</span>'
            f'<strong class="phase2-kpi-value">'
            f'{outputs["soc_signed_error_pp"]:+.3f} pp</strong>'
            f'<span class="phase2-kpi-foot">预测减实际</span></div>'
            f'<div class="phase2-kpi"><span class="phase2-kpi-label">太阳能量偏差</span>'
            f'<strong class="phase2-kpi-value">'
            f'{outputs["solar_energy_relative_error_pct"]:+.2f}%</strong>'
            f'<span class="phase2-kpi-foot">当前目标口径下的相对误差</span></div>'
            '</div>'
            + reference_note +
            '<div class="table-scroll"><table class="data-table">'
            '<thead><tr><th>输出量</th><th>预测</th>'
            '<th>实际条件值</th><th>偏差</th></tr></thead>'
            f'<tbody>{output_rows}</tbody></table></div>'
            '<div class="phase2-card"><div class="phase2-card-header">'
            '<h3 id="stage2-soc-comparison">预测 SOC 与实际条件 SOC</h3>'
            '<span>'
            + (
                f'红色虚线 = V-SOC 端点 '
                f'{outputs["v_soc_endpoint_soc_pct"]:.3f}%'
                if is_yj_case else '一分钟时间轴')
            + '</span></div>'
            f'<figure>{case_svg}<figcaption>'
            + (
                '预测曲线使用历史提取的15条任务和留出模型；实际条件曲线由'
                '初始V-SOC锚点加TMEA006电流积分得到。V-SOC端点72.737%仅作交叉参考。'
                if is_yj_case else
                '预测曲线使用2条历史提取任务，太阳阵功率由留出代理模型自动生成；'
                '实际条件曲线来自TMD019和项目V-SOC曲线。两线首点取自同一初始SOC。')
            +
            '</figcaption></figure></div>'
            '<div class="case-source">来源：' + source_links + '</div>'
            '<div class="phase2-evidence">'
            '<div class="supported"><h4>该案例能说明</h4><p>'
            '初始状态、相位、背景负载、任务输入、太阳阵/负载记账和SOC更新'
            '如何形成一条完整预测时间线，并可直接对照实际条件曲线。</p></div>'
            '<div class="limited"><h4>不能说明</h4><p>'
            + '；'.join(escape(item) for item in case['boundaries'])
            + '。</p></div></div></div>')
    else:
        case_html = ''
    common = (
        '<div class="panel method-panel" id="stage2-audit">'
        '<h3 id="stage2-audit-overview">审计口径、独立日期与不确定性</h3>'
        '<p>bootstrap 以日期为聚类基本样本、固定种子 '
        f'{manifest["metrics"]["soc_abs_error_pp"]["window_equal"]["bootstrap"]["seed"]} '
        '重复 10,000 次。窗口等权、日期等权、时长加权和能量加权分别回答不同问题；'
        '同一日期的多个窗口保留在同一bootstrap聚类中。</p>'
        f'<div class="phase2-meta"><span>schema <b>{escape(manifest["schema_version"])}</b></span>'
        f'<span>输入文件 <b>{len(manifest["inputs"])}</b></span>'
        f'<span>代码文件 <b>{len(manifest["code"])}</b></span>'
        f'<span>生成时间 <b>{escape(manifest["generated_at"])}</b></span></div>'
        '<p><a href="power_model/stage2_manifest.json">下载阶段清单 '
        'stage2_manifest.json</a>　'
        '<a href="power_model/stage2_audit.json">下载审计明细 '
        'stage2_audit.json</a>　'
        '<a href="stage2_testset/manifest.json">下载标准化测试集 '
        'stage2_testset/manifest.json</a>　'
        '<a href="unit_training/manifest.json">下载单机建模训练包 '
        'unit_training/manifest.json</a>　'
        '<a href="unit_training/README.md">查看单机训练数据说明</a></p>'
        '<h3 id="stage2-audit-metrics">总体与加权指标</h3>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>指标</th><th>窗口</th><th>有效日期</th>'
        '<th>窗口等权均值</th><th>bootstrap 95% 区间</th>'
        '<th>时长加权</th><th>能量加权</th><th>单位</th></tr></thead>'
        f'<tbody>{"".join(metric_rows)}</tbody></table></div>'
        '<p class="section-tip">bootstrap 区间描述这些历史窗口均值的不确定性，'
        '不是对未观测季节或全寿命的外推保证。</p></div>')
    if audit['satellite_id'] == 'SCS-01-01':
        return case_html + common

    saturation = audit['soc_saturation']
    saturation_cards = ''.join(
        f'<div class="phase2-kpi"><span class="phase2-kpi-label">{label}</span>'
        f'<strong class="phase2-kpi-value">{item["n_windows"]} 窗</strong>'
        f'<span class="phase2-kpi-foot">MAE '
        f'{_audit_value(item["mae_pp"], 3, " pp")}；p95 '
        f'{_audit_value(item["p95_abs_pp"], 3, " pp")}；'
        f'零点误差 {item["zero_error_count"]} 窗；预测触顶 '
        f'{item["predicted_ceiling_count"]} 窗</span></div>'
        for label, item in (
            ('全部窗口', saturation['all']),
            ('观测饱和窗口', saturation['observed_saturated']),
            ('观测非饱和窗口', saturation['observed_non_saturated'])))
    background = audit['background_load']['summary']
    background_items = []
    for key, label in (
            ('fixed', '固定起始快照'),
            ('rolling_30m', '前30分钟滚动中位'),
            ('rolling_60m', '前60分钟滚动中位'),
            ('hierarchical', '60分钟→前一日→固定回退')):
        item = background[key]
        if item['mean_absolute_error_W'] is not None:
            background_items.append({
                'label': label,
                'detail': (
                    f'{item["n_windows"]} 个可评估窗口；'
                    f'平均相对偏差 {_audit_value(item["mean_relative_error_pct"], 2, "%")}'),
                'value': item['mean_absolute_error_W'],
                'color': '#007e80' if key != 'fixed' else '#6e9ed7',
            })
    background_svg = phase2_horizontal_bars(
        '背景负载预测误差 A/B', background_items, 'W',
        'stage2-background-models',
        subtitle='滚动值只使用窗口起点前同工况历史，不读取窗口内未来遥测',
        value_digits=2).replace(
            'data-phase2-chart="stage2-background-models"',
            'data-stage2-audit-chart="stage2-background-models"')
    pairs = audit['ab_coactivity']['pairs']
    pair_items = [{
        'label': f'PL_{item["pl_channel"]} A/B',
        'detail': (
            f'{item["valid_minutes"]:,} 个有效分钟；A-only '
            f'{item["a_only_minutes"]:,}，B-only {item["b_only_minutes"]:,}'),
        'value': item['both_active_fraction'] * 100,
        'color': '#c02646' if item['both_active_fraction'] >= .5 else '#2554ad',
    } for item in pairs[:12]]
    ab_svg = phase2_horizontal_bars(
        'A/B 同时活跃分钟占比（前12组）', pair_items, '%',
        'stage2-ab-coactivity',
        subtitle=(
            f'共 {audit["ab_coactivity"]["n_pairs"]} 组可比通道；'
            '平台图确认同名A/B为同一物理负载的上下板并联支路'),
        value_digits=1, xmax=100).replace(
            'data-phase2-chart="stage2-ab-coactivity"',
            'data-stage2-audit-chart="stage2-ab-coactivity"')
    pair_rows = ''.join(
        f'<tr><td>PL_{item["pl_channel"]}</td>'
        f'<td>{item["valid_minutes"]:,}</td>'
        f'<td>{item["both_active_minutes"]:,}</td>'
        f'<td>{item["a_only_minutes"]:,}</td>'
        f'<td>{item["b_only_minutes"]:,}</td>'
        f'<td>{item["both_active_fraction"]:.1%}</td></tr>'
        for item in pairs)
    bcr = audit['bcr_outliers']
    topology = audit.get('ab_topology', {})
    solar_structure = audit.get('solar_structure', {})
    primary_solar_structure = audit.get('primary_solar_structure', {})
    return (
        case_html
        + common
        + '<div class="panel" id="stage2-audit-saturation">'
        '<h3>饱和与非饱和窗口分栏</h3>'
        f'<p>{escape(saturation["criterion"])}。'
        '非饱和组才是观察正常剩余电量调节能力的主要样本。</p>'
        f'<div class="phase2-kpis">{saturation_cards}</div></div>'
        '<div class="panel" id="stage2-audit-background">'
        '<h3>背景负载滚动模型审计</h3>'
        f'<p>{escape(audit["background_load"]["background_definition"])}</p>'
        f'<div class="phase2-card"><div class="phase2-card-header">'
        f'<h3>{escape("背景负载预测误差 A/B")}</h3>'
        f'<span>{escape(audit["background_load"]["method"])}</span></div>'
        f'<figure>{background_svg}<figcaption>'
        '固定值表示部署时的现有快照策略。分层候选按“前60分钟→'
        '前一天同相位时钟窗口→固定快照”选择；当前命中来源：'
        f'{escape(str(audit["background_load"].get("hierarchical", {}).get("summary", {}).get("selected_sources", {})))}。'
        '未配置真实轨道周期，前一日回退只是同相位时钟代理，不自动替换正式口径。</figcaption>'
        '</figure></div></div>'
        '<div class="panel" id="stage2-audit-ab">'
        '<h3>A/B 供电通道共活跃分析</h3>'
        f'<p>{escape(audit["ab_coactivity"]["method"])}。'
        '平台供配电图已确认同名A/B对应同一物理负载；共活跃表示两路同时向该负载'
        '供电，其功率和电流应相加，设备数量仍只计算一次。</p>'
        f'<p class="section-tip">拓扑契约状态：'
        f'<code>{escape(str(topology.get("status", "unresolved")))}</code>；'
        f'已解析 {topology.get("n_resolved", 0)} 组，'
        f'未确认 {topology.get("n_unresolved", 0)} 组，'
        f'任务通道求和门禁：'
        f'<code>{"blocked" if topology.get("blocks_task_aggregation") else "open"}</code>；'
        '本星22组均采用物理单机去重、支路求和。</p>'
        f'<div class="phase2-card"><div class="phase2-card-header">'
        '<h3>A/B 同时活跃分钟占比</h3><span>前12组 · %</span></div>'
        f'<figure>{ab_svg}<figcaption>红色表示两组同时达到活跃阈值的分钟占比'
        '不低于50%；在并联供电路径中，同时活跃是正常现象，'
        '随后按电流和功率求和。</figcaption></figure></div>'
        '<details class="aux-details"><summary>查看全部 A/B 共活跃统计</summary>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>供电通道</th><th>有效分钟</th><th>同时活跃</th>'
        '<th>A-only</th><th>B-only</th><th>同时活跃占比</th></tr></thead>'
        f'<tbody>{pair_rows}</tbody></table></div></details></div>'
        '<div class="panel" id="stage2-audit-solar-structure">'
        '<h3>BCR 物理结构适用性</h3>'
        f'<p>当前模型：<code>{escape(str(solar_structure.get("model_type", "—")))}</code>；'
        f'负载系数：<code>{solar_structure.get("load_coefficient")}</code>；'
        f'状态：<code>{escape(str(solar_structure.get("status", "unknown")))}</code>。</p>'
        f'<p>主留出模型负载系数：<code>{primary_solar_structure.get("load_coefficient")}</code>；'
        f'主留出模型状态：<code>{escape(str(primary_solar_structure.get("status", "unknown")))}</code>。</p>'
        f'<p class="section-tip">{escape(solar_structure.get("policy", ""))}'
        ' 尚缺特征：'
        f'<code>{escape(", ".join(solar_structure.get("missing_features", [])))}</code>。</p></div>'
        '<div class="panel" id="stage2-audit-bcr">'
        '<h3>BCR 异常值审计</h3>'
        '<div class="phase2-kpis">'
        f'<div class="phase2-kpi"><span class="phase2-kpi-label">超过缩尾上限</span>'
        f'<strong class="phase2-kpi-value">{bcr["above_cap_count"]}</strong>'
        f'<span class="phase2-kpi-foot">占有效分钟 '
        f'{bcr["above_cap_fraction"]:.3%}；上限 '
        f'{bcr["cap_W"]:.2f} W；主留出模型 '
        f'{bcr.get("primary_model_cap_W", bcr["cap_W"]):.2f} W</span></div>'
        f'<div class="phase2-kpi"><span class="phase2-kpi-label">异常值能量贡献</span>'
        f'<strong class="phase2-kpi-value">'
        f'{bcr["excess_energy_fraction"]:.2%}</strong>'
        '<span class="phase2-kpi-foot">按“原值−上限”求和后除以原值总能量代理</span></div>'
        f'<div class="phase2-kpi"><span class="phase2-kpi-label">原始最大值</span>'
        f'<strong class="phase2-kpi-value">{bcr["max_W"]:.1f} W</strong>'
        f'<span class="phase2-kpi-foot">p99 {bcr["p99_W"]:.2f} W，'
        f'p95 {bcr["p95_W"]:.2f} W</span></div></div>'
        f'<p class="section-tip">{escape(bcr["policy"])}。</p></div>'
    )


def phase2_sections(target=None, report_root=None):
    """读取第二阶段落盘摘要；target 指定只返回对应星别章节。"""
    project_root = Path(__file__).resolve().parent
    report_root = Path(report_root).resolve() if report_root else None
    if target not in (None, 'SCS', 'YJ'):
        raise ValueError(f'未知第二阶段目标: {target}')
    scs_model_dir = (report_root / 'power_model'
                     if target == 'SCS' and report_root else
                     project_root / 'satellites' / 'SCS-01-01' / 'power_model')
    yj_model_dir = (report_root / 'power_model'
                    if target == 'YJ' and report_root else
                    project_root / 'satellites' / 'YJ-3D' / 'power_model')
    scs_profile_root = scs_model_dir.parent
    yj_profile_root = yj_model_dir.parent
    scs_testset_dir = scs_profile_root / 'stage2_testset'
    yj_testset_dir = yj_profile_root / 'stage2_testset'
    scs_root = ('power_model' if target == 'SCS' and report_root else
                'satellites/SCS-01-01/power_model')
    yj_root = ('power_model' if target == 'YJ' and report_root else
               'satellites/YJ-3D/power_model')
    required = [
        scs_model_dir / 'unit_power_models.json',
        scs_model_dir / 'solar_power_model.json',
        scs_model_dir / 'batch_validation_summary.json',
        scs_model_dir / 'batch_validation_windows.csv',
        scs_testset_dir / 'evaluation_summary.json',
        scs_testset_dir / 'evaluation_cases.csv',
        scs_model_dir / 'extended_data_coverage.json',
        yj_model_dir / 'unit_power_models.json',
        yj_model_dir / 'physical_unit_models.json',
        yj_model_dir / 'ab_topology.json',
        yj_model_dir / 'solar_power_model.json',
        yj_model_dir / 'load_aware_comparison_summary.json',
        yj_model_dir / 'load_aware_comparison_results.csv',
        yj_model_dir / 'unit_task_validation_summary.json',
        yj_model_dir / 'battery_remaining_metrics_summary.json',
        yj_testset_dir / 'evaluation_summary.json',
        yj_testset_dir / 'evaluation_cases.csv',
    ]
    if not all(path.is_file() for path in required):
        return ''

    # 三体01星：冻结快照主指标与历史背景回放分开保存。
    scs_contract = phase2_json(scs_profile_root / 'config' / 'stage2_profile.json')
    scs_evaluation = phase2_json(scs_testset_dir / 'evaluation_summary.json')
    scs_evaluation_rows = phase2_csv(scs_testset_dir / 'evaluation_cases.csv')
    scs_extended_coverage = phase2_json(
        scs_model_dir / 'extended_data_coverage.json')
    scs_units = phase2_json(
        scs_profile_root / scs_contract['primary']['unit_model_path'])
    scs_solar = phase2_json(
        scs_profile_root / scs_contract['primary']['solar_model_path'])
    scs_batch = phase2_json(scs_model_dir / 'batch_validation_summary.json')
    scs_diagnostic_windows = sorted(
        phase2_csv(scs_model_dir / 'batch_validation_windows.csv'),
        key=lambda row: row['start'])
    scs_counts = scs_units.get('model_counts', {})
    scs_model_total = len(scs_units.get('models', {}))
    scs_high_models = {
        unit_id: model for unit_id, model in scs_units.get('models', {}).items()
        if model.get('high_threshold_W') is not None
    }
    scs_high_count = len(scs_high_models)
    scs_ep_a = scs_units.get('models', {}).get('electric_propulsion_a', {})
    scs_ep_b = scs_units.get('models', {}).get('electric_propulsion_b', {})
    scs_fiber = scs_units.get('models', {}).get('fiber_gyro', {})
    scs_uplink = scs_units.get('models', {}).get('uplink', {})
    scs_laser = scs_units.get('models', {}).get('laser_processor_1', {})
    scs_model_items = scs_units.get('models', {})
    scs_filtered_models = {
        unit_id: model.get('outlier_filter', {})
        for unit_id, model in scs_model_items.items()
        if model.get('outlier_filter', {}).get('upper_limit_W') is not None
    }
    scs_outlier_count = sum(
        int(item.get('outlier_samples', 0))
        for item in scs_filtered_models.values())
    scs_outlier_units = [
        f"{unit_id} {int(item.get('outlier_samples', 0))}点"
        for unit_id, item in scs_filtered_models.items()
        if int(item.get('outlier_samples', 0)) > 0
    ]
    scs_limit_values = [
        float(item['upper_limit_W'])
        for item in scs_filtered_models.values()
    ]
    scs_limit_range = (
        f"{min(scs_limit_values):g}–{max(scs_limit_values):g} W"
        if scs_limit_values else '—')
    scs_primary = scs_evaluation['subsets']['all']['metrics']
    scs_overall = {
        'soc_abs_error_mae_pp':
            scs_primary['soc_abs_error_pp']['window_equal_mean'],
        'soc_abs_error_p95_pp':
            scs_primary['soc_abs_error_pp']['window_p95'],
        'solar_energy_relative_error_median_pct':
            scs_primary['solar_energy_abs_relative_error_pct']['window_median'],
        'load_energy_relative_error_median_pct':
            scs_primary['load_energy_abs_relative_error_pct']['window_median'],
    }
    scs_diagnostic_overall = scs_batch['overall']
    scs_coverage = scs_batch['coverage']
    scs_primary_count = scs_evaluation['subsets']['all']['n_cases']
    scs_sun_count = scs_evaluation['subsets']['sun']['n_cases']
    scs_eclipse_count = scs_evaluation['subsets']['eclipse']['n_cases']
    scs_cv = scs_solar['cross_validation']
    scs_constant = scs_cv['constant_rmse_W']
    scs_selected = scs_cv['selected_rmse_W']
    scs_improvement = (1 - scs_selected / scs_constant) * 100

    scs_cv_items = [
        {'label': '恒定功率基线', 'detail': '训练集同一目标口径',
         'value': scs_constant, 'color': '#8b98aa'},
    ]
    for degree, number in scs_cv['candidate_rmse_W'].items():
        scs_cv_items.append({
            'label': f'连续阳照片段 · {degree} 阶',
            'detail': '片段分组确定性 5 折交叉验证',
            'value': number,
            'color': '#007e80' if abs(number - scs_selected) < 1e-9 else '#6e9ed7',
            'selected': abs(number - scs_selected) < 1e-9,
        })
    scs_cv_svg = phase2_horizontal_bars(
        '太阳阵代理功率模型选择', scs_cv_items, 'W',
        'scs-solar-cv', subtitle=(
            '13,065 个有效阳照分钟、3,008 个连续阳照片段；'
            '三阶模型相对恒定基线降低约 '
            f'{scs_improvement:.1f}% RMSE'),
        value_digits=2)

    scs_points = []
    for index, row in enumerate(scs_evaluation_rows, 1):
        observed = float(row['observed_target_soc_pct'])
        error = float(row['soc_error_pp'])
        scs_points.append({
            'x': observed,
            'y': observed + error,
            'group': row['season'],
            'label': (
                f"W{index:02d} {row['date']} · "
                f"{row['season']}/{row['duration_band']}/{row['load_band']}"),
        })
    scs_scatter_svg = phase2_soc_scatter(
        f'{scs_primary_count} 个留出窗口：预测与观测目标 SOC',
        scs_points, 'scs-soc-scatter')

    scs_by_season = defaultdict(list)
    for row in scs_evaluation_rows:
        scs_by_season[str(row['season'])].append(row)
    scs_season_rows = ""
    for season, rows in scs_by_season.items():
        soc_errors = [abs(float(row['soc_error_pp'])) for row in rows]
        solar_errors = [
            abs(float(row['solar_energy_relative_error_pct']))
            for row in rows
            if str(row.get('solar_qc_clean', 'True')).lower() == 'true']
        load_errors = [
            abs(float(row['load_energy_relative_error_pct']))
            for row in rows]
        soc_p95 = (
            soc_errors[0] if len(soc_errors) == 1 else
            statistics.quantiles(
                soc_errors, n=20, method='inclusive')[-1])
        scs_season_rows += (
            f"<tr><td>{escape(season)}</td><td>{len(rows)}</td>"
            f"<td>{statistics.fmean(soc_errors):.2f}</td>"
            f"<td>{soc_p95:.2f}</td>"
            + (f"<td>{statistics.median(solar_errors):.2f}%</td>"
               if solar_errors else "<td>不适用</td>")
            + f"<td>{statistics.median(load_errors):.2f}%</td></tr>")
    scs_window_rows = ''.join(
        f"<tr><td>{escape(row['date'])}</td>"
        f"<td>{escape(row['phase'])} · {escape(row['season'])} / "
        f"{escape(row['duration_band'])} / "
        f"{escape(row['load_band'])}</td>"
        f"<td>{row['task_count']}</td>"
        f"<td>{float(row['observed_target_soc_pct']) + float(row['soc_error_pp']):.2f}%</td>"
        f"<td>{float(row['observed_target_soc_pct']):.2f}%</td>"
        f"<td>{float(row['soc_error_pp']):+.2f}</td>"
        f"<td>{float(row['solar_energy_relative_error_pct']):+.2f}%</td></tr>"
        if str(row.get('solar_qc_clean', 'True')).lower() == 'true'
        else (
            f"<tr><td>{escape(row['date'])}</td>"
            f"<td>{escape(row['phase'])} · {escape(row['season'])} / "
            f"{escape(row['duration_band'])} / {escape(row['load_band'])}</td>"
            f"<td>{row['task_count']}</td>"
            f"<td>{float(row['observed_target_soc_pct']) + float(row['soc_error_pp']):.2f}%</td>"
            f"<td>{float(row['observed_target_soc_pct']):.2f}%</td>"
            f"<td>{float(row['soc_error_pp']):+.2f}</td><td>不适用</td></tr>")
        for row in scs_evaluation_rows)
    scs_diagnostic_window_rows = ''.join(
        f"<tr><td>{escape(row['start'][:16])}</td>"
        f"<td>{float(row['soc_error_pp']):+.2f}</td>"
        f"<td>{float(row['solar_energy_relative_error_pct']):+.2f}%</td>"
        f"<td>{float(row['load_energy_relative_error_pct']):+.2f}%</td></tr>"
        for row in scs_diagnostic_windows)
    scs_temperature_rows = ''.join(
        f"<tr><td>{escape(code)}</td><td>{escape(str(item.get('name', code)))}</td>"
        f"<td>{item['valid_minutes']:,}</td>"
        f"<td>{item['raw_min']:.2f}</td>"
        f"<td>{item['raw_median']:.2f}</td>"
        f"<td>{item['raw_max']:.2f}</td></tr>"
        for code, item in scs_extended_coverage[
            'temperature_codes'].items())
    scs_phase_rows = ''.join(
        f"<tr><td>{label}</td><td>{subset['n_cases']}</td>"
        f"<td>{_audit_value(subset['metrics']['soc_abs_error_pp']['window_equal_mean'], 3)}</td>"
        f"<td>{_audit_value(subset['metrics']['soc_abs_error_pp']['window_p95'], 3)}</td>"
        f"<td>{_audit_value(subset['metrics']['load_energy_abs_relative_error_pct']['window_equal_mean'], 3)}</td>"
        f"<td>{_audit_value(subset['metrics']['solar_energy_abs_relative_error_pct']['window_equal_mean'], 3)}</td></tr>"
        for label, subset in (
            ('全部', scs_evaluation['subsets']['all']),
            ('阳照', scs_evaluation['subsets']['sun']),
            ('地影', scs_evaluation['subsets']['eclipse']),
        ))

    scs_created = scs_units.get('created_from', {})
    scs_source = scs_solar.get('source', {})
    scs_unit_model_counts = (
        f"{scs_counts.get('constant', 0)} 个恒定 / "
        f"{scs_counts.get('mode_constant', 0)} 个状态分层 / "
        f"{scs_counts.get('polynomial', 0)} 个多项式")
    scs_ep_detail = (
        f"电推进 A/B 的稳态 active 分别为 "
        f"{scs_ep_a.get('active_power_W', 0):.3f} / "
        f"{scs_ep_b.get('active_power_W', 0):.3f} W，"
        f"共享 high 层约 {scs_ep_b.get('high_power_W', 0):.3f} W；"
        "A 路缺少本地 high 训练样本时使用 B 路同机先验"
        if scs_ep_a and scs_ep_b else '')
    scs_flow = phase2_flow([
        ('遥测快照与背景负载',
         'TMD016/018 计算当前总负载；任务单机功率从背景中拆出。'),
        ('单机功率模型',
         f'{scs_model_total} 个负载单机：{scs_unit_model_counts}；'
         f'{scs_high_count} 个通道含 high 层。'),
        ('相位与太阳阵',
         '未来相位由输入分段；地影 0 W，阳照使用历史三阶多项式代理。'),
        ('SOC 记账与回放',
         'V-SOC 初值 + 逐分钟充放电记账；16 个日期整组留出，分相评测。'),
    ])

    # 云尖3D星：物理单机负载、负载感知 BCR 与多片段条件验证。
    yj_contract = phase2_json(
        yj_profile_root / 'config' / 'stage2_profile.json')
    yj_units = phase2_json(
        yj_profile_root / yj_contract['primary']['unit_model_path'])
    yj_branch_units = phase2_json(yj_model_dir / 'unit_power_models.json')
    yj_topology = phase2_json(
        yj_profile_root / yj_contract['topology_contract'])
    yj_solar = phase2_json(yj_model_dir / 'solar_power_model.json')
    yj_load = phase2_json(yj_model_dir / 'load_aware_comparison_summary.json')
    yj_task = phase2_json(yj_model_dir / 'unit_task_validation_summary.json')
    yj_absolute = phase2_json(yj_model_dir / 'battery_remaining_metrics_summary.json')
    yj_rows_all = phase2_csv(yj_model_dir / 'load_aware_comparison_results.csv')
    yj_rows = sorted(
        [row for row in yj_rows_all if row['variant'] == 'load_task_aware_BCR'],
        key=lambda row: row['start'])
    yj_time_only = yj_load['by_variant']['time_only_BCR']
    yj_final = yj_load['by_variant']['load_task_aware_BCR']
    yj_evaluation = phase2_json(yj_testset_dir / 'evaluation_summary.json')
    yj_evaluation_rows = phase2_csv(yj_testset_dir / 'evaluation_cases.csv')
    yj_primary = yj_evaluation['subsets']['all']['metrics']
    yj_raw = yj_evaluation['subsets']['raw']['metrics']
    yj_clean_soc_mae = yj_primary['soc_abs_error_pp']['window_equal_mean']
    yj_clean_soc_p95 = yj_primary['soc_abs_error_pp']['window_p95']
    yj_clean_ah_mae = yj_primary[
        'remaining_Ah_abs_error']['window_equal_mean']
    yj_clean_solar_mae = yj_primary[
        'solar_energy_abs_relative_error_pct']['window_equal_mean']
    yj_clean_load_mae = yj_primary[
        'load_energy_abs_relative_error_pct']['window_equal_mean']
    yj_raw_soc_mae = yj_raw['soc_abs_error_pp']['window_equal_mean']
    yj_non_saturated_soc_mae = yj_evaluation['subsets']['non_saturated'][
        'metrics']['soc_abs_error_pp']['window_equal_mean']
    yj_units_count = len(yj_units.get('models', {}))
    yj_solar_cv = yj_solar['cross_validation']
    yj_constant_cv = yj_solar_cv['constant_rmse_W']
    yj_time_cv = yj_solar_cv['candidate_rmse_W']['1']
    yj_selected_cv = yj_solar_cv['selected_rmse_W']
    yj_feature = yj_solar['feature_model']
    intercept = yj_feature['intercept']
    time_coefficient, load_coefficient = yj_feature['coefficients']
    yj_task_total = sum(
        int(case.get('task_count', 0)) for case in yj_evaluation['cases'])
    yj_threshold_count = sum(
        model.get('active_threshold_W') is not None
        for model in yj_units.get('models', {}).values())
    yj_branch_count = len(yj_branch_units.get('models', {}))
    yj_ab_pair_count = len(yj_topology.get('pairs', []))

    yj_cv_items = [
        {'label': '恒定功率基线', 'value': yj_constant_cv,
         'detail': '连续阳照片段分组交叉验证', 'color': '#8b98aa'},
        {'label': '时间一阶', 'value': yj_time_cv,
         'detail': '仅使用阳照片段经历小时', 'color': '#6e9ed7'},
        {'label': '时间一阶 + 预测负载',
         'value': yj_selected_cv, 'detail': '最终采用；负载来自任务计划',
         'color': '#007e80', 'selected': True},
    ]
    yj_cv_svg = phase2_horizontal_bars(
        '云尖3D星 BCR 输出功率模型选择', yj_cv_items, 'W',
        'yj-solar-cv', subtitle=(
            '目标为 8 路 BCR 输出侧功率和；'
            f'最终模型 CV RMSE 较时间一阶降低 '
            f'{(1 - yj_selected_cv / yj_time_cv) * 100:.1f}%'),
        value_digits=2)

    yj_ab_svg = phase2_horizontal_bars(
        '同一 10 片段：太阳阵模型 A/B', [
            {'label': '时间一阶', 'value': yj_time_only['target_soc_MAE_pp'],
             'detail': f"目标 SOC p95 {yj_time_only['target_soc_p95_pp']:.2f} pp",
             'color': '#6e9ed7'},
            {'label': '时间一阶 + 未来预测负载',
             'value': yj_final['target_soc_MAE_pp'],
             'detail': f"目标 SOC p95 {yj_final['target_soc_p95_pp']:.2f} pp",
             'color': '#007e80', 'selected': True},
        ], 'pp', 'yj-target-soc-ab',
        subtitle='历史任务计划保持相同；仅比较太阳阵模型新增负载特征的影响',
        value_digits=3)

    yj_soc_error_items = []
    yj_solar_error_items = []
    yj_window_rows = []
    for index, row in enumerate(yj_evaluation_rows, 1):
        soc_error = (
            float(row['soc_error_pp'])
            if row.get('soc_qc_clean') == 'True' else None)
        solar_error = float(row['solar_energy_relative_error_pct'])
        case_id = row.get('case_id', f'W{index:02d}')
        detail = (
            f"{row['season']}/{row['duration_band']}/{row['load_band']} · "
            f"{row['date']} · {row['task_count']} 条任务")
        if soc_error is not None:
            yj_soc_error_items.append({
                'label': case_id,
                'detail': detail,
                'value': soc_error,
                'color': '#c02646' if abs(soc_error) >= 1.5 else '#2554ad',
            })
        yj_solar_error_items.append({
            'label': case_id,
            'detail': detail,
            'value': solar_error,
            'color': '#c02646' if abs(solar_error) >= 30 else (
                '#2554ad' if solar_error >= 0 else '#b66b18'),
        })
        observed = row.get('observed_target_soc_pct')
        predicted = (
            f"{float(observed) + float(row['soc_error_pp']):.2f}%"
            if soc_error is not None and observed not in (None, '') else 'N/A')
        observed_text = (
            f"{float(observed):.2f}%"
            if observed not in (None, '') else 'QC_FAIL')
        error_text = (
            f"{soc_error:+.2f}" if soc_error is not None else 'N/A')
        qc_text = (
            'PASS' if row.get('qc_clean') == 'True'
            else f"{row.get('soc_qc_status', 'fail')}/"
                 f"{row.get('solar_qc_status', 'fail')}/"
                 f"{row.get('load_qc_status', 'fail')}")
        yj_window_rows.append(
            f"<tr><td>{escape(row['date'])}</td>"
            f"<td>{escape(row['season'])} / {escape(row['duration_band'])} / "
            f"{escape(row['load_band'])}</td>"
            f"<td>{row['task_count']}</td>"
            f"<td>{escape(qc_text)}</td>"
            f"<td>{predicted}</td><td>{observed_text}</td>"
            f"<td>{error_text}</td>"
            f"<td>{solar_error:+.2f}%</td></tr>")
    yj_window_rows = ''.join(yj_window_rows)
    yj_soc_error_svg = phase2_diverging_bars(
        '10 个留出窗口：目标 SOC 有符号误差', yj_soc_error_items, 'pp',
        'yj-soc-errors',
        subtitle=(
            f"QC主指标 MAE {yj_clean_soc_mae:.3f} pp，"
            f"绝对误差 p95 {yj_clean_soc_p95:.3f} pp；"
            f"原始诊断 MAE {yj_raw_soc_mae:.3f} pp"),
        value_digits=2)
    yj_solar_error_svg = phase2_diverging_bars(
        '10 个留出窗口：太阳阵能量相对误差', yj_solar_error_items, '%',
        'yj-solar-errors',
        subtitle=(
            f"能量 MAE {yj_clean_solar_mae:.2f}%；"
            '红色尾部窗口仍暴露模型失配'),
        value_digits=2)

    yj_flow = phase2_flow([
        ('A/B物理单机',
         f'{yj_ab_pair_count} 对上下板支路先过滤后求和，设备只计一次。'),
        ('未来相位与任务',
         f'{yj_units_count} 个物理对象按历史功率层生成任务负载。'),
        ('BCR 输出功率',
         '8 路输出侧功率和为目标；模型把预测总负载作为输入。'),
        ('电池记账与回放',
         '专属 7.84 A V-SOC 条件映射；10 个片段整日从训练集排除。'),
    ])

    yj_absolute_soc = yj_absolute['absolute_metrics']
    yj_energy = yj_absolute['energy_metrics']
    yj_solar_source = yj_solar.get('source', {})
    yj_solar_training = yj_solar.get('training', {})

    scs_html = (
        # 三体01星
        '<section class="report-section phase2-section" id="phase2-scs-01-01">'
        '<div class="section-head"><h2>第二阶段｜三体01星任务能耗与剩余电量预测</h2>'
        '<p>由观测统计进入“未来任务窗口 + 相位分段 + SOC 记账”</p></div>'
        '<div class="phase2-hero"><h3>阶段目标与结论摘要</h3>'
        '<p>第二阶段把第一阶段的历史整星收支拆成背景负载、25 个单机功率模型、'
        '太阳阵代理功率与电池 SOC 记账。任务时间、期望工况和相位分段由规划输入；'
        '模型回答“在这些任务按计划执行的条件下，末时 SOC 和剩余容量大致是多少”。'
        '它不是对飞行程序执行结果的保证。</p>'
        '<div class="phase2-meta"><span>卫星 <b>SCS-01-01</b></span>'
        f'<span>模型版本 <b>{escape(str(scs_units.get("schema_version", "—")))}</b></span>'
        f'<span>校准至 <b>{escape(str(scs_created.get("time_end", "—")))}</b></span>'
        '<span>历史分组留出验证</span><span>条件 SOC / 任务规划级</span>'
        f'<span>high 层通道 <b>{scs_high_count}</b></span></div>'
        f'{scs_flow}</div>'
        '<div class="phase2-kpis">'
        f'<div class="phase2-kpi"><span class="phase2-kpi-label">单机功率模型</span>'
        f'<strong class="phase2-kpi-value">{scs_model_total}</strong>'
        f'<span class="phase2-kpi-foot">{escape(scs_unit_model_counts)}</span></div>'
        f'<div class="phase2-kpi"><span class="phase2-kpi-label">分组留出窗口</span>'
        f'<strong class="phase2-kpi-value">{scs_primary_count}</strong>'
        f'<span class="phase2-kpi-foot">{scs_sun_count} 个阳照 + '
        f'{scs_eclipse_count} 个地影；同一 16 个验证日期</span></div>'
        '<div class="phase2-kpi"><span class="phase2-kpi-label">目标 SOC 绝对误差</span>'
        f'<strong class="phase2-kpi-value">{scs_overall["soc_abs_error_mae_pp"]:.3f} pp</strong>'
        f'<span class="phase2-kpi-foot">MAE；p95 '
        f'{scs_overall["soc_abs_error_p95_pp"]:.3f} pp；观测值本身为 V-SOC 条件参考</span></div>'
        '<div class="phase2-kpi"><span class="phase2-kpi-label">太阳阵模型 CV RMSE</span>'
        f'<strong class="phase2-kpi-value">{scs_selected:.2f} W</strong>'
        f'<span class="phase2-kpi-foot">恒定基线 {scs_constant:.2f} W；'
        f'相对改善 {scs_improvement:.1f}%</span></div></div>'
        '<div class="panel method-panel"><h3>模型公式、输入与术语</h3>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>量</th><th>定义 / 计算</th><th>本报告如何解释</th></tr></thead>'
        '<tbody>'
        '<tr><td>预测总负载</td>'
        '<td><code>P_load(t) = P_background(t0) + Σ P_unit,i(t)</code></td>'
        '<td>背景负载取起始遥测快照；任务单机按任务计划切换功率层。未列入任务的负载变化不在模型内。</td></tr>'
        '<tr><td>太阳阵目标代理</td>'
        '<td><code>P_solar_target = P_load(TMD016×TMD018) + '
        'max(P_TMD017 − P_load, 0)</code></td>'
        '<td>方案B历史代理。P_TMD017 是母线测点折算功率；目标不是独立阵列功率测量。</td></tr>'
        '<tr><td>阳照功率预测</td>'
        '<td><code>P_solar_proxy = f_3(h_sun)</code>；阴影片段为 0 W</td>'
        '<td>h_sun 为连续阳照片段起点后的小时数；超出训练支撑后截断。温度、入射角和 S3R 工作点未显式建模。</td></tr>'
        '<tr><td>SOC 记账</td>'
        '<td><code>E_next = E + η_c·max(P_solar−P_load,0)·Δt − '
        'max(P_load−P_solar,0)/η_d·Δt</code></td>'
        '<td>当前参数 η_c=η_d=0.97、65 Ah、标称 28 V、SOC 下限 5%；'
        '这是能量簿记，不是独立电池容量或 SOH 验证。</td></tr>'
        '</tbody></table></div>'
        '<div class="phase2-terms">'
        '<div class="phase2-term"><strong>SOC / DoD</strong>'
        '<span>SOC 为荷电状态；DoD=100%−SOC。这里的 SOC 由固定 V-SOC 曲线和积分记账得到，'
        '受温度、倍率、滞回与老化影响。</span></div>'
        '<div class="phase2-term"><strong>MAE / p95</strong>'
        '<span>MAE 是各窗口绝对误差的平均；p95 是该批窗口绝对误差的 95 分位。'
        'pp 指百分点，不是相对百分比。</span></div>'
        '<div class="phase2-term"><strong>分组留出</strong>'
        '<span>验证窗口涉及日期从单机模型和太阳阵模型中整组排除；同一窗口内的分钟不跨训练/验证集泄漏。</span></div>'
        '<div class="phase2-term"><strong>active/high 与运行截断</strong>'
        f'<span>同一 active 状态内若存在中位功率至少为稳态层 14 倍的高功率瞬态，'
        '拆为 high 层；'
        f'当前 {scs_high_count} 个通道存在 high 层。{escape(scs_ep_detail)}，'
        '高功率任务必须显式使用 '
        '<code>power_level: "high"</code>。多项式验证和部署均先截断运行支撑，'
        '再按 active 层 p05–p95 截断，并要求支撑区间单调及首末误差通过 10% 边界门槛。</span></div>'
        '</div></div>'
        '<div class="panel method-panel" id="scs-unit-model-audit">'
        '<h3>单机模型整改与本轮适用性</h3>'
        '<p>本轮为全部负载通道增加功率筛选上限，并按实际供电轨修正支路折算。'
        '状态识别和模型拟合只使用剔除野值后的样本；单机模型仍按供电支路表达，'
        '不把A/B或主备冗余支路直接合并成物理单机总功耗。</p>'
        '<div class="table-scroll"><table class="data-table compact-table">'
        '<thead><tr><th>检查项</th><th>当前结果</th><th>解释</th></tr></thead><tbody>'
        f'<tr><td>筛选上限覆盖</td><td>{len(scs_filtered_models)}/{scs_model_total} 个负载通道，'
        f'范围 {escape(scs_limit_range)}</td>'
        f'<td>共剔除 {scs_outlier_count} 个超限点'
        f'{"：" + escape("、".join(scs_outlier_units)) if scs_outlier_units else ""}；'
        '上限是建模包络，不等同于铭牌功率。</td></tr>'
        f'<tr><td>供电轨修正</td><td>光纤陀螺 {scs_fiber.get("active_power_W", 0):.3f} W；'
        f'测传上注 {scs_uplink.get("active_power_W", 0):.3f} W</td>'
        '<td>前者使用 TMD059×TMD067，后者使用 TMD062×TMD056，'
        '不再把测点电压一律当作母线电压。</td></tr>'
        f'<tr><td>激光处理机1低层</td><td>{scs_laser.get("low_power_W", 0):.3f} W</td>'
        '<td>修复全局高尾候选失败时漏掉正常样本的问题；所有分档样本数现覆盖全部有效样本。</td></tr>'
        f'<tr><td>电推进高功率</td><td>{escape(scs_ep_detail)}</td>'
        '<td>验证任务中若出现 high 层，必须显式提供 '
        '<code>power_level: "high"</code>。</td></tr>'
        '</tbody></table></div>'
        '<p class="section-tip">逐通道结论、数据过滤和后续优先级见 '
        '<a href="../../19_三体01星单机模型适用性评估与优化.md">19号适用性评估</a>，'
        '机器可读结果见 <a href="power_model/stage2_audit.json">stage2_audit.json</a>。</p></div>'
        '<div class="phase2-card"><div class="phase2-card-header">'
        '<h3>太阳阵代理模型的选择依据</h3><span>越低越好 · W</span></div>'
        f'<figure>{scs_cv_svg}<figcaption>'
        '对比恒定基线与 1—3 阶时间多项式。最终选择三阶模型，是因为阳照片段分组 CV RMSE 最低；'
        '它仍只描述历史代理目标，不是 PV 阵列的物理发电模型。</figcaption></figure></div>'
        '<div class="phase2-card"><div class="phase2-card-header">'
        f'<h3>逐窗口目标 SOC：预测与观测</h3><span>{scs_primary_count} 个窗口 · 1:1 参考线</span></div>'
        f'<figure>{scs_scatter_svg}<figcaption>'
        '点越接近对角虚线，预测与观测越一致。颜色表示季节；观测目标是 TMD019 经固定 V-SOC 曲线映射的条件值。'
        '总体结果较好不等于每个季节、负载档或任务组合都已覆盖。</figcaption></figure></div>'
        '<div class="panel"><h3>验证覆盖与分季节误差</h3>'
        f'<p>{scs_primary_count} 个窗口覆盖 DJF/MAM/JJA，'
        f'其中地影 {scs_eclipse_count} 个；当前冻结验证日期仍没有 SON，'
        '新增 SON 数据已进入扩展训练覆盖，需下一轮独立留出后再评价；'
        f'分层矩阵仍有 {len(scs_coverage["missing_cells"])} 个空格。'
        '因此当前结果说明模型在已覆盖历史上可用，不能外推为全年无偏。</p>'
        '<div class="table-scroll"><table class="data-table compact-table">'
        '<thead><tr><th>相位</th><th>窗口</th><th>SOC MAE pp</th>'
        '<th>SOC p95 pp</th><th>负载能量 MAE %</th><th>太阳能量 MAE %</th>'
        '</tr></thead>'
        f'<tbody>{scs_phase_rows}</tbody></table></div>'
        '<div class="table-scroll"><table class="data-table compact-table">'
        '<thead><tr><th>季节</th><th>窗口</th><th>SOC MAE / pp</th>'
        '<th>SOC p95 / pp</th><th>太阳能量误差中位</th><th>负载能量误差中位</th>'
        f'</tr></thead><tbody>{scs_season_rows}</tbody></table></div>'
        '<p class="section-tip">太阳能量绝对相对误差中位为 '
        f'{scs_overall["solar_energy_relative_error_median_pct"]:.2f}%，'
        f'负载能量绝对相对误差中位为 '
        f'{scs_overall["load_energy_relative_error_median_pct"]:.2f}%。'
        '这些是留出窗口的代理目标误差，不是在独立硬件上测得的阵列和负载真值。</p></div>'
        '<div class="panel"><h3>历史逐分钟背景回放：上界/诊断值</h3>'
        '<p>该组计算把历史总负载减去历史任务单机功率后的逐分钟残差直接作为'
        '未来背景输入，因此等价于提前知道背景负载变化。它只用于检查闭环和给出'
        '上界，不进入上面的任务规划主指标。</p>'
        '<div class="table-scroll"><table class="data-table compact-table">'
        '<thead><tr><th>诊断指标</th><th>结果</th></tr></thead><tbody>'
        f'<tr><td>SOC MAE</td><td>{scs_diagnostic_overall["soc_abs_error_mae_pp"]:.3f} pp</td></tr>'
        f'<tr><td>SOC 绝对误差 p95</td><td>{scs_diagnostic_overall["soc_abs_error_p95_pp"]:.3f} pp</td></tr>'
        f'<tr><td>太阳能量误差中位</td><td>{scs_diagnostic_overall["solar_energy_relative_error_median_pct"]:+.2f}%</td></tr>'
        f'<tr><td>负载能量误差中位</td><td>{scs_diagnostic_overall["load_energy_relative_error_median_pct"]:+.2f}%</td></tr>'
        '</tbody></table></div>'
        '<details class="aux-details"><summary>查看19个历史背景回放窗口</summary>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>起始（北京时间）</th><th>SOC误差 pp</th>'
        '<th>太阳能量误差</th><th>负载能量误差</th></tr></thead><tbody>'
        f'{scs_diagnostic_window_rows}</tbody></table></div></details></div>'
        '<div class="panel"><h3>扩展取数覆盖：秋季、地影与温度遥测</h3>'
        f'<p>数据窗已扩展至 {escape(scs_extended_coverage["time_end_inclusive"][:16])}'
        f'（北京时间），共 {scs_extended_coverage["observed_minutes"]:,} 个观测分钟；'
        f'SON {scs_extended_coverage["season_counts"]["SON"]:,} 分钟，'
        f'地影 {scs_extended_coverage["phase_counts"].get("地影", 0):,} 分钟。'
        '按季节分层与地影样本已进入第一阶段审计和单机训练可用范围。</p>'
        '<div class="table-scroll"><table class="data-table compact-table">'
        '<thead><tr><th>温度遥测</th><th>名称</th><th>有效分钟</th>'
        '<th>原始最小</th><th>原始中位</th><th>原始最大</th></tr></thead>'
        f'<tbody>{scs_temperature_rows}</tbody></table></div>'
        '<p class="section-tip">温度只记录遥测原始数值；单位和比例换算、'
        '以及温度进入功率/SOC模型仍未完成，不能把原始中位数直接解释为摄氏度。</p></div>'
        f'<details class="aux-details"><summary>查看 {scs_primary_count} 个窗口的逐项结果</summary>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>起始（北京时间）</th><th>季节 / 时长 / 负载</th>'
        '<th>任务数</th><th>预测 SOC</th><th>观测 SOC</th><th>误差 pp</th>'
        '<th>太阳能量误差</th></tr></thead><tbody>'
        f'{scs_window_rows}</tbody></table></div></details>'
        '<div class="panel"><h3>数据来源与文件指纹</h3>'
        '<div class="phase2-source-grid">'
        '<div class="phase2-source"><b>历史遥测与第一阶段结果</b>'
        '<p>数据库 <code>chdb_3body.master_satellite_01</code>；按星上北京时间聚合。'
        f'第二阶段校准窗口为 {escape(str(scs_created.get("time_start", "—")))}'
        f' 至 {escape(str(scs_created.get("time_end", "—")))}，'
        '读取整星结果与单机分钟功率代理。</p>'
        f'<a href="{scs_root}/unit_power_model_report.md">单机模型报告</a>'
        f'<a href="{scs_root}/solar_power_model_report.md">太阳阵模型报告</a>'
        f'<a href="{scs_root}/README.md">输入/输出契约</a>'
        f'<a href="{scs_root}/example_prediction_output.json">示例预测输出</a></div>'
        '<div class="phase2-source"><b>验证与端到端回放</b>'
        '<p>冻结快照主指标、按日期聚类区间、留出日期和逐窗口误差均由机器可读文件保存；'
        '历史背景回放单独保留。</p>'
        f'<a href="stage2_testset/evaluation_summary.json">主评测摘要</a>'
        f'<a href="stage2_testset/evaluation_cases.csv">主评测逐窗数据</a>'
        f'<a href="{scs_root}/validation_split.json">16个验证日期</a>'
        f'<a href="{scs_root}/unit_power_holdout_models.json">留出单机模型</a>'
        f'<a href="{scs_root}/unit_power_holdout_report.md">留出模型报告</a>'
        f'<a href="{scs_root}/extended_data_coverage.json">扩展覆盖审计</a>'
        f'<a href="{scs_root}/batch_validation_summary.json">历史背景回放诊断</a></div>'
        '</div>'
        '<details class="aux-details"><summary>源文件 SHA-256</summary>'
        f'<p>单机分钟表 <code class="phase2-hash">'
        f'{escape(str(scs_created.get("component_csv_sha256", "—")))}</code></p>'
        f'<p>部件配置 <code class="phase2-hash">'
        f'{escape(str(scs_created.get("components_config_sha256", "—")))}</code></p>'
        f'<p>太阳阵训练目标 <code class="phase2-hash">'
        f'{escape(str(scs_source.get("sha256", "—")))}</code></p></details></div>'
        '<div class="phase2-evidence">'
        '<div class="supported"><h4>已经得到的结果</h4><ul>'
        f'<li>{scs_primary_count} 个冻结快照窗口支持历史任务条件下的闭环计算，'
        f'其中地影 {scs_eclipse_count} 个；'
        '任务总负载、太阳能量和 SOC 误差均可逐窗复核。</li>'
        f'<li>目标 SOC MAE 为 {scs_overall["soc_abs_error_mae_pp"]:.3f} pp，'
        f'p95 为 {scs_overall["soc_abs_error_p95_pp"]:.3f} pp。</li>'
        f'<li>三阶太阳阵代理的 CV RMSE 为 {scs_selected:.2f} W，'
        f'低于恒定模型的 {scs_constant:.2f} W。</li>'
        f'<li>{scs_high_count} 个通道拆出了独立 high 功率层；'
        f'{escape(scs_ep_detail)}。'
        '二阶候选因支撑区间非单调被拒绝，稳定性门槛得到实际执行。</li>'
        '</ul></div>'
        '<div class="limited"><h4>仍不能据此断言</h4><ul>'
        '<li>未来单机任务状态由规划输入，不由模型自动预测；当前验证使用历史提取任务，'
        '不证明任务能否按时执行。</li>'
        '<li>方案B太阳阵目标依赖 TMD017 测点差额，物理边界尚未独立闭合；'
        '不能把结果写成太阳阵实际总发电。</li>'
        '<li>SON 无样本且 25 路负载和未与总负载闭合，不能外推整年或给出飞行级 SOC/SOH。</li>'
        '<li>active/high 是基于现有功率分布的统计分层，不是硬件状态真值；'
        '高功率任务仍必须由规划输入显式指定。</li>'
        '</ul></div></div>'
        '<p class="phase2-warn"><strong>结论用法：</strong>'
        '本章结果适用于“任务计划已给定、模型假设成立”的规划与比较；'
        '若用于飞控决策，还需补齐任务状态确认、独立电池电流/容量、阵列与分流测点边界、'
        '温度与倍率模型，以及未覆盖季节的验证。</p>'
        + stage2_audit_html(scs_profile_root) +
        '</section>'
    )

    yj_html = (
        # 云尖3D星
        '<section class="report-section phase2-section" id="phase2-yj-3d">'
        '<div class="section-head"><h2>第二阶段｜云尖3D星任务负载与电池剩余量预测</h2>'
        '<p>以 8 路 BCR 输出为目标，联合单机任务、相位与专属 V-SOC</p></div>'
        '<div class="phase2-hero"><h3>阶段目标与结论摘要</h3>'
        '<p>云尖星第二阶段不沿用三体01星的太阳阵代理。它把 8 路 BCR 输出侧功率和作为'
        '太阳阵训练/验证目标，以 MPPT 后的公共功能节点描述功率，并让未来预测负载进入太阳阵模型。'
        '单机任务、太阳阵、电池记账三部分同日整组留出，重点检验误差是否在闭环中传播。</p>'
        '<div class="phase2-meta"><span>卫星 <b>YJ-3D</b></span>'
        '<span>控制机制 <b>MPPT / DC-DC</b></span>'
        f'<span>遥测训练至 <b>{escape(str(yj_solar_training.get("time_end", "—")))}</b></span>'
        '<span>JJA + SON 验证</span><span>条件 SOC / 任务规划级</span></div>'
        f'{yj_flow}</div>'
        '<div class="phase2-kpis">'
        '<div class="phase2-kpi"><span class="phase2-kpi-label">物理单机模型</span>'
        f'<strong class="phase2-kpi-value">{yj_units_count}</strong>'
        f'<span class="phase2-kpi-foot">22对A/B求和 + 10个加热主路；'
        f'{yj_threshold_count}个识别出active/idle阈值；'
        f'54个原始支路保留审计</span></div>'
        '<div class="phase2-kpi"><span class="phase2-kpi-label">分组留出回测</span>'
        '<strong class="phase2-kpi-value">10</strong>'
        f'<span class="phase2-kpi-foot">{yj_task_total} 条物理单机历史任务；'
        '覆盖 JJA/SON 与短/中/长光照</span></div>'
        '<div class="phase2-kpi"><span class="phase2-kpi-label">条件目标 SOC MAE</span>'
        f'<strong class="phase2-kpi-value">{yj_clean_soc_mae:.3f} pp</strong>'
        f'<span class="phase2-kpi-foot">QC主口径；原始诊断 {yj_raw_soc_mae:.3f} pp；'
        f'非饱和 {yj_non_saturated_soc_mae:.3f} pp</span></div>'
        '<div class="phase2-kpi"><span class="phase2-kpi-label">BCR 功率 CV RMSE</span>'
        f'<strong class="phase2-kpi-value">{yj_selected_cv:.2f} W</strong>'
        f'<span class="phase2-kpi-foot">时间一阶 {yj_time_cv:.2f} W；'
        f'降低 {(1-yj_selected_cv/yj_time_cv)*100:.1f}%</span></div></div>'
        '<div class="panel method-panel"><h3>模型公式、输入与术语</h3>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>量</th><th>定义 / 当前拟合</th><th>本报告如何解释</th></tr></thead>'
        '<tbody>'
        '<tr><td>太阳阵目标</td>'
        '<td><code>P_BCR_out = Σ(U_BCR_out,j × I_BCR_out,j), j=1…8</code></td>'
        '<td>8 路同源 BCR 输出侧功率和；位于 MPPT/DC-DC 之后，不是 PV 输入端实际功率。</td></tr>'
        '<tr><td>负载感知功率模型</td>'
        f'<td><code>P_BCR_out ≈ {intercept:.6f} '
        f'{time_coefficient:+.6f}·h {load_coefficient:+.6f}·P_load,pred</code></td>'
        '<td>h 为连续阳照片段经历小时；P_load,pred 由背景负载与任务计划得到。'
        '目标时刻之后的 BCR 遥测不作为输入。</td></tr>'
        '<tr><td>未来总负载</td>'
        '<td><code>P_load,pred(t) = P_background(t0) + Σ P_unit,i(mode,t)</code></td>'
        '<td>任务 run/after 模式驱动单机功率；未列入任务的热控和其他负载变化仍按背景快照保持。</td></tr>'
        '<tr><td>条件 SOC</td>'
        '<td><code>SOC = interp_VSOC(V_bat)</code>；'
        '<code>ΔSOC ≈ −∫I_bat dt / 37.69 Ah</code></td>'
        '<td>7.84 A 放电曲线，容量终点 37.69 Ah；只在相位、电流、电压条件匹配时可映射，'
        '不是全时段真实 SOC。</td></tr>'
        '</tbody></table></div>'
        '<div class="phase2-terms">'
        '<div class="phase2-term"><strong>BCR 输出功率</strong>'
        '<span>8 路 BCR 输出电压×电流之和。它是母线侧已变换功率，不是 PV 输入功率；'
        '因此当前模型不再乘母线效率。</span></div>'
        '<div class="phase2-term"><strong>条件 SOC</strong>'
        '<span>仅当放电方向、电流接近 7.84 A 且电压落在表内时，才由 V-SOC 曲线给出；'
        '曲线外不外推、不截断成 0/100%。</span></div>'
        '<div class="phase2-term"><strong>当前锚定误差</strong>'
        '<span>观测目标 SOC 从起始 V-SOC 锚点加独立 TMEA006 电流积分得到。'
        '它验证变化量跟踪，不等同于多窗口绝对 SOC 真值。</span></div>'
        '</div></div>'
        '<div class="phase2-card"><div class="phase2-card-header">'
        '<h3>从恒定、时间模型到负载感知模型</h3><span>越低越好 · W</span></div>'
        f'<figure>{yj_cv_svg}<figcaption>'
        '最终模型保留时间项并增加预测总负载。任务功率和活动单机数也测试过，'
        '但因闭环稳定性或分布偏移被排除。目标为 8 路 BCR 输出侧功率和。</figcaption></figure></div>'
        '<div class="phase2-card"><div class="phase2-card-header">'
        '<h3>加入未来预测负载后的目标 SOC A/B</h3>'
        '<span>10 个相同片段 · 历史任务不变</span></div>'
        f'<figure>{yj_ab_svg}<figcaption>'
        '在同一任务计划、同一留出窗口下，只替换太阳阵特征。'
        '本图为QC门禁前的原始A/B诊断，目标 SOC MAE 降低，剩余容量 MAE 也由 '
        f'{yj_time_only["remaining_Ah_MAE"]:.3f} 降至 '
        f'{yj_final["remaining_Ah_MAE"]:.3f} Ah。</figcaption></figure></div>'
        '<div class="phase2-card"><div class="phase2-card-header">'
        '<h3>逐窗口目标 SOC 误差</h3><span>有符号误差 · pp</span></div>'
        f'<figure>{yj_soc_error_svg}<figcaption>'
        '正值表示预测 SOC 偏高，负值表示偏低。图中只画通过SOC质检的窗口；'
        '原始积分和有效区间条件值仍保留在逐案例文件中。</figcaption></figure></div>'
        '<div class="phase2-card"><div class="phase2-card-header">'
        '<h3>太阳阵能量误差与尾部风险</h3><span>有符号相对误差 · %</span></div>'
        f'<figure>{yj_solar_error_svg}<figcaption>'
        '总体 MAE 会被少数尾部窗口主导。W08（2026-09-11 00:59 起始）太阳能量误差约 '
        f'{min(float(row["solar_energy_relative_error_pct"]) for row in yj_evaluation_rows):.2f}%'
        '（训练缩尾口径）；原始尖峰口径约 '
        f'{min(float(row["solar_energy_relative_error_raw_pct"]) for row in yj_evaluation_rows):.2f}%，'
        '说明当前还不能用单一总体指标掩盖工况失配。</figcaption></figure></div>'
        '<div class="panel"><h3>A/B双支路与物理单机建模</h3>'
        '<p>通用平台供配电图确认：PLA在上板、PLB在下板；'
        '两块板的DCDC输出分开，对应PL_Vn的配电开关输出并联到同一外部负载。'
        '因此同名A/B是同一物理单机的两个供电路径，而不是两台设备。</p>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>项目</th><th>当前值</th><th>模型处理</th>'
        '</tr></thead><tbody>'
        f'<tr><td>A/B物理对</td><td>{yj_ab_pair_count}</td>'
        f'<td>拓扑状态 <code>{escape(str(yj_topology.get("status", "unresolved")))}</code>；'
        '全部按<code>sum</code>合并</td></tr>'
        f'<tr><td>原始审计支路</td><td>{yj_branch_count}</td>'
        '<td>保留逐支路功率模型、状态与异常过滤记录</td></tr>'
        f'<tr><td>预测物理对象</td><td>{yj_units_count}</td>'
        '<td>22个PL物理单机 + 10个加热主路；设备数量只计一次</td></tr>'
        f'<tr><td>物理功率</td><td><code>P_plN=P_plN_A+P_plN_B</code></td>'
        '<td>两边都有效才生成分钟标签；缺支路不填0W</td></tr>'
        '</tbody></table></div>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>同一冻结测试集指标</th><th>支路级聚合前</th>'
        '<th>物理单机求和后</th></tr></thead><tbody>'
        f'<tr><td>负载能量 MAE</td><td>5.64%</td>'
        f'<td><strong>{yj_clean_load_mae:.2f}%</strong></td></tr>'
        f'<tr><td>太阳阵能量 MAE</td><td>13.28%</td>'
        f'<td><strong>{yj_clean_solar_mae:.2f}%</strong></td></tr>'
        f'<tr><td>目标 SOC MAE</td><td>0.151 pp</td>'
        f'<td><strong>{yj_clean_soc_mae:.3f} pp</strong></td></tr>'
        '</tbody></table></div>'
        '<p class="section-tip">A/B求和后，物理单机留出模型重新训练；'
        '实时ClickHouse快照也先展开A/B测点再聚合。'
        '平台图与第33/76页证据及冻结窗口对比见'
        f'<a href="{yj_root}/ab_physical_aggregation_report.md">A/B聚合报告</a>。</p></div>'
        '<div class="panel"><h3>绝对精度、相对变化与多片段能量分栏</h3>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>验收口径</th><th>窗口</th><th>当前结果</th><th>可作结论</th></tr></thead>'
        '<tbody>'
        f'<tr><td>绝对 V-SOC 端点</td><td>1</td>'
        f'<td>SOC {yj_absolute_soc["target_soc_error_pp"]:.2f} pp；'
        f'剩余 {yj_absolute_soc["remaining_Ah_error"]:.3f} Ah</td>'
        '<td>单个条件绝对点误差尚可；样本量不足，不能代表全部窗口。</td></tr>'
        f'<tr><td>当前锚定 SOC 变化</td><td>{yj_primary["soc_abs_error_pp"]["n_cases"]}</td>'
        f'<td>MAE {yj_clean_soc_mae:.3f} pp；'
        f'p95 {yj_clean_soc_p95:.3f} pp</td>'
        f'<td>QC主指标；原始诊断 {yj_raw_soc_mae:.3f} pp，不能替代独立硬件 SOC 真值。</td></tr>'
        '<tr><td>最终负载感知能量</td><td>10 / 7</td>'
        f'<td>太阳 {yj_clean_solar_mae:.2f}%；'
        f'负载 {yj_clean_load_mae:.2f}%</td>'
        '<td>总体能量精度仍需结合尾部窗口；太阳目标为 BCR 输出侧，负载仍有背景变化未建模。</td></tr>'
        '</tbody></table></div>'
        '<p class="section-tip">另一组零任务基线专项验收给出太阳能量 MAE '
        f'{yj_energy["solar_energy_MAE_pct"]:.2f}%、负载能量 MAE '
        f'{yj_energy["load_energy_MAE_pct"]:.2f}%。它与使用历史任务后的最终结果不是同一场景，'
        '不能混成单一“模型精度”。</p></div>'
        '<details class="aux-details"><summary>查看 10 个留出窗口的逐项结果</summary>'
        '<div class="table-scroll"><table class="data-table">'
        '<thead><tr><th>日期</th><th>季节 / 光照时长 / 负载</th>'
        '<th>任务数</th><th>SOC/BCR/负载 QC</th><th>预测 SOC</th>'
        '<th>锚定观测 SOC</th><th>SOC 误差 pp</th>'
        '<th>太阳能量误差</th></tr></thead><tbody>'
        f'{yj_window_rows}</tbody></table></div></details>'
        '<div class="panel"><h3>数据来源与文件指纹</h3>'
        '<div class="phase2-source-grid">'
        '<div class="phase2-source"><b>云尖专属遥测与电池曲线</b>'
        '<p>源表 <code>chdb_3D.yc_history</code>，UTC <code>timestamp</code> 转北京时间；'
        'A 路 EPS、BCR A/B 与专属 7.84 A 放电曲线分别读取。'
        f'太阳目标训练区间 {escape(str(yj_solar_training.get("time_start", "—")))}'
        f' 至 {escape(str(yj_solar_training.get("time_end", "—")))}，'
        f'有效样本 {yj_solar_training.get("n_valid_sun_samples", 0):,} 个。</p>'
        f'<a href="{yj_root}/README.md">预测契约与复现</a>'
        f'<a href="{yj_root}/solar_power_model_report.md">BCR 模型报告</a>'
        f'<a href="{yj_root}/physical_unit_model_report.md">32 物理单机模型报告</a>'
        f'<a href="{yj_root}/ab_physical_aggregation_report.md">A/B双支路聚合报告</a>'
        f'<a href="{yj_root}/unit_power_model_report.md">54 支路审计报告</a>'
        f'<a href="{yj_root}/prediction_output.schema.json">输出模式</a></div>'
        '<div class="phase2-source"><b>留出回测与证据链</b>'
        '<p>验证日期从单机与太阳阵训练集中整组排除；逐窗口结果、绝对 V-SOC '
        '与多片段能量分别保存，避免把不同口径混用。</p>'
        f'<a href="{yj_root}/load_aware_comparison_summary.json">负载感知 A/B 摘要</a>'
        f'<a href="{yj_root}/unit_task_validation_summary.json">单机任务 A/B</a>'
        f'<a href="{yj_root}/battery_remaining_metrics_summary.json">电池剩余量验收</a>'
        f'<a href="{yj_root}/segment_backtest_summary.json">多片段回测</a></div>'
        '</div>'
        '<details class="aux-details"><summary>源文件 SHA-256</summary>'
        f'<p>太阳阵训练集 <code class="phase2-hash">'
        f'{escape(str(yj_solar_source.get("sha256", "—")))}</code></p>'
        f'<p>模型代码 <code class="phase2-hash">'
        f'{escape(str(yj_solar.get("model_code", {}).get("sha256", "—")))}</code></p>'
        f'<p>V-SOC / 输出示例的哈希见 <a href="{yj_root}/README.md">模型 README</a>；'
        '源表时间列是 UTC，页面所有时间段统一显示北京时间。</p></details></div>'
        '<div class="phase2-evidence">'
        '<div class="supported"><h4>已经得到的结果</h4><ul>'
        f'<li>预测负载特征使太阳阵 CV RMSE 从 {yj_time_cv:.2f} W '
        f'降到 {yj_selected_cv:.2f} W。</li>'
        f'<li>QC主口径条件 SOC MAE 为 {yj_clean_soc_mae:.3f} pp；'
        f'原始未过滤诊断为 {yj_raw_soc_mae:.3f} pp。</li>'
        f'<li>A/B拓扑已批准：{yj_ab_pair_count}对同名上下板支路全部按求和合并，'
        f'原始{yj_branch_count}个支路模型保留审计，预测层为{yj_units_count}个物理对象。</li>'
        f'<li>最新冻结测试集负载能量 MAE 为 {yj_clean_load_mae:.2f}%，'
        f'太阳阵能量 MAE 为 {yj_clean_solar_mae:.2f}%；'
        f'QC主口径目标 SOC MAE 为 {yj_clean_soc_mae:.3f} pp。</li>'
        '</ul></div>'
        '<div class="limited"><h4>仍不能据此断言</h4><ul>'
        '<li>只有 1/10 窗口具有曲线内绝对 V-SOC 参考；多窗口主要验证相对变化，'
        '不能给出全时段绝对 SOC 或 SOH。</li>'
        f'<li>W08 按训练缩尾口径后的太阳能量误差约 '
        f'{min(float(row["solar_energy_relative_error_pct"]) for row in yj_evaluation_rows):.2f}%，'
        '说明背景负载、轨道/姿态或任务状态仍可能造成显著尾部误差。</li>'
        '<li>8 路 PV 输入电流未下行，BCR 输出侧模型不能替代 MPPT 输入功率模型；'
        'A/B同机拓扑已核准，但最终电缆网表、全部负载覆盖和电池参考仍未完全闭合。</li>'
        '</ul></div></div>'
        '<p class="phase2-warn"><strong>星间比较限制：</strong>'
        '三体01星与云尖星的太阳阵控制机制、测点、SOC 曲线、验证窗口和任务集合均不同，'
        '本页指标不能用于两星“谁更准”的排名。只可分别用于各自任务规划、'
        '误差追踪与后续模型改进；跨星推广前必须重新标定并验证。</p>'
        + stage2_audit_html(yj_profile_root) +
        '</section>'
    )
    if target == 'SCS':
        return scs_html
    if target == 'YJ':
        return yj_html
    return scs_html + yj_html


def phase2_scs_section(report_root):
    """只生成三体01星第二阶段章节，链接相对于该星报告。"""
    return phase2_sections(target='SCS', report_root=report_root)


def phase2_yj_section(report_root):
    """只生成云尖3D星第二阶段章节，链接相对于该星报告。"""
    return phase2_sections(target='YJ', report_root=report_root)


def line_chart(title, series_dict, xname='日期', ylabel='', ymax=None, ymin=None,
               gap_seconds=None, reference=None, day=None, scatter=False,
               window_start=None, window_end=None, dashed=False, highlight_time=None):
    """series_dict: 名称 -> (时间序列、数值序列、颜色)。可绘制纯散点或缺测断线。"""
    if not series_dict:
        return ''
    all_x, all_y = [], []
    normalized = {}
    for name, (xs, ys, color) in series_dict.items():
        xs = range(len(ys)) if xs is None else xs
        pairs = []
        for x, y in zip(xs, ys):
            try:
                t = stamp(x)
            except (ValueError, TypeError, OverflowError):
                continue
            v = value(y)
            pairs.append((t, v))
            all_x.append(t)
            if v is not None:
                all_y.append(v)
        normalized[name] = (pairs, color)
    if not all_x or not all_y:
        return ''
    if reference is not None:
        all_y.append(reference)
    lo, hi = min(all_y), max(all_y)
    if ymin is not None:
        lo = min(lo, ymin)
    if ymax is not None:
        hi = max(hi, ymax)
    pad = max((hi - lo) * .07, 0.1)
    lo, hi = lo - pad, hi + pad
    if window_start is not None and window_end is not None:
        t0, t1 = stamp(window_start), stamp(window_end)
        if t1 <= t0 or any(t < t0 or t > t1 for t in all_x):
            raise ValueError('曲线样本不在声明的观测时间段内')
    elif day is None:
        t0, t1 = min(all_x), max(all_x)
        if t0 == t1:
            t1 = t0 + 1
    else:
        midnight = datetime.fromisoformat(day)
        t0, t1 = midnight.timestamp(), (midnight + timedelta(days=1)).timestamp()
        if any(t < t0 or t >= t1 for t in all_x):
            raise ValueError(f'图表包含 {day} 之外的样本')
    X = lambda t: LEFT + (t - t0) / (t1 - t0) * PW
    Y = lambda v: TOP + (hi - v) / (hi - lo) * PH
    day_attr = f' data-day="{escape(day, quote=True)}"' if day else ''
    window_attr = (f' data-window-start="{escape(window_start, quote=True)}"'
                   f' data-window-end="{escape(window_end, quote=True)}"'
                   if window_start is not None and window_end is not None else '')
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" role="img"{day_attr}{window_attr} aria-label="{escape(title, quote=True)}">',
             f'<title>{escape(title)}</title>',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="{WIDTH/2}" y="21" text-anchor="middle" font-size="15" font-weight="600" fill="#1f2733">{escape(title)}</text>']
    for i in range(5):
        v = lo + i * (hi - lo) / 4
        y = Y(v)
        parts += [f'<line x1="{LEFT}" x2="{WIDTH-RIGHT}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e8eef5"/>',
                  f'<text x="{LEFT-9}" y="{y+4:.1f}" text-anchor="end" fill="#64748b" font-size="11">{fmt(v)}</text>']
    for i in range(5):
        t = t0 + i * (t1 - t0) / 4
        label = (f'{i * 6:02d}:00' if day and window_start is None else
                 datetime.fromtimestamp(t).strftime('%Y-%m-%d' if t1-t0 > 86400*3 else '%H:%M'))
        x = X(t)
        parts.append(f'<text x="{x:.1f}" y="{HEIGHT-17}" text-anchor="middle" fill="#64748b" font-size="10">{label}</text>')
    parts += [f'<text x="{WIDTH/2}" y="{HEIGHT-2}" text-anchor="middle" fill="#64748b" font-size="11">{escape(xname)}</text>',
              f'<text x="16" y="{TOP+PH/2}" text-anchor="middle" transform="rotate(-90 16 {TOP+PH/2})" fill="#64748b" font-size="11">{escape(ylabel)}</text>']
    if reference is not None:
        parts.append(f'<line x1="{LEFT}" x2="{WIDTH-RIGHT}" y1="{Y(reference):.1f}" y2="{Y(reference):.1f}" stroke="#dc4667" stroke-dasharray="6 5"/>')
    if highlight_time is not None:
        focus_t = stamp(highlight_time)
        if not t0 <= focus_t <= t1:
            raise ValueError('标注时间不在图表窗口内')
        focus_x = X(focus_t)
        parts.append(f'<line x1="{focus_x:.1f}" x2="{focus_x:.1f}" y1="{TOP}" y2="{TOP+PH}" stroke="#94a3b8" stroke-dasharray="3 4"/>')
        parts.append(f'<text x="{focus_x+4:.1f}" y="{TOP+11}" fill="#64748b" font-size="11">{escape(datetime.fromtimestamp(focus_t).strftime("%H:%M"))}</text>')
    legend_x = LEFT + 6
    for name, (pairs, color) in normalized.items():
        dash_attr = ' stroke-dasharray="7 5"' if dashed else ''
        parts.append(f'<line x1="{legend_x}" x2="{legend_x+15}" y1="36" y2="36" stroke="{color}" stroke-width="3"{dash_attr}/>')
        parts.append(f'<text x="{legend_x+20}" y="40" font-size="11" fill="#475569">{escape(name)}</text>')
        legend_x += max(90, len(name) * 13 + 30)
        if scatter:
            for t, v in pairs:
                if v is not None:
                    parts.append(f'<circle cx="{X(t):.1f}" cy="{Y(v):.1f}" r="2.8" fill="{color}" opacity="0.78"/>')
        else:
            commands = []
            previous_t = None
            for t, v in pairs:
                if v is None:
                    previous_t = None
                    continue
                move = previous_t is None or (gap_seconds is not None and t - previous_t > gap_seconds)
                commands.append(f'{"M" if move else "L"}{X(t):.1f},{Y(v):.1f}')
                previous_t = t
            if commands:
                parts.append(f'<path d="{" ".join(commands)}" fill="none" stroke="{color}" stroke-width="1.7" stroke-linejoin="round"{dash_attr}/>')
    parts.append('</svg>')
    return '\n'.join(parts)


def signed_battery_current(raw_tmh025, switch_state, discharge_state='通'):
    """按用户指定的放电开关规则赋正号；断开不推断充电。"""
    if raw_tmh025 is None or raw_tmh025 < 0 or switch_state != discharge_state:
        return None
    return abs(raw_tmh025)


def battery_power_nearby(result, satellite_id, minute, source_csv=BATTERY_WINDOW_CSV,
                         focus=BATTERY_FOCUS, expected_phase=None):
    """复算指定窗口功率；板端原值和只作诊断，负电压不作物理校正。"""
    source = BASE / source_csv
    manifest_path = source.with_suffix('.manifest.json')
    by_time = {row['t']: row for row in result}
    if satellite_id != 'SCS-01-01' or focus not in by_time or not source.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest['satellite_id'] != satellite_id or
            manifest['param_code'] != 'TMH025' or
            hashlib.sha256(source.read_bytes()).hexdigest() != manifest['data_sha256']):
        raise ValueError(f'{focus} 电池电流来源清单与数据不一致')
    current_rows = rows(source_csv)
    if len(current_rows) != manifest['row_count'] or len({r['t'] for r in current_rows}) != len(current_rows):
        raise ValueError(f'{focus} 电池电流分钟数或时间重复')
    current_rows.sort(key=lambda row: row['t'])
    wanted_times = {row['t'] for row in current_rows}
    panel_codes = {f'TMD{i:03d}' for i in (*range(3, 9), *range(10, 16))}
    panel_by_time = {}
    for row in minute:
        if row['t'] not in wanted_times or row['param_code'] not in panel_codes:
            continue
        values = panel_by_time.setdefault(row['t'], {})
        if row['param_code'] in values:
            raise ValueError(f'{focus} 帆板分钟测点重复: {row["t"]} {row["param_code"]}')
        values[row['param_code']] = row
    times, solar, load, battery_current_signed, battery, array_raw = [], [], [], [], [], []
    battery_current_raw, battery_current_raw_max = [], []
    focus_panel_values = []
    focus_invalid_panels = []
    focus_negative_voltages = []
    for item in current_rows:
        instant = item['t']
        if instant not in by_time or not (manifest['start_inclusive'] <= instant < manifest['end_exclusive']):
            raise ValueError(f'{focus} 电池电流与整星时间轴不一致: {instant}')
        row = by_time[instant]
        if expected_phase is not None and row['phase'] != expected_phase:
            raise ValueError(f'{focus} 窗口工况不一致: {instant} {row["phase"]}')
        v_bus, i_bus, i_load, v_bat = (value(row[key]) for key in
                                         ('V_bus', 'I_bus', 'I_load', 'V_bat'))
        i_bat, maximum, flagged = (value(item[key]) for key in
                                   ('avg_A_claimed', 'mx', 'flagged_n'))
        p_solar = v_bus * i_bus if v_bus is not None and i_bus is not None else None
        p_load = v_bus * i_load if v_bus is not None and i_load is not None else None
        i_bat_signed = (signed_battery_current(i_bat, row.get('battery_switch_state'))
                        if maximum is not None and maximum <= 100 and flagged == 0
                        else None)
        p_battery = v_bat * i_bat_signed if v_bat is not None and i_bat_signed is not None else None
        panel_rows = panel_by_time.get(instant, {})
        panel_values = [(value(panel_rows.get(f'TMD{i:03d}', {}).get('avg')),
                         value(panel_rows.get(f'TMD{i+7:03d}', {}).get('avg')))
                        for i in range(3, 9)]
        counts = [value(panel_rows.get(code, {}).get('count')) for code in panel_codes]
        p_array_raw = (sum(v * i for v, i in panel_values)
                       if len(panel_rows) == 12 and all(v is not None and i is not None
                                                        for v, i in panel_values)
                       and all(c is not None and c > 0 for c in counts)
                       and len(set(counts)) == 1 else None)
        if instant == focus:
            focus_panel_values = panel_values
            focus_invalid_panels = [chr(65 + index) for index, (v, i) in
                                    enumerate(panel_values)
                                    if v is not None and i is not None and (v < 0 or i < 0)]
            focus_negative_voltages = [f'{chr(65 + index)}={v:.2f} V'
                                       for index, (v, _) in enumerate(panel_values)
                                       if v is not None and v < 0]
        times.append(instant)
        solar.append(p_solar)
        load.append(p_load)
        battery_current_signed.append(i_bat_signed)
        battery_current_raw.append(i_bat)
        battery_current_raw_max.append(maximum)
        battery.append(p_battery)
        array_raw.append(p_array_raw)
    if (not times or focus not in times or
            battery[times.index(focus)] is None or
            array_raw[times.index(focus)] is None):
        raise ValueError(f'{focus} 附近缺少电池端或六组帆板原值功率试算')
    # 六板端原值功率和（诊断）于 2026-11-02 判为不可用，已从图表移除；
    # 光照组太阳阵功率按用户 2026-11-03 校正：017 测点功率 + 电池功率
    # （不再用「母线功率＋充电功率」派生式，改用电池端有符号试算值直接叠加）。
    if expected_phase != '地影':
        solar_array_calc = []
        for s, b in zip(solar, battery):
            if s is None or b is None:
                solar_array_calc.append(None)
            else:
                solar_array_calc.append(s + b)
        solar_array_focus = solar_array_calc[times.index(focus)]
        power_series = {
            'TMD017 测点功率': (times, solar, PALETTE[0]),
            '母线负载': (times, load, PALETTE[1]),
            '电池端有符号功率（试算）': (times, battery, PALETTE[2]),
            '光照太阳阵功率（017测点+电池）': (times, solar_array_calc, PALETTE[3])}
    else:
        solar_array_calc = None
        solar_array_focus = None
        power_series = {
            'TMD017 测点功率': (times, solar, PALETTE[0]),
            '母线负载': (times, load, PALETTE[1]),
            '电池端有符号功率（试算）': (times, battery, PALETTE[2])}
    svg = line_chart(
        f'{focus[:16]} 附近功率测点对照',
        power_series,
        xname='星上北京时间 · 观测分钟', ylabel='W', ymin=0,
        gap_seconds=90, window_start=times[0], window_end=times[-1],
        dashed=True, highlight_time=focus)
    current_svg = line_chart(
        f'{focus[:16]} 附近 TMH025_有符号 电流',
        {'TMH025_有符号（开关判向）': (times, battery_current_signed, PALETTE[2])},
        xname='星上北京时间 · 观测分钟', ylabel='A', reference=0,
        gap_seconds=90, window_start=times[0], window_end=times[-1],
        dashed=True, highlight_time=focus)
    raw_current_svg = line_chart(
        f'{focus[:16]} 附近 TMH025 数据库原值 · 未筛选',
        {'分钟均值原值': (times, battery_current_raw, PALETTE[0]),
         '分钟最大原值': (times, battery_current_raw_max, PALETTE[3])},
        xname='星上北京时间 · 观测分钟', ylabel='原值',
        gap_seconds=90, window_start=times[0], window_end=times[-1],
        dashed=True, highlight_time=focus)
    power_marker = ('data-eclipse-power-window' if expected_phase == '地影'
                    else 'data-battery-power-window')
    current_marker = ('data-eclipse-current-window' if expected_phase == '地影'
                      else 'data-battery-current-window')
    return {'svg': svg.replace('<svg ', f'<svg {power_marker}="true" ', 1),
            'current_svg': current_svg.replace('<svg ', f'<svg {current_marker}="true" ', 1),
            'raw_current_svg': raw_current_svg.replace(
                '<svg ', '<svg data-tmh025-raw-nearby="true" ', 1),
            'start': times[0], 'end': times[-1], 'n': len(times),
            'sample_count': manifest['raw_sample_count'],
            'solar_focus': solar[times.index(focus)],
            'load_focus': load[times.index(focus)],
            'switch_state_focus': by_time[focus].get('battery_switch_state', ''),
            'direction_current_focus': (value(by_time[focus]['I_load'])
                                        - value(by_time[focus]['I_bus'])),
            'battery_current_focus': battery_current_signed[times.index(focus)],
            'battery_focus': battery[times.index(focus)],
            'array_raw_focus': array_raw[times.index(focus)],
            'solar_array_focus': solar_array_focus,
            'array_panel_focus': focus_panel_values,
            'array_raw_minutes': sum(v is not None for v in array_raw),
            'tmd017_near_zero_minutes': sum(
                value(by_time[t]['I_bus']) is not None and
                abs(value(by_time[t]['I_bus'])) < 1 for t in times),
            'array_invalid_panels_focus': focus_invalid_panels,
            'array_negative_voltages_focus': focus_negative_voltages}


def satellite_02_power_nearby(result, minute, phase, start, end, focus):
    """02 星四项对照：未筛选原值的电压乘积单独作条件诊断。"""
    by_time = {row['t']: row for row in result if start <= row['t'] <= end}
    times = sorted(by_time)
    expected = [datetime.fromisoformat(start) + timedelta(minutes=i)
                for i in range(int((datetime.fromisoformat(end) -
                                    datetime.fromisoformat(start)).total_seconds() // 60) + 1)]
    if ([datetime.fromisoformat(t) for t in times] != expected or focus not in by_time or
            any(row['phase'] != phase for row in by_time.values())):
        raise ValueError(f'02 星 {phase} 附近功率窗口缺少连续同工况分钟')
    panel_codes = {f'TMD{i:03d}' for i in (*range(3, 9), *range(10, 16))}
    selected = {}
    for row in minute:
        if row['t'] in by_time and row['param_code'] in panel_codes | {'TMH025'}:
            bucket = selected.setdefault(row['t'], {})
            if row['param_code'] in bucket:
                raise ValueError(f'02 星附近遥测重复: {row["t"]} {row["param_code"]}')
            bucket[row['param_code']] = row
    solar, load, panel, raw_current, battery_raw_product = [], [], [], [], []
    raw_min, raw_max = [], []
    for t in times:
        row, sensors = by_time[t], selected.get(t, {})
        if value(row.get('P_battery_terminal_switch_W')) is not None:
            raise ValueError('02 星电池功率已有有效值，须重新核对未校准标注')
        solar.append(value(row.get('P_solar')))
        load.append(value(row.get('P_load')))
        raw_current.append(value(sensors.get('TMH025', {}).get('avg')))
        raw_min.append(value(sensors.get('TMH025', {}).get('mn')))
        raw_max.append(value(sensors.get('TMH025', {}).get('mx')))
        voltage = value(row.get('V_bat'))
        battery_raw_product.append(
            voltage * raw_current[-1]
            if voltage is not None and raw_current[-1] is not None else None)
        counts = [value(sensors.get(code, {}).get('count')) for code in panel_codes]
        pairs = [(value(sensors.get(f'TMD{i:03d}', {}).get('avg')),
                  value(sensors.get(f'TMD{i+7:03d}', {}).get('avg')))
                 for i in range(3, 9)]
        panel.append(sum(voltage * current for voltage, current in pairs)
                     if len(sensors.keys() & panel_codes) == 12 and
                     all(voltage is not None and current is not None
                         for voltage, current in pairs) and
                     all(count is not None and count > 0 for count in counts) and
                     len(set(counts)) == 1 else None)
    if (any(number is None for number in solar + load) or
            panel[times.index(focus)] is None or
            battery_raw_product[times.index(focus)] is None):
        raise ValueError(f'02 星 {phase} 焦点缺少对照遥测')
    power_svg = line_chart(
        f'02 星 {phase}附近三项测点及板端诊断功率',
        {'TMD017测点': (times, solar, PALETTE[0]),
         '负载测点': (times, load, PALETTE[1]),
         '六板端诊断': (times, panel, PALETTE[3])},
        xname='星上北京时间 · 观测分钟', ylabel='W', ymin=0,
        gap_seconds=90, window_start=start, window_end=end,
        dashed=True, highlight_time=focus)
    battery_svg = line_chart(
        f'02 星 {phase}附近第四项 · 电池电压×TMH025 未筛选原值',
        {'电压×原值（条件试算）': (times, battery_raw_product, PALETTE[2])},
        xname='星上北京时间 · 观测分钟', ylabel='V×原值',
        gap_seconds=90, window_start=start, window_end=end,
        dashed=True, highlight_time=focus)
    raw_svg = line_chart(
        f'02 星 {phase}附近 TMH025 数据库原值',
        {'分钟最小原值': (times, raw_min, PALETTE[0]),
         '分钟均值原值': (times, raw_current, PALETTE[2]),
         '分钟最大原值': (times, raw_max, PALETTE[3])},
        xname='星上北京时间 · 观测分钟', ylabel='原值',
        gap_seconds=90, window_start=start, window_end=end,
        dashed=True, highlight_time=focus)
    marker = 'sun' if phase == '日照' else 'eclipse'
    index = times.index(focus)
    return {'phase': phase, 'start': start, 'end': end, 'focus': focus,
            'n': len(times), 'n_panel': sum(x is not None for x in panel),
            'n_raw_current': sum(x is not None for x in raw_current),
            'n_battery_raw_product': sum(x is not None for x in battery_raw_product),
            'solar_focus': solar[index], 'load_focus': load[index],
            'panel_focus': panel[index], 'raw_current_focus': raw_current[index],
            'battery_raw_product_focus': battery_raw_product[index],
            'switch_focus': by_time[focus].get('battery_switch_state', ''),
            'svg': power_svg.replace('<svg ', f'<svg data-satellite02-{marker}-power="true" ', 1),
            'battery_svg': battery_svg.replace(
                '<svg ', f'<svg data-satellite02-{marker}-battery-raw-product="true" ', 1),
            'raw_svg': raw_svg.replace('<svg ', f'<svg data-satellite02-{marker}-tmh025="true" ', 1)}


def write_chart(name, title, times, data, unit, gap, *, low=None, high=None, reference=None,
                day=None, window_start=None, window_end=None):
    series = {label: (times, values, PALETTE[i % len(PALETTE)])
              for i, (label, values) in enumerate(data.items())}
    svg = line_chart(title, series, '观测时间 · 圆点为有数据的采样/分组', unit,
                     ymin=low, ymax=high, gap_seconds=gap, reference=reference, day=day,
                     scatter=True, window_start=window_start, window_end=window_end)
    if not svg:
        raise ValueError(f'缺少绘图数据：{name}')
    path = OUT / f'{name}.svg'
    path.write_text(svg, encoding='utf-8')
    print(f'{name}.svg  {len(times)} rows')


def write_unavailable_chart(name, title, message, start, end):
    """保留空窗口的明确缺测状态，不把缺失的功率画成零。"""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
           f'role="img" data-window-start="{escape(start, quote=True)}" '
           f'data-window-end="{escape(end, quote=True)}" '
           f'aria-label="{escape(title, quote=True)}">'
           f'<title>{escape(title)}</title>'
           '<rect width="100%" height="100%" fill="white"/>'
           f'<text x="{WIDTH / 2}" y="{HEIGHT / 2}" text-anchor="middle" '
           f'fill="#64748b" font-size="16">{escape(message)}</text></svg>')
    (OUT / f'{name}.svg').write_text(svg, encoding='utf-8')
    print(f'{name}.svg  0 valid rows')


def derive_observed_energy(result):
    """仅把有观测的分钟样本计作 1/60 小时，不对未观测时段外推。"""
    totals = defaultdict(lambda: defaultdict(float))
    for row in result:
        day = row['t'][:10]
        for key in ('P_solar', 'P_load', 'P_excess', 'P_shunt_diag'):
            v = value(row[key])
            if v is not None:
                totals[day][key] += v / 60000.0
    return totals


def select_representative_day(result):
    """选取同时覆盖日照和地影、有效分钟样本最多的一天；并列取最早日。"""
    counts = defaultdict(int)
    phases = defaultdict(set)
    for row in result:
        day = row['t'][:10]
        counts[day] += 1
        phases[day].add(row['phase'])
    eligible = [day for day in counts if {'日照', '地影'} <= phases[day]]
    if not eligible:
        raise ValueError('找不到同时有日照与地影判定样本的示例日')
    return min(eligible, key=lambda day: (-counts[day], day))


def select_observation_window(result, max_gap_seconds=180):
    """优先选择兼有日照/地影的连续片段，否则选最长的可用片段。"""
    chunks = []
    current = []
    previous = None
    for row in sorted(result, key=lambda item: item['t']):
        instant = datetime.fromisoformat(row['t'])
        if previous is not None and (instant - previous).total_seconds() > max_gap_seconds:
            chunks.append(current)
            current = []
        current.append(row)
        previous = instant
    if current:
        chunks.append(current)
    eligible = [chunk for chunk in chunks if {'日照', '地影'} <= {r['phase'] for r in chunk}]
    if not eligible:
        eligible = chunks
    if not eligible:
        raise ValueError('没有可绘制的观测片段')
    return min(eligible, key=lambda chunk: (-len(chunk), chunk[0]['t']))


def hour_buckets(result):
    """所选观测窗口每小时仅汇总已有分钟；跨午夜仍保留全部小时。"""
    buckets = defaultdict(list)
    for row in result:
        buckets[row['t'][:13]].append(row)
    start = datetime.fromisoformat(result[0]['t']).replace(minute=0, second=0)
    end = datetime.fromisoformat(result[-1]['t']).replace(minute=0, second=0)
    hours = []
    cursor = start
    while cursor <= end:
        hours.append(cursor.strftime('%Y-%m-%d %H:00:00'))
        cursor += timedelta(hours=1)
    keys = ('P_solar', 'P_load', 'P_excess', 'P_shunt_diag', 'SOC')
    avg = {key: [] for key in keys}
    energy = {key: [] for key in ('P_solar', 'P_load', 'P_shunt_diag')}
    for hour in hours:
        sample = buckets.get(hour[:13], [])
        for key in keys:
            observed = [v for row in sample if (v := value(row.get(key))) is not None]
            avg[key].append(sum(observed) / len(observed) if observed else None)
            if key in energy:
                # 未观测小时保持 None（折线断线），不填 0 误导读者
                energy[key].append(sum(observed) / 60000.0 if observed else None)
    return hours, avg, energy


def component_power_curves(window, component_summary):
    """在整星观测窗口内逐单机绘图，缺测保持断线。"""
    loads = [(name, item) for name, item in component_summary['components'].items()
             if item['role'] == 'load' and not item.get('context_only', False)]
    if not loads:
        return []
    start, end = window[0]['t'], window[-1]['t']
    times = [row['t'] for row in window]
    expected = {f'P_{name}_W' for name, _ in loads}
    by_time = {}
    with (BASE / 'out' / 'energy_v2_components.csv').open(
            encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        missing = expected - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f'单机功率明细缺少列: {sorted(missing)}')
        for row in reader:
            instant = row['t']
            if start <= instant <= end:
                if instant in by_time:
                    raise ValueError(f'单机功率明细时间重复: {instant}')
                by_time[instant] = row
    if set(times) - set(by_time):
        raise ValueError('单机功率明细与整星观测窗口不一致')
    series = {name: [value(by_time[t][f'P_{name}_W']) for t in times]
              for name, _ in loads}
    valid_counts = {name: sum(number is not None for number in values)
                    for name, values in series.items()}
    observed_names = [name for name, _ in loads if valid_counts[name] > 0]
    initial = ('router_main' if 'router_main' in observed_names else
               observed_names[0] if observed_names else loads[0][0])
    fragments = ['<div class="chart-picker"><label for="component-select">选择单机</label>',
                 '<select id="component-select" aria-controls="component-display">']
    for name, item in loads:
        source = ', '.join(item['source_codes'])
        fragments.append(f'<option data-option-group="component" value="{escape(name, quote=True)}"'
                         f'{" selected" if name == initial else ""}>'
                         f'{escape(item["name"])} · {escape(source)}</option>')
    fragments.extend([f'</select><span class="picker-count">共 {len(loads)} 路可选，'
                      f'{len(observed_names)} 路在当前窗口有样本</span></div>',
                      '<div id="component-display" class="chart-stack">'])
    for name, item in loads:
        values = series[name]
        valid = valid_counts[name]
        title = f"{item['name']} · 逐分钟耗电功率代理"
        svg = line_chart(title, {item['name']: (times, values, PALETTE[0])},
                         xname='星上北京时间 · 观测分钟', ylabel='W', ymin=0,
                         gap_seconds=180, scatter=valid < 500,
                         window_start=start, window_end=end)
        source = ', '.join(item['source_codes'])
        label = (f"{escape(item['name'])} · 有效 {valid}/{len(times)} 分钟 · "
                 f"{escape(source)}")
        fragments.append(f'<article class="component-panel" data-component-panel="'
                         f'{escape(name, quote=True)}"{" hidden" if name != initial else ""}>'
                         f'<div class="selected-chart-head"><h4>{escape(item["name"])}</h4>'
                         f'<span>{valid}/{len(times)} 有效分钟 · {escape(source)}</span></div>')
        if svg:
            svg = svg.replace(
                '<svg ',
                f'<svg data-component-id="{escape(name, quote=True)}" '
                f'data-valid-minutes="{valid}" data-total-minutes="{len(times)}" ', 1)
            fragments.append(f'<figure>{svg}<figcaption>{label}；'
                             '稀疏测点仅标出观测值，其余曲线在缺测和时间断档处断线。'
                             '</figcaption></figure>')
        else:
            fragments.append('<p>该观测窗口无有效功率样本。</p>')
        fragments.append('</article>')
    fragments.append('</div>')
    return fragments


def embed_charts_in_html(minute, all_minute=None):
    """由当前机器可读摘要生成现行 HTML，保留旧 v2.2 文档作为历史记录。

    all_minute 为未裁剪的分钟表；设计功耗对照用全部已观测分钟统计，
    不受遥测图表所用单段连续窗口限制。
    """
    summary = json.loads((BASE / 'out' / 'energy_v2_summary.json').read_text(
        encoding='utf-8'))
    manifest = json.loads((BASE / 'config' / 'charts.json').read_text(
        encoding='utf-8'))['charts']
    metrics, health, phase = summary['metrics'], summary['health'], summary['phase']
    interval = summary.get('interval_energy')
    components = summary.get('components')
    result = rows('out/energy_v2_result.csv')
    tmh_overview = tmh025_raw_overview()
    window = select_observation_window(result)
    battery_window = battery_power_nearby(result, summary['satellite_id'], minute)
    eclipse_window = battery_power_nearby(
        result, summary['satellite_id'], minute, ECLIPSE_WINDOW_CSV,
        ECLIPSE_FOCUS, expected_phase='地影')
    satellite_02_windows = []
    if summary['satellite_id'] == 'SCS-01-02':
        if (summary['provenance'].get('source') != 'chdb_3body.master_satellite_02' or
                metrics.get('n_battery_switch_current_valid') != 0):
            raise ValueError('02 星四项功率展示要求宽表来源及未校准电池电流状态')
        satellite_02_windows = [
            satellite_02_power_nearby(result, minute, phase, begin, stop, focus)
            for phase, begin, stop, focus in SATELLITE_02_POWER_WINDOWS]
    start, end = window[0]['t'], window[-1]['t']
    # 设计功耗对照：统计用全部观测分钟；单机曲线各自取有数据的峰值窗口。
    # 双向电流（磁力矩器）需要 mean(|I|) 序列，由 s6_fetch_abs_current.py 生成。
    abs_codes = design_compare.abs_current_codes()
    abs_series = None
    if abs_codes:
        abs_path = BASE / design_compare.ABS_CURRENT_FILE
        if abs_path.exists():
            abs_series = design_compare.load_abs_series(abs_path, abs_codes)
        else:
            print(f'[gen_charts] 缺少 {abs_path.name}：{len(abs_codes)} 个双向电流测点'
                  '将回退到有符号均值（功率接近 0），请运行 s6_fetch_abs_current.py',
                  file=sys.stderr)
    try:
        design_summary = design_compare.summarize(
            all_minute or minute, abs_series=abs_series)
    except (OSError, ValueError, KeyError) as error:
        print(f'[gen_charts] 设计功耗对照跳过：{error}', file=sys.stderr)
        design_summary = None
    design_ratio_svg = design_compare.ratio_chart(design_summary) if design_summary else ''
    design_power_svg = design_compare.power_chart(design_summary) if design_summary else ''
    # 分钟表缺逐单机支路电流时（如仅核心电源测点的旧快照）只有个别行可算，
    # 整节会退化为「无电流遥测」表格，故低于 5 行可对照就不出图。
    if design_summary and design_summary['n_measured'] < 5:
        print(f"[gen_charts] 设计功耗对照跳过：仅 {design_summary['n_measured']} 行可对照",
              file=sys.stderr)
        design_summary = None
    # 第二阶段只进入对应卫星的独立报告，不嵌入项目根目录总报告。
    phase2_html = (
        phase2_scs_section(BASE)
        if summary['satellite_id'] == 'SCS-01-01'
        and (BASE / 'power_model').is_dir() else '')
    phase2_nav = (
        '<a href="#phase2-scs-01-01">第二阶段</a>' if phase2_html else '')
    fmt_num = lambda value, digits=2: '缺测' if value is None else f'{value:.{digits}f}'
    score = ('无法评分' if health['hs'] is None else
             f"{health['hs']:.1f} 分（{escape(health['level'])}）")
    style_path = Path(__file__).resolve().parent / 'report_style.css'
    if not style_path.is_file():
        style_path = BASE / 'report_style.css'
    style = style_path.read_text(encoding='utf-8')
    score_value = fmt_num(health['hs'], 1)
    score_level = '无法评分' if health['hs'] is None else escape(str(health['level']))
    margin_value = (f"{fmt_num(metrics['margin_pct'], 1)}%"
                    if metrics['margin_pct'] is not None else '缺测')
    spacecraft_id = summary.get('spacecraft_id', summary['satellite_id'])
    report_title = (f'{spacecraft_id} · 能源评估（含第二阶段）'
                    if phase2_html else f'{spacecraft_id} · 能源评估')
    hero_title = '三体01星能源评估' if phase2_html else '三体星能源评估'
    hero_subtitle = ('观测样本评估 · 第二阶段的单机任务、太阳阵代理与 SOC 预测验证'
                     if phase2_html else
                     '当前观测样本报告 · 整星收支、单机功率与测量边界')
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<title>{escape(report_title)}</title>',
        '<style>', style, '</style></head><body>',
        '<header class="hero"><div class="hero-inner">',
        '<p class="eyebrow">Satellite energy / observed telemetry</p>',
        f'<h1>{escape(hero_title)}</h1>',
        f'<p class="hero-sub">{escape(hero_subtitle)}</p>',
        '<div class="hero-meta">',
        f'<span>卫星 <b>{escape(spacecraft_id)}</b></span>',
        f'<span>全部已观测 <b>{metrics["n_samples"]:,}</b> 分钟</span>',
        f'<span>曲线窗口 <b>{escape(start)}—{escape(end)}</b></span>',
        f'<span>来源状态 <b>{escape(summary["provenance"]["status"])}</b></span>',
        (f'<span>第二阶段 <b>任务能耗与剩余电量</b></span>'
         if phase2_html else ''),
        '</div></div></header>',
        '<nav class="report-nav" aria-label="报告章节"><div class="nav-inner">'
        '<a href="#overview">结论与质量</a><a href="#energy">分工况能量</a>'
        + ('<a href="#components">单机功率</a>'
           + ('<a href="#design-compare">设计对照</a>' if design_summary else '')
           + '<a href="#boundary">物理边界</a>'
           if components else '')
        + '<a href="#charts">遥测图表</a>' + phase2_nav + '</div></nav>',
        '<main class="report-shell">',
        '<section class="report-section" id="overview">',
        '<div class="section-head"><h2>结论与数据质量</h2>'
        '<p>评分对象为已观测样本，不能代表整段时期</p></div>',
        '<div class="scope-note"><strong>结论范围</strong><p>'
        '评分仅描述已观测样本；整段时期未评定。'
        '光照/地影优先按 TMK5315 状态判断；状态缺测用 TMD009 分钟极值回退。'
        'TMK5315 分钟内切换或状态异常，以及回退时电流跨阈值的样本，保持“未判定”。'
        '现行 P_solar 字段改释为<strong>太阳阵侧母线功率</strong> = U_bus(TMD016) × I_bus(TMD017)。'
        '据 2026-11-02 口径，TMD017 即<strong>太阳阵侧母线电流</strong>：地影期电流≈0（太阳阵不出力），'
        '光照期母线功率≈六板端原值功率和（诊断）。'
        '光照期<strong>太阳阵功率暂定 = 太阳阵侧母线功率 + 蓄电池充电功率</strong>'
        '（蓄电池充电功率 = U_bus×(I017−I018)，仅取正值）；该叠加因母线电流已含充电支路而存在重复计数，'
        '属暂定口径，待母线电流测点边界（PCDU 充电分支前/后）明确后校正。'
        '太阳阵电压遥测 TMD003–008 判为不可用，六组板端原值功率和等依赖电压的诊断不再计入正文。'
        'TMD009 帆板级总电流仅作工况回退判据与 TMD017 差额诊断；'
        'SOC 使用 10A 设计放电表，充电滞回未建模。能量只在相邻原始采样短间隔内积分，长缺口不外推。'
        + ('02 星尚未核实电池构型与 V-SOC 曲线；SOC、DoD 和综合评分仅为沿用 01 星曲线的条件试算。'
           if summary.get('soc_curve_status') == 'cross_satellite_provisional' else '')
        + '</p></div>',
        '<div class="kpi-grid">',
        f'<div class="kpi"><span class="kpi-label">观测样本评分</span>'
        f'<strong class="kpi-value">{score_value}</strong>'
        f'<span class="kpi-foot">{score_level} · '
        f'{"02 星曲线待核的条件试算" if summary.get("soc_curve_status") == "cross_satellite_provisional" else "TMD017=太阳阵侧母线电流（2026-11-02 口径）"}</span></div>',
        f'<div class="kpi"><span class="kpi-label">日历分钟观测率</span>'
        f'<strong class="kpi-value">{metrics["calendar_minute_coverage"]:.2%}</strong>'
        '<span class="kpi-foot">全评估时间范围内的分钟覆盖</span></div>',
        f'<div class="kpi"><span class="kpi-label">日照 TMD017/负载测点差比</span>'
        f'<strong class="kpi-value">{margin_value}</strong>'
        '<span class="kpi-foot">仅测点差比，不代表电池方向</span></div>',
        f'<div class="kpi"><span class="kpi-label">工况明确 / 未判定</span>'
        f'<strong class="kpi-value">{metrics["n_phase_classified"]:,} / '
        f'{metrics["n_phase_unknown"]:,}</strong>'
        '<span class="kpi-foot">分钟</span></div>',
        '</div>',
        '<div class="panel"><h3>质量与诊断明细</h3><div class="table-scroll">'
        '<table class="data-table compact-table"><thead><tr><th>项目</th><th>当前值</th>'
        '</tr></thead><tbody>',
        f"<tr><td>日历分钟观测率</td><td>{metrics['calendar_minute_coverage']:.2%}</td></tr>",
        f"<tr><td>工况明确 / 未判定</td><td>{metrics['n_phase_classified']} / "
        f"{metrics['n_phase_unknown']} 分钟</td></tr>",
        f"<tr><td>TMK5315 判定 / TMD009 回退</td><td>"
        f"{metrics.get('phase_source_counts', {}).get('TMK5315', 0)} / "
        f"{metrics.get('phase_source_counts', {}).get('TMD009_回退', 0)} 分钟</td></tr>",
        f"<tr><td>TMK5315 分钟内切换</td><td>"
        f"{metrics.get('phase_source_counts', {}).get('TMK5315_切换', 0)} 分钟</td></tr>",
        f"<tr><td>TMK5315 与 TMD009 明确冲突</td><td>"
        f"{metrics.get('n_phase_conflict', 0)} 分钟；工况仍按 TMK5315 判定</td></tr>",
        f"<tr><td>放电开关“通” / 与电流差额符号冲突</td><td>"
        f"{metrics.get('n_battery_switch_discharge', 0)} / "
        f"{metrics.get('n_battery_direction_conflict', 0)} 分钟</td></tr>",
        f"<tr><td>负向差额诊断</td><td>{metrics['n_negative_shunt_diag']} 分钟</td></tr>",
        f"<tr><td>观测样本评分</td><td>{score}</td></tr>",
        f"<tr><td>TMD017/负载测点差比（日照明确分钟）</td><td>"
        f"{fmt_num(metrics['margin_pct'], 1)}%</td></tr>",
        f"<tr><td>DoD p95 / SOC p5（全部有电池电压的观测）</td><td>"
        f"{fmt_num(metrics['dod_p95_pct'], 1)}% / {fmt_num(metrics['soc_p5_pct'], 1)}%</td></tr>",
        '</tbody></table></div></div></section>',
        '<section class="report-section" id="energy">'
        '<div class="section-head"><h2>分工况能量</h2>'
        '<p>日照、地影与未判定分别列账</p></div>',
        '<div class="panel">',
    ]
    if interval:
        parts.extend([f'<h3>原始采样间隔积分 <span class="badge">相邻间隔 ≤'
                      f'{interval["max_gap_seconds"]:g} 秒 · kWh</span></h3>',
                      '<div class="table-scroll"><table class="data-table">'
                      '<thead><tr><th>工况</th><th>有效秒</th><th>TMD017 测点折算</th><th>负载</th>'
                      '<th>两测点差额（非电池）</th><th>帆板与 TMD017 差额</th></tr></thead><tbody>'])
    else:
        parts.extend(['<h3>名义 60 秒等效能量 <span class="badge badge-warn">'
                      '非实际时长积分 · kWh</span></h3>',
                      '<div class="table-scroll"><table class="data-table">'
                      '<thead><tr><th>工况</th><th>名义分钟</th><th>TMD017 测点折算</th><th>负载</th>'
                      '<th>两测点差额（非电池）</th><th>帆板与 TMD017 差额</th></tr></thead><tbody>'])
    for label, key in [('日照', 'e_sun'), ('地影', 'e_ecl'), ('未判定', 'e_unknown')]:
        energy = interval['phase'][key] if interval else phase[key]
        duration = (f"{energy['observed_seconds']:.1f}" if interval else
                    str(energy['n_energy_samples']))
        parts.append(f"<tr><td>{label}</td><td>{duration}</td>"
                     f"<td>{energy['e_solar_kWh']:.3f}</td>"
                     f"<td>{energy['e_load_kWh']:.3f}</td>"
                     f"<td>{energy['e_batnet_kWh']:.3f}</td>"
                     f"<td>{energy['e_shunt_diag_kWh']:.3f}</td></tr>")
    parts.extend(['</tbody></table></div><p class="section-tip">“两测点差额”由前两列相减构造，'
                  '不代表电池净充放电，也不构成独立物理闭合验证。'
                  '旧版详细文档 framework_documentation.html '
                  '保留为 v2.2 历史快照。</p></div></section>'])
    if components:
        parts.extend([
            '<section class="report-section" id="components">'
            '<div class="section-head"><h2>单机与支路功率</h2>'
            '<p>逐单机查看功率代理、有效分钟与测点来源</p></div>',
            '<div class="panel"><p>下表只统计与整星主测点同一分钟的遥测；'
            '均值和 p95 只在该部件有效样本上计算。'
            '分钟功率由电压分钟均值乘电流分钟均值，未验证逐采样同步乘积。'
            '“母线等效”使用母线电压乘支路电流，待核实支路电压及测点边界。'
            '帆板 A–F 端电压乘电流不是分流前可用太阳阵功率。'
            '工况来源于状态文字，一分钟内多状态记为“分钟内切换”。</p>',
            f"<p><strong>全部负载支路同时有效：</strong>{components['n_valid_load_branch_sum']} 分钟；"
            f"支路和超过整星负载的分钟：{components['n_negative_load_minus_branch_sum']}。"
            '该差额仅供检查测点和标定，不能直接解释为损耗。</p></div>',
            '<div class="subsection"><h3 id="component-curves">单机耗电功率曲线</h3>'
            '<p>选择单机，在同一个图表区域查看其功率曲线。</p></div>',
            f'<p class="body-copy">以下曲线共用星上北京时间观测窗口 {escape(start)}—{escape(end)}；'
            '仅绘制有支路电流或电压/电流对的负载单机。数值为电压分钟均值 × 电流分钟均值的功率代理 W；'
            '只测得支路电流时使用母线电压，属于母线等效功率，'
            '智算机、路由器和激光处理机等曲线取自配置的供电支路电流；'
            'GPU 功耗遥测 TMS051/TMS451/TMS651 仅作上下文，尚未用来绘制或累加单机耗电。'
            '缺测与时间断档处断线，稀疏测点仅标出观测值；连线只辅助阅读，'
            '不代表连续耗电或实际积分电量。'
            '状态型加热器没有独立功率测点，不绘制耗电曲线。</p>',
        ])
        parts.extend(component_power_curves(window, components))
        parts.extend([
            '<details class="aux-details" id="component-stats">'
            '<summary>单机统计明细（均值和 p95 只基于有效分钟）</summary>'
            '<div class="table-scroll"><table class="data-table">'
            '<thead><tr><th>部件</th><th>角色</th><th>功率口径</th>'
            '<th>有效分钟</th><th>均值 W</th><th>p95 W</th><th>状态</th></tr></thead><tbody>',
        ])
        role_names = {'solar_array': '帆板端', 'load': '负载支路',
                      'context': '状态/电压'}
        basis_names = {'minute_mean_voltage_x_mean_current_proxy': '测点分钟均值 V×I',
                       'bus_voltage_x_branch_current_proxy': '母线等效 V×I',
                       'state_only': '不计算功率', 'direct_power': '直接功率'}
        for item in components['components'].values():
            if item.get('context_only', item['model'] == 'state'):
                continue
            parts.append('<tr>'
                         f"<td>{escape(item['name'])}</td>"
                         f"<td>{escape(role_names.get(item['role'], item['role']))}</td>"
                         f"<td>{escape(basis_names.get(item['power_basis'], item['power_basis']))}</td>"
                         f"<td>{item['n_valid']}/{item['n_total']}</td>"
                         f"<td>{fmt_num(item['power_mean_W'], 2)}</td>"
                         f"<td>{fmt_num(item['power_p95_W'], 2)}</td>"
                         f"<td>{escape(item['status'])}</td></tr>")
        parts.append('</tbody></table></div></details>')
        context_items = [item for item in components['components'].values()
                         if item.get('context_only', item['model'] == 'state')]
        if context_items:
            parts.append(f'<details class="aux-details"><summary>状态与辅助测点'
                         f'（{len(context_items)} 个）</summary>'
                         '<div class="table-scroll"><table class="data-table">'
                         '<thead><tr><th>部件</th><th>测点</th><th>有观测分钟</th>'
                         '<th>状态分钟分布</th><th>状态</th></tr></thead><tbody>')
            for item in context_items:
                counts = '、'.join(f'{escape(k)} {v}'
                                  for k, v in item['mode_counts'].items()) or '仅数值上下文'
                parts.append('<tr>'
                             f"<td>{escape(item['name'])}</td>"
                             f"<td>{escape(', '.join(item['source_codes']))}</td>"
                             f"<td>{item['n_observed_context']}/{item['n_total']}</td>"
                             f'<td>{counts}</td>'
                             f"<td>{escape(item['status'])}</td></tr>")
            parts.append('</tbody></table></div></details>')
        parts.append('<div class="mode-grid">')
        for item in components['components'].values():
            if item['power_by_mode']:
                parts.append(f'<details class="mode-details"><summary>'
                             f'{escape(item["name"])} · 按工况功率</summary>'
                             '<div class="table-scroll"><table class="data-table">'
                             '<thead><tr><th>工况</th><th>有效分钟</th>'
                             '<th>均值 W</th><th>p95 W</th></tr></thead><tbody>')
                for mode, row in item['power_by_mode'].items():
                    parts.append(f"<tr><td>{escape(mode)}</td><td>{row['n_valid']}</td>"
                                 f"<td>{row['power_mean_W']:.2f}</td>"
                                 f"<td>{row['power_p95_W']:.2f}</td></tr>")
                parts.append('</tbody></table></div></details>')
        parts.append('</div></section>')
        if design_summary:
            parts.extend(design_compare.html_section(
                design_summary, design_ratio_svg, design_power_svg))
        panel_diag = components.get('panel_to_bus_difference', {})
        # 新口径3（暂定）数值：本报告观测窗口内、按 60s 名义等效积分，与图表同源。
        _win = [r for r in result if start <= r['t'] <= end]
        def _cal3(phase):
            s_bus = s_chg = 0.0
            for r in _win:
                if r.get('phase') != phase:
                    continue
                ps = value(r.get('P_solar'))
                if ps is None:
                    continue
                pe = value(r.get('P_excess'))
                s_bus += ps
                s_chg += (pe if (pe is not None and pe > 0) else 0.0)
            return s_bus / 60000.0, s_chg / 60000.0
        _sun_bus, _sun_chg = _cal3('日照')
        _ecl_bus, _ecl_chg = _cal3('地影')
        parts.extend([
            '<section class="report-section" id="boundary">'
            '<div class="section-head"><h2>物理边界与结论限制</h2>'
            '<p>用独立测量核对能量守恒，当前数据尚不支持闭合判定</p></div>',
            '<div class="panel method-panel"><h3>分边界功率账本 '
            '<span class="badge badge-warn">物理闭合未验证</span></h3>',
            '<p><strong>设计方案：</strong>供配电分系统采用<strong>不调节母线 + S³R 顺序开关分流</strong>，'
            '蓄电池组充放电电压钳位一次母线。光照期按'
            '<strong>母线供负载 → 蓄电池充电 → S³R 分流</strong>分配太阳阵电能；'
            '短时太阳阵不足时，太阳阵与电池联合供电；阴影期由电池供电。'
            '“经 PCDU 充电”不等于绕过母线，PCDU 内的充电控制和公共母线可以共存。</p>',
            '<p><strong>电池方向规则：</strong>按用户确认的 TMD074_b1 开关语义，'
            '“通”时给 TMH025 幅值赋放电正号；“断”、同分钟切换或缺测不自动赋充电负号。'
            '<code>I_负载−I_TMD017</code> 仅保留为另一测点差额诊断；'
            '与开关规则相反的分钟记录冲突，不改写开关规则结果。'
            '地影且负载在工作时，设计拓扑支持电池放电；'
            '日照短时供电不足时可由太阳阵与电池联合供电。</p>',
            '<p><strong>规则边界：</strong>上述赋号采用用户给定的开关判向，'
            '不是 TMH025 原生有符号测量。“通”在阳照区也大量出现；'
            '若同时观察到电流差额与之相反，须核对电池充放电路径及各电流传感器位置。'
            '“断”只表示不按此规则判放电，不等同正在充电。</p>',
            '<p class="section-tip">设计摘录同时给出最大功率 1178 W、最大电流 29.6 A 和母线范围 '
            '23.1–29.2 V；1178/29.6≈39.8 V，超出所列母线范围。'
            '额定值需回查原图表，当前不把这组数值作为功率闭合阈值。</p>',
            '<p><strong>稳态供配电总体账本：</strong>太阳阵实际输入 + 电池端放电 = '
            '负载端输入 + 电池端充电 + 分流及供配电总耗散。'
            '各项须选在同一系统边界并计入线路/控制压降；暂态还需计入母线储能变化。'
            '不能把未实际产生的潜在太阳阵功率记作分流耗散。</p>',
            '<p><strong>地影区账本（公共母线边界）：</strong>'
            '<code>P_电池放电,母线侧 = P_全部负载 + P_其他 + dE_母线/dt</code>。'
            '此时太阳阵入母线近零、蓄电池不充电；电池端试算'
            '<code>P_B_dis_terminal = U_bat(TMD019) × |TMH025|</code>，'
            '母线侧放电功率须扣除电池到母线通路损耗。'
            'P_全部负载含热控等所有负载，但 TMD018 是否覆盖这些负载尚未核实。'
            'P_其他暂作未分解收支项，不等同于已测得的母线损耗或热控功率；'
            '电池端试算减 TMD018 功率也不能直接给 P_其他定值。</p>',
            '<p><strong>光照区账本（公共母线边界）：</strong>充电工况为'
            '<code>P_SA_node = P_load_all + P_B_ch_bus + P_bus_loss + dE_bus/dt</code>；'
            '太阳阵不足、与电池联合供电时为'
            '<code>P_SA_node + P_B_dis_bus = P_load_all + P_bus_loss + dE_bus/dt</code>。'
            '充电支路母线侧输入 = 电池端充电功率 + 充电通路损耗。'
            '另在阵列/S³R 边界有'
            '<code>P_SA_actual_preS3R = P_SA_node + P_shunt_actual + P_S3R_loss</code>。'
            '现有测点边界尚不足以数值闭合。</p>',
            '<p><strong>对应能量守恒：</strong>在同一连续有效工况时段积分，地影有'
            '<code>E_电池放电,母线侧 = E_全部负载 + E_其他 + ΔE_母线</code>；'
            '光照充电有'
            '<code>E_SA_node = E_load_all + E_B_ch_bus + E_bus_loss + ΔE_bus</code>；'
            '光照联合供电有'
            '<code>E_SA_node + E_B_dis_bus = E_load_all + E_bus_loss + ΔE_bus</code>。'
            'ΔE_bus 为该时段母线储能变化；数据长缺口不外推，缺失支路不按零处理。</p>',
            (f'<p><strong>新口径3 · 光照期太阳阵功率（暂定，2026-11-02）：</strong>'
             f'太阳阵功率 暂定 = 太阳阵侧母线功率 + 蓄电池充电功率。'
             f'本报告观测窗口（{escape(start)}—{escape(end)}）内按 60s 名义等效积分：'
             f'日照母线功率(P_solar)={_sun_bus:.2f} kWh，充电功率=max(P_excess,0)={_sun_chg:.2f} kWh，'
             f'故太阳阵功率(暂定)={_sun_bus + _sun_chg:.2f} kWh；'
             f'地影母线功率≈{_ecl_bus:.2f} kWh（≈0，太阳阵不出力）。'
             f'<strong>重要待澄清：</strong>数据显示 TMD017 已含充电支路'
             f'（I017−I018 正值占比 98.2%），因此“母线功率 + 充电功率”对充电功率重复计数，'
             f'上式会高估太阳阵功率（物理上应≈母线功率 + 分流耗散）。该口径为暂定，'
             f'待母线电流测点边界（PCDU 充电分支前/后）明确后校正。'
             f'（注：上式按本报告观测窗口逐分钟复算，与上方能量表“原始采样间隔积分”的汇总范围不同源，'
             f'绝对值不与表中母线功率直接比较。）</p>'),
            '<p><strong>TMD017 测点边界（2026-11-02 口径更新）：</strong>'
            'TMD017 改释为<strong>太阳阵侧母线电流</strong>。地影期太阳阵不出力，'
            '观测中 TMD017 接近零（全样本均值≈−0.06 A、中位≈−0.11 A，|值|>1A 仅约 0.5%），'
            '与“太阳阵侧母线电流在阴影期≈0”一致。'
            '光照期母线功率 U_bus×I017 与六组帆板端原值功率和之比≈0.95，'
            '印证母线功率≈六板端原值功率和（诊断意义）；'
            '同时 I017−I018 中位≈+7A、正值占比 98.2%，说明 TMD017 已含蓄电池充电支路。'
            '<code>TMD016×TMD017</code> 改称太阳阵侧母线功率；'
            '<code>TMD016×TMD018</code> 是负载测点折算功率；'
            '二者之差等于充电功率（U_bus×(I017−I018)）。</p>',
            '<p><strong>帆板端 ΣV×I − TMD017 差额（已弃用）：</strong>'
            '该诊断依赖太阳阵电压遥测 TMD003–008，2026-11-02 起判为不可用，'
            '故相关图表与差额数值不再计入正文口径，仅保留此说明。</p>',
            ('<p><strong>母线损耗尚不可算：</strong>02 星 TMH025 原值约为数百至上千，'
             '按既有 100 A 校验条件，没有可用于正式电池功率估算的分钟；'
             '下方原值图和电压乘积诊断均不按此条件筛选。'
             'TMD074_b1 的“通”只能作为开关状态记录，电池端功率尚未核准。'
             if metrics.get('n_battery_switch_current_valid', 0) == 0 else
             '<p><strong>母线损耗尚不可算：</strong>用户确认 CH 的 TMH025 是蓄电池母线电流、单位 A。'
             '现行输入已纳入该幅值与 TMD074_b1；开关规则赋号后的电池端功率与'
             'TMD017/TMD018 测点差额有冲突，不能把残差直接解释成被动损耗。')
            + '不同电压侧不能直接比较电流；TMD017、TMD018、TMH025 的传感器位置和'
            '负载覆盖范围，以及电池充放电路径仍需核对。'
            '现行 P_charge/P_disch 字段由测点差额拆分，代入旧公式得到零只是代数构造。'
            '若负载在单机输入端计量，单机内部耗散已包含在负载中，不能再次加作账本损耗。'
            '逐分钟部件功率与状态见 '
            '<a href="out/energy_v2_components.csv">部件明细 CSV</a>。</p>'
            '</div>',
        ])
        if tmh_overview:
            parts.extend([
                '<div class="panel battery-detail" id="tmh025-raw-overview">',
                '<h3>TMH025 本次取数全量分钟原值 <span class="badge badge-warn">未按 100 A 筛选</span></h3>',
                f'<p>星上北京时间 {escape(tmh_overview["start"])}—'
                f'{escape(tmh_overview["end"])}，共 {tmh_overview["n"]:,} 个有记录分钟；'
                f'{tmh_overview["n_avg"]:,} 个分钟均值可解析。'
                '图中逐分钟最小、均值、最大均保留原值，长时间缺口断线。'
                f'分钟均值中位数 {tmh_overview["median_avg"]:.2f}，'
                f'全部分钟最大原值中的最高值 {tmh_overview["maximum"]:.2f}；'
                f'{tmh_overview["n_above_100"]:,} 个分钟的最大原值大于 100。'
                '这些是数据库数值，不据此确认单位为 A；100 A 只是既有功率试算的有效范围，'
                '不会用于删减本图的数据。</p>',
                f'<figure>{tmh_overview["svg"]}<figcaption>'
                '全量分钟极值与均值图；不是逐条原始采样曲线，'
                '但每个分钟的原始极值均保留，异常高值不会被隐藏。'
                '</figcaption></figure></div>',
            ])
        if battery_window:
            panel_focus_rows = ('<p class="section-tip"><strong>六组帆板电压原值表（已弃用）：</strong>'
                                '原表逐项列出 TMD003–008 电压×TMD010–015 电流，'
                                '因太阳阵电压遥测于 2026-11-02 判为不可用，此处不再展示，'
                                '仅保留说明以避免误用。</p>')
            balance_gap = (battery_window['array_raw_focus'] +
                           battery_window['battery_focus'] -
                           battery_window['load_focus'])
            parts.extend([
                '<div class="panel battery-detail" id="battery-power-0603">',
                '<h3>2025-12-29 06:03 光照区附近四项功率 <span class="badge badge-warn">TMH025 按开关赋号</span></h3>',
                f'<p>星上北京时间 {escape(battery_window["start"])}—{escape(battery_window["end"])}；'
                f'{battery_window["n"]} 个连续观测分钟，四条虚线均为分钟均值乘积。'
                '灰色竖线标出 06:03。</p>',
                '<div class="battery-formulas">',
                '<p><strong>TMD017 测点功率（母线电压折算）：</strong><code>P_TMD017 = '
                'U_bus(TMD016) × I_bus(TMD017)</code></p>',
                '<p><strong>母线等效负载：</strong><code>P_load = '
                'U_bus(TMD016) × I_load(TMD018)</code></p>',
                '<p><strong>开关判向规则：</strong><code>TMD074_b1=通 ⇒ 放电正号</code>；'
                '断、分钟内切换或缺测保持方向未判定，不自动赋充电负号。'
                '<code>I_load(TMD018) − I_bus(TMD017)</code>仅作冲突诊断。</p>',
                '<p><strong>TMH025 有符号电流（派生）：</strong>'
                '<code>TMH025_signed_switch = +|TMH025.value|（开关通）</code>；'
                '开关非“通”、缺测或原值异常时不赋号。</p>',
                '<p><strong>电池端有符号功率（开关判向试算）：</strong>'
                '<code>P_bat_signed_trial = U_bat(TMD019) × TMH025_signed_switch</code>。'
                '正功率按开关规则标作放电；须满足电压和电流测点同端口的条件。</p>',
                '<p><strong>光照太阳阵功率（2026-11-03 校正）：</strong>'
                '<code>P_太阳阵 = P_017测点 + P_电池 = U_bus×I_bus + U_bat×TMH025_signed</code>。'
                '光照期太阳阵功率按 017 测点功率与电池端有符号试算功率之和计；'
                '因 TMD017 已含充电支路（I017−I018>0 占 98.2%），与电池端试算叠加存在口径重叠，'
                '待母线电流测点边界（PCDU 充电分支前/后）明确后校正。</p>',
                '<p><strong>遥测代号中文名：</strong>'
                'TMD016＝母线电压遥测；TMD017＝太阳阵侧母线电流遥测；'
                'TMD018＝负载电流遥测；TMD019＝蓄电池电压遥测；'
                'TMH025＝蓄电池组母线电流检测。</p>',
                '<p><strong>太阳阵代号：</strong>TMD003–008 为太阳阵 A–F 电压遥测'
                '（2026-11-02 起判为不可用，相关诊断不再计入）；'
                'TMD010–015 为太阳阵 A–F 电流遥测。</p>',
                '</div>',
                f'<figure>{battery_window["svg"]}<figcaption>'
                f'06:03：TMD017 测点 {battery_window["solar_focus"]:.1f} W，'
                f'负载 {battery_window["load_focus"]:.1f} W，'
                f'电池端有符号试算 {battery_window["battery_focus"]:.1f} W（开关通赋放电正号），'
                f'光照太阳阵功率 {battery_window["solar_array_focus"]:.1f} W'
                f'（= 017测点 {battery_window["solar_focus"]:.1f} + 电池 '
                f'{battery_window["battery_focus"]:.1f}）。'
                '</figcaption></figure>',
                f'<figure>{battery_window["current_svg"]}<figcaption>'
                'TMH025_有符号 电流：开关通按用户规则赋放电正号，非遥测原生方向；红色水平虚线为 0 A。'
                '开关非通、缺测或异常原值处断线。</figcaption></figure>',
                f'<figure>{battery_window["raw_current_svg"]}<figcaption>'
                'TMH025 数据库原值的分钟均值与最大值，未按 100 A 筛选；'
                '纵轴为未确认单位的原值，不与上图有符号电流混同。'
                '</figcaption></figure>',
                panel_focus_rows,
                f'<p class="section-tip">06:03 六组板端原值乘积之和（依赖电压遥测）为 '
                f'{battery_window["array_raw_focus"]:.1f} W；与 TMD017 测点折算 '
                f'{battery_window["solar_focus"]:.1f} W 相差 '
                f'{battery_window["array_raw_focus"] - battery_window["solar_focus"]:.1f} W。'
                '注：太阳阵电压遥测 TMD003–008 已于 2026-11-02 判为不可用，'
                '该原值和及下方电压表仅供历史对比，不再作为正文口径。'
                'D 组 9.5065 V 低于 A–C 组约 29 V，E/F 组电压为接近零的负值，'
                '正说明电压遥测不可靠。算术和可复现，但不能据此确认六组实际发电总功率。</p>',
                f'<p class="section-tip">若暂把阵列原值和、电池端放电试算与已计负载功率'
                f'都当作同一时刻、同一边界的真实功率，输入减已计负载为 '
                f'{battery_window["array_raw_focus"]:.1f} + '
                f'{battery_window["battery_focus"]:.1f} − '
                f'{battery_window["load_focus"]:.1f} = {balance_gap:.1f} W。'
                '该差额可能含未计负载、分流及测点边界误差，不能命名为损耗。'
                '优先核对 TMH025 的工程值换算与测点、D/E/F 的分流状态与电压测点、'
                'TMD017 的支路位置和采样同步。</p>',
                f'<p class="section-tip">06:03 放电开关状态为“{escape(battery_window["switch_state_focus"])}”，'
                '按用户规则给 TMH025 幅值赋放电正号。'
                f'同分钟 <code>I_负载 − I_TMD017</code> 为 '
                f'{battery_window["direction_current_focus"]:.4f} A，与开关规则相反；'
                '它只记录测点冲突，不再给电池电流赋号。'
                f'派生电流为 '
                f'{battery_window["battery_current_focus"]:.4f} A，'
                f'电池端有符号功率试算为 {battery_window["battery_focus"]:.1f} W。'
                'TMH025 原始记录保持原值，其原生方向编码和测点边界仍待核。</p>',
                f'<p class="section-tip">06:03 六组帆板电压和电流遥测齐全，但 '
                f'{escape("、".join(battery_window["array_negative_voltages_focus"]))}'
                ' 为负电压原值。按现行非负校验，只有 '
                f'{6 - len(battery_window["array_invalid_panels_focus"])}/6 组可用于板端功率估算；'
                '整阵实际发电功率此时无法确认。红色曲线仅展示六组原值乘积的代数和，'
                '不参与物理闭合或 S³R 损耗计算。</p>',
                f'<p class="section-tip">06:03 的 TMD017 测点减负载为 '
                f'{battery_window["solar_focus"] - battery_window["load_focus"]:.1f} W，'
                '只是两测点的代数差额；若充电支路在 TMD017 测点之前分出，'
                '不能拿它与电池端试算值相减求损耗或判定能量不守恒。</p>',
                f'<p class="section-tip">TMH025 来自 CH 在此窗口的 '
                f'{battery_window["sample_count"]:,} 条原始记录，先按分钟求均值；'
                '只在 TMH025 与 TMD019 为同一电池端口且数值确为安培工程值时，'
                '橙线才可作为电池端有符号电功率试算。本窗口按 <code>TMD074_b1=通</code> 给 TMH025 幅值赋放电正号；'
                'TMD017 所在支路及时间对齐仍须核查。'
                '功率幅值 751.5 W 是开关规则下的条件放电试算，曲线不代表母线损耗。'
                f'<a href="{escape(BATTERY_WINDOW_CSV, quote=True)}">电池电流分钟来源 CSV</a>。'
                '</p></div>',
            ])
        if eclipse_window:
            parts.extend([
                '<div class="panel battery-detail" id="battery-power-eclipse-0521">',
                '<h3>2025-12-29 05:21 附近地影四项功率 '
                '<span class="badge badge-warn">放电功率为电池端条件试算</span></h3>',
                f'<p>星上北京时间 {escape(eclipse_window["start"])}—'
                f'{escape(eclipse_window["end"])}，连续 {eclipse_window["n"]} 个地影分钟；'
                f'CH 的 TMH025 有 {eclipse_window["sample_count"]:,} 条原始记录。'
                f'其中 {eclipse_window["tmd017_near_zero_minutes"]}/'
                f'{eclipse_window["n"]} 分钟的 |TMD017| 小于 1 A。'
                '灰色竖线标出 05:21；三条虚线均由同一分钟的遥测均值计算。</p>',
                '<div class="battery-formulas">',
                '<p><code>P_TMD017 = U_bus(TMD016) × I_TMD017</code>；'
                '<code>P_load_obs = U_bus(TMD016) × I_load(TMD018)</code>；'
                '<code>P_B_dis_terminal_trial = U_bat(TMD019) × |TMH025.value|</code>。'
                '地影且负载工作时暂按放电方向显示电池端功率。</p>',
                '</div>',
                f'<figure>{eclipse_window["svg"]}<figcaption>'
                f'05:21：TMD017 测点 {eclipse_window["solar_focus"]:.1f} W（近零偏置），'
                f'负载测点 {eclipse_window["load_focus"]:.1f} W，'
                f'电池端放电试算 {eclipse_window["battery_focus"]:.1f} W。'
                '</figcaption></figure>',
                '<details><summary>查看地影期 TMH025 有符号电流曲线</summary>',
                f'<figure>{eclipse_window["current_svg"]}<figcaption>'
                f'05:21 电池电流幅值 {eclipse_window["battery_current_focus"]:.4f} A，'
                '按 TMD074_b1 为“通”的规则赋放电正号。'
                '零线和断线保留；该符号不是 TMH025 原生方向码。</figcaption></figure>',
                '</details>',
                f'<figure>{eclipse_window["raw_current_svg"]}<figcaption>'
                '地影窗口 TMH025 数据库原值的分钟均值与最大值，未按 100 A 筛选；'
                '纵轴为未确认单位的原值。'
                '</figcaption></figure>',
                f'<p class="section-tip">05:21 的电池端放电试算减去 TMD018 已计负载为 '
                f'{eclipse_window["battery_focus"] - eclipse_window["load_focus"]:.1f} W。'
                '该差额可能含未计负载、不同测点边界和 TMH025 工程值误差，'
                '<strong>不能直接称为母线损耗</strong>。'
                'TMD017 近零而负载仍工作，说明它不覆盖电池供负载的完整路径。'
                '<strong>TMD017 的值不再用于 TMH025 赋号</strong>；'
                '开关规则的物理边界仍需结合供配电拓扑核查。'
                f'<a href="{escape(ECLIPSE_WINDOW_CSV, quote=True)}">'
                '地影 TMH025 分钟来源 CSV</a>。</p>',
                '</div>',
            ])
        for nearby in satellite_02_windows:
            phase = nearby['phase']
            marker = 'sun' if phase == '日照' else 'eclipse'
            parts.extend([
                f'<div class="panel battery-detail" id="satellite02-power-{marker}">',
                f'<h3>02 星{phase}附近四项功率 '
                '<span class="badge badge-warn">第四项为原值条件试算</span></h3>',
                f'<p>星上北京时间 {escape(nearby["start"])}—{escape(nearby["end"])}，'
                f'连续 {nearby["n"]} 个{phase}观测分钟；灰色竖线标出 '
                f'{escape(nearby["focus"][11:16])}。'
                '前三项为 TMD017 测点、负载测点和六板端原值诊断；'
                '第四项在独立纵轴上展示电池电压乘 TMH025 未筛选分钟均值。'
                f'六板原值在 {nearby["n_panel"]}/{nearby["n"]} 分钟齐全。</p>',
                '<div class="battery-formulas">'
                '<p><code>P_TMD017 = U_bus(TMD016) × I_bus(TMD017)</code>；'
                '<code>P_load_obs = U_bus(TMD016) × I_load(TMD018)</code>。</p>'
                '<p><code>P_SA_raw_diag = Σ[U_A…F(TMD003–008) × '
                'I_A…F(TMD010–015)]</code>。六板端原值包含负值，'
                '没有确认分流前后测点，不视为实际整阵发电功率。</p>'
                '<p><strong>第四项仅为数值乘积：</strong>'
                '<code>U_bat(TMD019) × TMH025 数据库分钟均值原值</code>。'
                '02 星 TMH025 的工程换算与物理单位尚未核实；'
                '只有确认原值单位为 A 且测点属于同一电池端口后，乘积才可解释为 W。'
                '不按 100 A 删除或截断数据，也不把它纳入正式收支闭合。</p></div>',
                f'<figure>{nearby["svg"]}<figcaption>'
                f'{escape(nearby["focus"][11:16])}：TMD017 测点 '
                f'{nearby["solar_focus"]:.1f} W，负载测点 '
                f'{nearby["load_focus"]:.1f} W，'
                f'六板端原值和 {nearby["panel_focus"]:.1f} W（诊断）。'
                '</figcaption></figure>',
                f'<figure>{nearby["battery_svg"]}<figcaption>'
                f'第四项在 {nearby["n_battery_raw_product"]}/{nearby["n"]} 分钟可作数值乘积；'
                f'焦点为 {nearby["battery_raw_product_focus"]:.1f} V×原值。'
                '纵轴保持原始量级；未核准单位，不能将此数值当作已测电池功率。'
                '</figcaption></figure>',
                f'<figure>{nearby["raw_svg"]}<figcaption>'
                f'该窗口 {nearby["n_raw_current"]}/{nearby["n"]} 分钟有 TMH025 数据库原值；'
                f'焦点为 {nearby["raw_current_focus"]:.4f}，放电开关为“'
                f'{escape(nearby["switch_focus"])}”。纵轴是未校准的原值，'
                '最小、均值、最大均未按 100 A 筛选；'
                '不是已确认的安培值，也不是电池功率。'
                '</figcaption></figure>',
                '</div>',
            ])
        parts.append('</section>')
    parts.extend(['<section class="report-section" id="charts">'
                  '<div class="section-head"><h2>真实遥测 · 观测窗口图表</h2>'
                  '<p>原始测点与整星计算量</p></div>',
                  f'<p class="body-copy">观测窗口：{escape(start)}—{escape(end)}。'
                  '圆点仅表示实际取得的分钟或小时分组，不外推未观测时间。'
                  '各图单位不同，选择后在同一图表区域显示。</p>',
                  '<p class="section-tip">太阳阵电压遥测 TMD003–008 已于 2026-11-02 判为不可用，'
                  '原“帆板端 ΣV×I − TMD017 差额图”依赖电压遥测，已从图表清单移除。'
                  'TMH025 方向及测点边界未核实，试算残差大量为负，当前不绘制“母线损耗”数值曲线。</p>'])
    default_chart = ('chart_power_flow' if any(chart['name'] == 'chart_power_flow'
                                               for chart in manifest) else manifest[0]['name'])
    parts.extend(['<div class="chart-picker"><label for="telemetry-select">选择遥测图表</label>',
                  '<select id="telemetry-select" aria-controls="telemetry-display">'])
    for chart in manifest:
        name = chart['name']
        parts.append(f'<option data-option-group="telemetry" value="{escape(name, quote=True)}"'
                     f'{" selected" if name == default_chart else ""}>'
                     f'{escape(chart["title"])}</option>')
    parts.extend([f'</select><span class="picker-count">共 {len(manifest)} 张可选</span></div>',
                  '<div id="telemetry-display" class="chart-stack">'])
    for index, chart in enumerate(manifest, 1):
        name = chart['name']
        svg = (OUT / f'{name}.svg').read_text(encoding='utf-8')
        if not svg.startswith('<svg ') or not svg.rstrip().endswith('</svg>'):
            raise ValueError(f'图表内容不完整：{name}')
        svg = svg.replace('<svg ', f'<svg data-inline-chart="{escape(name, quote=True)}" ', 1)
        parts.append(f'<article class="chart-card" data-chart-panel="{escape(name, quote=True)}"'
                     f'{" hidden" if name != default_chart else ""}>'
                     f'<div class="chart-card-header">'
                     f'<span class="chart-index">{index:02d}</span>'
                     f'<h3>{escape(chart["title"])}</h3></div>'
                     f'<figure class="chart">{svg}<figcaption>'
                     f'{escape(chart["title"])}</figcaption></figure></article>')
    parts.extend(['</div></section>', phase2_html,
                  '<footer class="report-footer">'
                  '报告仅描述已观测遥测及其计算量；测点和功率边界需结合型号文件核实。'
                  '</footer></main>',
                  '<script>(function(){function bindPicker(selectId,attribute){'
                  'const select=document.getElementById(selectId);if(!select)return;'
                  'const panels=[...document.querySelectorAll(`[${attribute}]`)];'
                  'function show(){for(const panel of panels)'
                  'panel.hidden=panel.getAttribute(attribute)!==select.value;}'
                  'select.addEventListener("change",show);show();}'
                  'bindPicker("component-select","data-component-panel");'
                  'bindPicker("telemetry-select","data-chart-panel");})();</script>',
                  # 设计功耗对照：视图（总览/单机曲线）与单机选择
                  '<script>(function(){var view=document.getElementById("design-view-select");'
                  'if(!view){return;}'
                  'var unit=document.getElementById("design-unit-select");'
                  'var unitPicker=document.getElementById("design-unit-picker");'
                  'var views=[].slice.call(document.querySelectorAll("[data-design-view]"));'
                  'var units=[].slice.call(document.querySelectorAll("[data-design-unit]"));'
                  'function sync(){var kind=view.value;'
                  'views.forEach(function(box){box.hidden=box.getAttribute("data-design-view")!==kind;});'
                  'if(unitPicker){unitPicker.hidden=(kind!=="unit-series");}'
                  'if(kind==="unit-series"&&unit){units.forEach(function(box){'
                  'box.hidden=box.getAttribute("data-design-unit")!==unit.value;});}}'
                  'view.addEventListener("change",sync);'
                  'if(unit){unit.addEventListener("change",sync);}sync();})();</script>',
                  '</body></html>'])
    page = BASE / REPORT_NAME
    page.write_text('\n'.join(parts), encoding='utf-8')


def main(workspace_dir=None):
    global BASE, OUT, REPORT_NAME
    if workspace_dir is not None:
        BASE = Path(workspace_dir).resolve()
        OUT = BASE / 'charts'
        REPORT_NAME = f'framework_documentation_{BASE.name}.html'
    OUT.mkdir(parents=True, exist_ok=True)
    result = sorted(rows('out/energy_v2_result.csv'), key=lambda r: r['t'])
    minute = rows('data_power_minute.csv')
    all_minute = minute
    if not result or not minute:
        raise ValueError('本地真实遥测或 v2 评估输出为空')
    result = select_observation_window(result)
    start, end = result[0]['t'], result[-1]['t']
    minute = [row for row in minute if start <= row['t'] <= end]
    times = [r['t'] for r in result]
    hour_times, hourly, energy = hour_buckets(result)
    hour_indices = [i for i, hour in enumerate(hour_times)
                    if any(hourly[key][i] is not None for key in hourly)]
    hour_times = [max(hour_times[i], start) for i in hour_indices]
    hourly = {key: [values[i] for i in hour_indices] for key, values in hourly.items()}
    energy = {key: [values[i] for i in hour_indices] for key, values in energy.items()}
    minute_gap = 3 * 60
    hourly_gap = 90 * 60
    col = lambda key: [value(r.get(key)) for r in result]
    print(f'观测时间段: {start}—{end}，分钟样本 {len(result)}，其中地影 {sum(r["phase"] == "地影" for r in result)}')
    write_chart('chart_voltages', '母线与蓄电池电压 · TMD016 / TMD019', times,
                {'母线电压 (V)': col('V_bus'), '电池电压 (V)': col('V_bat')}, 'V', minute_gap, window_start=start, window_end=end)
    write_chart('chart_currents', '太阳阵总电流与负载电流 · TMD009 / TMD018', times,
                {'太阳阵总电流 (A)': col('I_solar'), '负载电流 (A)': col('I_load')}, 'A', minute_gap, low=0, window_start=start, window_end=end)
    write_chart('chart_power_flow', '功率计算量 · TMD017 测点 / 负载 / 差额', times,
                {'TMD017 测点折算 (W)': col('P_solar'), '负载测点折算 (W)': col('P_load'),
                 '两测点差额 (W)': col('P_excess')}, 'W', minute_gap, window_start=start, window_end=end)
    write_chart('chart_shunt', 'TMD009 与 TMD017 电流差额 · 非分流实测', times,
                {'电流差额折算 (W)': col('P_shunt_diag')}, 'W', minute_gap, window_start=start, window_end=end)
    # 帆板端 ΣV×I − TMD017 差额图依赖太阳阵电压遥测 TMD003-008，
    # 2026-11-02 起该电压遥测判为不可用，图表已从清单移除，此处不再生成。
    write_chart('chart_soc_dod', '蓄电池状态 · SOC / DoD（电压映射估计）', times,
                {'SOC (%)': col('SOC'), 'DoD (%)': col('DoD')}, '%', minute_gap, low=0, high=100,
                reference=50, window_start=start, window_end=end)
    # 帆板电流（A）单独绘制；太阳阵电压遥测 TMD003-008 于 2026-11-02 判为不可用，不再出图。
    grouped = defaultdict(dict)
    for row in minute:
        if row['param_code'] in {f'TMD{i:03d}' for i in range(10, 16)}:
            grouped[row['t']][row['param_code']] = value(row['avg'])
    panel_times = sorted(grouped)
    points = {f'{chr(65 + i)}板 TMD{code:03d}':
              [grouped[t].get(f'TMD{code:03d}') for t in panel_times]
              for i, code in enumerate(range(10, 16))}
    write_chart('chart_solar_panels', '太阳阵 A—F 电流 · 实测分钟均值',
                panel_times, points, 'A', minute_gap, low=0, window_start=start, window_end=end)
    write_chart('chart_daily_power', '观测窗口 · 按小时已有分钟平均功率', hour_times,
                {'TMD017 测点 (W)': hourly['P_solar'], '负载 (W)': hourly['P_load'],
                 '测点差额 (W)': hourly['P_excess']}, 'W', hourly_gap, window_start=start, window_end=end)
    write_chart('chart_daily_soc', '观测窗口 · 按小时已有分钟平均 SOC', hour_times,
                {'SOC (%)': hourly['SOC']}, '%', hourly_gap, low=0, high=100, window_start=start, window_end=end)
    write_chart('chart_daily_shunt', '观测窗口 · 按小时 TMD009 与 TMD017 电流差额', hour_times,
                {'电流差额折算 (W)': hourly['P_shunt_diag']}, 'W', hourly_gap, window_start=start, window_end=end)
    write_chart('chart_energy_daily', '观测窗口 · 按小时已有分钟名义 60 秒等效能量', hour_times,
                {'TMD017 测点 (kWh)': energy['P_solar'], '负载 (kWh)': energy['P_load'],
                 '帆板与母线差额 (kWh)': energy['P_shunt_diag']}, 'kWh', hourly_gap, window_start=start, window_end=end)
    clean_orphan_svgs()
    embed_charts_in_html(minute, all_minute)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace-dir', type=Path,
                        help='卫星专属数据/产物目录，默认使用脚本所在目录')
    args = parser.parse_args()
    main(args.workspace_dir)
