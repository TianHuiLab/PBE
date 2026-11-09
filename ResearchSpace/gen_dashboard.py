# -*- coding: utf-8 -*-
"""
gen_dashboard.py — 生成「卫星遥测数据看板」自包含离线 HTML

设计目标（对齐参考看板 卫星遥测代号异常看板_*.html）：
  1) 完全离线自包含：ECharts 库 + 看板 CSS 主题 + 遥测数据 全部内嵌，file:// 双击即用；
  2) 视觉风格与参考看板一致：复用其整套浅色专业主题 CSS；
  3) 交互逻辑一致：曲线查看复用参考的 dataZoom（滚轮缩放 / 拖拽平移 / 滑块拉伸 / 框选放大）
     + toolbox（框选放大 / 还原 / 导出图片）+ legend（点选显隐）+ tooltip（悬浮读数）；
  4) 数据源适配本项目遥测：
       · SCS-01-01 / SCS-01-02 为长表（param_code × 分钟，含 avg/mn/mx 数值）；
       · YJ-3D 为宽功率表（母线/蓄电池/太阳阵电压电流与功率，*_mean/_min/_max）。

产出：ResearchSpace/satellites/卫星遥测数据看板.html
"""
import csv
import json
import math
import os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
SAT_DIR = os.path.join(BASE, "satellites")
REF_HTML = r"C:/Users/zj/WorkBuddy/每周报表生成/output/卫星遥测代号异常看板_2026-09-17_2026-09-24.html"
ECHARTS_FILE = os.path.join(BASE, "vendor", "echarts.min.js")
OUT_FILE = os.path.join(SAT_DIR, "卫星遥测数据看板.html")

MAXPTS = 2000  # 每个参数序列下采样上限，兼顾平滑度与文件体积

# YJ-3D 宽表列基名 → 中文名（找不到则回退基名）
YJ3D_NAME_MAP = {
    "bus_voltage": "母线电压 (V)",
    "bus_current": "母线电流 (A)",
    "battery_voltage": "蓄电池电压 (V)",
    "battery_current": "蓄电池电流 (A)",
    "solar_current": "太阳阵电流 (A)",
    "solar_bus_proxy_W": "太阳阵入母线功率 (W)",
    "bus_measure_W": "母线测量功率 (W)",
    "battery_signed_trial_W": "蓄电池净功率 (W)",
    "residual_diag_W": "残差诊断功率 (W)",
}


