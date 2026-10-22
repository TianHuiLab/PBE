#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从已落地的真实分钟遥测和 v2 输出生成离线 SVG 图。"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
import csv
import math
import re

BASE = Path(__file__).resolve().parent
OUT = BASE / 'charts'
WIDTH, HEIGHT = 960, 310
LEFT, RIGHT, TOP, BOTTOM = 68, 22, 48, 48
PW, PH = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
PALETTE = ('#2563eb', '#059669', '#e58914', '#dc4667', '#7c3aed', '#0891b2')

# 图表清单（单一事实源：config/charts.json）；缺失时回退内置列表
FALLBACK_CHART_NAMES = [
    'chart_voltages', 'chart_currents', 'chart_solar_panels', 'chart_panel_voltages',
    'chart_power_flow', 'chart_shunt', 'chart_soc_dod', 'chart_daily_power',
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


def stamp(raw):
    if isinstance(raw, datetime):
        return raw.timestamp()
    if isinstance(raw, (int, float)):
        return float(raw)
    return datetime.fromisoformat(str(raw)).timestamp()


def fmt(number):
    return f'{number:.1f}' if abs(number) < 100 else f'{number:.0f}'


def line_chart(title, series_dict, xname='日期', ylabel='', ymax=None, ymin=None,
               gap_seconds=None, reference=None, day=None, scatter=False,
               window_start=None, window_end=None):
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
    legend_x = LEFT + 6
    for name, (pairs, color) in normalized.items():
        parts.append(f'<line x1="{legend_x}" x2="{legend_x+15}" y1="36" y2="36" stroke="{color}" stroke-width="3"/>')
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
                parts.append(f'<path d="{" ".join(commands)}" fill="none" stroke="{color}" stroke-width="1.7" stroke-linejoin="round"/>')
    parts.append('</svg>')
    return '\n'.join(parts)


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


def derive_observed_energy(result):
    """仅把有观测的分钟样本计作 1/60 小时，不对未观测时段外推。"""
    totals = defaultdict(lambda: defaultdict(float))
    for row in result:
        day = row['t'][:10]
        for key in ('P_solar', 'P_load', 'P_excess', 'P_shunt'):
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
    """优先选择包含日照/地影且样本数最多的连续片段；并列取最早开始的。"""
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
        raise ValueError('没有同时包含日照与地影判定的连续观测片段')
    return min(eligible, key=lambda chunk: (-len(chunk), chunk[0]['t']))


def hour_buckets(result, day):
    """当天每小时仅汇总已观测分钟的平均功率、SOC和能量，不插值。"""
    buckets = defaultdict(list)
    for row in result:
        if row['t'][:10] == day:
            buckets[row['t'][:13]].append(row)
    hours = [f'{day} {hour:02d}:00:00' for hour in range(24)]
    keys = ('P_solar', 'P_load', 'P_excess', 'P_shunt', 'SOC')
    avg = {key: [] for key in keys}
    energy = {key: [] for key in ('P_solar', 'P_load', 'P_shunt')}
    for hour in hours:
        sample = buckets.get(hour[:13], [])
        for key in keys:
            observed = [v for row in sample if (v := value(row.get(key))) is not None]
            avg[key].append(sum(observed) / len(observed) if observed else None)
            if key in energy:
                # 未观测小时保持 None（折线断线），不填 0 误导读者
                energy[key].append(sum(observed) / 60000.0 if observed else None)
    return hours, avg, energy


def embed_charts_in_html():
    """只替换当前文档里图表元素，不触碰正文；重复运行保持同样的输出。

    图表清单以 config/charts.json 为单一事实源；HTML 中的引用必须与清单
    完全一致（不多不少），否则拒绝部分更新。
    """
    page = BASE / 'framework_documentation.html'
    html = page.read_text(encoding='utf-8')
    expected = load_chart_manifest()
    names = re.findall(r'src="charts/(chart_[a-z_]+)\.svg"', html)
    if not names:
        names = re.findall(r'data-inline-chart="(chart_[a-z_]+)"', html)
    if len(names) != len(expected) or set(names) != set(expected):
        raise ValueError(
            f'HTML 图表清单与 config/charts.json 不一致（HTML {len(names)} 张 / '
            f'清单 {len(expected)} 张），拒绝部分更新')
    for name in names:
        svg = (OUT / f'{name}.svg').read_text(encoding='utf-8')
        if not svg.startswith('<svg ') or not svg.rstrip().endswith('</svg>'):
            raise ValueError(f'图表内容不完整：{name}')
        figure = re.compile(
            r'(<figure class="chart">)(?:<img src="charts/' + re.escape(name) +
            r'\.svg"[^>]*>|<svg\b[^>]*\bdata-inline-chart="' + re.escape(name) +
            r'"[^>]*>.*?</svg>)(<figcaption>)', re.DOTALL)
        svg = svg.replace('<svg ', f'<svg data-inline-chart="{name}" ', 1)
        html, replaced = figure.subn(lambda match: match.group(1) + svg + match.group(2), html)
        if replaced != 1:
            raise ValueError(f'图表引用缺失或重复：{name}')
    page.write_text(html, encoding='utf-8')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    result = sorted(rows('out/energy_v2_result.csv'), key=lambda r: r['t'])
    minute = rows('data_power_minute.csv')
    if not result or not minute:
        raise ValueError('本地真实遥测或 v2 评估输出为空')
    result = select_observation_window(result)
    start, end = result[0]['t'], result[-1]['t']
    day = start[:10]
    minute = [row for row in minute if start <= row['t'] <= end]
    times = [r['t'] for r in result]
    hour_times, hourly, energy = hour_buckets(result, day)
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
    write_chart('chart_power_flow', '功率计算量 · 发电 / 负载 / 富余', times,
                {'发电功率 (W)': col('P_solar'), '负载功率 (W)': col('P_load'),
                 '富余功率 (W)': col('P_excess')}, 'W', minute_gap, window_start=start, window_end=end)
    write_chart('chart_shunt', '分流功率 · 模型推断值', times,
                {'分流功率 (W)': col('P_shunt')}, 'W', minute_gap, low=0, window_start=start, window_end=end)
    write_chart('chart_soc_dod', '蓄电池状态 · SOC / DoD（电压映射估计）', times,
                {'SOC (%)': col('SOC'), 'DoD (%)': col('DoD')}, '%', minute_gap, low=0, high=100,
                reference=50, window_start=start, window_end=end)
    grouped = defaultdict(dict)
    for row in minute:
        if row['param_code'] in {f'TMD{i:03d}' for i in range(3, 16)}:
            grouped[row['t']][row['param_code']] = value(row['avg'])
    panel_times = sorted(grouped)
    for kind, codes, unit in (
        ('chart_solar_panels', range(10, 16), 'A'),
        ('chart_panel_voltages', range(3, 9), 'V'),
    ):
        # 单独绘制原始分钟聚合帆板；缺失参数是 None，不补点。
        points = {f'{chr(65 + i)}板 TMD{code:03d}':
                  [grouped[t].get(f'TMD{code:03d}') for t in panel_times]
                  for i, code in enumerate(codes)}
        write_chart(kind, '太阳阵 A—F ' + ('电流' if unit == 'A' else '电压') + ' · 实测分钟均值',
                    panel_times, points, unit, minute_gap, low=0, window_start=start, window_end=end)
    write_chart('chart_daily_power', '观测窗口 · 按小时已有分钟平均功率', hour_times,
                {'发电 (W)': hourly['P_solar'], '负载 (W)': hourly['P_load'],
                 '富余 (W)': hourly['P_excess']}, 'W', hourly_gap, window_start=start, window_end=end)
    write_chart('chart_daily_soc', '观测窗口 · 按小时已有分钟平均 SOC', hour_times,
                {'SOC (%)': hourly['SOC']}, '%', hourly_gap, low=0, high=100, window_start=start, window_end=end)
    write_chart('chart_daily_shunt', '观测窗口 · 按小时已有分钟平均分流', hour_times,
                {'分流 (W)': hourly['P_shunt']}, 'W', hourly_gap, low=0, window_start=start, window_end=end)
    write_chart('chart_energy_daily', '观测窗口 · 按小时已有分钟能量', hour_times,
                {'发电 (kWh)': energy['P_solar'], '负载 (kWh)': energy['P_load'],
                 '分流 (kWh)': energy['P_shunt']}, 'kWh', hourly_gap, low=0, window_start=start, window_end=end)
    clean_orphan_svgs()
    embed_charts_in_html()


if __name__ == '__main__':
    main()
