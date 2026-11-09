#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由云尖3D已核验分钟文件及短间隔积分摘要生成独立分析和离线图表。"""
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import pandas as pd

from gen_charts import PALETTE, line_chart, phase2_yj_section
from yj3d_assess import BASE, PROFILE, sha256
from yj3d_boundary import analyze as analyze_boundary
from yj3d_soc import analyze_minutes, load_calibration


def median(series):
    values = [float(value) for value in series if pd.notna(value) and math.isfinite(float(value))]
    return statistics.median(values) if values else None


def fmt(value, digits=2):
    return '—' if value is None or pd.isna(value) else f'{value:,.{digits}f}'


def balance_sections(balance, nearby_svg, daily_svg, nearby_count, window_start, window_end):
    """以同来源行配对功率生成粗、细两层账本，不把未知差额写成损耗。"""
    by_phase = balance['comparisons_by_phase']
    coarse_rows_html, fine_rows_html = [], []
    coarse_rows_md, fine_rows_md = [], []
    fine_names = {'A': 'PL A 路 21 条', 'B': 'PL B 路 21 条',
                  'AB': 'PL A+B 条件合计', 'HEAT': '10 条加热支路',
                  'ABH': 'PL A+B+加热条件合计'}
    for phase in ('日照', '地影'):
        group = by_phase[phase]
        bcr = group['comparisons']['BCR']
        count = bcr['paired_minutes']
        residual = bcr['bcr_plus_battery_minus_bus_W_p05_p50_p95']
        coarse = (phase, f'{count:,}/{group["clear_phase_minutes"]:,}',
                  fmt(bcr['source_W_p05_p50_p95'][1], 2),
                  fmt(bcr['battery_terminal_signed_W_p05_p50_p95'][1], 2),
                  fmt(bcr['bus_W_p05_p50_p95'][1], 2),
                  fmt(residual[1], 2),
                  f'{fmt(residual[0],2)}–{fmt(residual[2],2)}')
        coarse_rows_html.append('<tr>' + ''.join(f'<td>{escape(str(x))}</td>' for x in coarse) + '</tr>')
        coarse_rows_md.append('| ' + ' | '.join(coarse) + ' |')
        for key, name in fine_names.items():
            item = group['comparisons'][key]
            gap = item['bus_minus_source_W_p05_p50_p95']
            fine = (phase, name, f'{item["paired_minutes"]:,}',
                    fmt(item['bus_W_p05_p50_p95'][1], 2),
                    fmt(item['source_W_p05_p50_p95'][1], 2),
                    fmt(gap[1], 2),
                    fmt(100 * item['source_over_bus_ratio_p50'], 1) + '%')
            fine_rows_html.append('<tr>' + ''.join(f'<td>{escape(str(x))}</td>' for x in fine) + '</tr>')
            fine_rows_md.append('| ' + ' | '.join(fine) + ' |')
    sun_bcr = by_phase['日照']['comparisons']['BCR']
    eclipse_bcr = by_phase['地影']['comparisons']['BCR']
    sun_fine = by_phase['日照']['comparisons']['ABH']
    eclipse_fine = by_phase['地影']['comparisons']['ABH']
    coarse_note = (
        f'明确日照 {sun_bcr["paired_minutes"]:,} 个配对分钟的 R 中位 '
        f'{fmt(sun_bcr["bcr_plus_battery_minus_bus_W_p05_p50_p95"][1],2)} W；'
        f'地影 {eclipse_bcr["paired_minutes"]:,} 个配对分钟为 '
        f'{fmt(eclipse_bcr["bcr_plus_battery_minus_bus_W_p05_p50_p95"][1],2)} W。'
        '差额相对母线功率较小，说明这些同源遥测在配对样本上相互接近，'
        '不能据此认定独立物理闭合或效率。')
    fine_note = (
        f'A+B+加热条件组合在日照 {sun_fine["paired_minutes"]:,} 个配对分钟中，'
        f'支路/母线比中位 {fmt(100*sun_fine["source_over_bus_ratio_p50"],1)}%，'
        f'母线减支路和的分钟差额中位 {fmt(sun_fine["bus_minus_source_W_p05_p50_p95"][1],2)} W；'
        f'地影 {eclipse_fine["paired_minutes"]:,} 个配对分钟分别为 '
        f'{fmt(100*eclipse_fine["source_over_bus_ratio_p50"],1)}% 与 '
        f'{fmt(eclipse_fine["bus_minus_source_W_p05_p50_p95"][1],2)} W。')
    html = ('''<section class="report-section" id="whole-balance"><div class="section-head"><h2>整星能源平衡评估</h2><p>同来源行配对，按明确工况比较功率</p></div>
<div class="panel method-panel"><h3>粗粒度：BCR 输出、电池与母线</h3><p><code>R_供电诊断 = P_八路BCR输出 + P_电池端有符号 − P_母线测点</code>。电池原生正值按放电运行推断、负值按充电候选。下面先在同一来源行求功率，每分钟取配对行均值，再对分钟取分位；各列中位数不能彼此相减来重建 R 中位数。</p>
<div class="table-scroll"><table class="data-table"><thead><tr><th>工况</th><th>配对/明确分钟</th><th>BCR 输出 P50 W</th><th>电池端 P50 W</th><th>母线 P50 W</th><th>R P50 W</th><th>R P05–P95 W</th></tr></thead><tbody>''' + ''.join(coarse_rows_html) + '''</tbody></table></div><p class="section-tip">''' + escape(coarse_note) + '''</p>
<p>光照区简式 <code>P_太阳阵=P_母线+损耗</code> 只适用于电池净功率接近零、无上游分流且测点同边界的情况。云尖3D日照期存在电池充电和放电分钟，不能把 BCR 输出与母线的差额全部叫作损耗。八路 BCR 输出是调节器输出侧，不是阵列片端，也不同于前表的 TMEA003×TMEA007 母线电压代理；地影输出的微小非零值只作零偏诊断。地影简式 <code>P_电池放电≈P_母线</code> 可作为运行近似，电池端到母线的路径与传感器位置仍待核。</p></div>
<div class="panel method-panel"><h3>细粒度：母线与单机支路</h3><p>按候选边界：<code>P_母线 = ΣP_PL A+B + ΣP_加热 + P_其他负载 + P_配电损耗 + dE_母线储能/dt</code>。表中 A、B、A+B、加热及 A+B+加热分别用各自同来源行样本计算；A+B 和 A+B+加热只在这些支路互不重叠且位于母线下游时才是可加总口径。</p>
<div class="table-scroll"><table class="data-table"><thead><tr><th>工况</th><th>已测支路组合</th><th>配对分钟</th><th>母线 P50 W</th><th>支路和 P50 W</th><th>母线−支路和 P50 W</th><th>支路/母线比 P50</th></tr></thead><tbody>''' + ''.join(fine_rows_html) + '''</tbody></table></div><p>''' + escape(fine_note) + '''</p>
<p class="section-tip">各行差额由同分钟的配对功率计算，不是“母线损耗”。PL_25、PL_29 没有支路电流，PL_33 不在这 21 路 PL 和内；其他单机、加热支路与 PL 的拓扑关系也未核实。未计负载、配电损耗和测量误差都可能进入差额。明细口径与哈希见 <a href="balance_ledger_summary.json">配对功率账本摘要</a>。</p></div>
<div class="panel"><h3>母线与 PL A+B+加热条件合计：两条配对功率曲线</h3><p>切换窗口 ''' + escape(window_start) + '—' + escape(window_end) + f'''（北京时间）有 {nearby_count} 个同时配对且工况明确的分钟。两条线在每分钟使用完全相同的来源行：蓝线为 TMEA003×TMEA004，绿线为 21 条 PL A、21 条 PL B 与 10 条加热支路的条件和；缺测和未判定分钟不补线。</p>
<figure class="component-summary-figure">{nearby_svg}<figcaption>工况切换附近逐分钟同源配对功率；竖线标记选定切换时刻。</figcaption></figure>
<details class="aux-details"><summary>查看全时段有数据日的两条中位曲线</summary><figure class="component-summary-figure">{daily_svg}<figcaption>每天仅对有效配对分钟分别取母线与支路和的中位数；无数据日断线，不代表整日积分。</figcaption></figure></details><p class="section-tip">逐分钟配对值见 <a href="data_balance_pair_minute.csv">两曲线来源 CSV</a>。曲线差值包含未测负载和测量/配电差异，不能直接解释为母线损耗。</p></div></section>''')
    md = '''## 整星能源平衡评估：同来源行配对功率

### 粗粒度供电账本

`R_供电诊断 = P_八路BCR输出 + P_电池端有符号 − P_母线测点`。电池原生正值按放电运行推断、负值按充电候选。先在同来源行配对，再取每分钟平均功率，对明确且无冲突的 TMZS003 工况分钟求分位；R 的中位数由逐分钟 R 得到，不能用表内各列中位数相减重建。

| 工况 | 配对/明确分钟 | BCR 输出 P50 W | 电池端 P50 W | 母线 P50 W | R P50 W | R P05–P95 W |
|---|---:|---:|---:|---:|---:|---:|
''' + '\n'.join(coarse_rows_md) + '''

''' + coarse_note + '''

光照区简式 `P_太阳阵=P_母线+损耗` 只适用于电池净功率接近零、无上游分流且测点同边界的情况。日照期电池存在充放电，BCR 输出与母线的差额不能全部归为损耗。八路 BCR 输出是调节器输出侧，并非阵列片端，也不同于前表的 `TMEA003×TMEA007` 母线电压代理；地影输出的微小非零值只作零偏诊断。地影 `P_电池放电≈P_母线` 可作运行近似，电池端到母线的路径仍待核。

### 细粒度单机账本

候选边界公式：`P_母线 = ΣP_PL A+B + ΣP_加热 + P_其他负载 + P_配电损耗 + dE_母线储能/dt`。A+B 以及 A+B+加热的求和，以这些支路互不重叠且位于母线下游为条件。

| 工况 | 已测支路组合 | 配对分钟 | 母线 P50 W | 支路和 P50 W | 母线−支路和 P50 W | 支路/母线比 P50 |
|---|---|---:|---:|---:|---:|---:|
''' + '\n'.join(fine_rows_md) + '''

''' + fine_note + '''

每行差额先按同一分钟配对求得，不能把支路和与母线各自的全窗中位数相减。差额**不是母线损耗**：PL_25、PL_29 缺支路电流，PL_33 不在 21 路 PL 和内，其他单机、加热支路与 PL 的拓扑关系尚未核实。未计负载、配电损耗和测量误差均可能进入差额。复算口径见[配对功率账本摘要](balance_ledger_summary.json)。

### 母线与 PL A+B+加热的两条曲线

切换窗口 ''' + window_start + '—' + window_end + f'''（北京时间）有 {nearby_count} 个同时配对且工况明确的分钟。两条曲线分别是同批来源行的母线功率与 PL A+B+10 条加热支路条件和；缺测和未判定分钟不补线。[逐分钟配对数据](data_balance_pair_minute.csv)可复核每个点。

![切换附近母线与 PL A+B 加热功率](balance_pair_nearby.svg)

全时段各有数据日的配对分钟中位数另见[日中位双曲线](balance_pair_daily.svg)；日中位不代表整日能量。

'''
    return html, md