def parse_ts(s):
    """'YYYY-MM-DD HH:MM:SS' -> epoch ms (按 UTC 解释，保证显示与源串一致)。"""
    if not s:
        return None
    t = s.strip().replace(" ", "T")
    if len(t) <= 16:
        t += ":00"
    try:
        dt = datetime.strptime(t, "%Y-%m-%dT%H:%M:%S")
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def build_long(path):
    """SCS 长表：param_code 为行，avg/mn/mx 为数值。返回 {code: {name, series}}。"""
    counts = {}
    with open(path, encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        for row in r:
            c = (row.get("param_code") or "").strip()
            a = row.get("avg")
            if not c or a in (None, "", " "):
                continue
            counts[c] = counts.get(c, 0) + 1
    stride = {c: max(1, math.ceil(n / MAXPTS)) for c, n in counts.items()}
    params = {}
    idx = {}
    with open(path, encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        for row in r:
            c = (row.get("param_code") or "").strip()
            if c not in stride:
                continue
            a = row.get("avg")
            if a in (None, "", " "):
                continue
            t = parse_ts(row.get("t"))
            if t is None:
                continue
            i = idx.get(c, 0)
            idx[c] = i + 1
            if i % stride[c] != 0:
                continue
            try:
                avg = float(a)
                mn = float(row["mn"]) if row.get("mn") not in (None, "", " ") else avg
                mx = float(row["mx"]) if row.get("mx") not in (None, "", " ") else avg
            except Exception:
                continue
            params.setdefault(c, {"name": (row.get("param_name") or c).strip(), "series": []})
            params[c]["series"].append([t, round(avg, 4), round(mn, 4), round(mx, 4)])
    return params


def build_wide(path):
    """YJ-3D 宽表：每列 *_mean 配 *_min/*_max。返回 {base: {name, series}}。"""
    with open(path, encoding="utf-8", newline="") as fh:
        r = csv.DictReader(fh)
        cols = r.fieldnames or []
        mean_cols = [c for c in cols if c.endswith("_mean")]
        params = {}
        for row in r:
            t = parse_ts(row.get("t"))
            if t is None:
                continue
            for mc in mean_cols:
                base = mc[:-5]
                try:
                    avg = float(row[mc])
                except Exception:
                    continue
                mncol, mxcol = base + "_min", base + "_max"
                mn = float(row[mncol]) if (mncol in row and row[mncol] not in (None, "", " ")) else avg
                mx = float(row[mxcol]) if (mxcol in row and row[mxcol] not in (None, "", " ")) else avg
                params.setdefault(base, {"name": YJ3D_NAME_MAP.get(base, base), "series": []})
                params[base]["series"].append([t, round(avg, 4), round(mn, 4), round(mx, 4)])
    for v in params.values():
        s = v["series"]
        if len(s) > MAXPTS:
            st = math.ceil(len(s) / MAXPTS)
            v["series"] = [s[i] for i in range(0, len(s), st)]
    return params


def period_of(params):
    lo, hi = None, None
    for v in params.values():
        for p in v["series"]:
            if lo is None or p[0] < lo:
                lo = p[0]
            if hi is None or p[0] > hi:
                hi = p[0]
    return lo, hi


def build():
    satellites = {}
    meta_sats = {}
    plan = [
        ("SCS-01-01", os.path.join(SAT_DIR, "SCS-01-01", "data_power_minute.csv"), "long"),
        ("SCS-01-02", os.path.join(SAT_DIR, "SCS-01-02", "data_power_minute.csv"), "long"),
        ("YJ-3D", os.path.join(SAT_DIR, "YJ-3D", "data_power_minute.csv"), "wide"),
    ]
    for key, path, kind in plan:
        if not os.path.exists(path):
            print("  [跳过] 缺失:", path)
            continue
        print("  处理", key, kind, "...")
        params = build_long(path) if kind == "long" else build_wide(path)
        # 仅保留有序列的参数
        params = {k: v for k, v in params.items() if v["series"]}
        lo, hi = period_of(params)
        satellites[key] = {
            "name": key,
            "params": dict(sorted(params.items(), key=lambda kv: kv[0])),
            "period_from": lo,
            "period_to": hi,
        }
        meta_sats[key] = {
            "period_from": lo,
            "period_to": hi,
            "n_params": len(params),
        }
        print("    参数数:", len(params), " 时间范围:", lo, "~", hi)
    return {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "ClickHouse chdb_3D / 卫星分钟级聚合遥测 (SCS-01-01/02, YJ-3D)",
            "satellites": meta_sats,
        },
        "satellites": satellites,
    }


def extract_css():
    s = open(REF_HTML, encoding="utf-8").read()
    i = s.find("<style")
    j = s.find("</style>", i)
    return s[i:j + len("</style>")]


def main():
    os.makedirs(SAT_DIR, exist_ok=True)
    print("== 抽取参考 CSS 主题 ==")
    css = extract_css()
    print("== 读取内联 ECharts ==")
    echarts = open(ECHARTS_FILE, encoding="utf-8").read()
    print("== 构建遥测数据 ==")
    data = build()
    print("== 组装 HTML ==")
    html = TEMPLATE.replace("__CSS__", css)
    html = html.replace("__ECHARTS__", echarts)
    html = html.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("__GEN__", data["meta"]["generated_at"])
    open(OUT_FILE, "w", encoding="utf-8").write(html)
    size = os.path.getsize(OUT_FILE)
    print("== 完成 ==")
    print("输出:", OUT_FILE)
    print("大小: %.2f MB" % (size / 1e6))
    print("卫星:", list(data["satellites"].keys()))


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>卫星遥测数据看板</title>
__CSS__
</head>
<body>

<header class="hdr">
  <div class="hdr-in">
    <h1>卫星遥测数据看板<span class="badge" id="badgeOffline">离线自包含</span></h1>
    <div class="sub">Satellite Telemetry Dashboard · 多星遥测参数时间序列交互式查看</div>
    <div class="hdr-meta">
      <div>卫星：<b id="mSat">-</b></div>
      <div>统计周期：<b id="mPeriod">-</b></div>
      <div>数据源：<b id="mSource">-</b></div>
      <div>生成时间：<b id="mGen">-</b></div>
    </div>
  </div>
</header>

<div class="wrap">

  <section class="sec" id="secSat">
    <div class="sec-hd"><h2>卫星选择</h2><span class="note">切换卫星后参数列表与曲线自动刷新</span></div>
    <div class="card"><div class="card-bd">
      <div class="ctrl">
        <label for="selSat">卫星</label>
        <select id="selSat" aria-label="选择卫星"></select>
      </div>
    </div></div>
  </section>

  <section class="sec" id="secCurve">
    <div class="sec-hd"><h2>曲线查看</h2><span class="note">统一参数选择 · 均值/最小/最大可叠加 · 支持滚轮缩放、拖拽平移、滑块拉伸与框选放大 · 图例可点选显隐</span></div>
    <div class="card">
      <div class="card-hd">
        <h3 id="curveTitle">参数取值时间序列</h3>
        <div class="ctrl">
          <label for="selParam">参数</label>
          <select id="selParam" aria-label="选择遥测参数"></select>
          <span class="ctrl-inline">
            <label>叠加</label>
            <div class="chips" id="curveMode" role="group" aria-label="显示量">
              <span class="chip on" data-m="avg" role="button" tabindex="0">均值</span>
              <span class="chip" data-m="min" role="button" tabindex="0">最小</span>
              <span class="chip" data-m="max" role="button" tabindex="0">最大</span>
            </div>
          </span>
        </div>
      </div>
      <div class="card-bd" id="curveMeta" style="padding:12px 18px 0">
        <div class="pills" id="seriesStat"></div>
      </div>
      <div class="card-bd flush"><div id="chartCurve" class="chart tall"></div></div>
    </div>
  </section>

  <section class="sec" id="secCompare">
    <div class="sec-hd"><h2>多参数对比</h2><span class="note">勾选多个参数叠加查看，图例可单独点选显隐（最多 10 条）</span></div>
    <div class="card">
      <div class="card-hd"><h3>参数叠加对比</h3><span class="hint" id="cmpHint"></span></div>
      <div class="card-bd">
        <div class="chips" id="cmpChips"></div>
      </div>
      <div class="card-bd flush"><div id="chartCompare" class="chart tall"></div></div>
    </div>
  </section>

  <div class="foot">
    <div>本文件为完全离线的自包含 HTML：遥测数据与图表库均已内嵌，无需网络即可查看。</div>
    <div id="footGen">-</div>
  </div>
</div>

<script>__ECHARTS__</script>
<script>window.__TELE__ = __DATA__;</script>
<script>
(function () {
  "use strict";
  var D = window.__TELE__ || {};
  var SATS = D.satellites || {};
  var META = D.meta || {};
  var C = { confirmed: "#dc2626", repeat: "#ea8c00", failed: "#0d9488", normal: "#16a34a", primary: "#1d4ed8" };
  var PALETTE = ["#1d4ed8","#dc2626","#16a34a","#ea8c00","#0d9488","#7c3aed","#db2777","#0891b2","#65a30d","#c2410c"];
  var charts = {};

  /* ---------- 防御式工具 ---------- */
  function arr(x){ return Object.prototype.toString.call(x)==="[object Array]"?x:[]; }
  function obj(x){ return (x && typeof x==="object" && !Array.isArray(x))?x:{}; }
  function isNum(x){ return typeof x==="number" && isFinite(x); }
  function num(x){ var n=Number(x); return isFinite(n)?n:0; }
  function fmt(n){ if(n===null||n===undefined||!isFinite(Number(n))) return "-"; return String(n).replace(/\B(?=(\d{3})+(?!\d))/g,","); }
  function el(id){ return document.getElementById(id); }
  function esc(s){ return String(s===null||s===undefined?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
  function pad(n){ return n<10?"0"+n:""+n; }
  function mspad(ms){ var v=Math.floor(ms)%1000; return v<10?"00"+v:(v<100?"0"+v:""+v); }
  function fmtTime(ms){ if(!isFinite(ms)) return "-"; var d=new Date(ms); return d.getUTCFullYear()+"-"+pad(d.getUTCMonth()+1)+"-"+pad(d.getUTCDate())+" "+pad(d.getUTCHours())+":"+pad(d.getUTCMinutes())+":"+pad(d.getUTCSeconds()); }
  function fmtTimeMS(ms){ if(!isFinite(ms)) return "-"; return fmtTime(ms)+"."+mspad(ms); }
  function hide(id){ var s=el(id); if(s) s.style.display="none"; }
  function show(id){ var s=el(id); if(s) s.style.display=""; }
  var ERRS=[];
  function safe(name, fn){ try{ fn(); }catch(e){ ERRS.push(name+": "+(e&&e.message)); if(window.console) console.error("[dashboard]",name,e); } }

  function renderEmpty(id, msg){ var e=el(id); if(!e) return; if(charts[id]){ try{charts[id].dispose();}catch(_){} delete charts[id]; } e.innerHTML='<div class="empty">'+esc(msg)+"</div>"; }
  function setTitle(id,txt){ var n=el(id); if(!n) return; n.textContent=txt||"-"; n.title=txt||""; }

  function mk(id){
    if(charts[id]) return charts[id];
    var node=el(id); if(!node) return null;
    var c=echarts.init(node,null,{renderer:"canvas"});
    charts[id]=c; return c;
  }
  /* 统一缩放配置：滚轮缩放 / 拖拽平移 / 滑块拉伸 / 框选放大（与参考看板一致） */
  function zoomCfg(extra){
    var base=[
      { type:"inside", zoomOnMouseWheel:true, moveOnMouseMove:true, moveOnMouseWheel:false, filterMode:"none" },
      { type:"slider", brushSelect:true, filterMode:"none", height:22, bottom:8,
        borderColor:"#c8d0e0", fillerColor:"rgba(29,78,216,.10)",
        handleStyle:{ color:"#1d4ed8", borderColor:"#1d4ed8" },
        moveHandleStyle:{ color:"#c8d0e0" },
        dataBackground:{ lineStyle:{ color:"#b6c0d4", width:.6 }, areaStyle:{ color:"#eef2f7" } },
        selectedDataBackground:{ lineStyle:{ color:"#1d4ed8", width:.8 }, areaStyle:{ color:"rgba(29,78,216,.14)" } },
        textStyle:{ color:"#6b7280", fontSize:10 } }
    ];
    return extra ? base.concat(extra) : base;
  }
  var TOOLBOX = {
    right:12, top:6, itemSize:12.5, itemGap:7,
    iconStyle:{ borderColor:"#6b7280" },
    emphasis:{ iconStyle:{ borderColor:"#1d4ed8" } },
    feature:{
      dataZoom:{ title:{ zoom:"框选放大", back:"还原" }, icon:{}, yAxisIndex:"none" },
      restore:{ title:"还原" },
      saveAsImage:{ title:"导出图片", pixelRatio:2 }
    }
  };
  function legendCfg(names, gap){
    return { data:names, top:8, right:100, itemGap:gap||14, itemWidth:14, itemHeight:9, textStyle:{ fontSize:12, color:"#4f5a6e" } };
  }
  var AXIS_TIME = {
    type:"time",
    axisLine:{ lineStyle:{ color:"#c8d0e0" } },
    axisLabel:{ color:"#5b6577", fontSize:11, hideOverlap:true,
      formatter:function(v){ return fmtTime(Number(v)); } },
    splitLine:{ show:true, lineStyle:{ color:"#eef1f6" } }
  };
  var BASE_GRID = { left:62, right:26, top:40, bottom:64 };

  function setSelect(id, val){
    var s=el(id); if(!s) return;
    for(var i=0;i<s.options.length;i++){ if(s.options[i].value===val){ s.selectedIndex=i; return; } }
  }
  function bindChips(sel, onPick){
    var list=document.querySelectorAll(sel+" .chip");
    Array.prototype.forEach.call(list, function(c){
      function pick(){
        Array.prototype.forEach.call(list, function(x){ x.classList.remove("on"); });
        c.classList.add("on"); onPick(c);
      }
      c.addEventListener("click", pick);
      c.addEventListener("keydown", function(e){ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick(); } });
    });
  }

  /* ================= 主逻辑 ================= */
  var curSat=null, curParam=null;
  var mode={ avg:true, min:false, max:false };
  var cmpSel={};

  function boot(){
    var satKeys=Object.keys(SATS);
    if(!satKeys.length){ hide("secSat"); hide("secCurve"); hide("secCompare");
      var b=el("emptyState"); if(b) b.innerHTML='<div class="empty-state"><div class="es-title">无可用遥测数据</div></div>'; return; }
    var sel=el("selSat");
    sel.innerHTML=satKeys.map(function(k){ return '<option value="'+esc(k)+'">'+esc((SATS[k].name||k))+'</option>'; }).join("");
    sel.addEventListener("change", function(){ switchSat(sel.value); });
    switchSat(satKeys[0]);

    // 均值常显（不可关闭）；最小/最大为独立开关
    Array.prototype.forEach.call(document.querySelectorAll("#curveMode .chip"), function(c){
      function pick(){
        var m=c.getAttribute("data-m");
        if(m==="avg"){ c.classList.add("on"); return; }   // 均值恒定开启
        if(c.classList.contains("on")) c.classList.remove("on"); else c.classList.add("on");
        mode.min = document.querySelector('#curveMode .chip[data-m="min"]').classList.contains("on");
        mode.max = document.querySelector('#curveMode .chip[data-m="max"]').classList.contains("on");
        renderCurve();
      }
      c.addEventListener("click", pick);
      c.addEventListener("keydown", function(e){ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick(); } });
    });
    window.addEventListener("resize", function(){
      Object.keys(charts).forEach(function(k){ try{ charts[k].resize(); }catch(_){} });
    });
  }

  function switchSat(k){
    curSat=k;
    var S=obj(SATS[k]); var P=obj(S.params);
    var pkeys=Object.keys(P);
    var selP=el("selParam");
    selP.innerHTML=pkeys.map(function(code){
      var m=obj(P[code]);
      return '<option value="'+esc(code)+'">'+esc(code)+" · "+esc(m.name||code)+"</option>";
    }).join("");
    curParam = pkeys.length ? pkeys[0] : null;
    // 重置对比勾选
    cmpSel={};
    buildCmpChips(k);
    // 元信息
    var ms=obj(META.satellites)[k]||{};
    el("mSat").textContent = S.name||k;
    el("mPeriod").textContent = (isNum(ms.period_from)?fmtTime(ms.period_from):"-")+" ~ "+(isNum(ms.period_to)?fmtTime(ms.period_to):"-");
    el("mSource").textContent = META.source||"-";
    el("mGen").textContent = META.generated_at||"-";
    el("footGen").textContent = "生成于 "+(META.generated_at||"-");
    renderCurve();
    renderCompare();
  }

  function buildCmpChips(k){
    var P=obj(obj(SATS[k]).params);
    var pkeys=Object.keys(P);
    el("cmpHint").textContent = "共 "+pkeys.length+" 个参数";
    el("cmpChips").innerHTML = pkeys.map(function(code){
      var m=obj(P[code]);
      return '<span class="chip" data-c="'+esc(code)+'" role="button" tabindex="0">'+esc(code)+" · "+esc(m.name||code)+"</span>";
    }).join("");
    Array.prototype.forEach.call(document.querySelectorAll("#cmpChips .chip"), function(c){
      function pick(){
        var code=c.getAttribute("data-c");
        if(c.classList.contains("on")){ c.classList.remove("on"); delete cmpSel[code]; }
        else {
          if(Object.keys(cmpSel).length>=10){ alert("最多叠加 10 条参数"); return; }
          c.classList.add("on"); cmpSel[code]=true;
        }
        renderCompare();
      }
      c.addEventListener("click", pick);
      c.addEventListener("keydown", function(e){ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick(); } });
    });
  }

  /* ---- 单参数曲线（均值 / 最小 / 最大 可叠加） ---- */
  function renderCurve(){
    var S=obj(obj(SATS[curSat]).params);
    var m=obj(S[curParam]);
    var box=el("seriesStat");
    var ch=mk("chartCurve");
    setTitle("curveTitle", curParam+" · "+(m.name||curParam)+" 取值时间序列");
    if(!m || !arr(m.series).length){
      renderEmpty("chartCurve","参数 "+esc(curParam)+" 暂无有效采样序列");
      if(box) box.innerHTML=""; return;
    }
    var series=m.series;
    var defs=[];
    if(mode.avg) defs.push({ key:"avg", name:"均值", col:1, color:C.primary, width:1.5, dashed:false });
    if(mode.min) defs.push({ key:"min", name:"最小", col:2, color:"#94a3b8", width:1, dashed:true });
    if(mode.max) defs.push({ key:"max", name:"最大", col:3, color:C.repeat, width:1, dashed:true });

    var echSeries=defs.map(function(d){
      return {
        name:d.name, type:"line", showSymbol:false, connectNulls:false,
        lineStyle:{ width:d.width, color:d.color, type:d.dashed?"dashed":"solid" },
        itemStyle:{ color:d.color },
        emphasis:{ focus:"series" },
        data: series.map(function(p){ return [p[0], p[d.col]]; })
      };
    });

    var avgs=series.map(function(p){ return p[1]; });
    var mn=Math.min.apply(null,avgs), mx=Math.max.apply(null,avgs);
    var avgMean=(avgs.reduce(function(a,b){return a+b;},0)/avgs.length);
    var rng=(mx-mn)||1;
    if(box) box.innerHTML =
      '<span class="pill">采样点 <b>'+fmt(series.length)+'</b></span>'+
      '<span class="pill">均值 <b>'+avgMean.toFixed(3)+'</b></span>'+
      '<span class="pill">最小 <b>'+mn.toFixed(3)+'</b></span>'+
      '<span class="pill">最大 <b>'+mx.toFixed(3)+'</b></span>'+
      '<span class="pill">时窗 <b>'+(isNum(S.period_from)?"":"" )+fmtTime(series[0][0])+" ~ "+fmtTime(series[series.length-1][0])+'</b></span>';

    ch.setOption({
      tooltip:{
        trigger:"axis",
        backgroundColor:"rgba(255,255,255,.97)", borderColor:"#c8d0e0", borderWidth:1,
        textStyle:{ color:"#1f2937", fontSize:12 },
        formatter:function(ps){
          if(!ps||!ps.length) return "";
          var t=ps[0].value[0];
          var s="<b>"+fmtTimeMS(t)+"</b><br>";
          ps.forEach(function(p){
            var v=(p.value&&p.value[1]);
            s+="<span style='display:inline-block;width:9px;height:9px;border-radius:2px;background:"+p.color+";margin-right:5px'></span>"+
               esc(p.seriesName)+"：<b>"+(isNum(v)?Number(v).toFixed(3):"-")+"</b><br>";
          });
          return s;
        }
      },
      legend: legendCfg(defs.map(function(d){ return d.name; })),
      toolbox: TOOLBOX,
      grid: BASE_GRID,
      xAxis: AXIS_TIME,
      yAxis:{
        type:"value", name:(m.name||curParam), scale:true,
        min: mn - rng*0.12, max: mx + rng*0.08,
        nameTextStyle:{ color:"#6b7280", fontSize:11 },
        axisLine:{ lineStyle:{ color:"#c8d0e0" } },
        axisLabel:{ color:"#5b6577", fontSize:11 },
        splitLine:{ lineStyle:{ color:"#eef1f6" } }
      },
      dataZoom: zoomCfg(),
      series: echSeries
    }, true);
  }

  /* ---- 多参数对比（多折线 + 图例点选） ---- */
  function renderCompare(){
    var S=obj(obj(SATS[curSat]).params);
    var keys=Object.keys(cmpSel);
    var ch=mk("chartCompare");
    if(!keys.length){
      renderEmpty("chartCompare","勾选上方参数以叠加对比（最多 10 条）"); return;
    }
    var echSeries=[];
    var names=[];
    keys.forEach(function(code, i){
      var m=obj(S[code]);
      if(!m || !arr(m.series).length) return;
      var color=PALETTE[i%PALETTE.length];
      names.push(code);
      echSeries.push({
        name:code, type:"line", showSymbol:false, connectNulls:false,
        lineStyle:{ width:1.4, color:color }, itemStyle:{ color:color },
        emphasis:{ focus:"series" },
        data: m.series.map(function(p){ return [p[0], p[1]]; })
      });
    });
    if(!echSeries.length){ renderEmpty("chartCompare","所选参数无有效数据"); return; }
    ch.setOption({
      tooltip:{
        trigger:"axis",
        backgroundColor:"rgba(255,255,255,.97)", borderColor:"#c8d0e0", borderWidth:1,
        textStyle:{ color:"#1f2937", fontSize:12 },
        formatter:function(ps){
          if(!ps||!ps.length) return "";
          var s="<b>"+fmtTimeMS(ps[0].value[0])+"</b><br>";
          ps.forEach(function(p){
            var v=(p.value&&p.value[1]);
            s+="<span style='display:inline-block;width:9px;height:9px;border-radius:2px;background:"+p.color+";margin-right:5px'></span>"+
               esc(p.seriesName)+"：<b>"+(isNum(v)?Number(v).toFixed(3):"-")+"</b><br>";
          });
          return s;
        }
      },
      legend: legendCfg(names, 12),
      toolbox: TOOLBOX,
      grid: BASE_GRID,
      xAxis: AXIS_TIME,
      yAxis:{
        type:"value", name:"归一对比 (各参数均值)", scale:true,
        nameTextStyle:{ color:"#6b7280", fontSize:11 },
        axisLine:{ lineStyle:{ color:"#c8d0e0" } },
        axisLabel:{ color:"#5b6577", fontSize:11 },
        splitLine:{ lineStyle:{ color:"#eef1f6" } }
      },
      dataZoom: zoomCfg(),
      series: echSeries
    }, true);
  }

  safe("boot", boot);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
