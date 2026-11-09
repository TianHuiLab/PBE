#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 satellites/*/framework_documentation_*.html 里的静态 SVG 折线图
替换为内联 ECharts 交互式图表，复用参考看板（卫星遥测代号异常看板）的
交互样式：滚轮缩放 / 拖拽平移 / 滑块 / 框选放大 / 图例点选 / 悬停读数 / 还原 / 导出。

仅修改指定目录下的现有 HTML，不生成新页面。
数据从本地 CSV 重新派生（与 gen_charts.py / yj3d_report.py 同源），不再解析 SVG。
"""
import csv
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import gen_charts  # 复用 SCS 的派生逻辑与 PALETTE

BASE = Path(__file__).resolve().parent
VENDOR_ECHARTS = BASE / "vendor" / "echarts.min.js"
MAX_POINTS = 6000  # 单序列下采样上限，保证流畅

PALETTE = list(gen_charts.PALETTE)


# ----------------------------------------------------------------------------
# 时间解析：本地无时区字符串 → UTC 毫秒（与参考看板一致：轴/tooltip 走浏览器本地时区显示）
# ----------------------------------------------------------------------------
def ts_to_ms(s):
    if not s:
        return None
    s = s.strip().replace(" ", "T").replace("/", "-")
    if len(s) == 10:  # YYYY-MM-DD
        s += "T00:00:00"
    if not s.endswith("Z") and "T" in s:
        s += "Z"
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return None


def downsample(pairs):
    """pairs: list[[ms, v]]，过长则按步长抽稀，保留首尾。"""
    if len(pairs) <= MAX_POINTS:
        return pairs
    step = math.ceil(len(pairs) / MAX_POINTS)
    out = pairs[::step]
    if pairs[-1] not in out:
        out.append(pairs[-1])
    return out


def series_block(name, xs, vals):
    pairs = []
    for x, v in zip(xs, vals):
        ms = ts_to_ms(x) if x else None
        if isinstance(v, (int, float)) and math.isfinite(v):
            pairs.append([ms, v])
        else:
            pairs.append([ms, None])
    return {"name": name, "color": None, "data": downsample(pairs)}


# ----------------------------------------------------------------------------
# SCS 星：复用 gen_charts 的派生逻辑
# ----------------------------------------------------------------------------
def build_scs(sat_dir):
    sat_dir = Path(sat_dir)
    gen_charts.BASE = sat_dir
    result = sorted(gen_charts.rows("out/energy_v2_result.csv"), key=lambda r: r["t"])
    result = gen_charts.select_observation_window(result)
    start, end = result[0]["t"], result[-1]["t"]
    minute = [r for r in gen_charts.rows("data_power_minute.csv") if start <= r["t"] <= end]
    times = [r["t"] for r in result]
    hour_times, hourly, energy = gen_charts.hour_buckets(result)
    hour_indices = [i for i, hr in enumerate(hour_times)
                    if any(hourly[k][i] is not None for k in hourly)]
    hour_times = [max(hour_times[i], start) for i in hour_indices]
    hourly = {k: [v[i] for i in hour_indices] for k, v in hourly.items()}
    energy = {k: [v[i] for i in hour_indices] for k, v in energy.items()}
    col = lambda key: [gen_charts.value(r.get(key)) for r in result]

    charts = {}

    def add(name, unit, series, ymin=None, ymax=None, reference=None):
        charts[name] = {
            "unit": unit, "ymin": ymin, "ymax": ymax, "reference": reference,
            "series": [{**blk, "color": PALETTE[i % len(PALETTE)]}
                       for i, blk in enumerate(series)],
        }

    add("chart_voltages", "V", [
        series_block("母线电压 (V)", times, col("V_bus")),
        series_block("电池电压 (V)", times, col("V_bat")),
    ])
    add("chart_currents", "A", [
        series_block("太阳阵总电流 (A)", times, col("I_solar")),
        series_block("负载电流 (A)", times, col("I_load")),
    ], ymin=0)
    add("chart_power_flow", "W", [
        series_block("TMD017 测点折算 (W)", times, col("P_solar")),
        series_block("负载测点折算 (W)", times, col("P_load")),
        series_block("两测点差额 (W)", times, col("P_excess")),
    ])
    add("chart_shunt", "W", [
        series_block("电流差额折算 (W)", times, col("P_shunt_diag")),
    ])
    # 帆板端 ΣV×I − TMD017 差额依赖太阳阵电压遥测 TMD003-008，
    # 2026-11-02 起该电压遥测判为不可用，图表已从清单移除，此处不再构建。
    add("chart_soc_dod", "%", [
        series_block("SOC (%)", times, col("SOC")),
        series_block("DoD (%)", times, col("DoD")),
    ], ymin=0, ymax=100, reference=50)
    # 帆板 A-F：仅电流（A）。太阳阵电压遥测 TMD003-008 2026-11-02 起判为不可用，不再生成。
    grouped = defaultdict(dict)
    for row in minute:
        if row["param_code"] in {f"TMD{i:03d}" for i in range(10, 16)}:
            grouped[row["t"]][row["param_code"]] = gen_charts.value(row["avg"])
    panel_times = sorted(grouped)
    for kind, codes, unit in (("chart_solar_panels", range(10, 16), "A"),):
        pts = [series_block(f"{chr(65 + i)}板 TMD{code:03d}", panel_times,
                            [grouped[t].get(f"TMD{code:03d}") for t in panel_times])
               for i, code in enumerate(codes)]
        add(kind, unit, pts, ymin=0)
    add("chart_daily_power", "W", [
        series_block("TMD017 测点 (W)", hour_times, hourly["P_solar"]),
        series_block("负载 (W)", hour_times, hourly["P_load"]),
        series_block("测点差额 (W)", hour_times, hourly["P_excess"]),
    ])
    add("chart_daily_soc", "%", [
        series_block("SOC (%)", hour_times, hourly["SOC"]),
    ], ymin=0, ymax=100)
    add("chart_daily_shunt", "W", [
        series_block("电流差额折算 (W)", hour_times, hourly["P_shunt_diag"]),
    ])
    add("chart_energy_daily", "kWh", [
        series_block("TMD017 测点 (kWh)", hour_times, energy["P_solar"]),
        series_block("负载 (kWh)", hour_times, energy["P_load"]),
        series_block("帆板与母线差额 (kWh)", hour_times, energy["P_shunt_diag"]),
    ])
    return charts


def build_scs_components(sat_dir, html):
    """单机功率曲线：从 HTML 提取 component-panel 名称，映射 energy_v2_components.csv 列。"""
    sat_dir = Path(sat_dir)
    gen_charts.BASE = sat_dir
    names = re.findall(r'data-component-panel="([^"]+)"', html)
    if not names:
        return {}
    result = sorted(gen_charts.rows("out/energy_v2_result.csv"), key=lambda r: r["t"])
    result = gen_charts.select_observation_window(result)
    times = [r["t"] for r in result]
    comp_rows = gen_charts.rows("out/energy_v2_components.csv")
    cb = {r["t"]: r for r in comp_rows}
    out = {}
    for name in dict.fromkeys(names):  # 保序去重
        colname = f"P_{name}_W"
        if colname not in comp_rows[0]:
            continue
        vals = [gen_charts.value(cb[t].get(colname)) if t in cb else None for t in times]
        out[name] = {
            "unit": "W", "ymin": 0, "ymax": None, "reference": None,
            "series": [{"name": name, "color": PALETTE[0], **series_block(name, times, vals)}],
        }
    return out


# ----------------------------------------------------------------------------
# YJ-3D 星：纯 Python 派生（不依赖 pandas / 校验）
# ----------------------------------------------------------------------------
def _median(vals):
    vals = [v for v in vals if isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    if n % 2:
        return vals[n // 2]
    return (vals[n // 2 - 1] + vals[n // 2]) / 2


def _fnum(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def build_yj3d(sat_dir, html):
    sat_dir = Path(sat_dir)
    dp = sat_dir / "data_power_minute.csv"
    cols = ["t", "phase", "phase_conflict", "solar_current_mean", "bus_current_mean",
            "battery_current_mean", "bus_voltage_mean", "battery_voltage_mean",
            "solar_bus_proxy_W_mean", "bus_measure_W_mean",
            "battery_signed_trial_W_mean", "residual_diag_W_mean",
            "battery_current_min", "battery_current_mean", "battery_current_max"]
    rows = []
    with dp.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    # 窗口 / 焦点（从 HTML 文本取，与报告一致）
    m = re.search(r"切换窗口为北京时间\s*([\d-]+ [\d:]+)[—-]([\d-]+ [\d:]+)", html)
    m2 = re.search(r"竖线标记\s*([\d-]+ [\d:]+)", html)
    w_start, w_end = (m.group(1), m.group(2)) if m else (None, None)
    focus = m2.group(1) if m2 else None
    focus_ms = ts_to_ms(focus) if focus else None

    # 全时段日中位
    by_day = defaultdict(lambda: defaultdict(list))
    for r in rows:
        day = r["t"][:10]
        by_day[day]["solar"].append(_fnum(r["solar_bus_proxy_W_mean"]))
        by_day[day]["bus"].append(_fnum(r["bus_measure_W_mean"]))
        by_day[day]["battery"].append(_fnum(r["battery_signed_trial_W_mean"]))
    day_times = sorted(by_day)
    daily = {
        "solar": [[ts_to_ms(d + "T00:00:00Z"), _median(by_day[d]["solar"])] for d in day_times],
        "bus": [[ts_to_ms(d + "T00:00:00Z"), _median(by_day[d]["bus"])] for d in day_times],
        "battery": [[ts_to_ms(d + "T00:00:00Z"), _median(by_day[d]["battery"])] for d in day_times],
    }

    # 窗口内逐分钟
    win = [r for r in rows if w_start and w_end and w_start <= r["t"] <= w_end]

    def win_series(name, key):
        return series_block(name, [r["t"] for r in win], [_fnum(r[key]) for r in win])

    # 配对曲线
    pair_path = sat_dir / "data_balance_pair_minute.csv"
    pair = []
    if pair_path.is_file():
        with pair_path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                pair.append(r)
    pair_win = [r for r in pair if w_start and w_end and w_start <= r["t_beijing"] <= w_end]
    pair_by_day = defaultdict(lambda: defaultdict(list))
    for r in pair:
        day = r["t_beijing"][:10]
        pair_by_day[day]["bus"].append(_fnum(r["bus_W"]))
        pair_by_day[day]["branch"].append(_fnum(r["pl_ab_heat_W"]))
    pd_days = sorted(pair_by_day)
    pair_daily = {
        "bus": [[ts_to_ms(d + "T00:00:00Z"), _median(pair_by_day[d]["bus"])] for d in pd_days],
        "branch": [[ts_to_ms(d + "T00:00:00Z"), _median(pair_by_day[d]["branch"])] for d in pd_days],
    }

    charts = {}
    charts["balance-pair-nearby"] = {
        "unit": "W", "ymin": 0, "ymax": None, "reference": None, "markline_x": focus_ms,
        "series": [
            {"name": "母线·同源配对", "color": PALETTE[0], **series_block("母线·同源配对", [r["t_beijing"] for r in pair_win], [_fnum(r["bus_W"]) for r in pair_win])},
            {"name": "PL A+B+加热·条件和", "color": PALETTE[1], **series_block("PL A+B+加热·条件和", [r["t_beijing"] for r in pair_win], [_fnum(r["pl_ab_heat_W"]) for r in pair_win])},
        ],
    }
    charts["balance-pair-daily"] = {
        "unit": "W", "ymin": 0, "ymax": None, "reference": None,
        "series": [
            {"name": "母线·同源配对", "color": PALETTE[0], **{"data": downsample(pair_daily["bus"])}},
            {"name": "PL A+B+加热·条件和", "color": PALETTE[1], **{"data": downsample(pair_daily["branch"])}},
        ],
    }
    charts["nearby-power"] = {
        "unit": "W", "ymin": None, "ymax": None, "reference": None, "markline_x": focus_ms,
        "series": [
            {"name": "太阳阵·母线电压代理", "color": PALETTE[0], **win_series("太阳阵·母线电压代理", "solar_bus_proxy_W_mean")},
            {"name": "母线电流测点", "color": PALETTE[1], **win_series("母线电流测点", "bus_measure_W_mean")},
            {"name": "电池有符号试算", "color": PALETTE[2], **win_series("电池有符号试算", "battery_signed_trial_W_mean")},
            {"name": "跨测点残差诊断", "color": PALETTE[3], **win_series("跨测点残差诊断", "residual_diag_W_mean")},
        ],
    }
    charts["nearby-current"] = {
        "unit": "A", "ymin": None, "ymax": None, "reference": 0, "markline_x": focus_ms,
        "series": [
            {"name": "太阳阵 TMEA007", "color": PALETTE[0], **win_series("太阳阵 TMEA007", "solar_current_mean")},
            {"name": "母线 TMEA004", "color": PALETTE[1], **win_series("母线 TMEA004", "bus_current_mean")},
            {"name": "电池 TMEA006", "color": PALETTE[2], **win_series("电池 TMEA006", "battery_current_mean")},
        ],
    }
    charts["nearby-voltage"] = {
        "unit": "V", "ymin": None, "ymax": None, "reference": None, "markline_x": focus_ms,
        "series": [
            {"name": "母线 TMEA003", "color": PALETTE[0], **win_series("母线 TMEA003", "bus_voltage_mean")},
            {"name": "电池 TMEA005", "color": PALETTE[1], **win_series("电池 TMEA005", "battery_voltage_mean")},
        ],
    }
    charts["daily-power"] = {
        "unit": "W", "ymin": None, "ymax": None, "reference": None,
        "series": [
            {"name": "太阳阵·母线电压代理", "color": PALETTE[0], **{"data": downsample(daily["solar"])}},
            {"name": "母线电流测点", "color": PALETTE[1], **{"data": downsample(daily["bus"])}},
            {"name": "电池有符号试算", "color": PALETTE[2], **{"data": downsample(daily["battery"])}},
        ],
    }
    charts["battery-raw"] = {
        "unit": "A", "ymin": None, "ymax": None, "reference": None,
        "series": [
            {"name": "分钟最小", "color": PALETTE[0], **series_block("分钟最小", [r["t"] for r in rows], [_fnum(r["battery_current_min"]) for r in rows])},
            {"name": "分钟均值", "color": PALETTE[2], **series_block("分钟均值", [r["t"] for r in rows], [_fnum(r["battery_current_mean"]) for r in rows])},
            {"name": "分钟最大", "color": PALETTE[3], **series_block("分钟最大", [r["t"] for r in rows], [_fnum(r["battery_current_max"]) for r in rows])},
        ],
    }
    return charts


# ----------------------------------------------------------------------------
# HTML 注入
# ----------------------------------------------------------------------------
def inject_echarts_lib():
    return "<script>" + VENDOR_ECHARTS.read_text(encoding="utf-8") + "</script>"


RENDER_JS = r"""
<script>
(function(){
  "use strict";
  var DATA = window.__ECHARTS_DATA__ || {};
  var PALETTE = ["#2563eb","#059669","#e58914","#dc4667","#7c3aed","#0891b2"];
  var charts = {};
  function isNum(x){return typeof x==="number" && isFinite(x);}
  function pad(n){return n<10?"0"+n:""+n;}
  function fmtTime(ms){ if(!isNum(ms)) return "-"; var d=new Date(ms);
    return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate())+" "+pad(d.getHours())+":"+pad(d.getMinutes())+":"+pad(d.getSeconds()); }
  function zoomCfg(){ return [
    {type:"inside",zoomOnMouseWheel:true,moveOnMouseMove:true,moveOnMouseWheel:false,filterMode:"none"},
    {type:"slider",brushSelect:true,filterMode:"none",height:22,bottom:8,borderColor:"#c8d0e0",
     fillerColor:"rgba(29,78,216,.10)",handleStyle:{color:"#1d4ed8",borderColor:"#1d4ed8"},
     moveHandleStyle:{color:"#c8d0e0"},
     dataBackground:{lineStyle:{color:"#b6c0d4",width:.6},areaStyle:{color:"#eef2f7"}},
     selectedDataBackground:{lineStyle:{color:"#1d4ed8",width:.8},areaStyle:{color:"rgba(29,78,216,.14)"}},
     textStyle:{color:"#6b7280",fontSize:10}}]; }
  var TOOLBOX={right:12,top:6,itemSize:12.5,itemGap:7,iconStyle:{borderColor:"#6b7280"},
    emphasis:{iconStyle:{borderColor:"#1d4ed8"}},
    feature:{dataZoom:{title:{zoom:"框选放大",back:"还原"},icon:{},yAxisIndex:"none"},
      restore:{title:"还原"},saveAsImage:{title:"导出图片",pixelRatio:2}}};
  function legendCfg(names){ return {data:names,top:8,right:100,itemGap:14,itemWidth:14,itemHeight:9,
    textStyle:{fontSize:12,color:"#4f5a6e"}}; }
  var AXIS_TIME={type:"time",axisLine:{lineStyle:{color:"#c8d0e0"}},
    axisLabel:{color:"#5b6577",fontSize:11,hideOverlap:true},
    splitLine:{show:true,lineStyle:{color:"#eef1f6"}}};
  var BASE_GRID={left:58,right:26,top:40,bottom:62};
  function mk(id){ if(charts[id]) return charts[id]; var node=document.getElementById(id);
    if(!node) return null; var c=echarts.init(node,null,{renderer:"canvas"}); charts[id]=c; return c; }
  function render(host){
    var key=host.getAttribute("data-echart"); var cfg=DATA[key]; if(!cfg) return;
    var c=mk(host.id); if(!c) return;
    var names=cfg.series.map(function(s){return s.name;});
    var yAxis={type:"value",name:cfg.unit||"",scale:true,
      nameTextStyle:{color:"#6b7280",fontSize:11},
      axisLine:{lineStyle:{color:"#c8d0e0"}},
      axisLabel:{color:"#5b6577",fontSize:11},
      splitLine:{lineStyle:{color:"#eef1f6"}}};
    if(isNum(cfg.ymin)) yAxis.min=cfg.ymin;
    if(isNum(cfg.ymax)) yAxis.max=cfg.ymax;
    if(!isNum(cfg.ymin) && !isNum(cfg.ymax)){ yAxis.min=null; yAxis.max=null; yAxis.scale=true; }
    var series=cfg.series.map(function(s,idx){
      var color=s.color||PALETTE[idx%PALETTE.length];
      return {name:s.name,type:"line",connectNulls:false,
        showSymbol:s.data.length<=3000,symbolSize:6,
        lineStyle:{width:1.5,color:color},itemStyle:{color:color},
        data:s.data,emphasis:{focus:"series"}};
    });
    if(isNum(cfg.reference)){
      series[0].markLine={silent:true,symbol:"none",
        lineStyle:{color:"#dc4667",type:"dashed",width:1},
        label:{fontSize:10,color:"#6b7280",formatter:function(p){return (cfg.reference).toFixed(2);}},
        data:[{yAxis:cfg.reference}]};
    }
    if(cfg.markline_x!=null){
      series[0].markLine=series[0].markLine||{silent:true,symbol:"none",
        lineStyle:{color:"#94a3b8",type:"dashed",width:1},label:{show:false},
        data:[{xAxis:cfg.markline_x}]};
    }
    c.setOption({
      tooltip:{trigger:"axis",axisPointer:{type:"cross",label:{backgroundColor:"#6b7280"}},
        backgroundColor:"rgba(255,255,255,.97)",borderColor:"#c8d0e0",borderWidth:1,
        textStyle:{color:"#1f2937",fontSize:12},
        formatter:function(ps){ if(!ps||!ps.length) return "";
          var t=ps[0].value[0]; var h="<b>"+fmtTime(t)+"</b>"; if(cfg.unit) h+=" · "+cfg.unit; h+="<br>";
          ps.forEach(function(p){ if(p.seriesName==null) return;
            var v=(p.value&&p.value[1]!=null)?p.value[1]:"—";
            h+=p.marker+" "+p.seriesName+"：<b>"+v+"</b><br>"; }); return h; }},
      legend:legendCfg(names), toolbox:TOOLBOX, grid:BASE_GRID, xAxis:AXIS_TIME, yAxis:yAxis,
      dataZoom:zoomCfg(), series:series
    }, true);
  }
  function isVisible(el){ var p=el; while(p){ if(p.nodeType===1){
      if(p.hidden) return false; var s=getComputedStyle(p);
      if(s.display==="none"||s.visibility==="hidden") return false; } p=p.parentElement; } return true; }
  function sync(){ document.querySelectorAll(".chart-echart").forEach(function(el){
      var c=charts[el.id]; if(isVisible(el)){ if(!c) render(el); else c.resize(); } }); }
  function bindSelect(id){ var s=document.getElementById(id); if(s) s.addEventListener("change",function(){ setTimeout(sync,0); }); }
  window.addEventListener("resize",function(){ document.querySelectorAll(".chart-echart").forEach(function(el){
      if(charts[el.id] && isVisible(el)) charts[el.id].resize(); }); });
  bindSelect("telemetry-select"); bindSelect("component-select");
  if(document.readyState!=="loading") sync(); else document.addEventListener("DOMContentLoaded",sync);
})();
</script>
"""


def div_for(key):
    return ('<div class="chart-echart" id="ec-%s" data-echart="%s" '
            'style="width:100%%;height:360px"></div>') % (key, key)


def replace_svg(html, key, marker_attr):
    """直接匹配 svg 上的属性（SCS 遥测 = data-inline-chart，SCS 单机 = data-component-id）。"""
    pat = re.compile(r'<svg %s="%s"[^>]*>.*?</svg>' % (marker_attr, re.escape(key)), re.S)
    return pat.sub(div_for(key), html, count=1)


def replace_svg_in_article(html, key):
    """YJ-3D：svg 本身无标识属性，需定位 data-chart-panel 所在 article 再替换内部 svg。"""
    pat = re.compile(r'(<article class="chart-card" data-chart-panel="%s"[^>]*>)(.*?)(</article>)'
                     % re.escape(key), re.S)

    def repl(m):
        g2 = re.sub(r'<svg[^>]*>.*?</svg>', div_for(key), m.group(2), count=1, flags=re.S)
        return m.group(1) + g2 + m.group(3)

    return pat.sub(repl, html, count=1)


def process(sat_dir):
    sat_dir = Path(sat_dir)
    html_path = sat_dir / f"framework_documentation_{sat_dir.name}.html"
    if not html_path.is_file():
        print(f"[跳过] 未找到 {html_path}")
        return
    html = html_path.read_text(encoding="utf-8")
    if "chart-echart" in html:
        print(f"[已处理] {html_path} 已含交互图表，跳过")
        return

    if sat_dir.name.startswith("SCS"):
        charts = build_scs(sat_dir)
        comp = build_scs_components(sat_dir, html)
        charts.update(comp)
        for key in list(charts.keys()):
            if re.search(r'data-inline-chart="%s"' % re.escape(key), html):
                html = replace_svg(html, key, "data-inline-chart")
            elif re.search(r'data-component-id="%s"' % re.escape(key), html):
                html = replace_svg(html, key, "data-component-id")
            else:
                charts.pop(key, None)
    else:  # YJ-3D
        charts = build_yj3d(sat_dir, html)
        for key in list(charts.keys()):
            if re.search(r'data-chart-panel="%s"' % re.escape(key), html):
                html = replace_svg_in_article(html, key)
            else:
                charts.pop(key, None)

    if "chart-echart" not in html:
        print(f"[跳过] {html_path} 未匹配到任何图表 SVG")
        return

    # 备份原始文件
    bak = html_path.with_suffix(".html.bak")
    if not bak.exists():
        bak.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")

    data_script = '<script>window.__ECHARTS_DATA__ = ' + json.dumps(
        charts, ensure_ascii=False) + ";</script>"
    inject = inject_echarts_lib() + "\n" + data_script + "\n" + RENDER_JS
    # 注入到 </body> 之前
    if "</body>" in html:
        html = html.replace("</body>", inject + "\n</body>", 1)
    else:
        html = html + inject

    html_path.write_text(html, encoding="utf-8")
    n = len(charts)
    print(f"[完成] {html_path} · 注入 {n} 张交互图表 · 备份 {bak.name}")


def main():
    targets = [
        BASE / "satellites" / "SCS-01-01",
        BASE / "satellites" / "SCS-01-02",
        BASE / "satellites" / "YJ-3D",
    ]
    for t in targets:
        process(t)


if __name__ == "__main__":
    main()