def calibration_chart(calibration):
    """把本星电压标定点绘为独立 SVG，不把电压当成时间轴。"""
    points = calibration['points']
    left, right, top, bottom = 72, 930, 42, 258
    low, high = points[0]['voltage_V'], points[-1]['voltage_V']
    x = lambda voltage: left + (voltage - low) / (high - low) * (right - left)
    y = lambda soc: bottom - soc / 100 * (bottom - top)
    path = ' '.join(f'{"M" if index == 0 else "L"}{x(point["voltage_V"]):.1f},'
                    f'{y(point["soc_pct"]):.1f}' for index, point in enumerate(points))
    grid = ''.join(
        f'<line x1="{left}" x2="{right}" y1="{y(level):.1f}" y2="{y(level):.1f}" stroke="#e8eef5"/>'
        f'<text x="{left-10}" y="{y(level)+4:.1f}" text-anchor="end" fill="#64748b" font-size="11">{level}%</text>'
        for level in (0, 25, 50, 75, 100))
    markers = ''.join(
        f'<circle cx="{x(point["voltage_V"]):.1f}" cy="{y(point["soc_pct"]):.1f}" r="4" fill="#2554ad"/>'
        for point in points)
    ticks = ''.join(
        f'<text x="{x(point["voltage_V"]):.1f}" y="{bottom+19}" text-anchor="middle" fill="#64748b" font-size="10">'
        f'{point["voltage_V"]:.2f}</text>'
        for point in (points[0], points[2], points[4], points[6], points[-1]))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 310" role="img" '
            f'aria-label="云尖3D星 7.84 A 放电标定电压与 SOC 曲线">'
            '<title>云尖3D星放电标定 V–SOC 曲线</title>'
            '<rect width="100%" height="100%" fill="white"/>'
            f'{grid}<line x1="{left}" x2="{right}" y1="{bottom}" y2="{bottom}" stroke="#94a3b8"/>'
            f'<path d="{path}" fill="none" stroke="#2554ad" stroke-width="2.5"/>{markers}{ticks}'
            '<text x="501" y="302" text-anchor="middle" fill="#64748b" font-size="12">电池端电压 V</text>'
            '</svg>')


def chart_panel(key, number, title, svg, caption, visible=False):
    hidden = '' if visible else ' hidden'
    return (f'<article class="chart-card" data-chart-panel="{escape(key, quote=True)}"{hidden}>'
            f'<div class="chart-card-header"><span class="chart-index">{number:02d}</span>'
            f'<h3>{escape(title)}</h3></div><figure>{svg}'
            f'<figcaption>{escape(caption)}</figcaption></figure></article>')


def component_summary_chart(component_summary):
    """仅用来源行聚合值绘制横条，不暗示存在支路分钟曲线。"""
    specs = list(component_summary['components'].values())
    maximum = max(max(spec['mean_bus_voltage_proxy_W'] or 0,
                      spec['p95_bus_voltage_proxy_W'] or 0) for spec in specs)
    scale = 600 / maximum if maximum else 0
    rows = []
    for index, spec in enumerate(specs):
        y = 66 + index * 34
        mean = spec['mean_bus_voltage_proxy_W'] or 0
        p95 = spec['p95_bus_voltage_proxy_W'] or 0
        rows.append(
            f'<text x="206" y="{y+12}" text-anchor="end" font-size="12" fill="#334155">'
            f'{escape(spec["name"])}</text>'
            f'<rect x="220" y="{y}" width="{mean*scale:.1f}" height="10" rx="2" fill="#2554ad"/>'
            f'<rect x="220" y="{y+12}" width="{p95*scale:.1f}" height="10" rx="2" fill="#007e80"/>'
            f'<text x="{min(924, 225+max(mean,p95)*scale):.1f}" y="{y+19}" font-size="10" fill="#52627a">'
            f'{mean:.2f} / {p95:.2f} W</text>')
    height = 77 + len(specs) * 34
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 {height}" role="img" '
            'aria-label="云尖3D星十条加热支路平均值与95分位功率代理比较">'
            '<title>加热支路母线电压功率代理</title>'
            '<rect width="100%" height="100%" fill="white"/>'
            '<rect x="220" y="19" width="14" height="8" fill="#2554ad"/>'
            '<text x="240" y="27" font-size="11" fill="#52627a">均值</text>'
            '<rect x="306" y="19" width="14" height="8" fill="#007e80"/>'
            '<text x="326" y="27" font-size="11" fill="#52627a">p95</text>'
            + ''.join(rows) + '</svg>')


def choose_window(frame, solar_threshold=1.0):
    """选一个连续、两工况齐全且标志与太阳阵电流相符的窗口。"""
    subset = frame[['t', 'phase']].reset_index(drop=True)
    times = subset['t']
    phases = subset['phase']
    for index in range(1, len(subset)):
        if ({phases.iloc[index - 1], phases.iloc[index]} != {'日照', '地影'} or
                (times.iloc[index] - times.iloc[index - 1]).total_seconds() > 300):
            continue
        focus = times.iloc[index]
        selected = frame[(frame['t'] >= focus - timedelta(minutes=60)) &
                         (frame['t'] <= focus + timedelta(minutes=60))]
        counts = selected['phase'].value_counts()
        sun_current = median(selected.loc[selected['phase'].eq('日照'), 'solar_current_mean'])
        eclipse_current = median(selected.loc[selected['phase'].eq('地影'), 'solar_current_mean'])
        if (counts.get('日照', 0) >= 10 and counts.get('地影', 0) >= 10 and
                len(selected) >= 50 and selected['phase_conflict'].sum() <= 2 and
                sun_current is not None and sun_current >= solar_threshold and
                eclipse_current is not None and eclipse_current < solar_threshold):
            return selected, focus
    eclipse = frame[frame['phase'].eq('地影')]
    if not eclipse.empty:
        focus = eclipse['t'].iloc[0]
        return frame[(frame['t'] >= focus - timedelta(minutes=60)) &
                     (frame['t'] <= focus + timedelta(minutes=60))], focus
    focus = frame['t'].iloc[0]
    return frame.iloc[:min(120, len(frame))], focus


