#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""供配电设计表（表 5-32）× 电路图（图 5-38）× 遥测单机电流 的功率对照。

口径要点（与 config/design_load_table.json 的 _meta 一致）：
1. 设计表“功耗”为单台功耗，供电路数只是并联/冗余通道数，不作倍乘；
   同一单机列多行功耗表示不同工作模式（智算机 93.5/49），不叠加。
2. 供电电压取自电路图：一次母线负载按分钟实测母线电压 TMD016 折算，
   5 V / 12 V 负载按对应二次电源实测电压（TMD059 / TMD062）折算，
   不得把二次电源支路电流乘母线电压。
3. 支路电流的负值按零电流处理（解析式的零点偏置 B），不做绝对值翻转。
4. 工作态 = 实测功率 ≥ 单台设计值 × working_state_ratio（默认 10%），
   用于把占空比工作的单机（电推进）与常开单机分开统计。

本模块只做计算与静态 SVG，不写文件；由 gen_charts.py 调用并把结果放进 HTML。
"""
import bisect
import csv
import json
import math
import re
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / 'config' / 'design_load_table.json'
WIDTH = 980
PALETTE = ('#2563eb', '#059669', '#e58914', '#dc4667', '#7c3aed', '#0891b2')
INK = '#17243a'
MUTED = '#65748a'
GRID = '#e9eef6'
DESIGN_COLOR = '#1f3b66'

BLOCK_CATEGORY = {
    'no_port_current': ('配电端口无电流测点', '该路在 PCDU 只有开关状态或温度，没有电流采集'),
    'secondary_no_branch': ('二次电源无分路电流', 'PCDU 只测 5 V / 12 V 合路输出电压，没有分路电流'),
    'voltage_only': ('仅工作电压、无电流', '该单机下传工作电压，但没有电流测点'),
    'switch_only': ('仅开关状态', '只有加热器/继电器开关状态，无法折算功率'),
    'defined_not_downlinked': ('表内有定义、在轨无数据', '遥测表定义了电流检测代号，但库中查不到任何记录'),
    'candidate_unconfirmed': ('有候选测点、口径未确认', '存在同量级测点，但语义或统计口径不匹配，暂不计入'),
}

MATCH_LABEL = {
    'exact': '已对照',
    'partial': '部分覆盖',
    'approx': '名称近似',
    'none': '不可对照',
}


def load_table(path=CONFIG):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def _number(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _percentile(values, q):
    """线性插值分位（与 numpy 默认口径一致），输入须已排序。"""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return values[int(position)]
    return values[low] + (values[high] - values[low]) * (position - low)


def collect_minute_values(minute_rows, codes):
    """一次遍历分钟表：返回 {code: {t: avg}} 与 {code: 中文名}。"""
    wanted = set(codes)
    series = {code: {} for code in wanted}
    names = {}
    for row in minute_rows:
        code = row.get('param_code')
        if code not in wanted:
            continue
        if code not in names and row.get('param_name'):
            names[code] = row['param_name']
        amount = _number(row.get('avg'))
        if amount is not None:
            series[code][row['t']] = amount
    return series, names


def _branch_codes(measure):
    groups = measure.get('devices')
    if groups is None:
        branches = measure.get('branches') or []
        return [list(branches)] if branches else []
    return [list(group) for group in groups]


def _device_powers(row, series, rails, abs_series=None):
    """按电路图电压口径返回每台设备的 {t: W}；缺电压或缺全部支路电流的分钟不入序列。

    一台设备可以有多条并联/冗余支路（如智算机 F+G、路由器主+备、微波端机 A+B），
    同一设备的多条支路按分钟求和；不同设备各自成序列，比较时取代表性的一台。
    """
    measure = row.get('measure') or {}
    mode = measure.get('mode')
    if mode == 'vi_pair':
        voltage = series.get(measure.get('voltage'), {})
        current = series.get(measure.get('current'), {})
        return [{t: max(v, 0.0) * max(current[t], 0.0)
                 for t, v in voltage.items() if t in current}]
    if mode != 'rail_current':
        raise ValueError(f"未知测量模式: {mode}")
    rail = rails[row['rail']]
    voltage = series.get(rail['voltage_code'], {})
    current_source, _ = _current_source(row, series, abs_series)
    devices = []
    for group in _branch_codes(measure):
        branches = [current_source.get(code, {}) for code in group]
        power = {}
        for t, v in voltage.items():
            if v <= 0:
                continue
            total = 0.0
            observed = 0
            for branch in branches:
                current = branch.get(t)
                if current is None:
                    continue
                total += max(current, 0.0)
                observed += 1
            if observed:
                power[t] = v * total
        devices.append(power)
    return devices


def _mode_text(item):
    """表格/标题里的设计值写法：多模式并列，单模式直写。"""
    if len(item['modes']) > 1:
        return ' / '.join(f'{mode["W"]:g}' for mode in item['modes']) + ' W（两种模式）'
    return f'{item["mode_W"]:g} W'


ABS_CURRENT_FILE = 'data_abs_current_minute.csv'


def abs_current_codes(table=None):
    """配置里所有需要「绝对值」口径的电流代号（去重保序）。"""
    table = table or load_table()
    codes = []
    for row in table['rows']:
        measure = row.get('measure') or {}
        if measure.get('stat') != 'abs_current':
            continue
        for group in _branch_codes(measure):
            for code in group:
                if code not in codes:
                    codes.append(code)
    return codes


def load_abs_series(path, codes):
    """读取 s6_fetch_abs_current.py 输出的 mean(|I|) 分钟序列：{code: {t: A}}。"""
    path = Path(path)
    wanted = set(codes)
    series = {code: {} for code in wanted}
    if not path.exists() or not wanted:
        return series
    with path.open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            code = row.get('param_code')
            if code not in wanted:
                continue
            amount = _number(row.get('avg_abs'))
            if amount is not None:
                series[code][row['t']] = amount
    return series


def _current_source(row, series, abs_series):
    """双向电流测点用 mean(|I|) 序列；其余用分钟均值。缺绝对值序列时回退并标记。"""
    measure = row.get('measure') or {}
    if measure.get('stat') != 'abs_current':
        return series, False
    if abs_series and any(abs_series.get(code) for group in _branch_codes(measure)
                          for code in group):
        return abs_series, False
    return series, True


def _branch_powers(row, series, rails, abs_series=None):
    """每条支路各自的 {t: W}，供“每路一张图”使用；不做同机合路。"""
    measure = row.get('measure') or {}
    if measure.get('mode') == 'vi_pair':
        voltage = series.get(measure.get('voltage'), {})
        current = series.get(measure.get('current'), {})
        code = measure.get('current') or 'vi_pair'
        return {code: {t: max(v, 0.0) * max(current[t], 0.0)
                       for t, v in voltage.items() if t in current}}
    rail = rails[row['rail']]
    voltage = series.get(rail['voltage_code'], {})
    current_source, _ = _current_source(row, series, abs_series)
    powers = {}
    for group in _branch_codes(measure):
        for code in group:
            branch = current_source.get(code, {})
            powers[code] = {t: v * max(branch[t], 0.0)
                            for t, v in voltage.items() if t in branch and v > 0}
    return powers


def _describe_device(index, count):
    return f'第 {index + 1} 台' if count > 1 else ''


def summarize(minute_rows, table=None, with_series=True, abs_series=None):
    """返回逐行统计与整体口径信息，供 HTML/SVG 使用。

    设计值口径（2026-11-03 用户修正）：功耗列为单台功耗，供电路数不作倍乘；
    同一单机多个设计值视为不同工作模式（智算机 93.5/49），比较时取更接近实测的一种。
    多台同型单机各算各的，取实测功率最高的那台作为该行代表。
    """
    table = table or load_table()
    rails = table['rails']
    rows = table['rows']
    codes = []
    for row in rows:
        measure = row.get('measure') or {}
        for group in _branch_codes(measure):
            codes.extend(group)
        if measure.get('voltage'):
            codes.append(measure['voltage'])
        if measure.get('current'):
            codes.append(measure['current'])
    codes.extend(rail['voltage_code'] for rail in rails.values())
    for entry in table['_meta'].get('unmatched_telemetry', []):
        codes.append(entry['code'])
        codes.append(entry['voltage_code'])
    series, names = collect_minute_values(minute_rows, codes)
    total_minutes = len(series.get(rails['bus']['voltage_code'], {}))
    ratio = float(table['_meta'].get('working_state_ratio', 0.1))

    items = []
    for row in rows:
        modes = row.get('modes') or [{'label': '', 'W': float(row['design_W'])}]
        design_W = max(float(mode['W']) for mode in modes)
        threshold = design_W * ratio
        devices = _device_powers(row, series, rails, abs_series)
        stats = []
        for power in devices:
            values = sorted(power.values())
            working = [v for v in values if v >= threshold]
            stats.append({
                'power': power,
                'n': len(values),
                'median': _percentile(values, .5),
                'p95': _percentile(values, .95),
                'max': values[-1] if values else None,
                'work_n': len(working),
                'work_median': _percentile(working, .5),
                'work_p95': _percentile(working, .95),
            })
        # 代表设备：工作态 p95 最高的一台；都没有则取峰值最高的一台
        representative = max(stats, key=lambda row_stats: (
            row_stats['work_p95'] if row_stats['work_p95'] is not None else -1.0,
            row_stats['max'] if row_stats['max'] is not None else -1.0), default=None)
        if representative is None:
            representative = {'power': {}, 'n': 0, 'median': None, 'p95': None,
                              'max': None, 'work_n': 0, 'work_median': None,
                              'work_p95': None}
        total_stats = None
        if len(stats) > 1:
            moments = set()
            for power in devices:
                moments.update(power)
            totals = {t: sum(power.get(t, 0.0) for power in devices) for t in moments}
            values = sorted(totals.values())
            working = [v for v in values if v >= threshold]
            total_stats = {'n': len(values), 'median': _percentile(values, .5),
                           'p95': _percentile(values, .95),
                           'work_p95': _percentile(working, .95),
                           'max': values[-1] if values else None}
        routes = []
        for code, power in _branch_powers(row, series, rails, abs_series).items():
            values = sorted(power.values())
            working = [v for v in values if v >= threshold]
            routes.append({
                'code': code,
                'label': names.get(code) or code,
                'series': sorted(power.items()) if with_series else None,
                'median': _percentile(values, .5),
                'work_p95': _percentile(working, .95),
                'n': len(values),
            })
        work_p95 = representative['work_p95']
        if work_p95 is None:
            matched = modes[0]
        else:
            matched = min(modes, key=lambda mode: abs(work_p95 - float(mode['W'])))
        mode_W = float(matched['W'])
        item = {
            'no': row['no'],
            'name': row['name'],
            'subsystem': row['subsystem'],
            'routes': row['routes'],
            'modes': [{'label': mode.get('label', ''), 'W': float(mode['W'])} for mode in modes],
            'design_W': design_W,
            'mode_W': mode_W,
            'mode_label': matched.get('label', ''),
            'rail': row['rail'],
            'rail_name': rails[row['rail']]['name'],
            'switch': row['switch'],
            'match': row['match'],
            'match_label': MATCH_LABEL.get(row['match'], row['match']),
            'blocked': row.get('blocked'),
            'note': row.get('note', ''),
            'vi_pair': (row.get('measure') or {}).get('mode') == 'vi_pair',
            'branches': ([(row.get('measure') or {}).get('voltage'),
                          (row.get('measure') or {}).get('current')]
                         if (row.get('measure') or {}).get('mode') == 'vi_pair'
                         else [code for group in _branch_codes(row.get('measure') or {})
                               for code in group]),
            'n_devices': len(stats),
            'device_label': _describe_device(stats.index(representative), len(stats)) if stats else '',
            'device_stats': [{'n': row_stats['n'], 'work_p95': row_stats['work_p95'],
                              'median': row_stats['median']} for row_stats in stats],
            'n': representative['n'],
            'coverage': (representative['n'] / total_minutes) if total_minutes else None,
            'median': representative['median'],
            'p95': representative['p95'],
            'max': representative['max'],
            'work_n': representative['work_n'],
            'work_median': representative['work_median'],
            'work_p95': work_p95,
            'total': total_stats,
            'working_ratio': ratio,
            'route_list': routes,
            'series': sorted(representative['power'].items()) if with_series else None,
        }
        item['ratio'] = (work_p95 / mode_W) if (work_p95 is not None and mode_W) else None
        items.append(item)

    unmatched = []
    for entry in table['_meta'].get('unmatched_telemetry', []):
        voltage = series.get(entry['voltage_code'], {})
        current = series.get(entry['code'], {})
        power = {t: max(v, 0.0) * max(current[t], 0.0)
                 for t, v in voltage.items() if t in current and v > 0}
        values = sorted(power.values())
        unmatched.append({
            'code': entry['code'],
            'name': entry['name'],
            'voltage_code': entry['voltage_code'],
            'note': entry.get('note', ''),
            'n': len(values),
            'median': _percentile(values, .5),
            'p95': _percentile(values, .95),
        })
    measured = [item for item in items if item['n'] and item['work_p95'] is not None]
    return {
        'satellite_id': table['satellite_id'],
        'rails': rails,
        'total_minutes': total_minutes,
        'working_state_ratio': ratio,
        'items': items,
        'measured': measured,
        'n_rows': len(items),
        'design_rows': int(table['_meta'].get('design_rows', len(items))),
        'n_measured': len(measured),
        'blocked': [item for item in items if item['match'] == 'none'],
        'unmatched': unmatched,
        'design_total_W': sum(item['design_W'] for item in items),
        'design_compare_W': sum(item['mode_W'] for item in measured),
        'work_median_sum_W': sum(item['work_median'] for item in measured),
        'work_p95_sum_W': sum(item['work_p95'] for item in measured),
    }


# --------------------------------------------------------------------------
# 图表：全部静态 SVG，白底浅色，与报告内其它 SVG 同一视觉语言
# --------------------------------------------------------------------------
def _log_x(value, low, high, left, right):
    value = max(min(value, high), low)
    return left + ((math.log10(value) - math.log10(low)) /
                   (math.log10(high) - math.log10(low))) * (right - left)


def _linear_x(value, low, high, left, right):
    value = min(max(value, low), high)
    return left + (value - low) / (high - low) * (right - left)


def _header(subtitle, width):
    """图内只放副标题；主标题由所在 panel 的 h3 给出，避免重复。"""
    return [
        f'<rect width="100%" height="100%" fill="white"/>',
        f'<text x="20" y="22" font-size="11" fill="{MUTED}">{escape(subtitle)}</text>',
    ]


def ratio_chart(summary, width=WIDTH):
    """实测工作 p95 ÷ 单台设计值（多模式取最接近的一种）的横向条图。"""
    items = [item for item in summary['measured'] if item['ratio'] is not None]
    items.sort(key=lambda item: -(item['ratio'] or 0))
    if not items:
        return ''
    row_h = 32
    top = 52
    height = top + row_h * len(items) + 58
    left, right = 252, width - 92
    limit = max(160.0, math.ceil(max(item['ratio'] for item in items) * 100 / 20) * 20)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
             f'role="img" aria-label="实测/设计功耗比">',
             '<title>实测功耗与设计功耗比值</title>']
    parts.extend(_header('工作态 p95 ÷ 单台设计值（多模式取最接近的一种）；100% 表示实测恰好达到设计值', width))
    ticks = sorted({tick for tick in (0, 40, 80, 100, 120, 160) if tick <= limit})
    for tick in ticks:
        x = _linear_x(tick, 0, limit, left, right)
        dash = ' stroke-dasharray="5 4"' if tick == 100 else ''
        color = '#dc4667' if tick == 100 else GRID
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top - 8}" y2="{top + row_h * len(items) - 8}" '
                     f'stroke="{color}"{dash}/>')
        if tick != 100:  # 100% 处由红色虚线与图例说明，避免刻度文字被线穿过
            parts.append(f'<text x="{x:.1f}" y="{height - 34}" text-anchor="middle" font-size="10" '
                         f'fill="{MUTED}">{tick}%</text>')
    for index, item in enumerate(items):
        y = top + index * row_h
        dark = item['ratio'] >= 1
        ratio_pct = item['ratio'] * 100
        median_pct = (item['work_median'] / item['mode_W'] * 100
                      if item['work_median'] is not None else 0)
        parts.append(f'<text x="{left - 12}" y="{y + 12}" text-anchor="end" font-size="11.5" '
                     f'fill="{INK}">{escape(item["name"])}</text>')
        subtitle = f'设计 {item["mode_W"]:g} W'
        if item['mode_label']:
            subtitle += f' · {item["mode_label"]}'
        others = [mode for mode in item['modes']
                  if abs(float(mode['W']) - item['mode_W']) > 1e-9]
        if others and item['work_p95'] is not None:
            subtitle += '｜' + '／'.join(
                f'{float(mode["W"]):g} W→{item["work_p95"] / float(mode["W"]) * 100:.0f}%'
                for mode in others)
        parts.append(f'<text x="{left - 12}" y="{y + 25}" text-anchor="end" font-size="9.5" '
                     f'fill="{MUTED}">{escape(subtitle)}</text>')
        x_p95 = _linear_x(ratio_pct, 0, limit, left, right)
        x_median = _linear_x(median_pct, 0, limit, left, right)
        parts.append(f'<rect x="{left}" y="{y + 2}" width="{max(x_p95 - left, 0):.1f}" height="6" '
                     f'rx="2" fill="#bcd4f7"/>')
        parts.append(f'<rect x="{left}" y="{y + 10}" width="{max(x_median - left, 0):.1f}" height="7" '
                     f'rx="2" fill="#2563eb"/>')
        parts.append(f'<text x="{right + 8}" y="{y + 11}" font-size="11" '
                     f'font-weight="600" fill="{"#c02646" if dark else "#1d4ed8"}">'
                     f'{ratio_pct:.0f}%</text>')
        parts.append(f'<text x="{right + 8}" y="{y + 23}" font-size="9.5" fill="{MUTED}">'
                     f'{item["work_p95"]:.1f} W</text>')
    legend_y = height - 10
    parts.extend([
        f'<rect x="{left}" y="{legend_y - 9}" width="10" height="6" rx="2" fill="#bcd4f7"/>',
        f'<text x="{left + 15}" y="{legend_y - 2}" font-size="10" fill="{MUTED}">工作中位</text>',
        f'<rect x="{left + 85}" y="{legend_y - 9}" width="10" height="6" rx="2" fill="#2563eb"/>',
        f'<text x="{left + 100}" y="{legend_y - 2}" font-size="10" fill="{MUTED}">工作 p95</text>',
        f'<line x1="{left + 170}" x2="{left + 181}" y1="{legend_y - 6}" y2="{legend_y - 6}" '
        f'stroke="#dc4667" stroke-dasharray="4 3"/>',
        f'<text x="{left + 186}" y="{legend_y - 2}" font-size="10" fill="{MUTED}">设计值 100%</text>',
    ])
    parts.append('</svg>')
    return '\n'.join(parts)


def power_chart(summary, width=WIDTH):
    """设计功耗与实测中位/p95/峰值的横向哑铃图，横轴为对数功率。"""
    items = [item for item in summary['measured'] if item['max'] is not None]
    items.sort(key=lambda item: -item['mode_W'])
    if not items:
        return ''
    row_h = 40
    top = 58
    height = top + row_h * len(items) + 56
    left, right = 208, width - 118
    low, high = 0.5, max(1200.0, math.ceil(max(item['max'] for item in items) / 100) * 100)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
             f'role="img" aria-label="设计功耗与实测功率对照">',
             '<title>设计功耗与实测功率对照</title>']
    parts.extend(_header('横轴对数刻度；实测为支路电流 × 电路图对应电压的分钟功率代理', width))
    for decade in (1, 10, 100, 1000):
        if low <= decade <= high:
            for factor in (1, 3):
                tick = decade * factor
                if tick > high:
                    continue
                x = _log_x(tick, low, high, left, right)
                parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top - 10}" '
                             f'y2="{top + row_h * len(items) - 12}" stroke="{GRID}"/>')
                parts.append(f'<text x="{x:.1f}" y="{height - 30}" text-anchor="middle" font-size="10" '
                             f'fill="{MUTED}">{tick:g} W</text>')
    for index, item in enumerate(items):
        y = top + index * row_h
        x_design = _log_x(item['mode_W'], low, high, left, right)
        x_median = _log_x(item['work_median'], low, high, left, right)
        x_p95 = _log_x(item['work_p95'], low, high, left, right)
        x_max = _log_x(item['max'], low, high, left, right)
        parts.append(f'<text x="{left - 12}" y="{y + 4}" text-anchor="end" font-size="11.5" '
                     f'fill="{INK}">{escape(item["name"])}</text>')
        parts.append(f'<text x="{left - 12}" y="{y + 17}" text-anchor="end" font-size="9.5" '
                     f'fill="{MUTED}">{escape(item["rail_name"])} · 设计 {item["mode_W"]:g} W</text>')
        parts.append(f'<line x1="{min(x_design, x_p95):.1f}" x2="{max(x_design, x_p95):.1f}" '
                     f'y1="{y - 3}" y2="{y - 3}" stroke="#c8d0e0" stroke-width="2"/>')
        parts.append(f'<circle cx="{x_max:.1f}" cy="{y - 3}" r="5" fill="none" stroke="#dc4667" '
                     f'stroke-dasharray="2.5 2"/>')
        parts.append(f'<circle cx="{x_median:.1f}" cy="{y - 3}" r="3" fill="#9ec1f0"/>')
        parts.append(f'<circle cx="{x_p95:.1f}" cy="{y - 3}" r="4.6" fill="#2563eb"/>')
        for mode in item['modes']:
            x_mode = _log_x(float(mode['W']), low, high, left, right)
            if abs(float(mode['W']) - item['mode_W']) < 1e-9:
                parts.append(f'<path d="M{x_mode:.1f},{y - 9} L{x_mode + 6:.1f},{y - 3} '
                             f'L{x_mode:.1f},{y + 3} L{x_mode - 6:.1f},{y - 3} Z" '
                             f'fill="{DESIGN_COLOR}"/>')
            else:
                # 其他工作模式：空心菱形，只提示存在另一种设计值
                parts.append(f'<path d="M{x_mode:.1f},{y - 9} L{x_mode + 6:.1f},{y - 3} '
                             f'L{x_mode:.1f},{y + 3} L{x_mode - 6:.1f},{y - 3} Z" '
                             f'fill="white" stroke="{DESIGN_COLOR}" stroke-width="1.4"/>')
        ratio = item['ratio'] or 0
        parts.append(f'<text x="{right + 14}" y="{y - 1}" font-size="11" font-weight="600" '
                     f'fill="{"#c02646" if ratio >= 1 else "#1d4ed8"}">{ratio * 100:.0f}%</text>')
        parts.append(f'<text x="{right + 14}" y="{y + 12}" font-size="9.5" fill="{MUTED}">'
                     f'{item["work_p95"]:.1f} W</text>')
    legend = [('diamond', DESIGN_COLOR, '设计功耗'),
              ('dot', '#9ec1f0', '工作中位'),
              ('dot', '#2563eb', '工作 p95'),
              ('ring', '#dc4667', '实测峰值')]
    if any(len(item['modes']) > 1 for item in items):
        legend.append(('hollow', DESIGN_COLOR, '其他模式'))
    legend_x = left
    legend_y = height - 10
    for kind, color, label in legend:
        if kind == 'diamond':
            parts.append(f'<path d="M{legend_x},{legend_y - 9} L{legend_x + 5},{legend_y - 4} '
                         f'L{legend_x},{legend_y + 1} L{legend_x - 5},{legend_y - 4} Z" fill="{color}"/>')
        elif kind == 'ring':
            parts.append(f'<circle cx="{legend_x}" cy="{legend_y - 4}" r="4" fill="none" '
                         f'stroke="{color}" stroke-dasharray="2.5 2"/>')
        elif kind == 'hollow':
            parts.append(f'<path d="M{legend_x},{legend_y - 9} L{legend_x + 5},{legend_y - 4} '
                         f'L{legend_x},{legend_y + 1} L{legend_x - 5},{legend_y - 4} Z" '
                         f'fill="white" stroke="{color}" stroke-width="1.4"/>')
        else:
            parts.append(f'<circle cx="{legend_x}" cy="{legend_y - 4}" r="4" fill="{color}"/>')
        parts.append(f'<text x="{legend_x + 10}" y="{legend_y}" font-size="10" fill="{MUTED}">'
                     f'{escape(label)}</text>')
        legend_x += 108
    parts.append('</svg>')
    return '\n'.join(parts)


def select_series_window(item, half_minutes=60, min_points=8, fallback_minutes=180):
    """取该单机“工作分钟最密集”的窗口：以每个观测分钟为中心试 half_minutes 半宽，
    先比窗口内工作分钟数，再比窗口内功率和，最后比点数；点数不足时扩到 fallback。

    返回 {start, end, points, half_minutes, active}；start/end 为窗口内实际观测的首末分钟，
    横轴不出现整段空白。这样每台单机的曲线都落在它确有数据的时段，而不是整段待机窗口。
    """
    points = item.get('series') or []
    if not points:
        return None
    threshold = item['design_W'] * item.get('working_ratio', 0.1)
    stamps = [datetime.fromisoformat(point[0]) for point in points]
    active_prefix, energy_prefix = [0], [0.0]
    for _, watt in points:
        active_prefix.append(active_prefix[-1] + (1 if watt >= threshold else 0))
        energy_prefix.append(energy_prefix[-1] + watt)

    def window(center_index, half):
        left = bisect.bisect_left(stamps, stamps[center_index] - timedelta(minutes=half))
        right = bisect.bisect_right(stamps, stamps[center_index] + timedelta(minutes=half))
        score = (active_prefix[right] - active_prefix[left],
                 energy_prefix[right] - energy_prefix[left],
                 right - left)
        return score, left, right

    for half in (half_minutes, fallback_minutes):
        best = None
        for index in range(len(points)):
            score, left, right = window(index, half)
            if best is None or score > best[0]:
                best = (score, left, right)
        _, left, right = best
        chosen = points[left:right]
        if len(chosen) >= min_points or half == fallback_minutes:
            break
    if not chosen:
        return None
    # 裁到实际工作分钟前后各 10 分钟，避免整段待机分钟把曲线压成贴底直线
    working = [point for point in chosen if point[1] >= threshold]
    if working:
        first = datetime.fromisoformat(working[0][0]) - timedelta(minutes=10)
        last = datetime.fromisoformat(working[-1][0]) + timedelta(minutes=10)
        chosen = [point for point in chosen
                  if first <= datetime.fromisoformat(point[0]) <= last]
    return {'start': chosen[0][0], 'end': chosen[-1][0], 'points': chosen,
            'half_minutes': half, 'active': sum(1 for point in chosen
                                                if point[1] >= threshold)}


def unit_series_chart(item, segment=None, width=WIDTH, gap_seconds=300):
    """单机实测功率曲线 + 设计值水平虚线；缺测与时间断档处断线。"""
    segment = segment or select_series_window(item)
    points = segment['points'] if segment else []
    if not points:
        return ''
    start, end = segment['start'], segment['end']
    t0, t1 = datetime.fromisoformat(start).timestamp(), datetime.fromisoformat(end).timestamp()
    if t1 <= t0:
        return ''
    design = float(item['mode_W'])
    modes = item.get('modes') or [{'label': '', 'W': design}]
    values = [value for _, value in points]
    high = max(max(values), max(float(mode['W']) for mode in modes)) * 1.12
    high = max(high, 1.0)
    height = 340
    left, right, top, bottom = 70, 28, 58, 54
    plot_w, plot_h = width - left - right, height - top - bottom
    X = lambda stamp: left + (datetime.fromisoformat(stamp).timestamp() - t0) / (t1 - t0) * plot_w
    Y = lambda value: top + (high - value) / high * plot_h
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
             f'role="img" aria-label="{escape(item["name"])} 实测功率与设计值">',
             f'<title>{escape(item["name"])} 实测功率与设计值</title>',
             '<rect width="100%" height="100%" fill="white"/>']
    # 图例（顶部）
    parts.extend([
        f'<line x1="{left}" x2="{left + 22}" y1="16" y2="16" stroke="#2563eb" stroke-width="2.2"/>',
        f'<text x="{left + 28}" y="20" font-size="11" fill="{MUTED}">实测功率（分钟功率代理）</text>',
        f'<line x1="{left + 190}" x2="{left + 212}" y1="16" y2="16" stroke="#dc4667" '
        f'stroke-width="1.8" stroke-dasharray="6 4"/>',
        f'<text x="{left + 218}" y="20" font-size="11" fill="{MUTED}">设计值 '
        f'{design:g} W{escape("（" + item["mode_label"] + "）") if item.get("mode_label") else ""}'
        f'</text>',
    ])
    legend_x = left + 330
    for mode in modes:
        if abs(float(mode['W']) - design) < 1e-9:
            continue
        parts.extend([
            f'<line x1="{legend_x}" x2="{legend_x + 22}" y1="16" y2="16" stroke="#e59aa9" '
            f'stroke-width="1.6" stroke-dasharray="3 4"/>',
            f'<text x="{legend_x + 28}" y="20" font-size="11" fill="{MUTED}">另一模式 '
            f'{float(mode["W"]):g} W'
            f'{escape("（" + mode["label"] + "）") if mode.get("label") else ""}</text>',
        ])
        legend_x += 190
    # 纵轴网格与刻度（量级小时保留一位小数，避免出现重复刻度文字）
    digits = 1 if high < 10 else 0
    for index in range(5):
        value = high * index / 4
        y = Y(value)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" '
                     f'stroke="{GRID}"/>')
        parts.append(f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end" font-size="10.5" '
                     f'fill="{MUTED}">{value:.{digits}f}</text>')
    parts.append(f'<text x="18" y="{top + plot_h / 2}" text-anchor="middle" font-size="10.5" '
                 f'fill="{MUTED}" transform="rotate(-90 18 {top + plot_h / 2})">功率 W</text>')
    # 时间轴
    span_hours = (t1 - t0) / 3600
    label_fmt = '%m-%d %H:%M' if span_hours <= 72 else '%m-%d'
    for index in range(6):
        moment = t0 + (t1 - t0) * index / 5
        x = left + plot_w * index / 5
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{top + plot_h}" '
                     f'stroke="{GRID}"/>')
        parts.append(f'<text x="{x:.1f}" y="{height - 30}" text-anchor="middle" font-size="10" '
                     f'fill="{MUTED}">{datetime.fromtimestamp(moment).strftime(label_fmt)}</text>')
    parts.append(f'<text x="{left + plot_w / 2:.1f}" y="{height - 10}" text-anchor="middle" '
                 f'font-size="10.5" fill="{MUTED}">星上北京时间</text>')
    # 设计值虚线：比较所用的模式用深红长虚线，其它工作模式用浅红短虚线
    for mode in sorted(modes, key=lambda mode: float(mode['W'])):
        mode_w = float(mode['W'])
        design_y = Y(mode_w)
        primary = abs(mode_w - design) < 1e-9
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{design_y:.1f}" '
                     f'y2="{design_y:.1f}" stroke="{"#dc4667" if primary else "#e59aa9"}" '
                     f'stroke-width="{1.8 if primary else 1.3}" '
                     f'stroke-dasharray="{"6 4" if primary else "3 4"}"/>')
        label = f'设计 {mode_w:g} W' + (f'（{mode["label"]}）' if mode.get('label') else '')
        parts.append(f'<text x="{width - right - 4}" y="{design_y - 6:.1f}" text-anchor="end" '
                     f'font-size="10.5" fill="{"#c02646" if primary else "#b06a78"}">'
                     f'{escape(label)}</text>')
    # 实测曲线：跨断档不连线
    commands, previous = [], None
    sparse = len(points) < 400
    for stamp, value in points:
        x, y = X(stamp), Y(value)
        move = previous is None or (datetime.fromisoformat(stamp).timestamp() - previous) > gap_seconds
        commands.append(f'{"M" if move else "L"}{x:.1f},{y:.1f}')
        previous = datetime.fromisoformat(stamp).timestamp()
    parts.append(f'<path d="{" ".join(commands)}" fill="none" stroke="#2563eb" stroke-width="1.4" '
                 'stroke-linejoin="round"/>')
    if sparse:
        for stamp, value in points:
            parts.append(f'<circle cx="{X(stamp):.1f}" cy="{Y(value):.1f}" r="2.1" '
                         'fill="#2563eb" opacity="0.75"/>')
    parts.append('</svg>')
    return '\n'.join(parts)


# --------------------------------------------------------------------------
# HTML 片段
# --------------------------------------------------------------------------
def _fmt(value, digits=2):
    return '—' if value is None else f'{value:.{digits}f}'


RAIL_SHORT = {'bus': '母线', 'rail_12v': '12 V', 'rail_5v': '5 V'}


def _compact_codes(codes):
    """TMD031、TMD032… → TMD031–034；TMK6821… → TMK6821–6824。"""
    if not codes:
        return '—'
    head = re.fullmatch(r'([A-Z]{3})(\d{3,4})', codes[0])
    if head is None:
        return '、'.join(codes)
    prefix, width = head.group(1), len(head.group(2))
    pattern = re.compile(rf'{prefix}(\d{{{width}}})')
    numbers = []
    for code in codes:
        match = pattern.fullmatch(code)
        if match is None:
            return '、'.join(codes)
        numbers.append(int(match.group(1)))
    numbers.sort()
    spans = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        spans.append((start, previous))
        start = previous = number
    spans.append((start, previous))
    def label(low, high):
        if low == high:
            return f'{prefix}{low:0{width}d}'
        # 与旧写法保持一致：TMD031–034，后半段省略相同前缀
        return f'{prefix}{low:0{width}d}–{high:0{width}d}'

    return '、'.join(label(low, high) for low, high in spans)


def _category_counts(summary):
    """不可对照行的类型分布：{category: 行数}。"""
    counts = {}
    for item in summary.get('blocked') or []:
        key = (item.get('blocked') or {}).get('category')
        counts[key] = counts.get(key, 0) + 1
    return counts


def _category_spread(summary):
    """“配电端口无电流测点 3 行、二次电源无分路电流 2 行……”一行式描述。"""
    counts = _category_counts(summary)
    # 类别名里本身有顿号（“仅工作电压、无电流”），用分号分隔才不歧义
    return '；'.join(
        f'{BLOCK_CATEGORY.get(key, (key, ""))[0]} {number} 行'
        for key, number in sorted(counts.items(), key=lambda pair: -pair[1]))


def _blocked_summary_text(summary):
    """“未覆盖 N 行”段落：名单与类型分布随配置变化，不写死行数。"""
    items = summary.get('blocked') or []
    if not items:
        return ''
    names = '、'.join(item['name'] for item in items)
    spread = _category_spread(summary)
    extra = ''
    if any((item.get('blocked') or {}).get('category') == 'switch_only'
           for item in items):
        extra = ('其中加热回路 250 W 为全表最大非推进负载，'
                 '仅凭 48 路开关状态无法折算功率，需要加热器阻值或功率遥测才能补上。')
    return (f'<p><strong>未覆盖的 {len(items)} 行：</strong>{escape(names)}。'
            f'它们并非同一种“缺测”：{escape(spread)}。'
            '逐行依据（现有遥测、缺什么、怎么补）见上文“不可对照逐行核查”。'
            f'{extra}</p>')


def html_section(summary, svg_ratio, svg_power):
    items = summary['items']
    blocked_count = len(summary.get('blocked') or [])
    measured = summary['measured']
    over = [item for item in measured if item['ratio'] and item['ratio'] >= 1]
    near = [item for item in measured if item['ratio'] and 0.8 <= item['ratio'] < 1]
    low = [item for item in measured if item['ratio'] and item['ratio'] < 0.8]
    parts = [
        '<section class="report-section" id="design-compare">',
        '<div class="section-head"><h2>设计功耗对照</h2>'
        '<p>供配电设计表 × 电路图供电电压 × 遥测单机电流</p></div>',
        '<div class="panel method-panel">',
        '<h3>口径 <span class="badge">表 5-32 / 图 5-38</span></h3>',
        '<p>本节的单机功率不再统一乘母线电压，而是按<strong>图 5-38 供配电框图标注的供电电压</strong>折算：'
        '一次母线负载（23.1–29.2 V）用同分钟实测母线电压 <code>TMD016</code>；'
        '5 V 二次电源负载（光纤陀螺、MEMS 陀螺、星敏、磁强计）用 <code>TMD059</code>；'
        '12 V 二次电源负载（微波天线、磁力矩器、X 频段上行螺旋天线）用 <code>TMD062</code>；'
        '测传一体机与测控应答机 A/B 机经确认同样由 12 V 二次电源供电——设计表这三处“电压”列'
        '写 23.1~29.2 V，与实际不符（待勘误）——电压取各自分系统遥测 '
        '<code>TMC121</code> / <code>TMC020</code> / <code>TMC050</code>（实测 11.65–11.75 V），'
        '不乘母线电压。'
        '光纤陀螺一项若沿用母线电压口径，实测功率会被高估约 5.5 倍。</p>',
        '<p><strong>设计功耗口径：</strong>表 5-32 的“功耗/W”是<strong>单台功耗</strong>，'
        '“供电路数”只是并联/冗余通道数，<strong>不作倍乘</strong>（全表单台合计 '
        f'{summary["design_total_W"]:.1f} W）。同一单机列多行功耗时是<strong>不同工作模式</strong>：'
        '智算机-智加 NX1 的 93.5 W（高性能）与 49 W（常规）不叠加，实测取更接近的一种作比较值。'
        '同一行含多台同型单机时（微波 2 端机、激光 2 台、高性能 2/3 模块、飞轮 4 台），'
        '按单台比较并<strong>取实测最大的那一台</strong>，同样不相加。</p>',
        '<p><strong>统计口径：</strong>本节使用<strong>全部已观测分钟</strong>'
        f'（{summary["total_minutes"]:,} 个有母线电压的分钟），不限于遥测图表所用的单段连续窗口。'
        '分钟功率 = 电压分钟均值 × 电流分钟均值，未验证逐采样同步乘积；'
        '支路电流负值按零电流处理（解析式零点偏置）；'
        f'“工作态”指实测功率 ≥ 单台设计值 × {summary["working_state_ratio"]:.0%} 的分钟，'
        '用于把电推进这类占空比工作的单机与常开单机分开。</p>',
        f'<p><strong>覆盖情况：</strong>设计表 {summary["design_rows"]} 行'
        f'（序号 1、2 合并为一行）中 {summary["n_measured"]} 行有可用电流遥测，'
        f'{blocked_count} 行不可对照。'
        + (f'这 {blocked_count} 行的原因并不相同——{_category_spread(summary)}，'
           '逐行依据见下文“不可对照逐行核查”。' if blocked_count else '')
        + ''
        f'可对照项的单台设计值合计 {summary["design_compare_W"]:.1f} W；'
        f'实测工作态中位逐项相加 {summary["work_median_sum_W"]:.1f} W、'
        f'工作态 p95 逐项相加 {summary["work_p95_sum_W"]:.1f} W。'
        '逐项分位相加不等于同一时刻的整星负荷，只用于量级核对。</p>',
        '</div>',
        '<div class="panel"><h3>逐行对照 <span class="badge">'
        f'{summary["n_measured"]} 行可对照</span></h3>'
        '<div class="table-scroll"><table class="data-table compact-table design-table">'
        '<thead><tr><th>序号</th><th>单机</th><th>供电</th>'
        '<th>设计功耗 W</th><th>遥测支路</th>'
        '<th>工作中位 W</th><th>工作 p95 W</th><th>实测/设计</th><th>状态</th>'
        '</tr></thead><tbody>',
    ]
    for item in items:
        if len(item['modes']) > 1:
            design = (' / '.join(f'{mode["W"]:g}' for mode in item['modes'])
                      + '<span class="cell-sub">（两种模式）</span>')
        else:
            design = f'{item["mode_W"]:g}'
        if item['vi_pair'] and len(item['branches']) == 2:
            branches = (f'{escape(item["branches"][0])}（V）/ '
                        f'{escape(item["branches"][1])}（I）')
        else:
            branches = _compact_codes(item['branches'])
        if item['n_devices'] > 1:
            branches = f'{branches}<span class="cell-sub">（{item["n_devices"]} 台中最大）</span>'
        ratio = f'{item["ratio"] * 100:.0f}%' if item['ratio'] is not None else '—'
        if len(item['modes']) > 1 and item['work_p95'] is not None:
            others = [mode for mode in item['modes']
                      if abs(mode['W'] - item['mode_W']) > 1e-9]
            ratio = (f'{ratio}<span class="cell-sub">对{item["mode_label"]} '
                     + f'{item["mode_W"]:g} W</span>')
            if others:
                ratio += ('<span class="cell-sub">｜'
                          + '／'.join(f'{item["work_p95"] / mode["W"] * 100:.0f}% 对'
                                     f'{escape(mode["label"])} {mode["W"]:g} W'
                                     for mode in others)
                          + '</span>')
        ratio_class = ' class="ratio-over"' if (item['ratio'] or 0) >= 1 else ''
        p95_cell = _fmt(item['work_p95'], 1)
        if item.get('total'):
            p95_cell += (f'<span class="cell-sub">合计 {item["total"]["work_p95"]:.1f} W'
                         f'（{item["n_devices"]} 台）</span>')
        parts.append(
            '<tr>'
            f'<td>{escape(item["no"])}</td>'
            f'<td>{escape(item["name"])}'
            f'<span class="cell-sub">{escape(item["subsystem"])} · {item["routes"]} 路</span></td>'
            f'<td>{RAIL_SHORT.get(item["rail"], escape(item["rail_name"]))}</td>'
            f'<td>{design}</td>'
            f'<td class="cell-code">{branches}</td>'
            f'<td>{_fmt(item["work_median"], 1)}</td>'
            f'<td>{p95_cell}</td>'
            f'<td{ratio_class}>{ratio}</td>'
            f'<td class="cell-state">{escape(item["match_label"])}</td>'
            '</tr>')
    parts.extend([
        '</tbody></table></div>',
        '<p class="section-tip">“供电”列为图 5-38 的供电电压：<strong>母线</strong> = 一次母线 23.1–29.2 V，'
        '按同分钟实测 TMD016 折算；<strong>5 V / 12 V</strong> = 经 DC/DC 的二次电源，'
        '按 TMD059 / TMD062 实测电压折算。“设计功耗”直接用表 5-32 表列值，'
        '不乘供电路数；多行同名单机按工作模式并列，不叠加。'
        '“遥测支路”标注“N 台中最大”的行，实测统计取功率最高的那一台，不是各台相加。'
        '标“不可对照”的行只列设计值、不做估算，逐行原因见下一张表。</p>',
        '<details class="aux-details"><summary>逐行说明与测点对应依据</summary>'
        '<div class="table-scroll"><table class="data-table compact-table">'
        '<thead><tr><th>序号</th><th>单机</th><th>说明</th></tr></thead><tbody>',
    ])
    for item in items:
        parts.append(f'<tr><td>{escape(item["no"])}</td><td>{escape(item["name"])}</td>'
                     f'<td class="cell-note">{escape(item["note"])}</td></tr>')
    parts.append('</tbody></table></div></details></div>')
    # ---- 不可对照逐行核查 ----
    blocked = summary.get('blocked') or []
    unmatched = [entry for entry in (summary.get('unmatched') or []) if entry['n']]
    if blocked:
        parts.append('<div class="panel"><h3>不可对照逐行核查 '
                     f'<span class="badge">{len(blocked)} 行</span></h3>')
        counts = _category_counts(summary)
        kinds = [BLOCK_CATEGORY.get(key, (key, ''))[0]
                 for key, _ in sorted(counts.items(), key=lambda pair: -pair[1])]
        parts.append(f'<p class="body-copy">这 {len(blocked)} 行不是笼统的“没有测点”，'
                     f'而是 {len(counts)} 类不同情况：{escape("；".join(kinds))}。'
                     '下表逐行给出该设备在库里究竟能看到什么、缺哪个量、怎么补。</p>')
        parts.append('<ul class="block-summary">')
        for key, number in sorted(counts.items(), key=lambda pair: -pair[1]):
            label, note = BLOCK_CATEGORY.get(key, (key, ''))
            parts.append(f'<li><b>{escape(label)}</b>（{number} 行）：{escape(note)}</li>')
        parts.append('</ul>')
        parts.append('<div class="table-scroll"><table class="data-table compact-table block-table">'
                     '<thead><tr><th>序号</th><th>单机</th><th>设计 W</th><th>判定</th>'
                     '<th>该设备现有的遥测</th><th>缺什么、怎么补</th></tr></thead><tbody>')
        for item in blocked:
            info = item.get('blocked') or {}
            label, _ = BLOCK_CATEGORY.get(info.get('category'),
                                          (info.get('category') or '—', ''))
            seen = '；'.join(info.get('seen') or []) or '—'
            detail = info.get('detail') or ''
            fix = info.get('fix') or ''
            parts.append(
                '<tr>'
                f'<td>{escape(item["no"])}</td>'
                f'<td>{escape(item["name"])}'
                f'<span class="cell-sub">{RAIL_SHORT.get(item["rail"], escape(item["rail_name"]))}'
                f' · {item["routes"]} 路</span></td>'
                f'<td>{item["design_W"]:g}</td>'
                f'<td class="cell-block{" cell-block-soft" if info.get("category") == "candidate_unconfirmed" else ""}">'
                f'{escape(label)}</td>'
                f'<td class="cell-note">{escape(seen)}</td>'
                f'<td class="cell-note">{escape(detail)}'
                + (f'<span class="cell-fix">补：{escape(fix)}</span>' if fix else '')
                + '</td></tr>')
        parts.append('</tbody></table></div>')
        parts.append('<p class="section-tip">判定依据来自 2026-11-03 的全库核查：'
                     'PCDU 遥测表（0C2H / 0C4H）只对 9 个配电端口设了电流采集，'
                     '其余端口仅电压、开关状态或温度；分系统遥测表虽有电流项，'
                     '但多数与设计表不是同一截面（见下）。</p>')
        parts.append('</div>')

    if unmatched:
        parts.append('<div class="panel"><h3>反向缺口 '
                     '<span class="badge">有实测、无设计行</span></h3>')
        parts.append('<p class="body-copy">完整对账要双向看。设计表 22 行之外，'
                     f'库里还有 {len(unmatched)} 个设备级电流测点没有被任何一行接住——'
                     '缺口不只是“遥测少了”，设计表本身也没有覆盖全部用电设备。'
                     '这些设备的实测值按下表列出，仅作量级参考，不参与本节比值。</p>')
        parts.append('<div class="table-scroll"><table class="data-table compact-table block-table">'
                     '<thead><tr><th>遥测代号</th><th>名称</th><th>取压代号</th>'
                     '<th>实测中位 W</th><th>实测 p95 W</th><th>说明</th></tr></thead><tbody>')
        for entry in unmatched:
            parts.append(
                '<tr>'
                f'<td class="cell-code">{escape(entry["code"])}</td>'
                f'<td>{escape(entry["name"])}</td>'
                f'<td class="cell-code">{escape(entry["voltage_code"])}</td>'
                f'<td>{_fmt(entry["median"], 1)}</td>'
                f'<td>{_fmt(entry["p95"], 1)}</td>'
                f'<td class="cell-note">{escape(entry["note"])}</td>'
                '</tr>')
        parts.append('</tbody></table></div>')
        parts.append('</div>')

    if svg_ratio:
        parts.extend([
            '<div class="panel"><h3>实测 / 设计比值</h3>',
            f'<figure class="design-figure">{svg_ratio}<figcaption>'
            '按实测工作态 p95 与单台设计值之比排序；浅色条为工作中位，深色条为工作 p95，'
            '红色虚线为设计值 100%。实测普遍低于设计包络，说明设计值按峰值工况给定、'
            '在轨多数时间处于部分负载；高于 100% 的项需核查测点口径与负载实际工况。'
            '</figcaption></figure></div>',
        ])
    if svg_power:
        series_items = [item for item in measured if item.get('series')]
        parts.append('<div class="panel"><h3>设计功耗与实测功率</h3>')
        parts.append('<div class="chart-picker"><label for="design-view-select">图表视图</label>'
                     '<select id="design-view-select" aria-controls="design-view-display">'
                     '<option value="overview" selected>设计 vs 实测总览</option>'
                     + ('<option value="unit-series">单机实测功率曲线</option>'
                        if series_items else '')
                     + '</select><span class="picker-count">切换总览与逐单机实测曲线</span></div>')
        if series_items:
            parts.append('<div class="chart-picker" id="design-unit-picker" hidden>'
                         '<label for="design-unit-select">单机</label>'
                         '<select id="design-unit-select" aria-controls="design-view-display">')
            route_total = 0
            for index, item in enumerate(series_items):
                parts.append(f'<optgroup label="{escape(item["name"])}">')
                if item['n_devices'] > 1:
                    primary = (f'代表台（{item["n_devices"]} 台中最大）')
                else:
                    primary = '整机（合路）'
                parts.append(f'<option value="unit-{index}">{escape(primary)}</option>')
                for route_index, route in enumerate(
                        item['route_list'] if len(item['route_list']) > 1 else []):
                    route_total += 1
                    parts.append(f'<option value="route-{index}-{route_index}">'
                                 f'{escape(route["label"])} · {escape(route["code"])}</option>')
                parts.append('</optgroup>')
            parts.append(f'</select><span class="picker-count">共 {len(series_items)} 台、'
                         f'{route_total} 条支路，均按图 5-38 对应电压折算</span></div>')
        parts.append('<div id="design-view-display">')
        parts.append('<div class="design-view" data-design-view="overview">'
                     f'<figure class="design-figure">{svg_power}<figcaption>'
                     '横轴为对数功率；菱形为单台设计值（智算机两种模式各一个，实心为比较所用），'
                     '圆点为实测工作中位与工作 p95，'
                     '空心红圈为该单机在全部观测分钟内的实测峰值。'
                     '电推进仅 0.7% 分钟处于工作态，其中位 394 W 与设计 460 W 接近，'
                     '峰值约 919 W 对应两路同时输出，需核查推力器工作模式与电流系数。'
                     '在“图表视图”中选择“单机实测功率曲线”，可逐台查看实测曲线与设计虚线。'
                     '</figcaption></figure></div>')
        if series_items:
            parts.append('<div class="design-view" data-design-view="unit-series" hidden>')
            parts.append('<p class="body-copy">下图逐台显示<b>实测功率曲线</b>（蓝色实线）与'
                         '<b>设计值水平虚线</b>（红色）。每台单机取其在全部观测数据中'
                         '<strong>工作分钟最密集的 1 小时</strong>（数据过稀时扩到 3 小时），'
                         '再裁到实际工作分钟前后各 10 分钟，因此各图时间范围不同，'
                         '都落在该单机确有数据的时段；分钟功率 = 支路电流 × 图 5-38 对应电压，'
                         '“单机”下拉里每台先给整机/合路，再按支路各给一张；单路的设计线仍是该行表列值。'
                         '观测断档超过 5 分钟处断线。表格中的工作中位与工作 p95 取自全部 '
                         f'{summary["total_minutes"]:,} 个观测分钟，与曲线窗口不同源。</p>')
            for index, item in enumerate(series_items):
                segment = select_series_window(item)
                svg = unit_series_chart(item, segment)
                branches = escape(_compact_codes(item['branches']))
                if segment:
                    points = segment['points']
                    active = sum(1 for _, watt in points
                                 if watt >= item['design_W'] * summary['working_state_ratio'])
                    window_text = (f'{escape(segment["start"][5:16])}—'
                                   f'{escape(segment["end"][5:16])} · {len(points)} 个有效分钟'
                                   f'（工作 {active}）')
                else:
                    window_text = '无有效分钟'
                parts.append(f'<div class="component-panel design-unit" data-design-unit="unit-{index}"'
                             f'{" hidden" if index else ""}>'
                             f'<div class="selected-chart-head"><h4>{escape(item["name"])}</h4>'
                             f'<span>{escape(item["rail_name"])} · 设计 {_mode_text(item)} · '
                             f'工作 p95 {item["work_p95"]:.1f} W · {branches}</span></div>'
                             f'<p class="design-window">曲线窗口 {window_text}</p>')
                if svg:
                    parts.append(f'<figure class="design-figure">{svg}<figcaption>'
                                 '蓝色实线为分钟功率代理，红色虚线为该行设计合计；'
                                 '纵轴自适应到设计值与实测峰值的较大者，两条线可直接比较差值。'
                                 '</figcaption></figure>')
                else:
                    parts.append('<p>该单机在全期观测中没有可用功率样本。</p>')
                parts.append('</div>')
                # 每路一张：支路自身曲线 + 该行设计值
                # 单支路单机不重复给支路图（与整机同源）
                for route_index, route in enumerate(
                        item['route_list'] if len(item['route_list']) > 1 else []):
                    route_item = {
                        'name': f'{item["name"]} · {route["label"]}',
                        'series': route['series'],
                        'modes': item['modes'],
                        'design_W': item['design_W'],
                        'mode_W': item['mode_W'],
                        'mode_label': item['mode_label'],
                        'working_ratio': item['working_ratio'],
                    }
                    route_segment = select_series_window(route_item)
                    route_svg = unit_series_chart(route_item, route_segment)
                    route_window = (f'{escape(route_segment["start"][5:16])}—'
                                    f'{escape(route_segment["end"][5:16])}'
                                    if route_segment else '无有效分钟')
                    parts.append(f'<div class="component-panel design-unit" '
                                 f'data-design-unit="route-{index}-{route_index}" hidden>'
                                 f'<div class="selected-chart-head"><h4>'
                                 f'{escape(item["name"])} · {escape(route["label"])}</h4>'
                                 f'<span>{escape(route["code"])} · 设计 {_mode_text(item)} · '
                                 f'该路工作 p95 {_fmt(route["work_p95"], 1)} W</span></div>'
                                 f'<p class="design-window">曲线窗口 {route_window}</p>')
                    if route_svg:
                        parts.append(f'<figure class="design-figure">{route_svg}<figcaption>'
                                     '蓝色实线为该支路自身的分钟功率，红色虚线为该行设计值；'
                                     '并联/冗余支路（如智算机 F/G、路由器主备、微波 A/B）单路只占整机一部分，'
                                     '与表列整机功耗不可直接比较，整机请选上一项。'
                                     '</figcaption></figure>')
                    else:
                        parts.append('<p>该支路在观测期内没有可用功率样本。</p>')
                    parts.append('</div>')
            parts.append('</div>')
        parts.append('</div></div>')
    parts.extend([
        '<div class="panel"><h3>限制与待核实</h3>',
        '<p><strong>可比范围的边界：</strong>'
        f'低于设计值 80% 的 {len(low)} 项、达到 80–100% 的 {len(near)} 项、'
        f'达到或超过设计的 {len(over)} 项（'
        + ('、'.join(escape(item['name']) + (f'（对{item["mode_label"]}）' if item['mode_label'] else '')
                    for item in over) or '无')
        + '）。'
        '低于设计不代表设计错误：设计功耗是峰值包络，而实测统计包含待机、低占空比与部分负载分钟；'
        '反之，超过设计也不必然违规，可能是设计值口径（单台/整行）、测点包含线损或系数误差。</p>',
        _blocked_summary_text(summary)
        + '<p><strong>测点边界：</strong>支路电流取自 PCDU 各配电输出，实测功率是<strong>单机输入端</strong>'
        '的功率代理，可能含配电线路损耗，与设计表“单机自身功耗”不是同一截面；'
        '若单机内部还有二次变换损耗，两者差值会更大。'
        '相控阵一项的遥测名称为“测控相控阵”，与设计表“X 频段数传下行相控阵天线”是否同一台设备尚未核实。</p>',
        '</div></section>',
    ])
    return parts