def render(root=PROFILE):
    root = Path(root)
    summary = json.loads((root / 'energy_summary.json').read_text(encoding='utf-8'))
    data_path = root / 'data_power_minute.csv'
    if sha256(data_path) != summary['provenance']['minute_csv_sha256']:
        raise ValueError('云尖3D分钟文件与来源摘要 SHA-256 不一致')
    frame = pd.read_csv(data_path, parse_dates=['t'])
    if frame.empty or len(frame) != summary['observed_minutes']:
        raise ValueError('云尖3D分钟文件与摘要计数不一致')
    if frame['t'].duplicated().any() or not frame['t'].is_monotonic_increasing:
        raise ValueError('云尖3D分钟文件时间重复或未排序')
    config = json.loads((root / 'config' / 'telemetry_params.json').read_text(encoding='utf-8'))
    design = json.loads((root / 'config' / 'assessment_params.json').read_text(encoding='utf-8'))
    if sha256(root / 'config' / 'telemetry_params.json') != summary['provenance']['telemetry_config_sha256']:
        raise ValueError('云尖3D遥测配置已更改，需重新取数')
    if sha256(root / 'config' / 'assessment_params.json') != summary['provenance']['assessment_config_sha256']:
        raise ValueError('云尖3D评估参数已更改，需重新取数')

    calibration = load_calibration(root)
    soc_summary, soc_rows = analyze_minutes(frame, calibration)
    soc_summary['minute_source_sha256'] = sha256(data_path)
    soc_path = root / 'data_battery_soc_minute.csv'
    soc_rows.to_csv(soc_path, index=False, encoding='utf-8')
    soc_summary['soc_csv_sha256'] = sha256(soc_path)
    (root / 'battery_soc_summary.json').write_text(
        json.dumps(soc_summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')

    boundary = analyze_boundary(frame, root)
    (root / 'boundary_summary.json').write_text(
        json.dumps(boundary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    boundary_config = json.loads((root / 'config' / 'boundary_candidates.json').read_text(encoding='utf-8'))
    bcr_path = root / 'bcr_boundary_summary.json'
    bcr_summary = json.loads(bcr_path.read_text(encoding='utf-8')) if bcr_path.is_file() else None
    if bcr_summary:
        if (bcr_summary['candidate_config_sha256'] != boundary['candidate_config_sha256'] or
                bcr_summary['workbook_sha256'] != summary['provenance']['workbook_sha256'] or
                bcr_summary['window_start_utc'] != summary['provenance']['window_start_inclusive_utc'] or
                bcr_summary['window_end_exclusive_utc'] != summary['provenance']['window_end_exclusive_utc']):
            raise ValueError('云尖3D BCR 查询摘要与本次评估配置、工作簿或窗口不一致')
        if len(bcr_summary['channels']) != len(boundary_config['pv_channels']):
            raise ValueError('云尖3D BCR 通道数量与配置不一致')
        for item, measured in zip(boundary_config['pv_channels'], bcr_summary['channels']):
            if any(measured[f'{field}_code'] != item[field]
                   for field in ('input_voltage', 'output_voltage', 'output_current')):
                raise ValueError('云尖3D BCR 查询摘要代号与配置不一致')
    unit_inventory_path = root / 'unit_inventory.json'
    unit_power_path = root / 'unit_power_summary.json'
    unit_inventory = json.loads(unit_inventory_path.read_text(encoding='utf-8'))
    unit_power = json.loads(unit_power_path.read_text(encoding='utf-8'))
    if (unit_inventory['source_workbook_sha256'] != summary['provenance']['workbook_sha256'] or
            unit_power['inventory_sha256'] != sha256(unit_inventory_path) or
            unit_power['workbook_sha256'] != summary['provenance']['workbook_sha256'] or
            unit_power['window_start_utc'] != summary['provenance']['window_start_inclusive_utc'] or
            unit_power['window_end_exclusive_utc'] != summary['provenance']['window_end_exclusive_utc']):
        raise ValueError('云尖3D单机清单或功率摘要与本次评估来源不一致')
    if len(unit_inventory['units']) != 24:
        raise ValueError('云尖3D单机 PL 通道数量需复核')
    balance_path = root / 'balance_ledger_summary.json'
    balance = json.loads(balance_path.read_text(encoding='utf-8'))
    if (balance['source'] != 'chdb_3D.yc_history' or
            balance['minute_csv_sha256'] != sha256(data_path) or
            balance['inventory_sha256'] != sha256(unit_inventory_path) or
            balance['bcr_config_sha256'] != sha256(root / 'config' / 'boundary_candidates.json') or
            balance['component_config_sha256'] != sha256(root / 'config' / 'components.json') or
            balance['window_start_inclusive_beijing'] != design['assessment_start_beijing'] or
            balance['window_end_exclusive_beijing'] != design['assessment_end_exclusive_beijing']):
        raise ValueError('云尖3D配对功率账本与本次数据或配置不一致')
    pair_path = root / 'data_balance_pair_minute.csv'
    if sha256(pair_path) != balance['paired_minute_csv_sha256']:
        raise ValueError('云尖3D配对曲线分钟文件与功率账本 SHA-256 不一致')
    pair_frame = pd.read_csv(pair_path, parse_dates=['t_beijing'])
    if (pair_frame.empty or len(pair_frame) != balance['paired_minute_csv_rows'] or
            pair_frame['t_beijing'].duplicated().any() or
            not pair_frame['t_beijing'].is_monotonic_increasing or
            not pair_frame['phase'].isin(('日照', '地影')).all() or
            pair_frame[['bus_W', 'pl_ab_heat_W', 'bus_minus_pl_ab_heat_W']].isna().any().any()):
        raise ValueError('云尖3D配对曲线分钟数据无效')
    if (pair_frame['bus_W'] - pair_frame['pl_ab_heat_W'] -
            pair_frame['bus_minus_pl_ab_heat_W']).abs().max() > 1e-6:
        raise ValueError('云尖3D配对曲线差额与两个功率测点不一致')
    for phase in ('日照', '地影'):
        expected = balance['comparisons_by_phase'][phase]['comparisons']['ABH']['paired_minutes']
        if pair_frame['phase'].eq(phase).sum() != expected:
            raise ValueError('云尖3D配对曲线工况分钟数与功率账本不一致')

    day = frame.assign(day=frame['t'].dt.floor('D')).groupby('day', sort=True).agg(
        minutes=('t', 'size'),
        solar=('solar_bus_proxy_W_mean', 'median'),
        bus=('bus_measure_W_mean', 'median'),
        battery=('battery_signed_trial_W_mean', 'median'))
    window, focus = choose_window(frame, design['solar_phase_threshold_A'])
    window_start = window['t'].iloc[0].strftime('%Y-%m-%d %H:%M:%S')
    window_end = window['t'].iloc[-1].strftime('%Y-%m-%d %H:%M:%S')
    full_start = frame['t'].iloc[0].strftime('%Y-%m-%d %H:%M:%S')
    full_end = frame['t'].iloc[-1].strftime('%Y-%m-%d %H:%M:%S')
    # gen_charts.stamp 对 pandas.Timestamp 与无时区字符串采用不同的时区解释。
    # 全部改为北京时间无时区字符串，保证曲线和声明窗口共享同一时间轴。
    day_times = day.index.strftime('%Y-%m-%d %H:%M:%S')
    full_times = frame['t'].dt.strftime('%Y-%m-%d %H:%M:%S')
    window_times = window['t'].dt.strftime('%Y-%m-%d %H:%M:%S')
    pair_window = pair_frame.loc[
        pair_frame['t_beijing'].between(window['t'].iloc[0], window['t'].iloc[-1])]
    if pair_window.empty:
        raise ValueError('选定切换窗口没有母线与单机的同源配对分钟')
    pair_window_times = pair_window['t_beijing'].dt.strftime('%Y-%m-%d %H:%M:%S')
    pair_nearby_svg = line_chart(
        '云尖3D切换附近母线与 PL A+B+加热条件功率',
        {'母线·同源配对': (pair_window_times, pair_window['bus_W'], PALETTE[0]),
         'PL A+B+加热·条件和': (pair_window_times, pair_window['pl_ab_heat_W'], PALETTE[1])},
        xname='北京时间 · 同源配对分钟', ylabel='W', ymin=0,
        gap_seconds=90, window_start=window_start, window_end=window_end,
        highlight_time=focus.strftime('%Y-%m-%d %H:%M:%S'))
    pair_day = pair_frame.assign(day=pair_frame['t_beijing'].dt.floor('D')).groupby('day', sort=True).agg(
        bus=('bus_W', 'median'), branch=('pl_ab_heat_W', 'median'))
    pair_day_times = pair_day.index.strftime('%Y-%m-%d %H:%M:%S')
    pair_daily_svg = line_chart(
        '云尖3D有数据日母线与 PL A+B+加热条件功率中位数',
        {'母线·同源配对': (pair_day_times, pair_day['bus'], PALETTE[0]),
         'PL A+B+加热·条件和': (pair_day_times, pair_day['branch'], PALETTE[1])},
        xname='北京时间 · 有配对数据日', ylabel='W', ymin=0,
        gap_seconds=36 * 3600,
        window_start=pair_day.index[0].strftime('%Y-%m-%d %H:%M:%S'),
        window_end=(pair_day.index[-1] + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'))
    (root / 'balance_pair_nearby.svg').write_text(pair_nearby_svg, encoding='utf-8')
    (root / 'balance_pair_daily.svg').write_text(pair_daily_svg, encoding='utf-8')
    balance_html, md_balance = balance_sections(
        balance, pair_nearby_svg, pair_daily_svg, len(pair_window), window_start, window_end)

    daily_svg = line_chart(
        '各有数据日的分钟功率中位数（未填补缺口）',
        {'太阳阵·母线电压代理': (day_times, day['solar'], PALETTE[0]),
         '母线电流测点': (day_times, day['bus'], PALETTE[1]),
         '电池有符号试算': (day_times, day['battery'], PALETTE[2])},
        xname='北京时间 · 有数据日', ylabel='W 或条件试算 W',
        gap_seconds=2 * 86400, window_start=day.index[0].strftime('%Y-%m-%d %H:%M:%S'),
        window_end=(day.index[-1] + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'))
    full_battery_svg = line_chart(
        'TMEA006 全部观测分钟原值 · 最小/均值/最大',
        {'分钟最小': (full_times, frame['battery_current_min'], PALETTE[0]),
         '分钟均值': (full_times, frame['battery_current_mean'], PALETTE[2]),
         '分钟最大': (full_times, frame['battery_current_max'], PALETTE[3])},
        xname='北京时间 · 观测分钟', ylabel='数据库已解码原值（标称 A）',
        gap_seconds=180, window_start=full_start, window_end=full_end)
    power_svg = line_chart(
        '工况切换附近四项功率与诊断差额',
        {'太阳阵·母线电压代理': (window_times, window['solar_bus_proxy_W_mean'], PALETTE[0]),
         '母线电流测点': (window_times, window['bus_measure_W_mean'], PALETTE[1]),
         '电池有符号试算': (window_times, window['battery_signed_trial_W_mean'], PALETTE[2]),
         '跨测点残差诊断': (window_times, window['residual_diag_W_mean'], PALETTE[3])},
        xname='北京时间 · 观测分钟', ylabel='W 或条件试算 W',
        gap_seconds=90, window_start=window_start, window_end=window_end,
        highlight_time=focus.strftime('%Y-%m-%d %H:%M:%S'))
    current_svg = line_chart(
        '工况切换附近三路电流 · 原生符号',
        {'太阳阵 TMEA007': (window_times, window['solar_current_mean'], PALETTE[0]),
         '母线 TMEA004': (window_times, window['bus_current_mean'], PALETTE[1]),
         '电池 TMEA006': (window_times, window['battery_current_mean'], PALETTE[2])},
        xname='北京时间 · 观测分钟', ylabel='A', reference=0,
        gap_seconds=90, window_start=window_start, window_end=window_end,
        highlight_time=focus.strftime('%Y-%m-%d %H:%M:%S'))
    voltage_svg = line_chart(
        '工况切换附近母线与电池电压',
        {'母线 TMEA003': (window_times, window['bus_voltage_mean'], PALETTE[0]),
         '电池 TMEA005': (window_times, window['battery_voltage_mean'], PALETTE[1])},
        xname='北京时间 · 观测分钟', ylabel='数据库已解码原值 V',
        gap_seconds=90, window_start=window_start, window_end=window_end,
        highlight_time=focus.strftime('%Y-%m-%d %H:%M:%S'))
    soc_times = soc_rows['t_beijing'].dt.strftime('%Y-%m-%d %H:%M:%S')
    soc_svg = (line_chart(
        '匹配 7.84 A 地影分钟的条件 SOC',
        {'条件 SOC': (soc_times, soc_rows['soc_7p84A_pct'], PALETTE[0])},
        xname='北京时间 · 匹配观测分钟', ylabel='SOC %', ymin=0, ymax=100,
        scatter=True, window_start=full_start, window_end=full_end)
        if not soc_rows.empty else '')
    calibration_svg = calibration_chart(calibration)

    phase_stats = {}
    for phase in ('日照', '地影', '未判定'):
        group = frame[frame['phase'].eq(phase)]
        phase_stats[phase] = {
            'minutes': len(group),
            'solar_current_median_A': median(group['solar_current_mean']),
            'bus_current_median_A': median(group['bus_current_mean']),
            'battery_current_median_A': median(group['battery_current_mean']),
            'solar_bus_proxy_median_W': median(group['solar_bus_proxy_W_mean']),
            'bus_measure_median_W': median(group['bus_measure_W_mean']),
            'battery_signed_trial_median_W': median(group['battery_signed_trial_W_mean']),
            'residual_diag_median_W': median(group['residual_diag_W_mean']),
        }
    quality = {key: {'n_numeric_minutes': int(frame[f'{key}_n'].gt(0).sum()),
                     'raw_min': float(frame[f'{key}_min'].min()),
                     'raw_max': float(frame[f'{key}_max'].max())}
               for key in ('bus_voltage', 'bus_current', 'battery_voltage',
                           'battery_current', 'solar_current')}
    analysis = {'phase_stats': phase_stats, 'quality': quality,
                'window': {'start_beijing': window_start, 'end_beijing': window_end,
                           'focus_beijing': focus.strftime('%Y-%m-%d %H:%M:%S'),
                           'minutes': len(window),
                           'phase_minutes': window['phase'].value_counts().to_dict()},
                'days_with_observations': len(day)}
    (root / 'analysis_summary.json').write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')

    component_path = root / 'component_summary.json'
    component_summary = (json.loads(component_path.read_text(encoding='utf-8'))
                         if component_path.is_file() else None)
    if component_summary and sha256(root / 'config' / 'components.json') != component_summary['configuration_sha256']:
        raise ValueError('云尖3D部件配置已更改，需重新查询部件')
    component_rows_html = ''
    md_component_rows = ''
    if component_summary:
        for spec in component_summary['components'].values():
            component_rows_html += ('<tr>' + ''.join(f'<td>{escape(str(cell))}</td>' for cell in (
                spec['name'], spec['code'], f"{spec['valid_source_rows']:,}",
                fmt(spec['mean_bus_voltage_proxy_W'], 2),
                fmt(spec['p95_bus_voltage_proxy_W'], 2),
                fmt(spec['raw_max_A'], 2))) + '</tr>')
            md_component_rows += (f"| {spec['name']} | `{spec['code']}` | "
                                  f"{spec['valid_source_rows']:,} | "
                                  f"{fmt(spec['mean_bus_voltage_proxy_W'],2)} | "
                                  f"{fmt(spec['p95_bus_voltage_proxy_W'],2)} | "
                                  f"{fmt(spec['raw_max_A'],2)} |\n")

    phase_rows_html = ''.join(
        '<tr>' + ''.join(f'<td>{cell}</td>' for cell in (
            phase, f'{phase_stats[phase]["minutes"]:,}',
            fmt(phase_stats[phase]['solar_current_median_A'], 3),
            fmt(phase_stats[phase]['bus_current_median_A'], 3),
            fmt(phase_stats[phase]['battery_current_median_A'], 3),
            fmt(phase_stats[phase]['solar_bus_proxy_median_W'], 1),
            fmt(phase_stats[phase]['bus_measure_median_W'], 1),
            fmt(phase_stats[phase]['battery_signed_trial_median_W'], 1))) + '</tr>'
        for phase in ('日照', '地影', '未判定'))
    energy_rows_html = ''.join(
        '<tr>' + ''.join(f'<td>{cell}</td>' for cell in (
            phase, f'{summary["interval_energy_kWh"][phase]["intervals"]:,}',
            fmt(summary['interval_energy_kWh'][phase]['seconds'], 1),
            fmt(summary['interval_energy_kWh'][phase]['solar_bus_proxy_W'], 3),
            fmt(summary['interval_energy_kWh'][phase]['bus_measure_W'], 3),
            fmt(summary['interval_energy_kWh'][phase]['battery_signed_trial_W'], 3))) + '</tr>'
        for phase in ('日照', '地影', '未判定'))
    quality_rows_html = ''.join(
        '<tr>' + ''.join(f'<td>{escape(str(cell))}</td>' for cell in (
            config['measurements'][key]['name'], config['measurements'][key]['code'],
            f"{summary['numeric_and_range_counts'][key]['outside_workbook_range']:,}",
            fmt(quality[key]['raw_min'], 3), fmt(quality[key]['raw_max'], 3))) + '</tr>'
        for key in ('bus_voltage', 'bus_current', 'battery_voltage',
                    'battery_current', 'solar_current'))
    curve_rows_html = ''.join(
        f'<tr><td>{point["voltage_V"]:.2f}</td><td>{point["capacity_Ah"]:.2f}</td>'
        f'<td>{point["soc_pct"]:.0f}</td><td>{point["dod_pct"]:.0f}</td></tr>'
        for point in calibration['points'])
    soc_rows_html = ''.join(
        f'<tr><td>{escape(str(row.t_beijing))}</td><td>{row.battery_current_mean_A:.3f}</td>'
        f'<td>{row.battery_voltage_mean_V:.3f}</td><td>{row.soc_7p84A_pct:.2f}</td>'
        f'<td>{row.dod_7p84A_pct:.2f}</td></tr>'
        for row in soc_rows.itertuples(index=False))
    chart_specs = [
        ('balance-pair-nearby', '母线与 PL A+B+加热配对功率', pair_nearby_svg,
         '两条曲线均按同一来源行配对、逐分钟求均值；只含工况明确且所有支路有效的分钟，缺测断线。'),
        ('balance-pair-daily', '母线与 PL A+B+加热日中位', pair_daily_svg,
         '各有数据日的配对分钟中位数；无数据日断线，不是整日能量积分。'),
        ('nearby-power', '切换附近四项功率', power_svg,
         '太阳阵代理、母线测点、电池有符号试算和跨测点残差共享北京时间；电池符号和残差只作条件诊断。'),
        ('nearby-current', '切换附近三路电流', current_svg,
         'TMEA006 保留数据库原生正负号，未由放电开关重新赋号。'),
        ('nearby-voltage', '切换附近母线与电池电压', voltage_svg,
         'TMEA003 与 TMEA005 为不同测点；曲线保留分钟均值原值。'),
        ('daily-power', '全时段有数据日功率', daily_svg,
         '各有数据日的观测分钟中位数；没有观测的日期不补点。'),
        ('battery-raw', '全时段电池电流原值', full_battery_svg,
         'TMEA006 每个观测分钟的最小、均值和最大原值均保留；超量程点仍展示，功率计算另行筛选。'),
    ]
    chart_options_html = ''.join(
        f'<option value="{key}"{" selected" if index == 0 else ""}>{escape(title)}</option>'
        for index, (key, title, _, _) in enumerate(chart_specs))
    chart_panels_html = ''.join(
        chart_panel(key, index + 1, title, svg, caption, visible=index == 0)
        for index, (key, title, svg, caption) in enumerate(chart_specs))
    component_svg = component_summary_chart(component_summary) if component_summary else ''
    def unit_codes(spec):
        voltage = spec['voltage']['code']
        return voltage + (' × ' + spec['current']['code'] if 'current' in spec else '（仅电压）')

    def unit_power_cell(measured):
        values = measured.get('power_p50_p95_W')
        return (f'{values[0]:.3g} / {values[1]:.3g}' if values else '—')

    unit_rows = []
    for index, unit in enumerate(unit_inventory['units']):
        a = unit_power['banks']['A']['channels'][index]
        b = unit_power['banks']['B']['channels'][index]
        if any(measured['pl_channel'] != unit['pl_channel'] for measured in (a, b)):
            raise ValueError('云尖3D单机功率摘要与清单顺序不一致')
        unit_rows.append((f"PL_{unit['pl_channel']}", unit['group'], unit['name'],
                          unit_codes(unit['banks']['A']), unit_power_cell(a),
                          unit_codes(unit['banks']['B']), unit_power_cell(b),
                          '仅电压' if unit['measurement_status'] == 'voltage_only' else '电压/电流可配对'))
    unit_rows_html = ''.join(
        '<tr>' + ''.join(f'<td>{escape(str(cell))}</td>' for cell in row) + '</tr>'
        for row in unit_rows)
    md_unit_rows = '\n'.join('| ' + ' | '.join(row) + ' |' for row in unit_rows)
    paired_units = sum(x['measurement_status'] == 'paired_voltage_current'
                       for x in unit_inventory['units'])
    pl_a = unit_power['banks']['A']
    pl_b = unit_power['banks']['B']
    bcr_sum = (bcr_summary['all_eight_output_sum_p05_p50_p95_W']
               if bcr_summary else None)
    bcr_sum_n = (bcr_summary['all_eight_output_paired_source_rows']
                 if bcr_summary else 0)
    unit_ledger_html = f'''<section class="report-section" id="unit-ledger"><div class="section-head"><h2>单机清单与分边界功率账本</h2><p>把同一层的测点放在一起；各层不直接相减</p></div>
<div class="panel"><h3>本星有哪些可识别的供电单机通道</h3><p>遥测大表的 PL A/B 与 BCR A/B 页共列出 {len(unit_rows)} 个具名 PL 供电通道：{paired_units} 个在 A、B 路各有电压和电流，PL_25 推进与 PL_29 智算机 YX 28V2 只有电压。PL_33 GNSS 备份位于 BCR 页；另有 EPS A 路 10 条已配置加热电流支路。通用平台供配电图确认PLA在上板、PLB在下板，同编号A/B开关输出并联到同一外部负载。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>通道</th><th>类别</th><th>名称</th><th>A 路 U×I 代号</th><th>A P50/P95 W</th><th>B 路 U×I 代号</th><th>B P50/P95 W</th><th>计量状态</th></tr></thead><tbody>{unit_rows_html}</tbody></table></div><p class="section-tip">P50/P95 是同一来源行电压×电流的分位数：工作簿有量程的通道按工作区间及关机区间筛选，PL_33 未给物理量程，只保留非负有限值。多数时间关闭的支路中位数可为 0。这些数不是额定功率或单机总能耗；预测层已将22对同名A/B合并为物理单机，设备只计一次、功率和电流相加。</p></div>
<div class="panel"><h3>六层账本与加总规则</h3><div class="table-scroll"><table class="data-table"><thead><tr><th>边界</th><th>已测功率或代理</th><th>本窗口的可复核量</th><th>加总与比较规则</th></tr></thead><tbody>
<tr><td>① PV 输入 / BCR 输出</td><td>8 路 BCR 输入仅有 U<sub>in</sub>；输出有同路 U<sub>out</sub>×I<sub>out</sub></td><td>8 路输出同时配对 {bcr_sum_n:,} 来源行，原值合计中位 {fmt(bcr_sum[1] if bcr_sum else None,2)} W</td><td>这是 BCR 输出侧同来源行诊断和；缺 PV 输入电流，不能算阵列片端发电，也不能直接等同母线输入。</td></tr>
<tr><td>② 电池端</td><td>TMEA005×TMEA006，保留原生正负号</td><td>地影正电流运行推断；端口方向待连接图确认</td><td>充放电端口与母线之间可能有路径损耗；不与母线功率直接做闭合差。</td></tr>
<tr><td>③ EPS 母线测点</td><td>TMEA003×TMEA004</td><td>日照观测分钟中位 {fmt(phase_stats['日照']['bus_measure_median_W'],2)} W；地影 {fmt(phase_stats['地影']['bus_measure_median_W'],2)} W</td><td>这是母线电流测点，不等于已核实的全部负载之和。</td></tr>
<tr><td>④ PL 单机输出</td><td>21 条具名 PL 支路在 A 路或 B 路同来源行求和</td><td>A：{pl_a['pl_complete_source_rows']:,} 完整行，中位 {fmt(pl_a['pl_complete_sum_p05_p50_p95_W'][1],2)} W；B：{pl_b['pl_complete_source_rows']:,} 完整行，中位 {fmt(pl_b['pl_complete_sum_p05_p50_p95_W'][1],2)} W</td><td>两列是各自21路的同来源行和；PL_25、PL_29无电流，PL_33在BCR包。预测层按同编号PA/PB配对求和，任一支路缺测则物理单机功率保持缺测。</td></tr>
<tr><td>⑤ 加热与内部子支路</td><td>10 条 EPS A 加热电流×母线电压代理；另有 TTC PA/LNA、电源轨等遥测</td><td>加热支路逐项见下节；未形成同时间全负载和</td><td>属于负载内部或并列支路，不能在 EPS 母线功率之外再加一次；内部子支路不能与所属单机重复相加。</td></tr>
<tr><td>⑥ 未测及损耗</td><td>推进、YX 28V2 缺支路电流；充电、分流、配电路径边界待核</td><td>未形成独立可测功率</td><td>不能把任何跨边界差额直接命名为未计负载、分流或损耗。</td></tr>
</tbody></table></div><p class="section-tip">当前 BCR、EPS、PL 统计来自不同来源行与层级，表中分位数不能互相相加，故暂不计算闭合残差。</p></div>
<div class="panel method-panel"><h3>光照区与地影区的守恒式</h3><p>以下式子统一取公共母线边界：太阳源与电池放电为流入，负载、电池充电和配电损耗为流出。充放电功率分别取非负幅值；母线电容等储能增加时 dE<sub>母线储能</sub>/dt 为正。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>工况</th><th>同一时刻的功率守恒</th><th>同一连续时段的能量守恒</th></tr></thead><tbody><tr><td>光照区</td><td><code>P_太阳源入母线 + P_电池放电入母线 = P_全部负载 + P_母线入电池充电 + P_配电损耗 + dE_母线储能/dt</code></td><td><code>E_太阳源入母线 + E_电池放电入母线 = E_全部负载 + E_母线入电池充电 + E_配电损耗 + ΔE_母线储能</code></td></tr><tr><td>地影区</td><td><code>P_电池放电入母线 = P_全部负载 + P_配电损耗 + dE_母线储能/dt</code></td><td><code>E_电池放电入母线 = E_全部负载 + E_配电损耗 + ΔE_母线储能</code></td></tr></tbody></table></div><p>光照区充电时，放电项为 0；光照区负载较高而电池助供时，充电项为 0。地影式以没有其他母线电源、没有充电为条件；地影 TMEA007 的非零原值仍保留作零偏诊断，不代入真实太阳发电。各能量项 <code>E_X=∫P_X dt</code>，只在工况一致、测点同步且连续的区间积分；<code>ΔE_母线储能=E_末−E_初</code>。</p><p class="section-tip">“全部负载”已包含 PL、加热及其他负载，不能再把这些子支路加到总负载之外。分流若发生在母线之前，应记入阵列/BCR 上游账本，不在上述母线式重复列入。现有 BCR 输出、EPS 母线电流、电池端和 PL 的测点边界尚未对齐；本页分工况 kWh 是各测点的观测积分，不能直接代入这些式子验证闭合。</p></div></section>'''
    md_unit_ledger = f'''## 单机清单与分边界功率账本

工作簿的 PL A/B 与 BCR A/B 页列出 **{len(unit_rows)} 个具名 PL 供电通道**，其中 **{paired_units} 个**在 A/B 路各有电压与电流，**PL_25 推进供电、PL_29 智算机 YX 28V2** 仅有电压。PL_33 GNSS 备份位于 BCR 页；另有 EPS A 路 **10 条加热电流支路**。供配电图确认PLA/PLB是同编号外部负载的上下板双支路。OBC、TTC、EPS、BCR等遥测包仍不能直接当作与PL通道并列的独立负载。来源代号和工作簿行号见[单机清单](unit_inventory.json)，在轨来源行分位数见[单机功率摘要](unit_power_summary.json)。

| 通道 | 类别 | 名称 | A 路 U×I 代号 | A P50/P95 W | B 路 U×I 代号 | B P50/P95 W | 状态 |
|---|---|---|---|---:|---|---:|---|
{md_unit_rows}

P50/P95为同一来源行电压乘电流的分位数；工作簿有量程的通道按工作电压区间、关机电压区间和电流范围筛选，PL_33未给物理量程，仅保留非负有限值。重复来源行可能影响分位；大量关闭分钟会让中位数为零。现行物理模型已将同编号A/B按并联支路求和，设备数量只计一次。

| 边界 | 已测量 | 本窗口证据 | 加总规则 |
|---|---|---|---|
| PV 输入 / BCR 输出 | 8 路输入仅有电压；输出同路电压×电流 | 8 路输出同时配对 {bcr_sum_n:,} 来源行，原值输出和中位 {fmt(bcr_sum[1] if bcr_sum else None,2)} W | BCR 输出侧同来源行诊断和，不是阵列片端或已核实母线输入 |
| 电池端 | `TMEA005×TMEA006` | 地影正电流支持放电方向，连接图未核 | 电池端与母线侧可能有路径损耗 |
| EPS 母线测点 | `TMEA003×TMEA004` | 日照分钟中位 {fmt(phase_stats['日照']['bus_measure_median_W'],2)} W，地影 {fmt(phase_stats['地影']['bus_measure_median_W'],2)} W | 测点覆盖全部负载未核 |
| PL 单机输出 | 各支路21条具名支路同来源行求和 | A {pl_a['pl_complete_source_rows']:,}完整行、中位{fmt(pl_a['pl_complete_sum_p05_p50_p95_W'][1],2)}W；B {pl_b['pl_complete_source_rows']:,}完整行、中位{fmt(pl_b['pl_complete_sum_p05_p50_p95_W'][1],2)}W | 不含缺电流的PL_25/29，PL_33在BCR包；预测层同编号A/B求和，缺支路不按0W处理 |
| 加热和内部子支路 | 10 条加热支路功率代理；TTC PA/LNA、电源轨等内部测点 | 逐项见下节 | 属于负载层，不能在母线测点功率之外再加一次；内部子支路不能与所属单机重复计入 |
| 未测及损耗 | 充电、分流、配电路径及缺电流支路 | 暂无独立同边界功率 | 不把跨边界差额命名为损耗或未计负载 |

### 光照区与地影区的能量守恒

统一取**公共母线边界**，太阳源和电池放电为流入，全部负载、电池充电和配电损耗为流出。`P_电池放电入母线`、`P_母线入电池充电` 分别是非负幅值；母线电容等储能增加时 `dE_母线储能/dt` 为正。

```text
光照区功率：P_太阳源入母线 + P_电池放电入母线
          = P_全部负载 + P_母线入电池充电 + P_配电损耗 + dE_母线储能/dt
光照区能量：E_太阳源入母线 + E_电池放电入母线
          = E_全部负载 + E_母线入电池充电 + E_配电损耗 + ΔE_母线储能

地影区功率：P_电池放电入母线
          = P_全部负载 + P_配电损耗 + dE_母线储能/dt
地影区能量：E_电池放电入母线
          = E_全部负载 + E_配电损耗 + ΔE_母线储能
```

光照区充电时，放电项为 0；光照区负载较高而电池助供时，充电项为 0。地影式以没有其他母线电源、没有充电为条件；地影 `TMEA007` 的非零原值保留作零偏诊断，不代入真实太阳发电。各能量项 `E_X=∫_I P_X dt`，积分区间须工况一致、测点同步且连续；`ΔE_母线储能=E_末−E_初`。

`P_全部负载` 已包含 PL、加热及其他负载，不能在总负载之外再加一次。分流若发生在母线之前，应记入阵列/BCR 上游账本，不在母线式重复列入。当前 BCR、EPS、PL 来自不同来源行和测量层级，表内分位数不能互相相加或相减；本报告分工况 kWh 是各测点观测积分，不能直接代入这些式子验证物理闭合。

'''
    bcr_rows = []
    for index, item in enumerate(boundary_config['pv_channels']):
        measured = bcr_summary['channels'][index] if bcr_summary else None
        bcr_rows.append((item['name'], item['input_voltage'],
                         fmt(measured['input_voltage_p05_p50_p95_V'][1], 2) if measured else '—',
                         item['output_voltage'], item['output_current'],
                         fmt(measured['output_paired_source_rows'], 0) if measured else '—',
                         fmt(measured['output_power_p05_p50_p95_W'][1], 2) if measured else '—'))
    bcr_rows_html = ''.join(
        '<tr>' + ''.join(f'<td>{escape(str(cell))}</td>' for cell in row) + '</tr>'
        for row in bcr_rows)
    md_bcr_rows = '\n'.join(
        f'| {name} | `{vin}` | {vin_p50} | `{vout}` | `{iout}` | {paired_n} | {power_p50} |'
        for name, vin, vin_p50, vout, iout, paired_n, power_p50 in bcr_rows)
    bcr_evidence = (f"宽表全窗口有 {bcr_summary['source_rows']:,} 条来源行；八路各有约 "
                    f"{bcr_summary['channels'][0]['input_numeric_source_rows']:,} 条可解析输入电压记录。"
                    '该统计覆盖全表来源行，与 EPS A 路限定的来源行不是同一统计母体。'
                    if bcr_summary else '宽表在轨值尚未汇总。')
    bcr_source_link = ('[BCR 在轨统计](bcr_boundary_summary.json)'
                       if bcr_summary else 'BCR 在轨统计尚未生成')
    phase_crosscheck_evidence = (
        f"同一批地影分钟中，{boundary['eclipse_solar_current_below_1A_minutes']:,} 个分钟的 "
        f"TMEA007 均值小于 1 A；{boundary['eclipse_solar_current_outside_workbook_range_minutes']} 个分钟"
        '超出该测点 0–14 A 工作簿量程。工况标志与太阳阵电流并非处处一致，异常值保留待核。')
    voltage_coverage_evidence = (
        f"全时段 {summary['observed_minutes']:,} 个观测分钟里，电池分钟平均电压仅 "
        f"{soc_summary['observed_minutes_voltage_within_curve']:,} 个落在放电曲线 "
        f"{calibration['voltage_V'][0]:.2f}–{calibration['voltage_V'][-1]:.2f} V 内"
        f"（{soc_summary['observed_minutes_voltage_within_curve']/summary['observed_minutes']:.2%}）；"
        f"{soc_summary['observed_minutes_voltage_above_curve']:,} 个高于上界，"
        f"{soc_summary['observed_minutes_voltage_below_curve']:,} 个低于下界。"
        f"若按上界截断，{soc_summary['observed_minutes_voltage_above_curve']/summary['observed_minutes']:.2%} "
        '的观测分钟会机械地变成 100% SOC；这只反映曲线工况不匹配。'
        '越界值不截断成 0% 或 100% SOC，日照充电或静置电压也不能直接套用单一放电曲线。')
    other_diag = bcr_summary['other_diagnostics'] if bcr_summary else None
    temp_evidence = (f"电池主路温度 TMEA010 有 {other_diag['battery_temperature_main_numeric_source_rows']:,} 条可解析来源行，"
                     f"中位 {fmt(other_diag['battery_temperature_main_p05_p50_p95_C'][1],2)} ℃；"
                     f"备路 TMEB010 中位 {fmt(other_diag['battery_temperature_backup_p05_p50_p95_C'][1],2)} ℃。"
                     f"TTC 的 TMUA021 与 EPS 的 TMEA006 仅 {other_diag['ttc_eps_current_same_source_row_pairs']} 条同来源行配对，"
                     f"两路均在 ±10 A 内且绝对值均大于 0.2 A 的只有 {other_diag['ttc_eps_nonzero_within_10A_source_rows']} 条，"
                     '不足以独立确认电流方向。温度测点已经有值，但仍缺少温度、倍率和充电滞回校准，不能据此补出全时段 SOC。'
                     if other_diag else '温度和 TTC 电流候选测点仍待在轨值核验。')
    exception_rows_html = ''.join(
        f'<tr><td>{escape(item["t_beijing"])}</td><td>{item["battery_current_mean_A"]:.3f}</td>'
        f'<td>{item["solar_current_mean_A"]:.3f}</td></tr>'
        for item in boundary['eclipse_battery_current_nonpositive_examples'])
    boundary_html = f'''<section class="report-section" id="verification"><div class="section-head"><h2>测点核验与待评指标</h2><p>工作簿测点、在轨观测和电路结论分别列明</p></div>
<div class="panel"><h3>电池电流方向：地影观测支持正值放电</h3><p>在 TMZS003 明确为地影、工况无冲突且电池电压和电流分钟均值处于工作簿量程的 {boundary['eclipse_clear_valid_minutes']:,} 分钟中，放电开关均为“开”，{boundary['eclipse_battery_current_positive_over_0p2A_minutes']:,} 分钟的 TMEA006 均值大于 0.2 A。{boundary['eclipse_bus_battery_paired_minutes']:,} 个同时具有有效母线测点的地影分钟，TMEA004−TMEA006 中位为 {fmt(boundary['eclipse_bus_minus_battery_current_median_A'],3)} A，5–95 分位为 {fmt(boundary['eclipse_bus_minus_battery_current_p05_A'],3)} 至 {fmt(boundary['eclipse_bus_minus_battery_current_p95_A'],3)} A。这支持“正电流对应地影放电”的运行推断，仍需电流传感器箭头和供配电连接图确认。</p><p>日照有效分钟 {boundary['sun_clear_valid_minutes']:,} 个，其中开关为“开”的有 {boundary['sun_discharge_switch_open_minutes']:,} 个；负电流低于 −0.2 A 的有 {boundary['sun_battery_current_negative_below_minus_0p2A_minutes']:,} 个，正电流高于 0.2 A 的有 {boundary['sun_battery_current_positive_over_0p2A_minutes']:,} 个。因此不能仅凭开关“开”给电流赋放电方向。以下异常分钟保留原值，需检查工况标志与太阳阵电流是否一致。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>北京时间</th><th>TMEA006 分钟均值 A</th><th>TMEA007 分钟均值 A</th></tr></thead><tbody>{exception_rows_html}</tbody></table></div></div>
<div class="panel"><h3>BCR 八路光伏输入与输出测点</h3><p>遥测工作簿定义 {boundary['bcr_pv_input_voltage_channel_count']} 路 BCR PV 输入侧电压，以及对应的输出侧电压和电流。{bcr_evidence} 输入侧电流代号尚未找到，不能把输入电压乘输出电流当成太阳阵端功率。BCR PV 输入侧电压也不能未经接线与压降核对就称作阵列片端电压。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>通道</th><th>PV 输入电压代号</th><th>输入电压中位 V</th><th>输出电压代号</th><th>输出电流代号</th><th>输出侧配对行</th><th>输出侧功率中位 W</th></tr></thead><tbody>{bcr_rows_html}</tbody></table></div><p class="section-tip">中位数基于全窗口可解析的来源行，包含日照和地影，未按唯一时间戳去重；输出侧功率为同一来源行 U<sub>out</sub>×I<sub>out</sub> 的分位统计，不等于阵列片端发电功率，也不能直接与 EPS A 路能量相加。</p></div>
<div class="panel"><h3>容量、SOH 与整星评分的证据条件</h3><div class="table-scroll"><table class="data-table"><thead><tr><th>目标</th><th>当前能确认</th><th>完成评定还需要</th></tr></thead><tbody><tr><td>全时段 SOC</td><td>仅 {soc_summary['matched_minutes']} 个 7.84 A 地影分钟可作条件电压映射</td><td>电池方向与时间对齐、初始 SOC、温度/倍率/充电滞回校准和可验证的电流积分重置点</td></tr><tr><td>在轨可用容量、SOH</td><td>37.69 Ah 是标定表终点容量；{boundary['observed_eclipse_runs']} 段地影观测中最长连续 {boundary['longest_contiguous_observed_eclipse_minutes']} 分钟</td><td>覆盖明确起止 SOC 与放电截止条件的连续在轨放电或专门容量试验，以及同口径基准容量</td></tr><tr><td>全部负载与能源闭合</td><td>TMEA004 为母线电流测点；另有 10 条加热支路代理</td><td>EPS/PCDU 单线图、全部负载支路目录、测点边界和供电/分流路径</td></tr><tr><td>健康评分</td><td>已有质量、短间隔能量和局部条件 SOC</td><td>先核准上述物理边界，再定义本星评分指标、权重、阈值和有效覆盖要求</td></tr></tbody></table></div><p class="section-tip">目前不把 7.84 A 单一放电曲线、零散地影分钟或跨测点残差转换成全时段 SOC、在轨容量、SOH 或整星健康分。</p></div></section>'''
    marker = '</tbody></table></div></div>\n<div class="panel"><h3>BCR'
    if boundary_html.count(marker) != 1:
        raise ValueError('云尖3D电池方向核验段落结构异常')
    boundary_html = boundary_html.replace(
        marker, f'</tbody></table></div><p>{escape(phase_crosscheck_evidence)}</p>'
                f'<p>{escape(temp_evidence)}</p></div>\n<div class="panel"><h3>BCR', 1)
    scope_bcr = ('八路 BCR PV 输入电压在轨值已核验，阵列片端边界待核；'
                 if bcr_summary else '工作簿已列出八路 BCR PV 输入电压代号，在轨值与阵列片端边界待核；')
    phase2_html = phase2_yj_section(root)
    phase2_nav = ('<a href="#phase2-yj-3d">第二阶段</a>'
                  if phase2_html else '')
    title_suffix = '（含第二阶段）' if phase2_html else ''
    subtitle_suffix = (' · 第二阶段任务负载、BCR 与剩余电量验证'
                       if phase2_html else '')
    css = (BASE / 'report_style.css').read_text(encoding='utf-8')
    css += '''\ncode{background:#edf2f7;padding:2px 4px;border-radius:4px;overflow-wrap:anywhere}
.battery-figures{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:15px 0}
.battery-figures figure,.component-summary-figure{margin:0;padding:12px;border:1px solid var(--line);border-radius:10px;background:#fff}
.battery-figures svg,.component-summary-figure svg{display:block;width:100%;height:auto}
.battery-figures figcaption,.component-summary-figure figcaption{color:var(--soft);font-size:11px}
.component-summary-figure{overflow-x:auto}.component-summary-figure svg{min-width:760px}
.source-links{display:flex;flex-wrap:wrap;gap:8px 18px}
@media(max-width:850px){.battery-figures{grid-template-columns:1fr}}'''
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>云尖3D星 · 能源评估{title_suffix}</title><style>{css}</style></head><body>
<header class="hero"><div class="hero-inner"><p class="eyebrow">Satellite energy / observed telemetry</p><h1>云尖3D星能源评估</h1><p class="hero-sub">宽表遥测观测样本 · 分工况能量、放电标定与测量边界{subtitle_suffix}</p><div class="hero-meta"><span>卫星 <b>YJ-3D</b></span><span>已观测 <b>{summary['observed_minutes']:,}</b> 分钟</span><span>评估窗口 <b>{escape(design['assessment_start_beijing'])}—{escape(design['assessment_end_exclusive_beijing'])}（不含终点）</b></span><span>来源 <b>chdb_3D.yc_history · UTC</b></span></div></div></header>
<nav class="report-nav" aria-label="报告章节"><div class="nav-inner"><a href="#overview">结论与质量</a><a href="#energy">分工况能量</a><a href="#battery">电池标定</a><a href="#components">加热支路</a><a href="#boundary">物理边界</a><a href="#charts">遥测图表</a><a href="#sources">来源明细</a>{phase2_nav}</div></nav>
<main class="report-shell"><section class="report-section" id="overview"><div class="section-head"><h2>结论与数据质量</h2><p>只描述已观测样本及其可核实的计算量</p></div>
<div class="scope-note"><strong>结论范围</strong><p>本星 7.84 A 放电标定曲线仅支持匹配工况的条件 SOC/DoD；全时段 SOC、在轨容量、SOH 与健康评分均未评定。太阳阵端电压、全部负载覆盖范围和电池电流方向连接图尚待核实；跨测点差额不作母线损耗解释。</p></div>
<div class="kpi-grid"><div class="kpi"><span class="kpi-label">已观测分钟</span><strong class="kpi-value">{summary['observed_minutes']:,}</strong><span class="kpi-foot">首末实测之间，缺口不补</span></div><div class="kpi"><span class="kpi-label">首末跨度分钟覆盖率</span><strong class="kpi-value">{summary['calendar_minute_coverage']:.2%}</strong><span class="kpi-foot">不是整个配置窗口的覆盖率</span></div><div class="kpi"><span class="kpi-label">明确地影分钟</span><strong class="kpi-value">{summary['phase_minutes'].get('地影',0):,}</strong><span class="kpi-foot">观测样本的 {summary['phase_minutes'].get('地影',0)/summary['observed_minutes']:.2%}</span></div><div class="kpi"><span class="kpi-label">标定条件匹配分钟</span><strong class="kpi-value">{soc_summary['matched_minutes']}</strong><span class="kpi-foot">7.84±0.50 A 地影；SOC 中位 {fmt(soc_summary['soc_pct_median'],2)}%</span></div></div>
<div class="panel"><h3>来源与覆盖</h3><p>EPS A 路来源记录 {summary['source_rows']:,} 行、唯一时间戳 {summary['unique_timestamps']:,} 个；合并重复记录 {summary['duplicate_rows']:,} 行，冲突时间戳 {summary['conflicted_timestamps']:,} 个。首末实测为 {escape(summary['first_sample_beijing'])} 至 {escape(summary['last_sample_beijing'])}；工况判据冲突 {summary['phase_conflict_minutes']:,} 分钟。</p><p>状态优先使用 TMZS003；缺测时根据 TMEA007 的 1 A 阈值回退。分钟内状态切换或阈值跨越保持“未判定”。</p></div>
<div class="panel"><h3>遥测质量与工况</h3><p class="table-caption">超量程原值仍保留在原值图；功率与积分仅使用工作簿范围内的配对样本。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>遥测</th><th>代号</th><th>超工作簿量程的唯一时间戳</th><th>原值最小</th><th>原值最大</th></tr></thead><tbody>{quality_rows_html}</tbody></table></div><div class="table-scroll" style="margin-top:15px"><table class="data-table"><thead><tr><th>工况</th><th>分钟</th><th>太阳阵电流中位 A</th><th>母线电流中位 A</th><th>电池电流中位 A</th><th>太阳阵代理中位 W</th><th>母线测点中位 W</th><th>电池试算中位 W</th></tr></thead><tbody>{phase_rows_html}</tbody></table></div></div></section>
<section class="report-section" id="energy"><div class="section-head"><h2>分工况能量</h2><p>日照、地影与未判定分别列账</p></div><div class="panel"><h3>原始相邻采样梯形积分 <span class="badge">0 &lt; Δt ≤ 10 秒 · kWh</span></h3><p>只纳入相邻两点三路功率均在工作簿量程内的区间。跨工况区间列入“未判定”；长缺口不外推，表中数值不是整个日历窗口的总电量。</p><div class="table-scroll"><table class="data-table"><thead><tr><th>工况</th><th>有效区间</th><th>有效秒数</th><th>太阳阵代理 kWh</th><th>母线测点 kWh</th><th>电池试算 kWh</th></tr></thead><tbody>{energy_rows_html}</tbody></table></div><p class="section-tip">太阳阵列值采用 TMEA003 母线电压折算，并非阵列端实际发电；电池列保留 TMEA006 原生符号，端口方向待连接图核准。</p></div></section>
<section class="report-section" id="battery"><div class="section-head"><h2>电池放电标定与条件 SOC</h2><p>本星 7.84 A 放电表，曲线外不外推</p></div><div class="panel battery-detail"><h3>本星标定 <span class="badge">容量终点 {calibration['capacity_Ah']:.2f} Ah</span></h3><p><a href="../../../Data/0010-云尖3D星/volt_capacity_3D_v0.csv">原始标定表</a>记录 21.00–28.18 V 对应 10%–100% SOC。37.69 Ah 是表内容量，不是在轨实测可用容量；表点之间按电压线性插值，越界不赋值。</p><div class="battery-figures"><figure>{calibration_svg}<figcaption>7.84 A 放电条件下的 V–SOC 表点与线性插值。</figcaption></figure><figure>{soc_svg}<figcaption>仅显示同时满足工况、电流和电压条件的 {soc_summary['matched_minutes']} 个分钟，散点之间不连线。</figcaption></figure></div><details class="aux-details"><summary>查看 10 个标定点</summary><div class="table-scroll"><table class="data-table compact-table"><thead><tr><th>标定电压 V</th><th>表内容量 Ah</th><th>SOC %</th><th>DoD %</th></tr></thead><tbody>{curve_rows_html}</tbody></table></div></details><p>地影观测 {soc_summary['observed_eclipse_minutes']:,} 分钟中，{soc_summary['eclipse_minutes_above_curve_voltage']:,} 个分钟平均电压超过曲线 28.18 V 上界。仅对 TMZS003 明确标记且无工况冲突、分钟平均电流在 {calibration['current_A']:.2f}±{calibration['config']['current_match_tolerance_A']:.2f} A、分钟平均电压在曲线内的 {soc_summary['matched_minutes']} 分钟映射：SOC 中位 {fmt(soc_summary['soc_pct_median'],2)}%，5 分位 {fmt(soc_summary['soc_pct_p5'],2)}%，最低 {fmt(soc_summary['soc_pct_min'],2)}%；最大对应 DoD {fmt(soc_summary['dod_pct_max'],2)}%。±0.5 A 是分析容差，不是标定误差保证；分钟均值也不能证明每个原始样本均在该电流范围内。</p><details class="aux-details"><summary>查看 {soc_summary['matched_minutes']} 个条件映射分钟</summary><div class="table-scroll"><table class="data-table"><thead><tr><th>北京时间</th><th>分钟平均电流 A</th><th>分钟平均电压 V</th><th>条件 SOC %</th><th>条件 DoD %</th></tr></thead><tbody>{soc_rows_html}</tbody></table></div></details></div></section>
<section class="report-section" id="components"><div class="section-head"><h2>加热支路功率代理</h2><p>10 条 EPS A 路主路加热测点</p></div><div class="panel"><p>按本星工作簿的支路代号分别配置。下列功率为 TMEA003 母线电压 × 支路电流的来源行代理值；重复来源行可能影响均值和 p95。本表的单支路文件只含全窗口聚合量，因此不绘制单支路连续曲线；整星平衡章节另有十条加热支路合计的配对分钟曲线，该合计不等于全部负载。</p><figure class="component-summary-figure">{component_svg}<figcaption>蓝条为来源行均值，绿条为 p95；同一支路的两个量均只在工作簿范围内配对样本上计算。</figcaption></figure><details class="aux-details"><summary>查看支路测点与数值</summary><div class="table-scroll"><table class="data-table"><thead><tr><th>支路</th><th>代号</th><th>量程内配对行</th><th>平均代理 W</th><th>p95 代理 W</th><th>原值最大 A</th></tr></thead><tbody>{component_rows_html}</tbody></table></div></details></div></section>
<section class="report-section" id="boundary"><div class="section-head"><h2>功率口径与物理边界</h2><p>区分测点功率、母线代理与条件诊断</p></div><div class="panel method-panel"><div class="table-scroll"><table class="data-table"><thead><tr><th>页面量</th><th>计算</th><th>可作何种解释</th></tr></thead><tbody><tr><td>太阳阵·母线电压代理</td><td><code>TMEA003 × TMEA007</code></td><td>太阳阵电流按母线电压折算；阵列端电压未提供。</td></tr><tr><td>母线电流测点功率</td><td><code>TMEA003 × TMEA004</code></td><td>母线电流测点；是否覆盖全部负载待核。</td></tr><tr><td>电池端有符号试算</td><td><code>TMEA005 × TMEA006</code></td><td>保留数据库原生符号；电流传感器方向待连接图确认。</td></tr><tr><td>跨测点残差</td><td><code>太阳阵代理 + 电池试算 − 母线测点</code></td><td>不同电压侧与支路边界的诊断差额，不是母线损耗或物理闭合。</td></tr></tbody></table></div><p class="section-tip">地影仅 {summary['phase_minutes'].get('地影',0):,}/{summary['observed_minutes']:,} 个观测分钟，不能据此推断整段轨道的日照比例。地影 TMEA007 电流中位仍为 {fmt(phase_stats['地影']['solar_current_median_A'],3)} A，母线电压折算值为 {fmt(phase_stats['地影']['solar_bus_proxy_median_W'],1)} W；应核查零偏，不能解释为地影发电。电池曲线的充电滞回、电流倍率、温度和老化未建模。</p></div></section>
<section class="report-section" id="charts"><div class="section-head"><h2>遥测图表</h2><p>全时段与日照/地影切换附近共用一个图表区域</p></div><div class="panel"><p>切换窗口为北京时间 {escape(window_start)}—{escape(window_end)}，竖线标记 {escape(focus.strftime('%Y-%m-%d %H:%M:%S'))}；其中日照 {analysis['window']['phase_minutes'].get('日照',0)} 分钟、地影 {analysis['window']['phase_minutes'].get('地影',0)} 分钟。全时段曲线不填补缺口；连线仅辅助阅读，不代表未观测区间的状态。</p><div class="chart-picker"><label for="telemetry-select">选择图表</label><select id="telemetry-select" aria-controls="telemetry-display">{chart_options_html}</select><span class="picker-count">共 {len(chart_specs)} 张</span></div><div class="chart-stack" id="telemetry-display">{chart_panels_html}</div></div></section>
<section class="report-section" id="sources"><div class="section-head"><h2>来源与明细</h2><p>本地文件可复核测点、时间窗与计算结果</p></div><div class="panel"><div class="source-links"><a href="data_power_minute.csv">逐分钟数据</a><a href="energy_summary.json">来源与积分摘要</a><a href="analysis_summary.json">分工况统计</a><a href="battery_soc_summary.json">电池条件映射摘要</a><a href="data_battery_soc_minute.csv">电池条件映射分钟</a><a href="config/battery_curve.json">电池曲线配置</a><a href="component_summary.json">支路统计</a><a href="config/components.json">支路配置</a><a href="config/telemetry_params.json">遥测映射</a></div><p class="section-tip">遥测工作簿 SHA-256：<code>{summary['provenance']['workbook_sha256']}</code>。数据库 UTC 时间戳统一转换为北京时间展示。</p></div></section>
<footer class="report-footer">本报告只描述已观测遥测及其条件计算量；测点边界需结合本星电路资料核实。</footer></main>'''
    html += '''<script>(function(){const select=document.getElementById("telemetry-select");const panels=[...document.querySelectorAll("[data-chart-panel]")];function show(){for(const panel of panels)panel.hidden=panel.getAttribute("data-chart-panel")!==select.value;}select.addEventListener("change",show);show();})();</script></body></html>'''
    html_replacements = (
        ('<a href="#components">加热支路</a>',
         '<a href="#unit-ledger">单机清单</a><a href="#whole-balance">整星平衡</a><a href="#components">加热支路</a>'),
        ('<a href="#boundary">物理边界</a>',
         '<a href="#boundary">物理边界</a><a href="#verification">测点核验</a>'),
        ('太阳阵端电压、全部负载覆盖范围和电池电流方向连接图尚待核实；',
         scope_bcr + '全部负载覆盖范围和电池电流方向连接图尚待核实；'),
        ('太阳阵电流按母线电压折算；阵列端电压未提供。',
         '太阳阵电流按母线电压折算；BCR PV 输入电压另有测点，尚未与该电流同侧配对。'),
        ('表点之间按电压线性插值，越界不赋值。</p>',
         '表点之间按电压线性插值，越界不赋值。</p><p>' + escape(voltage_coverage_evidence) + '</p>'),
        ('<section class="report-section" id="charts">',
         boundary_html + '<section class="report-section" id="charts">'),
        ('<section class="report-section" id="components">',
         unit_ledger_html + balance_html + '<section class="report-section" id="components">'),
        ('<footer class="report-footer">',
         phase2_html + '<footer class="report-footer">'),
        ('<a href="analysis_summary.json">分工况统计</a>',
         '<a href="analysis_summary.json">分工况统计</a><a href="boundary_summary.json">测点核验摘要</a><a href="bcr_boundary_summary.json">BCR 在轨统计</a><a href="config/boundary_candidates.json">BCR 测点清单</a>' if bcr_summary else '<a href="analysis_summary.json">分工况统计</a><a href="boundary_summary.json">测点核验摘要</a><a href="config/boundary_candidates.json">BCR 测点清单</a>'),
        ('<a href="component_summary.json">支路统计</a>',
         '<a href="unit_inventory.json">单机清单</a><a href="unit_power_summary.json">支路功率摘要</a><a href="power_model/physical_unit_model_report.md">物理单机模型</a><a href="power_model/ab_physical_aggregation_report.md">A/B聚合证据</a><a href="balance_ledger_summary.json">配对功率账本</a><a href="data_balance_pair_minute.csv">配对分钟曲线数据</a><a href="component_summary.json">支路统计</a>'),
    )
    for old, new in html_replacements:
        if html.count(old) != 1:
            raise ValueError(f'云尖3D HTML 目标文本数量异常: {old[:30]}')
        html = html.replace(old, new, 1)
    (root / 'framework_documentation_YJ-3D.html').write_text(html, encoding='utf-8')

    md_phase = '\n'.join(
        f"| {phase} | {phase_stats[phase]['minutes']:,} | {fmt(phase_stats[phase]['solar_current_median_A'],3)} | {fmt(phase_stats[phase]['bus_current_median_A'],3)} | {fmt(phase_stats[phase]['battery_current_median_A'],3)} | {fmt(phase_stats[phase]['solar_bus_proxy_median_W'],1)} | {fmt(phase_stats[phase]['bus_measure_median_W'],1)} | {fmt(phase_stats[phase]['battery_signed_trial_median_W'],1)} |"
        for phase in ('日照', '地影', '未判定'))
    md_energy = '\n'.join(
        f"| {phase} | {summary['interval_energy_kWh'][phase]['intervals']:,} | {fmt(summary['interval_energy_kWh'][phase]['seconds'],1)} | {fmt(summary['interval_energy_kWh'][phase]['solar_bus_proxy_W'],3)} | {fmt(summary['interval_energy_kWh'][phase]['bus_measure_W'],3)} | {fmt(summary['interval_energy_kWh'][phase]['battery_signed_trial_W'],3)} |"
        for phase in ('日照', '地影', '未判定'))
    md_quality = '\n'.join(
        f"| {config['measurements'][key]['name']} | `{config['measurements'][key]['code']}` | "
        f"{summary['numeric_and_range_counts'][key]['outside_workbook_range']:,} | "
        f"{fmt(quality[key]['raw_min'],3)} | {fmt(quality[key]['raw_max'],3)} |"
        for key in ('bus_voltage', 'bus_current', 'battery_voltage',
                    'battery_current', 'solar_current'))
    md_curve = '\n'.join(
        f'| {point["voltage_V"]:.2f} | {point["capacity_Ah"]:.2f} | '
        f'{point["soc_pct"]:.0f} | {point["dod_pct"]:.0f} |'
        for point in calibration['points'])
    md = f'''# 云尖3D星整星能源评估（UTC 宽表复算）

## 来源、时间和代号

来源为 `chdb_3D.yc_history` 宽表，时间列 `timestamp` 类型为 `DateTime64(3,'UTC')`。按北京时间半开窗口 `[{design['assessment_start_beijing']}, {design['assessment_end_exclusive_beijing']})` 取数；数据库内存在超出当前日期的异常时间记录，未纳入本窗口。工作簿为 [云尖3D星遥测大表](../../../Data/0010-云尖3D星/云尖3D星_遥测大表0813.xlsx)，校验哈希和查询边界见[来源摘要](energy_summary.json)。[代号配置](config/telemetry_params.json)依据 EPS A 路和姿控状态表逐项重配。

| 用途 | 本星代号 | 工作簿名称 | 计算用法 |
|---|---|---|---|
| 母线电压 | `TMEA003` | 母线电压遥测 | 功率电压基准 |
| 母线电流 | `TMEA004` | 母线电流遥测 | 母线测点功率；全部负载覆盖范围待核 |
| 电池输出电压 | `TMEA005` | 蓄电池输出电压遥测 | 电池端电压 |
| 电池充放电电流 | `TMEA006` | 充放电电流遥测 | 保留原生正负号的电池端条件试算 |
| 太阳阵电流 | `TMEA007` | 太阳阵电流遥测 | 工况回退及母线电压折算代理 |
| 光照标志 | `TMZS003` | 计算的光照区标志 | 主判据；分钟内切换不归类 |
| 放电开关 | `TMEA001` | 放电开关状态 | 上下文；不改写电流符号 |

`TMEB003/006/007` 属 B 路备份诊断，工作簿中 `TMEB004/005` 标为“/”，本轮不把 B 路当作独立总功率测点。太阳阵端电压、各负载支路总和、PCDU 边界尚未核实。

## 数据覆盖与质量

EPS A 路有 {summary['source_rows']:,} 条来源记录、{summary['unique_timestamps']:,} 个唯一 UTC 时间戳；合并完全重复或可合并记录 {summary['duplicate_rows']:,} 条，冲突时间戳 {summary['conflicted_timestamps']:,} 个。首末实测为 **{summary['first_sample_beijing']} 至 {summary['last_sample_beijing']}**，其间有数据 {summary['observed_minutes']:,} 分钟，日历分钟覆盖率 {summary['calendar_minute_coverage']:.2%}。工况判据冲突 {summary['phase_conflict_minutes']:,} 分钟，长于 10 秒的相邻采样缺口 {summary['gaps_over_limit']:,} 个。

分钟文件保留各测点可解析原值的最小、均值和最大；超出工作簿量程的值不从原值图删除。功率和短间隔积分仅使用范围内的配对样本。逐项计数见[机器分析摘要](analysis_summary.json)和[逐分钟数据](data_power_minute.csv)。

| 遥测 | 代号 | 超工作簿量程的唯一样本 | 未筛选原值最小 | 未筛选原值最大 |
|---|---|---:|---:|---:|
{md_quality}

## 工况与功率

光照优先按 `TMZS003` 的“光照区/阴影区”判断；缺测时按同分钟 `TMEA007` 极值与 1 A 阈值回退。`P_solar_proxy=TMEA003×TMEA007` 是**太阳阵电流在母线电压下的代理功率**，不是太阳阵端实际发电；`P_bus=TMEA003×TMEA004` 是母线电流测点功率；`P_battery_trial=TMEA005×TMEA006` 保留电池电流原生符号。地影电池电流为正、日照常接近零或为负，方向与放电假设相符，但最终仍需连接图核准。

| 工况 | 分钟 | 太阳阵电流中位 A | 母线电流中位 A | 电池电流中位 A | 太阳阵代理中位 W | 母线测点中位 W | 电池试算中位 W |
|---|---:|---:|---:|---:|---:|---:|---:|
{md_phase}

跨测点残差 `P_solar_proxy+P_battery_trial−P_bus` 只用于发现不一致；不同电压侧和支路边界未对齐，不能命名为母线损耗或物理闭合。

地影仅 {summary['phase_minutes'].get('地影',0):,}/{summary['observed_minutes']:,} 个观测分钟（{summary['phase_minutes'].get('地影',0)/summary['observed_minutes']:.2%}），不能据此推断全段轨道的日照占比。地影 `TMEA007` 电流中位数仍有 {fmt(phase_stats['地影']['solar_current_median_A'],3)} A，在母线电压下形成 {fmt(phase_stats['地影']['solar_bus_proxy_median_W'],1)} W 代理值；需核对传感器零偏，不能解释为地影发电。

## 短间隔能量

仅对相邻两点三路功率都有效且 `0<Δt≤10 s` 的区间进行梯形积分，缺口不外推。工况使用两端所在分钟的判定；跨工况区间列入“未判定”，与 01 星的分钟工况积分口径一致。下列值只代表实际纳入的观测秒数，不是整个 {summary['span_calendar_minutes']:,} 分钟日历跨度的能量。

| 工况 | 有效区间 | 有效秒数 | 太阳阵代理 kWh | 母线测点 kWh | 电池试算 kWh |
|---|---:|---:|---:|---:|---:|
{md_energy}

## 本星电池放电曲线与条件 SOC

[本星标定 CSV](../../../Data/0010-云尖3D星/volt_capacity_3D_v0.csv)记录 **7.84 A 恒流放电**条件下的电压、容量、DoD 和 SOC；终点容量为 **{calibration['capacity_Ah']:.2f} Ah**。该值是标定表容量，不等于在轨实测可用容量。[曲线配置](config/battery_curve.json)登记专属来源、电流、测点和分析容差；曲线及配置哈希见[条件映射摘要](battery_soc_summary.json)。

| 电压 V | 表内容量 Ah | SOC % | DoD % |
|---:|---:|---:|---:|
{md_curve}

采用表点间电压线性插值。只选 `TMZS003` 明确标记为地影且无工况冲突、`TMEA006` 分钟平均电流在 **{calibration['current_A']:.2f}±{calibration['config']['current_match_tolerance_A']:.2f} A**、`TMEA005` 分钟平均电压在 **{calibration['voltage_V'][0]:.2f}–{calibration['voltage_V'][-1]:.2f} V** 的分钟。电压越界不外推或截断；该 ±0.5 A 是分析容差，不是标定表提供的误差保证。地影观测 {soc_summary['observed_eclipse_minutes']:,} 分钟中，{soc_summary['eclipse_minutes_above_curve_voltage']:,} 个分钟的平均电压高于曲线上界，仅 **{soc_summary['matched_minutes']} 分钟**同时满足上述条件。其条件 SOC 中位 **{fmt(soc_summary['soc_pct_median'],2)}%**、5 分位 **{fmt(soc_summary['soc_pct_p5'],2)}%**、最小 **{fmt(soc_summary['soc_pct_min'],2)}%**，相应最大 DoD **{fmt(soc_summary['dod_pct_max'],2)}%**。逐分钟数值见[条件映射数据](data_battery_soc_minute.csv)。

这里由分钟平均电压插值，分钟平均电流的匹配不能证明每个原始样本都在 7.84 A 附近。匹配分钟只分布在 **{soc_summary['first_matched_beijing']} 至 {soc_summary['last_matched_beijing']}**，不代表整个评估窗口的 SOC 分布或最大 DoD；充电滞回、电流倍率、温度及老化未建模。没有独立在轨容量试验，不能从这张曲线推断 SOH。

## 可配置能源支路

按[本星部件配置](config/components.json)分别启停 10 个 EPS A 路主路加热电流测点；[支路统计](component_summary.json)按工作簿 0–1 A 电流量程和 21–29.2 V 母线电压量程计算 `TMEA003×支路电流`。数值是母线电压代理，不是完整负载拆解；下表的均值、p95 基于来源行，重复记录可能产生权重偏差。原值极值未筛选展示。

| 支路 | 代号 | 量程内配对行 | 平均代理 W | p95 代理 W | 未筛选原值最大 A |
|---|---|---:|---:|---:|---:|
{md_component_rows}

## 评估边界

现已采用云尖3D星专属 37.69 Ah / 7.84 A 放电标定表，未套用三体 01 星的参数。当前只给匹配观测分钟的条件 SOC/DoD，不给全时段 SOC、在轨容量、SOH 或健康评分。母线电流是否覆盖全部负载、太阳阵电压及电池电流传感器方向仍需电路资料核对。[离线图表报告](framework_documentation_YJ-3D.html)展示全时段和工况切换附近曲线。
'''
    md_verification = f'''## 测点核验与待评指标

### 电池电流方向

在 `TMZS003` 明确地影、无工况冲突且电池电压、电流均处于工作簿量程的 **{boundary['eclipse_clear_valid_minutes']:,}** 个分钟中，`TMEA001` 放电开关均为“开”，**{boundary['eclipse_battery_current_positive_over_0p2A_minutes']:,}** 个分钟的 `TMEA006` 均值大于 0.2 A。**{boundary['eclipse_bus_battery_paired_minutes']:,}** 个同时具备有效母线测点的地影分钟，`TMEA004−TMEA006` 中位 **{fmt(boundary['eclipse_bus_minus_battery_current_median_A'],3)} A**，5–95 分位 **{fmt(boundary['eclipse_bus_minus_battery_current_p05_A'],3)} 至 {fmt(boundary['eclipse_bus_minus_battery_current_p95_A'],3)} A**。这支持“正值对应地影放电”的运行推断；传感器方向仍需供配电连接图确认。

日照有效分钟 **{boundary['sun_clear_valid_minutes']:,}** 个，其中放电开关为“开”的 **{boundary['sun_discharge_switch_open_minutes']:,}** 个，电池电流小于 −0.2 A 的 **{boundary['sun_battery_current_negative_below_minus_0p2A_minutes']:,}** 个，大于 0.2 A 的 **{boundary['sun_battery_current_positive_over_0p2A_minutes']:,}** 个。开关“开”不能单独作为电流正负的赋号依据。地影条件下另有 **{len(boundary['eclipse_battery_current_nonpositive_examples'])}** 个不符合正值放电的分钟，详情及其太阳阵电流原值见[测点核验摘要](boundary_summary.json)，不删除或强制改号。

{phase_crosscheck_evidence}

{temp_evidence}

### BCR 光伏输入与输出

工作簿列出以下 **8 路 BCR PV 输入侧电压**，及各自的 BCR 输出侧电压和电流。{bcr_evidence} 工作簿未找到同侧 PV 输入电流代号；不能把输入侧电压乘输出侧电流称为阵列端发电功率。BCR PV 输入侧电压是否等于阵列片端电压还需核对线缆与测点位置。

| 通道 | PV 输入电压代号 | 输入电压中位 V | 输出电压代号 | 输出电流代号 | 输出侧配对行 | 输出侧功率中位 W |
|---|---|---:|---|---|---:|---:|
{md_bcr_rows}

代号及工作簿页签见[BCR 测点清单](config/boundary_candidates.json)；来源行分位值见{bcr_source_link}。中位数覆盖日照与地影，未按唯一时间戳去重；输出侧功率使用同一来源行的输出电压乘输出电流，不等于阵列片端发电功率，也不能与 EPS A 路能量直接相加。

### 尚需的独立证据

| 目标 | 目前证据 | 完成评定还需 |
|---|---|---|
| 全时段 SOC | 仅 {soc_summary['matched_minutes']} 个近 7.84 A 地影分钟的条件映射 | 电池电流方向和时间对齐、初始 SOC、温度/倍率/充电滞回校准、电流积分重置点 |
| 在轨可用容量、SOH | 37.69 Ah 为标定表终点容量；{boundary['observed_eclipse_runs']} 段地影观测最长连续 {boundary['longest_contiguous_observed_eclipse_minutes']} 分钟 | 起止 SOC 与截止条件明确的连续在轨放电或容量试验，同口径基准容量 |
| 全部负载与能源闭合 | `TMEA004` 母线测点及 10 条加热支路代理 | EPS/PCDU 单线图、全部负载目录、测点边界和供电/分流路径 |
| 健康评分 | 数据质量、短间隔能量、局部条件 SOC | 物理边界核准后，本星指标、权重、阈值及有效覆盖要求 |

不把单一 7.84 A 放电曲线、零散地影分钟或跨测点残差换算为全时段 SOC、在轨容量、SOH 或健康评分。

'''
    if md.count('## 评估边界') != 1:
        raise ValueError('云尖3D Markdown 评估边界章节数量异常')
    md = md.replace('## 评估边界', md_verification + '## 评估边界', 1)
    if md.count('## 可配置能源支路') != 1:
        raise ValueError('云尖3D Markdown 单机章节插入位置异常')
    md = md.replace('## 可配置能源支路', md_unit_ledger + md_balance + '## 可配置能源支路', 1)
    md = md.replace('采用表点间电压线性插值。',
                    voltage_coverage_evidence + '\n\n采用表点间电压线性插值。', 1)
    md = md.replace('太阳阵端电压、各负载支路总和、PCDU 边界尚未核实。',
                    'BCR PV 输入侧电压代号及在轨值已核验，阵列片端边界仍待核；各负载支路总和、PCDU 边界尚未核实。' if bcr_summary else
                    'BCR PV 输入侧电压代号已找到，在轨值及阵列片端边界仍待核；各负载支路总和、PCDU 边界尚未核实。', 1)
    md = md.replace('母线电流是否覆盖全部负载、太阳阵电压及电池电流传感器方向仍需电路资料核对。',
                    '母线电流是否覆盖全部负载、BCR 输入电压测点位置及电池电流传感器方向仍需电路资料核对。', 1)
    (root / '能源评估分析_YJ-3D.md').write_text(md, encoding='utf-8')
    return analysis


if __name__ == '__main__':
    info = render()
    print(json.dumps(info['window'], ensure_ascii=False))
