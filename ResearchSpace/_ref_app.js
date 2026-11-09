<script>/* 卫星遥测代号异常看板 —— 前端交互与图表渲染
 *
 * 设计原则（鲁棒性）：
 *  1) 所有外部数据一律经 arr()/obj()/num() 兜底后使用，字段缺失、类型异常都不会中断渲染；
 *  2) 每个渲染步骤用 safe() 包裹，单点失败只影响该模块，不会白屏；
 *  3) 无数据时走空状态分支，隐藏全部图表区并给出明确提示。
 */
(function () {
  "use strict";

  var D = window.__DATA__ || {};
  var A = window.__ANALYSIS__ || {};
  var C = { confirmed: "#dc2626", repeat: "#ea8c00", failed: "#0d9488", normal: "#16a34a", primary: "#1d4ed8" };
  var LABEL = { confirmed: "异常确认", repeat: "异常重复", failed: "校验未通过" };
  var charts = {};

  /* 取值点来源编码（与 fetch_data.py 保持一致） */
  var SRC_CONFIRMED = 0, SRC_INTERP = 1, SRC_REPEAT = 2, SRC_MISSING = 3;
  var SRC = {};
  SRC[SRC_CONFIRMED] = { n: "异常确认（采样自带精确时间戳）", c: C.confirmed, sym: "circle", sz: 6 };
  SRC[SRC_INTERP] = { n: "异常确认（时间戳由核验窗口插值）", c: C.confirmed, sym: "emptyCircle", sz: 6 };
  SRC[SRC_REPEAT] = { n: "异常重复补全（取值缺失，按异常态补全）", c: C.repeat, sym: "triangle", sz: 8 };
  SRC[SRC_MISSING] = { n: "缺失（无可补全取值）", c: "#94a3b8", sym: "rect", sz: 8 };

  /* ---------- 防御式工具 ---------- */
  function arr(x) { return Object.prototype.toString.call(x) === "[object Array]" ? x : []; }
  function obj(x) { return (x && typeof x === "object" && !Array.isArray(x)) ? x : {}; }
  function isNum(x) { return typeof x === "number" && isFinite(x); }
  function num(x) { var n = Number(x); return isFinite(n) ? n : 0; }
  function fmt(n) {
    if (n === null || n === undefined || !isFinite(Number(n))) return "-";
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function pct(a, b) { b = num(b); return b ? (num(a) / b * 100) : 0; }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function mspad(ms) { var v = ms % 1000; return v < 10 ? "00" + v : (v < 100 ? "0" + v : "" + v); }
  function fmtTime(ms) {
    if (!isNum(ms)) return "-";
    var d = new Date(ms);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
           pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  /* 精确时间点：秒 + 毫秒，用于取值序列的横轴读数 */
  function fmtTimeMS(ms) {
    if (!isNum(ms)) return "-";
    return fmtTime(ms) + "." + mspad(ms);
  }
  function el(id) { return document.getElementById(id); }
  /* 卡片标题统一入口：显示文本截断（CSS），全量文本挂到 title 供悬浮查看 */
  function setTitle(id, txt) {
    var n = el(id);
    if (!n) return;
    n.textContent = txt || "-";
    n.title = txt || "";
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  /* 轻量数值标签：柔和底色 + 加深字重，用于突出关键数据（避免整列重色带破坏视觉） */
  function chip(v) {
    var s = String(v === null || v === undefined ? "" : v);
    if (!s || s === "-") return "-";
    return '<span class="v">' + esc(s) + "</span>";
  }
  /* 任一渲染步骤的异常都被捕获，避免整页崩溃 */
  var ERRS = [];
  function safe(name, fn) {
    try { fn(); } catch (e) {
      ERRS.push(name + ": " + (e && e.message));
      if (window.console) console.error("[dashboard]", name, e);
    }
  }
  /* 图表降级为文字占位：销毁实例后写入提示，绝不留下空白 canvas */
  function renderEmpty(id, msg) {
    var e = el(id);
    if (!e) return;
    if (charts[id]) { try { charts[id].dispose(); } catch (_) {} delete charts[id]; }
    e.innerHTML = '<div class="empty">' + esc(msg) + "</div>";
  }
  function hide(id) { var s = el(id); if (s) s.style.display = "none"; }
  function show(id) { var s = el(id); if (s) s.style.display = ""; }

  /* 折叠面板：点击 / 回车切换 open 类并同步 aria-expanded。
   * 折叠只切换显示状态，内容节点始终留在 DOM 中，不影响复制、导出与后续交互。 */
  function bindFold(wrap, head) {
    if (!wrap || !head) return;
    var sync = function () {
      head.setAttribute("aria-expanded", wrap.classList.contains("open") ? "true" : "false");
    };
    var toggle = function () { wrap.classList.toggle("open"); sync(); };
    sync();
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", function (e) {
      // 空格/回车在 button 上会触发原生 click，这里 preventDefault 避免重复切换
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  }

  // 统一的数据缩放配置：滚轮缩放 / 拖拽平移 / 滑块拉伸 / 框选放大
  function zoomCfg(extra) {
    var base = [
      { type: "inside", zoomOnMouseWheel: true, moveOnMouseMove: true, moveOnMouseWheel: false, filterMode: "none" },
      {
        type: "slider", brushSelect: true, filterMode: "none", height: 22, bottom: 8,
        borderColor: "#c8d0e0", fillerColor: "rgba(29,78,216,.10)",
        handleStyle: { color: "#1d4ed8", borderColor: "#1d4ed8" },
        moveHandleStyle: { color: "#c8d0e0" },
        dataBackground: { lineStyle: { color: "#b6c0d4", width: .6 }, areaStyle: { color: "#eef2f7" } },
        selectedDataBackground: { lineStyle: { color: "#1d4ed8", width: .8 }, areaStyle: { color: "rgba(29,78,216,.14)" } },
        textStyle: { color: "#6b7280", fontSize: 10 }
      }
    ];
    return extra ? base.concat(extra) : base;
  }
  function mk(id) {
    if (charts[id]) return charts[id];
    var node = el(id);
    if (!node) return null;
    var c = echarts.init(node, null, { renderer: "canvas" });
    charts[id] = c;
    return c;
  }
  var TOOLBOX = {
    right: 12, top: 6, itemSize: 12.5, itemGap: 7,
    iconStyle: { borderColor: "#6b7280" },
    emphasis: { iconStyle: { borderColor: "#1d4ed8" } },
    feature: {
      dataZoom: { title: { zoom: "框选放大", back: "还原" }, icon: {}, yAxisIndex: "none" },
      restore: { title: "还原" },
      saveAsImage: { title: "导出图片", pixelRatio: 2 }
    }
  };
  /* 图例统一配置：right 留出 toolbox 图标位（原先 right:14 会与「框选放大/导出」图标叠压），
   * 文字色与正文字号统一，避免各图表图例风格漂移。 */
  function legendCfg(names, gap) {
    return {
      data: names, top: 8, right: 100, itemGap: gap || 14, itemWidth: 14, itemHeight: 9,
      textStyle: { fontSize: 12, color: "#4f5a6e" }
    };
  }
  var AXIS_TIME = {
    type: "time",
    axisLine: { lineStyle: { color: "#c8d0e0" } },
    axisLabel: { color: "#5b6577", fontSize: 11, hideOverlap: true },
    splitLine: { show: true, lineStyle: { color: "#eef1f6" } }
  };
  var BASE_GRID = { left: 58, right: 26, top: 40, bottom: 62 };

  /* ================= 启动 =================
   * 注意：boot() 必须在文件末尾调用 —— 它依赖下方各模块声明的状态变量
   * （gran / policy / selCurveCode / view / numericCodes 等），提前调用会读到 undefined。 */

  function fatal(e) {
    // 兜底：即使脚本整体出错，也要给出可读页面而不是白屏
    var box = el("emptyState");
    if (box) {
      box.innerHTML = '<div class="empty-state"><div class="es-title">看板渲染异常</div>' +
        '<div class="es-sub">' + esc(e && e.message ? e.message : String(e)) + "</div></div>";
    }
    if (window.console) console.error(e);
  }

  function boot() {
    /* ---------- header ---------- */
    var meta = obj(D.meta);
    safe("header", function () {
      el("mPeriod").textContent = (meta.start || "?") + " ~ " + (meta.end || "?");
      el("mRange").textContent = meta.data_from ? (meta.data_from + " ~ " + (meta.data_to || "?")) : "本周期无数据";
      el("mSource").textContent = meta.source || "-";
      el("mGen").textContent = meta.generated_at || "-";
      el("footGen").textContent = "生成于 " + (meta.generated_at || "-");
    });

    var ov = obj(D.overview);
    var EMPTY = !!meta.empty || !(num(ov.total) > 0);

    /* ---------- 空数据兜底 ---------- */
    if (EMPTY) {
      safe("emptyState", renderEmptyState);
      safe("llm", renderAnalysis);
      ["secCurve", "secTrend", "secEvent", "secCodes"].forEach(hide);
      return;   // 无数据：不再尝试渲染任何图表
    }

    safe("curve", initCurve);      // 统一「曲线查看」模块（两视图合一）
    safe("trend", initTrend);
    safe("event", renderEventSection);   // 异常事件时间轴 + 异常事件构成
    safe("codes", renderCodes);
    safe("llm", renderAnalysis);

    if (ERRS.length) {
      var box = el("emptyState");
      if (box) {
        box.innerHTML = '<div class="note-box">看板已生成，但有 ' + ERRS.length +
          ' 个模块渲染异常（已降级处理）：' + esc(ERRS.join(" | ")) + "</div>";
      }
    }

    /* ---------- responsive ---------- */
    window.addEventListener("resize", function () {
      Object.keys(charts).forEach(function (k) {
        try { charts[k].resize(); } catch (_) {}
      });
    });
  }

  /* ================= 空数据兜底 ================= */
  function renderEmptyState() {
    var meta = obj(D.meta), db = obj(D.db);
    // 无数据时同步修正分析区副标题，避免出现「已做自动评估」的误导性说明
    var ln = el("llmNote");
    if (ln) ln.textContent = "本周期无遥测记录，本节不参与健康评估";
    var last = db.last || "库内无任何记录";
    var cells = [
      { k: "统计周期", v: (meta.start || "?") + " ~ " + (meta.end || "?") },
      { k: "本周期记录数", v: "0 条" },
      { k: "库内数据范围", v: db.first ? (db.first + " ~ " + db.last) : "无" },
      { k: "距最后一条记录", v: isNum(meta.gap_days) ? (meta.gap_days + " 天") : "-" }
    ];
    el("emptyState").innerHTML =
      '<div class="empty-state">' +
      '<div class="es-ico">0</div>' +
      '<div class="es-title">本周期无遥测异常记录</div>' +
      '<div class="es-sub">数据源：' + esc(meta.source || "-") + ' · 生成时间 ' + esc(meta.generated_at || "-") + "</div>" +
      '<div class="es-grid">' + cells.map(function (c) {
        return '<div class="es-cell"><div class="k">' + esc(c.k) + '</div><div class="v">' + esc(c.v) + "</div></div>";
      }).join("") + "</div>" +
      '<div class="es-tips"><b>可能原因与排查建议</b><ol>' +
      "<li>确认统计周期是否覆盖卫星实际过境时段（星上哨兵记录呈分段分布，非全天连续）。</li>" +
      "<li>确认监测服务在本周期是否正常运行、是否写入 <code>status_cmd_records</code> 表。</li>" +
      "<li>核对数据源连通性与库内时间范围（" + esc(last) + "）。</li>" +
      "<li>本周期确无异常即为健康状态，可结合上一周期看板对比确认趋势。</li>" +
      "</ol></div></div>";
  }

  /* ================= 统一「曲线查看」模块 =================
   * 两种数据视图（取值时间序列 / 数值型遥测曲线）合并进同一区块：
   *   · 共用同一个代号选择器 selCurve（状态 selCurveCode），避免多个筛选器互相冲突；
   *   · 共用同一个图表容器 chartCurve，切换视图时 setOption(notMerge) 复用各视图原有的
   *     坐标轴 / 图例 / tooltip / dataZoom / toolbox 配置，交互行为保持一致；
   *   · 「缺失点」策略仅作用于取值序列视图，切换视图时隐藏而非丢弃，筛选能力不丢失。
   * 注：「核验窗口异常命中率」视图已按需求移除；核验窗口数据（D.windows）现仅用于
   *     数值型遥测曲线的采样取值，不再参与任何口径统计（事件口径由 D.events 提供）。
   */
  var policy = obj(D.series).policy === "skip" ? "skip" : "mark";
  var selCurveCode = null;
  var view = "series";        // 当前视图：series | numeric
  var numericCodes = [];      // 数值型代号（采样值 float64）
  var allCodes = [];          // 统一代号列表（去重，按事件量降序）

  /* 扫描核验窗口，收集数值型代号（数值曲线视图可用性判定） */
  function collectMeta() {
    arr(D.windows).forEach(function (w) {
      arr(w.codes).forEach(function (c) {
        if (!c || !c.code) return;
        if (c.num && numericCodes.indexOf(c.code) < 0) numericCodes.push(c.code);
      });
    });
    var rank = {};
    arr(D.codes).forEach(function (c) {
      if (!c || !c.code) return;
      rank[c.code] = num(c.total);
      if (allCodes.indexOf(c.code) < 0) allCodes.push(c.code);
    });
    var S = obj(D.series);
    arr(S.order).concat(Object.keys(obj(S.codes)), numericCodes).forEach(function (c) {
      if (c && allCodes.indexOf(c) < 0) allCodes.push(c);
    });
    allCodes.sort(function (a, b) { return (rank[b] || 0) - (rank[a] || 0); });
  }

  function setSelect(id, val) {
    var s = el(id);
    if (!s) return;
    for (var i = 0; i < s.options.length; i++) {
      if (s.options[i].value === val) { s.selectedIndex = i; return; }
    }
  }
  /* 胶囊按钮组：鼠标与键盘（Enter / 空格）行为一致，选中态视觉统一 */
  function bindChips(sel, onPick) {
    var list = document.querySelectorAll(sel + " .chip");
    Array.prototype.forEach.call(list, function (c) {
      function pick() {
        Array.prototype.forEach.call(list, function (x) { x.classList.remove("on"); });
        c.classList.add("on");
        onPick(c);
      }
      c.addEventListener("click", pick);
      c.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
      });
    });
  }

  /* 切换视图：同步页签高亮（含无障碍状态）并刷新同一容器内的图表 */
  function setView(v) {
    view = v;
    Array.prototype.forEach.call(document.querySelectorAll("#curveTabs .tab"), function (t) {
      var on = t.getAttribute("data-v") === v;
      if (on) t.classList.add("on"); else t.classList.remove("on");
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    safe("curve", renderCurve);
  }

  function initCurve() {
    collectMeta();
    var S = obj(D.series);
    var sel = el("selCurve");
    if (!sel) return;
    if (!allCodes.length) { hide("secCurve"); return; }   // 无任何代号 → 整节隐藏

    // 统一代号选择器：标注「数值型」「无取值序列」，让三种视图的可用性一眼可见
    sel.innerHTML = allCodes.map(function (c) {
      var m = obj(D.code_meta)[c] || {};
      var tag = numericCodes.indexOf(c) >= 0 ? "（数值型）"
              : (obj(S.codes)[c] ? "" : "（无取值序列）");
      return '<option value="' + esc(c) + '">' + esc(c) + " · " + esc(m.name || "未知") + tag + "</option>";
    }).join("");
    selCurveCode = allCodes[0];
    sel.addEventListener("change", function () { selCurveCode = sel.value; renderCurve(); });

    // 页签：本周期无对应数据的视图置灰，避免切到空图
    var avail = {
      series: Object.keys(obj(S.codes)).length > 0,
      numeric: numericCodes.length > 0
    };
    Array.prototype.forEach.call(document.querySelectorAll("#curveTabs .tab"), function (t) {
      var v = t.getAttribute("data-v");
      if (!avail[v]) {
        t.classList.add("off");
        t.title = "本周期无该视图可用数据";
        t.setAttribute("aria-disabled", "true");   // 置灰视图：鼠标与键盘均不响应
      }
      t.addEventListener("click", function () {
        if (t.classList.contains("off")) return;   // 不可用视图忽略点击
        setView(v);
      });
      t.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (!t.classList.contains("off")) setView(v); }
      });
    });
    // 默认视图优先级：取值序列 > 数值曲线
    view = avail.series ? "series" : (avail.numeric ? "numeric" : "series");

    // 缺失点策略（仅取值序列视图生效）
    bindChips("#curvePolicy", function (c) {
      policy = c.getAttribute("data-p") === "skip" ? "skip" : "mark";
      if (view === "series") safe("curve", renderCurve);
    });

    // 来源图例：明确区分主源 / 补全 / 缺失三类点
    el("seriesLegend").innerHTML =
      '<span><i style="background:' + C.confirmed + '"></i>异常确认（精确采样）</span>' +
      '<span><i class="sq" style="background:#fff;border-color:' + C.confirmed + '"></i>异常确认（时间戳插值）</span>' +
      '<span><i class="tri" style="background:' + C.repeat + '"></i>异常重复补全</span>' +
      '<span><i class="sq" style="background:#94a3b8"></i>缺失（不可补全）</span>';

    // 无取值序列的代号（多为仅出现在校验未通过中）统一提示
    var rest = allCodes.filter(function (c) { return !obj(S.codes)[c]; });
    if (rest.length) {
      el("seriesNote").innerHTML = '<div class="note-box tight">下列代号本周期未出现「异常确认」记录，' +
        '取值序列视图暂无数据（多为仅出现在「校验未通过」中的代号）：<b>' + esc(rest.join("、")) + "</b></div>";
    }
    setView(view);
  }

  /* 视图分发：刷新同一容器内的图表，并只展示当前视图需要的控件 */
  function renderCurve() {
    var isSeries = view === "series", isNum = view === "numeric";
    var pw = el("curvePolicyWrap"), mt = el("curveMeta");
    if (pw) pw.style.display = isSeries ? "" : "none";      // 缺失点策略仅序列视图有意义
    if (mt) mt.style.display = isSeries ? "" : "none";      // 统计胶囊 / 来源图例仅序列视图
    var nt = el("curveNote");
    if (nt) nt.innerHTML = "";

    if (isNum) {
      // 数值视图只对数值型参数有效：当前代号非数值型时自动切到第一个数值型参数并提示
      if (numericCodes.length && numericCodes.indexOf(selCurveCode) < 0) {
        var to = numericCodes[0];
        nt.innerHTML = '<div class="note-box inset">「数值型遥测曲线」仅适用于数值型参数，' +
          '已自动切换至 <b>' + esc(to) + " · " + esc((obj(D.code_meta)[to] || {}).name || "未知") + "</b></div>";
        setSelect("selCurve", to);
        selCurveCode = to;
      }
      renderNumericView();
      return;
    }
    renderSeriesView();
  }

  /* ---- 视图一：代号取值时间序列（精确时间点） ---- */
  function renderSeriesView() {
    var S = obj(D.series);
    var code = selCurveCode;
    var c = obj(S.codes)[code];
    var m = obj(D.code_meta)[code] || {};
    var box = el("seriesStat");
    var ch = mk("chartCurve");
    if (ch) ch.off("click");   // 清掉窗口视图遗留的点击事件

    // 该代号无序列（例如只有校验未通过记录）→ 明确提示，不画空图
    if (!c || !arr(c.pts).length) {
      renderEmpty("chartCurve", "代号 " + code + " 本周期暂无取值序列（未出现「异常确认」或「异常重复」记录）");
      if (box) box.innerHTML = "";
      setTitle("curveTitle", code + " · " + (m.name || "") + " 取值时间序列");
      return;
    }

    var raw = arr(c.pts);
    var pts = [];
    raw.forEach(function (p) {
      if (!p || !isNum(p[0])) return;                       // 时间戳缺失/非法 → 直接跳过该点
      var y = (p[1] === null || p[1] === undefined || !isFinite(Number(p[1]))) ? null : Number(p[1]);
      var src = isNum(p[3]) ? p[3] : SRC_CONFIRMED;
      if (y === null && src !== SRC_MISSING) src = SRC_MISSING;  // 无值即视为缺失，统一标注
      if (y === null && policy === "skip") return;               // 策略：跳过缺失点
      pts.push({ t: p[0], y: y, lv: isNum(p[2]) ? p[2] : -1, src: src });
    });
    pts.sort(function (a, b) { return a.t - b.t; });

    if (!pts.length) {
      renderEmpty("chartCurve", "代号 " + code + " 在「跳过缺失点」策略下暂无可绘制的取值，可切换为「标注为缺失」查看");
      if (box) box.innerHTML = "";
      return;
    }

    var stat = { confirmed: 0, repeat: 0, missing: 0, total: pts.length };
    pts.forEach(function (p) {
      if (p.src === SRC_MISSING) stat.missing++;
      else if (p.src === SRC_REPEAT) stat.repeat++;
      else stat.confirmed++;
    });
    setTitle("curveTitle", code + " · " + (m.name || "") + " 取值时间序列");
    box.innerHTML =
      '<span class="pill p-confirmed">异常确认 <b>' + fmt(stat.confirmed) + "</b></span>" +
      '<span class="pill p-repeat">重复补全 <b>' + fmt(stat.repeat) + "</b></span>" +
      '<span class="pill p-missing">缺失 <b>' + fmt(stat.missing) + "</b></span>" +
      '<span class="pill">合计 <b>' + fmt(stat.total) + "</b></span>" +
      '<span class="pill">类型 <b>' + (c.kind === "numeric" ? "数值型" : "枚举/状态型") + "</b></span>" +
      '<span class="pill">补全容差 <b>' + (isNum(S.tol_ms) ? (S.tol_ms / 1000) + "s" : "-") + "</b></span>";

    var isEnum = c.kind !== "numeric";
    var ys = pts.filter(function (p) { return p.y !== null; }).map(function (p) { return p.y; });
    var mn = ys.length ? Math.min.apply(null, ys) : 0;
    var mx = ys.length ? Math.max.apply(null, ys) : 1;
    var rng = (mx - mn) || 1;
    // 缺失点绘制在坐标区下方的一条"缺失带"上，避免与真实取值混淆
    var missY = isEnum ? -0.28 : (mn - rng * 0.12);

    var lineData = pts.map(function (p) {
      var s = SRC[p.src] || SRC[SRC_CONFIRMED];
      return {
        value: [p.t, p.y],                    // y 为 null 时折线在此断开（gap）
        t: p.t, y: p.y, lv: p.lv, src: p.src,
        symbol: s.sym, symbolSize: s.sz,
        itemStyle: { color: s.c, borderColor: "#fff", borderWidth: p.src === SRC_INTERP ? 1.4 : 0 }
      };
    });
    var missData = pts.filter(function (p) { return p.src === SRC_MISSING; })
      .map(function (p) {
        return {
          value: [p.t, missY], t: p.t, y: null, lv: p.lv, src: p.src,
          symbol: "rect", symbolSize: 8, itemStyle: { color: "#94a3b8" }
        };
      });

    var yAxis;
    if (isEnum) {
      // 固定 -0.5 ~ 1.5、步长 0.5，保证刻度恰好落在 0/1 上，正常/异常标签才显示得出来
      yAxis = {
        type: "value", name: "取值状态", min: -0.5, max: 1.5, interval: 0.5,
        nameTextStyle: { color: "#6b7280", fontSize: 11 },
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisLabel: {
          color: "#5b6577", fontSize: 11,
          formatter: function (v) { return v === 1 ? "异常" : (v === 0 ? "正常" : ""); }
        },
        splitLine: { lineStyle: { color: "#eef1f6" } }
      };
    } else {
      yAxis = {
        type: "value", name: (m.name || code), scale: true,
        min: mn - rng * 0.22, max: mx + rng * 0.12,
        nameTextStyle: { color: "#6b7280", fontSize: 11 },
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisLabel: { color: "#5b6577", fontSize: 11 },
        splitLine: { lineStyle: { color: "#eef1f6" } }
      };
    }

    function valText(p) {
      if (p.y === null || p.y === undefined) return "缺失（无可补全取值）";
      if (p.src === SRC_REPEAT) return "异常态（由「异常重复」补全，无原始取值）";
      if (!isEnum) return String(p.y);
      var rawTxt = (p.lv >= 0 && arr(c.levels)[p.lv] !== undefined) ? arr(c.levels)[p.lv] : null;
      if (p.y === 1) return rawTxt ? rawTxt : "异常态";
      if (p.y === 0) return rawTxt ? rawTxt : "正常态";
      return String(p.y);
    }
    function srcText(p) { return (SRC[p.src] || SRC[SRC_CONFIRMED]).n; }

    var series = [{
      name: "取值", type: "line", connectNulls: false,
      step: isEnum ? "middle" : false,               // 状态型用阶梯线，数值型用折线
      showSymbol: pts.length <= 3000, symbolSize: 6,
      lineStyle: { width: 1.5, color: C.primary },
      data: lineData,
      emphasis: { focus: "series" },
      markLine: (!isEnum && ys.length) ? {
        silent: true, symbol: "none",
        label: { fontSize: 10, color: "#6b7280", formatter: function (p) { return p.name; } },
        lineStyle: { color: "#f59e0b", type: "dashed", width: 1 },
        data: [{ name: "最大 " + mx.toFixed(3), yAxis: mx }, { name: "最小 " + mn.toFixed(3), yAxis: mn }]
      } : undefined
    }];
    if (missData.length) {
      series.push({
        name: "缺失（不可补全）", type: "scatter", symbol: "rect", symbolSize: 8,
        itemStyle: { color: "#94a3b8" }, data: missData, z: 5
      });
    }

    mk("chartCurve").setOption({
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(255,255,255,.97)", borderColor: "#c8d0e0", borderWidth: 1,
        textStyle: { color: "#1f2937", fontSize: 12 },
        formatter: function (p) {
          var d = p.data || {};
          if (!isNum(d.t)) return "";
          return "<b>" + fmtTimeMS(d.t) + "</b><br>" +
                 "<b>" + esc(code) + "</b> " + esc(m.name || "") + "<br>" +
                 "取值：<b>" + esc(valText(d)) + "</b><br>" +
                 "来源：" + esc(srcText(d));
        }
      },
      legend: legendCfg(series.map(function (s) { return s.name; })),
      toolbox: TOOLBOX,
      grid: BASE_GRID,
      xAxis: AXIS_TIME,
      yAxis: yAxis,
      dataZoom: zoomCfg(),
      series: series
    }, true);
  }

  /* ================= 异常事件时间趋势（按事件口径重建） =================
   * 数据来源：data.events.events —— 每个元素 = 一个异常事件（merge_id 去重后的一条形态链）。
   * 后端 scripts/event_dedup.py 把「曲线查看 · 取值时间序列」作为唯一输入交给
   * status_event_dedup 提取；本模块只做呈现，不再自行统计原始记录或另立口径。
   *
   * 展示规划（旧版「分钟/小时粒度 × 事件数 / 频次」柱线双系列整体废弃）：
   *   ① 摘要条：三项口径（事件/频次/物理段）+ 事件批次数 + 单事件时长（中位/最长）+ 峰值时段；
   *   ② 频次图：自适应时间桶内堆叠「单项 / 组合代号异常事件」数（合计 = anomaly_events），
   *              持续时长按档位放进 tooltip —— 回答"多频繁、每次多久"；
   *   ③ 时间线：y = 代号异常事件（代号组合）、x = 时间，每条 = 一个事件的起止跨度（甘特），
   *              回答"谁在什么时候、持续多久"，持续型硬故障与瞬时扰动一眼可分。
   */
  var scope = "all";
  var TREND_COLOR = { single: C.primary, combo: C.repeat };
  /* 术语统一（原「单项签名 / 组合签名」已废弃）：
   * 图例、坐标轴、tooltip、筛选胶囊一律走这两个常量，避免各处各写一版导致口径漂移。 */
  var EVT_TERM = { single: "单项代号异常事件", combo: "组合代号异常事件", all: "全部代号异常事件" };
  function evtTerm(kind) {
    return String(kind || "").indexOf("组合") >= 0 ? EVT_TERM.combo : EVT_TERM.single;
  }

  function initTrend() {
    renderTrend();
    bindChips("#trendScope", function (c) {
      scope = c.getAttribute("data-s") === "single" ? "single" : "all";
      renderTrend();
    });
  }

  /* 时间字符串 "YYYY-MM-DD HH:mm:ss" → 毫秒；非法返回 null（调用方跳过该事件） */
  function tms(s) {
    var t = new Date(String(s || "").replace(" ", "T")).getTime();
    return isFinite(t) ? t : null;
  }
  function fmtDur(sec) {
    var s = num(sec);
    if (!s) return "0 s";
    if (s < 60) return Math.round(s) + " s";
    if (s < 3600) return (s / 60).toFixed(1) + " min";
    return (s / 3600).toFixed(1) + " h";
  }
  function p2(n) { return n < 10 ? "0" + n : "" + n; }
  function bucketLabel(k, w) {
    var d = new Date(k), s = p2(d.getMonth() + 1) + "-" + p2(d.getDate());
    return w >= 864e5 ? s : s + " " + p2(d.getHours()) + ":" + p2(d.getMinutes());
  }
  function bucketUnit(w) {
    if (w >= 864e5) return "天";
    if (w >= 36e5) return Math.round(w / 36e5) + " 小时";
    return Math.round(w / 6e4) + " 分钟";
  }
  /* 桶宽自适应：按事件时间跨度选档，避免 24 h 里画 1400 个分钟桶、或 7 天里只画 7 根柱 */
  function bucketPlan(list) {
    var ts = list.map(function (e) { return tms(e.start); })
      .filter(function (t) { return t !== null; });
    if (!ts.length) return 36e5;
    var spanH = (Math.max.apply(null, ts) - Math.min.apply(null, ts)) / 3.6e6;
    if (spanH <= 12) return 30 * 6e4;
    if (spanH <= 48) return 36e5;
    if (spanH <= 96) return 2 * 36e5;
    if (spanH <= 24 * 7) return 6 * 36e5;
    if (spanH <= 24 * 14) return 12 * 36e5;
    return 864e5;
  }
  function blankBucket(k) {
    return { k: k, n: 0, single: 0, combo: 0, segs: 0, dur: 0, maxDur: 0, avg: 0, codesN: 0,
             bands: [0, 0, 0, 0] };
  }
  /* 持续时长分档：把"强度"编码进堆叠结构。
   * 不用右轴折线——事件数与持续时长天然正相关，折线几乎总落在柱体内部被完全遮挡。 */
  var DUR_BANDS = [
    { n: "≤ 5 分钟", max: 300, c: "#93b4f5" },
    { n: "5–60 分钟", max: 3600, c: "#5b8def" },
    { n: "1–6 小时", max: 21600, c: "#2f5fd0" },
    { n: "> 6 小时", max: Infinity, c: "#1e3a8a" }
  ];
  function bandOf(sec) {
    var s = num(sec);
    for (var i = 0; i < DUR_BANDS.length - 1; i++) if (s <= DUR_BANDS[i].max) return i;
    return DUR_BANDS.length - 1;
  }
  /* 事件 → 连续时间桶（空桶补 0，保证时间轴不断档） */
  function buildBuckets(list, w) {
    var map = {}, keys = [];
    list.forEach(function (e) {
      var t = tms(e.start);
      if (t === null) return;
      var k = Math.floor(t / w) * w;
      var b = map[k];
      if (!b) { b = map[k] = blankBucket(k); keys.push(k); }
      b.n++;
      if (e.kind === "组合") b.combo++; else b.single++;
      b.segs += num(e.segs);
      b.dur += num(e.duration_s);
      b.maxDur = Math.max(b.maxDur, num(e.duration_s));
      b.bands[bandOf(e.duration_s)]++;
      b._codes = b._codes || {};
      arr(e.codes).forEach(function (c) { b._codes[c] = 1; });
    });
    if (!keys.length) return [];
    keys.sort(function (a, b) { return a - b; });
    var out = [];
    for (var k = keys[0], last = keys[keys.length - 1]; k <= last; k += w) {
      var b = map[k] || blankBucket(k);
      b.avg = b.n ? b.dur / b.n : 0;
      b.codesN = b._codes ? Object.keys(b._codes).length : 0;
      out.push(b);
    }
    return out;
  }

  function renderTrend() {
    var E = obj(D.events);
    var st = obj(scope === "single" ? E.stats_single : E.stats);
    var all = arr(E.events);
    // 口径切换只筛"事件"本身：仅单项 = 只保留单项代号异常事件；全部 = 单项 + 同采样点并发组合
    var list = all.filter(function (e) {
      if (!e) return false;
      return scope === "single" ? e.kind !== "组合" : true;
    });
    setTitle("trendTitle", "事件频次与结构 · " +
      (scope === "single" ? "仅" + EVT_TERM.single : EVT_TERM.all));

    renderTrendSum(st, list);
    if (!list.length) {
      renderEmpty("chartTrend", "本周期暂无异常事件（无成立异常段，或去重后为空）");
      renderEmpty("chartTrendTl", "本周期暂无异常事件");
      setTitle("trendTlHint", "");
      return;
    }
    safe("trendFreq", function () { renderTrendFreq(list); });
    safe("trendTl", function () { renderTrendTimeline(list); });
  }

  /* ---- ① 摘要条：把事件级口径一次说清，避免读者把「事件 / 频次 / 物理段」混为一谈 ---- */
  function renderTrendSum(st, list) {
    var box = el("trendSum");
    if (!box) return;
    // 事件批次：相邻事件起始时刻相隔 ≤5 分钟归为同一波（同一采样点并发代号异常事件的自然聚合）
    var ts = list.map(function (e) { return tms(e.start); })
      .filter(function (t) { return t !== null; }).sort(function (a, b) { return a - b; });
    var waves = 0, prev = null;
    ts.forEach(function (t) {
      if (prev === null || t - prev > 5 * 6e4) waves++;
      prev = t;
    });
    var durs = list.map(function (e) { return num(e.duration_s); }).sort(function (a, b) { return a - b; });
    var med = durs.length ? durs[Math.floor((durs.length - 1) / 2)] : 0;
    var mx = durs.length ? durs[durs.length - 1] : 0;
    // 峰值时段：事件数最多的桶（与频次图同一套自适应桶宽）
    var plan = bucketPlan(list);
    var buckets = buildBuckets(list, plan);
    var peak = null;
    buckets.forEach(function (b) { if (!peak || b.n > peak.n || (b.n === peak.n && b.maxDur > peak.maxDur)) peak = b; });

    box.innerHTML = '<div class="pills">' +
      '<span class="pill p-confirmed">异常事件 <b>' + fmt(st.anomaly_events) + "</b> 个</span>" +
      '<span class="pill p-repeat">异常频次 <b>' + fmt(st.anomaly_frequency) + "</b> 次</span>" +
      '<span class="pill">物理段 <b>' + fmt(st.physical_segments) + "</b> 段</span>" +
      '<span class="pill">事件批次 <b>' + fmt(waves) + "</b> 波</span>" +
      '<span class="pill">单事件时长 中位 <b>' + esc(fmtDur(med)) + "</b> · 最长 <b>" + esc(fmtDur(mx)) + "</b></span>" +
      (peak && peak.n
        ? '<span class="pill">峰值时段 <b>' + esc(bucketLabel(peak.k, plan)) + "</b> · " +
          fmt(peak.n) + " 个事件</span>"
        : "") +
      "</div>";
  }

  /* ---- ② 频次与结构：时间桶内堆叠「单项 / 组合代号异常事件」数（合计 = anomaly_events） ---- */
  function renderTrendFreq(list) {
    var w = bucketPlan(list);
    var buckets = buildBuckets(list, w);
    if (!buckets.length) { renderEmpty("chartTrend", "本周期暂无异常事件"); return; }
    var cats = buckets.map(function (b) { return bucketLabel(b.k, w); });
    var series = [
      {
        name: EVT_TERM.single, type: "bar", stack: "ev", barMaxWidth: 30,
        itemStyle: { color: TREND_COLOR.single }, emphasis: { focus: "series" },
        data: buckets.map(function (b) { return b.single; })
      },
      {
        name: EVT_TERM.combo, type: "bar", stack: "ev", barMaxWidth: 30,
        itemStyle: { color: TREND_COLOR.combo, borderRadius: [2, 2, 0, 0] },
        emphasis: { focus: "series" },
        data: buckets.map(function (b) { return b.combo; })
      }
    ];

    mk("chartTrend").setOption({
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: "rgba(255,255,255,.97)", borderColor: "#c8d0e0", borderWidth: 1,
        textStyle: { color: "#1f2937", fontSize: 12 },
        formatter: function (ps) {
          if (!ps || !ps.length) return "";
          var b = buckets[ps[0].dataIndex] || {};
          if (!b.n) return "<b>" + esc(bucketLabel(b.k, w)) + "</b><br>该时段无异常事件";
          // 持续时长按档给出，避免第二条 Y 轴（折线会被柱体完全遮挡）
          var parts = [];
          DUR_BANDS.forEach(function (d, i) {
            if (b.bands[i]) parts.push(d.n + " × " + b.bands[i]);
          });
          return "<b>" + esc(bucketLabel(b.k, w)) + "</b> 起的 1 " + bucketUnit(w) + " 内<br>" +
                 "异常事件：<b>" + fmt(b.n) + "</b> 个<br>" +
                 "· " + EVT_TERM.single + " <b>" + fmt(b.single) + "</b> 个<br>" +
                 "· " + EVT_TERM.combo + " <b>" + fmt(b.combo) + "</b> 个<br>" +
                 "持续时长：" + esc(parts.join(" · ")) + "<br>" +
                 "物理段 " + fmt(b.segs) + " 段 · 涉及代号 " + fmt(b.codesN) + " 个 · 最长 " + esc(fmtDur(b.maxDur));
        }
      },
      legend: legendCfg([EVT_TERM.single, EVT_TERM.combo], 12),
      toolbox: TOOLBOX,
      grid: BASE_GRID,
      xAxis: {
        type: "category", data: cats, boundaryGap: true,
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisTick: { alignWithLabel: true },
        axisLabel: { color: "#5b6577", fontSize: 11, hideOverlap: true }
      },
      yAxis: {
        type: "value", name: "事件数", minInterval: 1,
        nameTextStyle: { color: "#6b7280", fontSize: 11 },
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisLabel: { color: "#5b6577", fontSize: 11 },
        splitLine: { lineStyle: { color: "#eef1f6" } }
      },
      dataZoom: zoomCfg(),
      series: series
    }, true);
  }

  /* ---- ③ 事件时间线（甘特）：一个事件一条，跨度 = merged_start → merged_end ---- */
  /* 甘特条通用绘制器：按类目带宽画矩形，最小宽度 3px 保证零/短时长仍可见。
   * 供「事件时间线」与「异常事件时间轴」共用，两处条带视觉完全一致。 */
  function ganttItem(params, api) {
    var y = api.value(0);
    var s = api.coord([api.value(1), y]);
    var e = api.coord([api.value(2), y]);
    var band = api.size([0, 1])[1];
    var h = Math.max(6, Math.min(16, band * 0.56));
    var wpx = Math.max(e[0] - s[0], 3);
    return {
      type: "rect",
      shape: { x: s[0], y: s[1] - h / 2, width: wpx, height: h, r: 2 },
      style: api.style()
    };
  }

  /* 代号组合标签：窄屏下只用「首代号 +N」，避免长组合名把绘图区挤没（全量组合在 tooltip 里） */
  function sigLabel(combo, brief) {
    var parts = String(combo || "-").split("+");
    if (parts.length <= 1) return combo;
    if (brief) return parts[0] + " +" + (parts.length - 1);
    if (parts.length === 2) return combo;
    return parts[0] + "+" + parts[1] + " 等" + parts.length + " 项";
  }
  function renderTrendTimeline(list) {
    var evs = list.filter(function (e) { return tms(e.start) !== null; });
    if (!evs.length) { renderEmpty("chartTrendTl", "本周期暂无异常事件（时间戳不可用）"); return; }

    // 按代号组合分组，y 轴顺序 = 首次出现时间（读出故障扩散顺序），同起点按事件数降序
    var groups = {}, order = [];
    evs.forEach(function (e) {
      var k = String(e.combo || "-");
      if (!groups[k]) { groups[k] = { key: k, n: 0, first: tms(e.start) }; order.push(k); }
      groups[k].n++;
      groups[k].first = Math.min(groups[k].first, tms(e.start));
    });
    order.sort(function (a, b) {
      var A = groups[a], B = groups[b];
      return (A.first - B.first) || (B.n - A.n) || String(a).localeCompare(String(b));
    });
    var idxOf = {};
    order.forEach(function (k, i) { idxOf[k] = i; });

    var singleD = [], comboD = [], tMin = null, tMax = null;
    evs.forEach(function (e) {
      var s = tms(e.start), t2 = tms(e.end);
      if (t2 === null || t2 < s) t2 = s;          // 零时长事件：仍画成 3px 短条，不丢点
      var yi = idxOf[String(e.combo || "-")];
      if (yi === undefined) return;
      var item = { value: [yi, s, t2], ev: e };
      (e.kind === "组合" ? comboD : singleD).push(item);
      tMin = (tMin === null) ? s : Math.min(tMin, s);
      tMax = (tMax === null) ? t2 : Math.max(tMax, t2);
    });
    if (tMin === null) { renderEmpty("chartTrendTl", "本周期暂无异常事件（时间戳不可用）"); return; }

    function tip(p) {
      var e = (p.data && p.data.ev) || {};
      return "<b>" + esc(e.combo || "-") + "</b>（" + esc(evtTerm(e.kind)) + "）<br>" +
             "起止：" + esc(e.start || "-") + " → " + esc(e.end || "-") + "<br>" +
             "持续：<b>" + esc(fmtDur(e.duration_s)) + "</b>（累计异常时长）<br>" +
             "物理段 " + fmt(e.segs) + " 段 · 跨时间组 " + fmt(e.groups) + " 个 · 采样 " + fmt(e.samples) + " 点" +
             "<br>事件 ID：" + esc(e.id || "-");
    }
    function mkSeries(name, color, data) {
      return {
        name: name, type: "custom", renderItem: ganttItem,
        encode: { x: [1, 2], y: 0 },
        itemStyle: { color: color },
        data: data, clip: true,
        emphasis: { itemStyle: { opacity: 1, shadowBlur: 6, shadowColor: "rgba(15,23,42,.25)" } }
      };
    }

    setTitle("trendTlHint", order.length + " 类代号异常事件 · " + evs.length + " 个事件 · 条长即持续跨度");
    // 窄屏下代号组合标签会把绘图区挤没：按容器宽度收窄左侧留白（标签本身已做「等 N 项」缩写）
    var cw = (el("chartTrendTl") || {}).clientWidth || 900;
    var brief = cw < 820;                       // 标签改用「首代号 +N」
    var gLeft = cw < 560 ? 82 : (cw < 820 ? 96 : 132);
    mk("chartTrendTl").setOption({
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(255,255,255,.97)", borderColor: "#c8d0e0", borderWidth: 1,
        textStyle: { color: "#1f2937", fontSize: 12 },
        formatter: tip
      },
      legend: legendCfg([EVT_TERM.single, EVT_TERM.combo], 12),
      toolbox: TOOLBOX,
      grid: { left: gLeft, right: 22, top: 44, bottom: 62 },
      xAxis: {
        type: "time",
        min: tMin - 15 * 6e4, max: tMax + 15 * 6e4,   // 两端留 15 分钟，避免短条贴边
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisLabel: {
          color: "#5b6577", fontSize: 11, hideOverlap: true,
          // 默认时间轴在跨天处只显示 "24"，这里改为"日期 + 时刻"，零点刻度换成日期
          formatter: function (v) {
            var d = new Date(v);
            if (d.getHours() === 0 && d.getMinutes() === 0) return p2(d.getMonth() + 1) + "-" + p2(d.getDate());
            return p2(d.getHours()) + ":" + p2(d.getMinutes());
          }
        },
        splitLine: { show: true, lineStyle: { color: "#eef1f6" } }
      },
      yAxis: {
        type: "category", data: order, inverse: true,
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisTick: { show: false },
        axisLabel: {
          color: "#4f5a6e", fontSize: 11,
          formatter: function (v) { return sigLabel(v, brief); }
        },
        splitLine: { show: true, lineStyle: { color: "#f4f6fa" } }
      },
      dataZoom: zoomCfg(),
      series: [mkSeries(EVT_TERM.single, TREND_COLOR.single, singleD),
               mkSeries(EVT_TERM.combo, TREND_COLOR.combo, comboD)]
    }, true);
  }

  /* ================= 图4：异常事件时间轴 / 异常事件构成 =================
   * 数据源与「异常事件时间趋势」完全一致：data.events.events（merge_id 去重后的事件对象）。
   *
   * 为什么替换旧的两张「代号」图：
   *   旧「代号异常活动时间轴」画的是 5 秒核验窗口散点（确认/重复/未通过三色），
   *   旧「代号异常构成」按同样三态堆叠 —— 两者都是核验窗口口径，
   *   与全看板统一后的「异常事件」口径（6 秒成段 → 91 分钟形态链合并 → merge_id 去重）不一致，
   *   同一批数据在两处会给出互相矛盾的读法，故整体改为事件口径视图。
   *
   * 两张图分工（都按代号归集，与趋势模块的「按代号组合」视角互补、不重复）：
   *   · 时间轴：y = 代号、x = 时间，条 = 该代号参与事件的持续区间 —— 回答"哪些代号在什么时候异常、多久"；
   *   · 构成：  y = 代号、x = 事件数，堆叠「单项事件 / 组合事件」 —— 回答"这个代号的事件是独立发生还是成组发生"。
   *   事件含 N 个代号时按代号各计 1 次，因此代号事件数合计 > 事件总数（卡片 hint 已注明，不制造"总数对不上"的错觉）。
   */

  /* 事件 → 按代号归集的区间。
   * 同一代号在同一时段会同时命中「A 单项」与「A+B 组合」两种代号异常事件（同一物理异常的两种表达），
   * 若逐事件各画一条，两条会完全重合、视觉上无法分辨；因此同代号内"真重叠"的区间合并为一条，
   * 有间隙（哪怕只有几分钟）则不合并，事件边界得以保留。 */
  function eventSpansByCode(list) {
    var by = {}, order = [];
    list.forEach(function (e) {
      if (!e) return;                            // 脏数据：空条目直接跳过
      var s = tms(e.start), t2 = tms(e.end);
      if (s === null) return;
      if (t2 === null || t2 < s) t2 = s;        // 零时长事件仍画成最短条，不丢点
      arr(e.codes).forEach(function (c) {
        if (!c) return;
        if (!by[c]) { by[c] = []; order.push(c); }
        by[c].push({ s: s, t: t2, e: e });
      });
    });
    var out = order.map(function (c) {
      var items = by[c].slice().sort(function (a, b) { return (a.s - b.s) || (a.t - b.t); });
      var spans = [];
      items.forEach(function (it) {
        var last = spans[spans.length - 1];
        if (last && it.s <= last.t) {
          last.t = Math.max(last.t, it.t);
        } else {
          last = { s: it.s, t: it.t, n: 0, single: 0, combo: 0, sigs: [] };
          spans.push(last);
        }
        last.n++;
        if (it.e.kind === "组合") last.combo++; else last.single++;
        if (last.sigs.indexOf(it.e.combo) < 0 && last.sigs.length < 5) last.sigs.push(it.e.combo);
      });
      return { code: c, n: items.length, spans: spans };
    });
    out.sort(function (a, b) { return (b.n - a.n) || String(a.code).localeCompare(String(b.code)); });
    return out;
  }

  function eventCodeLabel(code) {
    var m = obj(D.code_meta)[code] || {};
    return code + (m.name ? " " + m.name : "");
  }

  function renderEventSection() {
    var E = obj(D.events);
    var list = arr(E.events);                       // 事件口径全量，不受趋势图「仅单项」切换影响
    if (!list.length) {
      renderEmpty("chartEventTime", "本周期暂无异常事件（无成立异常段，或去重后为空）");
      renderEmpty("chartEventBar", "本周期暂无异常事件");
      setTitle("eventTimeHint", "");
      setTitle("eventBarHint", "");
      return;
    }
    var rows = eventSpansByCode(list);
    if (!rows.length) {
      renderEmpty("chartEventTime", "本周期暂无异常事件（时间戳不可用）");
      renderEmpty("chartEventBar", "本周期暂无异常事件");
      setTitle("eventTimeHint", "");
      setTitle("eventBarHint", "");
      return;
    }
    var spans = 0;
    rows.forEach(function (r) { spans += r.spans.length; });
    setTitle("eventTimeHint", rows.length + " 个代号 · " + spans + " 个异常区间 · 条长即持续跨度");
    setTitle("eventBarHint", "按代号归集 · 本期 " + arr(E.events).length +
      " 个事件（组合事件按代号各计 1 次）");
    safe("eventTime", function () { renderEventTime(rows); });
    safe("eventBar", function () { renderEventComp(rows); });
  }

  /* ---- ① 异常事件时间轴：y = 代号，条 = 该代号参与事件的区间 ---- */
  function renderEventTime(rows) {
    var data = [], tMin = null, tMax = null;
    rows.forEach(function (r, yi) {
      r.spans.forEach(function (sp) {
        data.push({ value: [yi, sp.s, sp.t], r: r, sp: sp });
        tMin = (tMin === null) ? sp.s : Math.min(tMin, sp.s);
        tMax = (tMax === null) ? sp.t : Math.max(tMax, sp.t);
      });
    });
    if (!data.length) { renderEmpty("chartEventTime", "本周期暂无异常事件（时间戳不可用）"); return; }

    // 代号 + 中文名可能很长：按容器宽度收缩左侧留白并截断标签，避免挤没绘图区
    var cw = (el("chartEventTime") || {}).clientWidth || 700;
    var gLeft = cw < 520 ? 104 : (cw < 760 ? 164 : 268);
    var yData = rows.map(function (r) { return eventCodeLabel(r.code); });
    var p = 15 * 6e4;                            // 两端各留 15 分钟，避免短条贴边

    mk("chartEventTime").setOption({
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(255,255,255,.97)", borderColor: "#c8d0e0", borderWidth: 1,
        textStyle: { color: "#1f2937", fontSize: 12 },
        formatter: function (pt) {
          var d0 = (pt.data || {}), r = d0.r || {}, sp = d0.sp || {};
          var m = obj(D.code_meta)[r.code] || {};
          var s = "<b>" + esc(r.code) + "</b> " + esc(m.name || "") + "<br>" +
                  "分系统：" + esc(m.apid || "-") + "<br>" +
                  "区间：" + esc(fmtTime(sp.s)) + " → " + esc(fmtTime(sp.t)) + "<br>" +
                  "持续：<b>" + esc(fmtDur((num(sp.t) - num(sp.s)) / 1000)) + "</b><br>" +
                  "事件数：<b>" + fmt(sp.n) + "</b>（单项 " + fmt(sp.single) + " · 组合 " + fmt(sp.combo) + "）";
          if (arr(sp.sigs).length) s += "<br>命中代号异常事件：" + esc(arr(sp.sigs).join(" / "));
          return s;
        }
      },
      toolbox: TOOLBOX,
      grid: { left: gLeft, right: 26, top: 26, bottom: 62 },
      xAxis: {
        type: "time", min: tMin - p, max: tMax + p,
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisLabel: {
          color: "#5b6577", fontSize: 11, hideOverlap: true,
          // 跨天时默认刻度只显示 "24"：零点换成日期，其余显示时刻
          formatter: function (v) {
            var d = new Date(v);
            if (d.getHours() === 0 && d.getMinutes() === 0) return p2(d.getMonth() + 1) + "-" + p2(d.getDate());
            return p2(d.getHours()) + ":" + p2(d.getMinutes());
          }
        },
        splitLine: { show: true, lineStyle: { color: "#eef1f6" } }
      },
      yAxis: {
        type: "category", data: yData, inverse: true,
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisTick: { show: false },
        axisLabel: {
          color: "#4f5a6e", fontSize: 10.5,
          width: gLeft - 14, overflow: "truncate",
          formatter: function (v) { return String(v); }
        },
        splitLine: { show: true, lineStyle: { color: "#f4f6fa" } }
      },
      dataZoom: zoomCfg(),
      series: [{
        name: "异常事件区间",
        type: "custom", renderItem: ganttItem,
        encode: { x: [1, 2], y: 0 },
        itemStyle: { color: C.primary },
        data: data, clip: true,
        emphasis: { itemStyle: { opacity: 1, shadowBlur: 6, shadowColor: "rgba(15,23,42,.25)" } }
      }]
    }, true);
  }

  /* ---- ② 异常事件构成：y = 代号，堆叠「单项事件 / 组合事件」数 ---- */
  function renderEventComp(rows) {
    var yData = rows.map(function (r) { return eventCodeLabel(r.code); });
    function sumOf(k) {
      return rows.map(function (r) {
        var n = 0;
        r.spans.forEach(function (sp) { n += num(sp[k]); });
        return n;
      });
    }
    var singleD = sumOf("single"), comboD = sumOf("combo");
    var total = 0;
    rows.forEach(function (r) { total += r.n; });

    var defs = [
      { n: "单项事件", c: TREND_COLOR.single, d: singleD, stack: "ev" },
      { n: "组合事件", c: TREND_COLOR.combo, d: comboD, stack: "ev" }
    ];
    var cw = (el("chartEventBar") || {}).clientWidth || 700;
    var gLeft = cw < 520 ? 104 : (cw < 760 ? 164 : 268);

    mk("chartEventBar").setOption({
      tooltip: {
        trigger: "axis", axisPointer: { type: "shadow" },
        backgroundColor: "rgba(255,255,255,.97)", borderColor: "#c8d0e0", borderWidth: 1,
        textStyle: { color: "#1f2937", fontSize: 12 },
        formatter: function (ps) {
          if (!ps || !ps.length) return "";
          var r = rows[ps[0].dataIndex] || {};
          var m = obj(D.code_meta)[r.code] || {};
          var s = "<b>" + esc(r.code) + "</b> " + esc(m.name || "") + "<br>" +
                  "分系统：" + esc(m.apid || "-") + "<br>";
          ps.forEach(function (x) {
            if (num(x.value) > 0) s += x.marker + x.seriesName + "：<b>" + fmt(x.value) + "</b><br>";
          });
          s += "合计：<b>" + fmt(r.n) + "</b> 个事件（该代号参与）";
          if (total) s += " · 占 " + (num(r.n) / total * 100).toFixed(1) + "%";
          return s;
        }
      },
      legend: legendCfg(defs.map(function (d) { return d.n; })),
      toolbox: TOOLBOX,
      grid: { left: gLeft, right: 40, top: 40, bottom: 62 },
      xAxis: {
        type: "value", name: "事件数", minInterval: 1,
        nameLocation: "middle", nameGap: 30,
        nameTextStyle: { color: "#6b7280", fontSize: 11 },
        axisLine: { lineStyle: { color: "#c8d0e0" } },
        axisLabel: { color: "#5b6577", fontSize: 11 },
        splitLine: { lineStyle: { color: "#eef1f6" } }
      },
      yAxis: {
        type: "category", data: yData, inverse: true,
        axisLine: { lineStyle: { color: "#c8d0e0" } }, axisTick: { show: false },
        axisLabel: { color: "#4f5a6e", fontSize: 10.5, width: gLeft - 14, overflow: "truncate" },
        splitLine: { show: false }
      },
      dataZoom: [{
        type: "inside", yAxisIndex: 0, filterMode: "none",
        zoomOnMouseWheel: true, moveOnMouseMove: true
      }],
      series: defs.map(function (d, i) {
        return {
          name: d.n, type: "bar", stack: d.stack, barMaxWidth: 22,
          itemStyle: { color: d.c, borderRadius: i === defs.length - 1 ? [0, 3, 3, 0] : 0 },
          data: d.d, emphasis: { focus: "series" }
        };
      })
    }, true);
  }


  /* ---- 视图二：数值型遥测曲线 ----
   * numericCodes 由 collectMeta() 统一收集，代号复用统一选择器 selCurveCode。 */
  function renderNumericView() {
    var code = selCurveCode;
    var m = obj(D.code_meta)[code] || {};
    var ch0 = mk("chartCurve");
    if (ch0) ch0.off("click");
    setTitle("curveTitle", code + " · " + (m.name || "") + " 数值型遥测曲线");
    if (!numericCodes.length) {
      renderEmpty("chartCurve", "本周期暂无数值型遥测参数（全部代号均为枚举 / 状态型）");
      return;
    }
    var pts = [];
    arr(D.windows).forEach(function (w) {
      var c = null;
      arr(w.codes).forEach(function (x) { if (x.code === code && x.num) c = x; });
      if (!c) return;
      var t0 = isNum(w.t0) ? w.t0 : w.et;
      if (!isNum(t0)) return;
      var vals = arr(c.vals), n = vals.length;
      var span = (isNum(w.t1) && isNum(w.t0)) ? (w.t1 - w.t0) : (n - 1) * 1200;
      vals.forEach(function (v, i) {
        // 缺失 / 类型异常（含布尔，Number(true)=1 会污染曲线）→ 跳过该点
        if (v === null || v === undefined || typeof v === "boolean" || !isFinite(Number(v))) return;
        pts.push([t0 + (n > 1 ? span * i / (n - 1) : 0), Number(v)]);
      });
    });
    if (!pts.length) { renderEmpty("chartCurve", "代号 " + code + " 本周期暂无有效数值采样点"); return; }
    pts.sort(function (a, b) { return a[0] - b[0]; });
    var vals = pts.map(function (p) { return p[1]; });
    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    mk("chartCurve").setOption({
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross", label: { backgroundColor: "#1d4ed8" } },
        backgroundColor: "rgba(255,255,255,.97)", borderColor: "#c8d0e0", borderWidth: 1,
        textStyle: { color: "#1f2937", fontSize: 12 },
        formatter: function (ps) {
          if (!ps.length) return "";
          return "<b>" + fmtTime(ps[0].value[0]) + "</b><br>" + esc(code) + "：<b>" + ps[0].value[1] + "</b>";
        }
      },
      toolbox: TOOLBOX,
      grid: BASE_GRID,
      xAxis: AXIS_TIME,
      yAxis: {
        type: "value", name: (m.name || code), scale: true,
        nameTextStyle: { color: "#6b7280", fontSize: 11 },
        axisLabel: { color: "#5b6577", fontSize: 11 },
        splitLine: { lineStyle: { color: "#eef1f6" } }
      },
      dataZoom: zoomCfg(),
      series: [{
        name: code, type: "line", showSymbol: false, symbol: "circle", symbolSize: 4,
        sampling: "lttb", smooth: 0.1,
        lineStyle: { width: 1.2, color: C.primary },
        itemStyle: { color: C.primary },
        data: pts,
        markLine: {
          silent: true, symbol: "none",
          label: { fontSize: 10, color: "#6b7280", formatter: function (p) { return p.name; } },
          lineStyle: { color: "#f59e0b", type: "dashed", width: 1 },
          data: [{ name: "最大 " + mx.toFixed(3), yAxis: mx }, { name: "最小 " + mn.toFixed(3), yAxis: mn }]
        }
      }]
    }, true);
  }

  /* ================= 代号明细表 ================= */
  var sortKey = "total", sortAsc = false;
  function renderCodes() {
    var t = el("tblCodes");
    if (!t) return;
    var src = arr(D.codes);
    if (!src.length) {
      t.innerHTML = '<tbody><tr><td><div class="empty">本周期暂无代号级异常记录</div></td></tr></tbody>';
      return;
    }
    // 活跃跨度 = 末次 − 首次，统一算成毫秒后参与排序，避免按「3.2 h」这类字符串比较失真
    function spanMs(c) {
      if (!c || !c.first_t || !c.last_t) return NaN;
      var a = new Date(String(c.first_t).replace(" ", "T")).getTime();
      var b = new Date(String(c.last_t).replace(" ", "T")).getTime();
      return (isFinite(a) && isFinite(b) && b >= a) ? (b - a) : NaN;
    }
    function spanText(c) {
      var ms = spanMs(c);
      if (!isFinite(ms)) return "-";
      var h = ms / 3.6e6;
      return h >= 1 ? h.toFixed(1) + " h" : Math.round(h * 60) + " min";
    }
    var rows = src.slice().map(function (c) {
      var o = {};
      for (var k in c) { if (Object.prototype.hasOwnProperty.call(c, k)) o[k] = c[k]; }
      o.span_ms = spanMs(c);
      return o;
    });
    // 排序仍按数据字段取值：隐藏列（合计等）同样可参与排序，只是不显示
    var sk = (sortKey === "span") ? "span_ms" : sortKey;
    rows.sort(function (a, b) {
      var va = a[sk], vb = b[sk];
      if (typeof va === "string" || typeof vb === "string") {
        return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      }
      return sortAsc ? num(va) - num(vb) : num(vb) - num(va);
    });
    /* 列配置：hide=按配置隐藏（数据仍完整渲染、仅视觉隐藏，不删除） · hl=高亮列 · num=右对齐数字列 */
    var cols = [
      { k: "code", n: "代号" }, { k: "name", n: "参数名称" }, { k: "apid", n: "分系统" },
      { k: "confirmed", n: "异常确认", hide: true, num: true },
      { k: "repeated", n: "异常重复", hide: true, num: true },
      { k: "failed", n: "校验未通过", hide: true, num: true },
      { k: "total", n: "合计", hide: true, num: true },
      { k: "span", n: "活跃跨度", hl: true, num: true },
      { k: "first_t", n: "首次", hl: true }, { k: "last_t", n: "末次", hl: true },
      // AI 生成的「变化规律与趋势」描述列：文本长，可换行、超长省略、悬浮看全文
      { k: "trend", n: "变化规律与趋势（AI）", wide: true }
    ];
    // 名称 / 分系统来自字典表、不参与排序：统一标为 nosort，避免给出错误的「可点击」暗示
    // 趋势列为 AI 文本，排序无语义，同样标为 nosort
    var NOSORT = { name: 1, apid: 1, trend: 1 };
    function clsOf(c, tag) {
      var cls = [];
      if (c.num) cls.push("num");
      if (c.hl) cls.push("cell-hl");
      if (c.wide) cls.push("td-trend");
      if (c.hide) cls.push("col-off");           // 隐藏列：display:none，DOM 与数据保留
      cls.push(NOSORT[c.k] ? "nosort" : "sortable");
      return cls.join(" ");
    }
    var visible = cols.filter(function (c) { return !c.hide; }).length;
    t.innerHTML =
      "<thead><tr>" + cols.map(function (c) {
        var ar = c.k === sortKey ? (sortAsc ? '<span class="ar">▲</span>' : '<span class="ar">▼</span>') : "";
        var aria = c.k === sortKey ? (sortAsc ? "ascending" : "descending") : "none";
        return '<th data-k="' + c.k + '" class="' + clsOf(c) + '"' +
               (NOSORT[c.k] ? "" : ' tabindex="0" role="button" aria-sort="' + aria + '"') +
               (c.hide ? ' aria-hidden="true"' : "") +
               ">" + c.n + ar + "</th>";
      }).join("") + "</tr></thead><tbody>" +
      rows.map(function (c) {
        var m = obj(D.code_meta)[c.code] || {};
        // 名称 / 分系统可能较长：截断 + title 悬浮查看全量
        var nm = m.name || "-", ap = m.apid || "-";
        var f1 = c.first_t ? String(c.first_t) : "", f2 = c.last_t ? String(c.last_t) : "";
        // AI 趋势描述来自分析数据的 code_trends；缺失时给中性占位，不臆造内容
        var trend = obj(A.code_trends)[c.code] || "—";
        return "<tr>" +
          '<td class="code" data-code="' + esc(c.code) + '" tabindex="0" title="查看 ' +
            esc(c.code) + ' 的曲线">' + esc(c.code) + "</td>" +
          '<td class="ellip" title="' + esc(nm) + '">' + esc(nm) + "</td>" +
          '<td class="ellip" title="' + esc(ap) + '">' + esc(ap) + "</td>" +
          '<td class="num col-off">' + (num(c.confirmed) ? '<span class="tag confirmed">' + fmt(c.confirmed) + "</span>" : "-") + "</td>" +
          '<td class="num col-off">' + (num(c.repeated) ? '<span class="tag repeat">' + fmt(c.repeated) + "</span>" : "-") + "</td>" +
          '<td class="num col-off">' + (num(c.failed) ? '<span class="tag failed">' + fmt(c.failed) + "</span>" : "-") + "</td>" +
          '<td class="num col-off"><b>' + fmt(c.total) + "</b></td>" +
          // 高亮列：活跃跨度 / 首次 / 末次 —— 用轻量标签（chip）突出数值，不做整列色带
          '<td class="num cell-hl">' + chip(spanText(c)) + "</td>" +
          '<td class="cell-hl" title="' + esc(f1) + '">' + chip(f1 ? f1.slice(5, 16) : "-") + "</td>" +
          '<td class="cell-hl" title="' + esc(f2) + '">' + chip(f2 ? f2.slice(5, 16) : "-") + "</td>" +
          // AI 趋势描述：多行截断 + title 全文
          '<td class="td-trend" title="' + esc(trend) + '"><span class="tw">' + esc(trend) + "</span></td></tr>";
      }).join("") + "</tbody>";
    // 空态 / 异常行的 colspan 需与实际可见列数一致
    t.setAttribute("data-cols", String(visible));

    function sortBy(th) {
      var k = th.getAttribute("data-k");
      if (!k || NOSORT[k]) return;              // 名称 / 分系统不支持排序
      if (k === sortKey) sortAsc = !sortAsc; else { sortKey = k; sortAsc = false; }
      safe("codes", renderCodes);
    }
    Array.prototype.forEach.call(t.querySelectorAll("th"), function (th) {
      th.addEventListener("click", function () { sortBy(th); });
      // 键盘可达：表头聚焦后回车 / 空格触发排序（与鼠标点击行为一致）
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortBy(th); }
      });
    });
    Array.prototype.forEach.call(t.querySelectorAll("td.code"), function (td) {
      td.addEventListener("click", function () { jumpToCode(td.getAttribute("data-code")); });
      td.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); jumpToCode(td.getAttribute("data-code")); }
      });
    });
  }

  /* 代号 → 曲线查看联动：同步代号选择器，并挑一个该代号有数据的视图 */
  function jumpToCode(code) {
    if (!code || !el("selCurve")) return;
    setSelect("selCurve", code);
    selCurveCode = code;
    var pref = obj(obj(D.series).codes)[code] ? "series"
             : (numericCodes.indexOf(code) >= 0 ? "numeric" : "series");
    var cand = [pref];
    ["series", "numeric"].forEach(function (v) {
      if (cand.indexOf(v) < 0) cand.push(v);
    });
    var v = pref;
    for (var i = 0; i < cand.length; i++) {
      var tab = document.querySelector('#curveTabs .tab[data-v="' + cand[i] + '"]');
      if (tab && !tab.classList.contains("off")) { v = cand[i]; break; }  // 跳过本周期无数据的视图
    }
    setView(v);
    var target = el("chartCurve");
    if (target && target.clientHeight > 0) target.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /* ================= LLM 分析渲染 ================= */
  function renderAnalysis() {
    var box = el("llmBody");
    if (!box) return;
    var meta = obj(D.meta);
    // 空数据 + 无分析内容 → 明确提示，而不是空白区块
    if (!A || !arr(A.sections).length) {
      box.innerHTML = '<div class="empty">' +
        (meta.empty ? "本周期无遥测数据，暂无智能分析结论" : "本周期暂无智能分析结论") + "</div>";
      var m0 = el("llmMeta");
      if (m0) m0.textContent = "未生成分析内容";   // 避免右上角长期显示占位符「-」
      return;
    }
    el("llmMeta").textContent = (A.model || "LLM 分析") + " · 生成于 " + (A.generated_at || meta.generated_at || "-");

    // 注意：分析正文（summary/body/paragraphs/bullets）由生成器产出，允许 <b>/<code> 等标记；
    // 仅标题类纯文本字段做转义，避免结构被注入破坏。
    var html = "";
    var h = obj(A.health);
    if (h.score !== undefined) {
      html += '<div class="health ' + esc(h.level || "warn") + '">' +
        '<div class="score">' + esc(h.score) + "</div>" +
        '<div class="txt"><div class="lv">' + esc(h.level_text || "") + "</div>" + (h.summary || "") + "</div></div>";
    }
    if (arr(A.findings).length) {
      html += "<h4>关键发现</h4><div class=\"findings\">" + arr(A.findings).map(function (f) {
        var sev = ({ high: "高", mid: "中", low: "低" })[f.severity] || "中";
        return '<div class="finding"><div class="f-hd">' +
          '<span class="sev ' + esc(f.severity || "mid") + '">' + sev + "</span>" +
          '<span class="t">' + esc(f.title || "") + "</span></div>" +
          '<div class="f-bd">' + (f.body || "") + "</div></div>";
      }).join("") + "</div>";
    }
    /* 「AI 详细分析」折叠模块：一、二、三…各章节统一收进一个默认折叠的面板，
     * 压缩首屏长度；入口区保留「AI 徽标 + 标题 + 章节数 + 章节标题摘要」，
     * 折叠状态下依然可辨识模块内容。章节本身也可单独收起（默认展开）。 */
    var secs = arr(A.sections);
    if (secs.length) {
      var titles = secs.map(function (s) { return s.title || ""; }).filter(Boolean).join(" · ");
      html += '<div class="ai-mod" id="aiMod">' +
        '<button type="button" class="ai-hd" id="aiHd" aria-expanded="false" aria-controls="aiBody" ' +
          'title="展开 / 收起 AI 详细分析">' +
          '<span class="ai-ico">AI</span>' +
          '<span class="ai-ttl">AI 详细分析</span>' +
          '<span class="ai-cnt">' + secs.length + " 章</span>" +
          '<span class="ai-sum">' + esc(titles) + "</span>" +
          '<span class="ai-act" aria-hidden="true"></span>' +
          '<span class="ai-caret" aria-hidden="true"></span>' +
        "</button>" +
        '<div class="ai-bd" id="aiBody">' + secs.map(sectionBlock).join("") + "</div>" +
        "</div>";
    }
    box.innerHTML = html;
    // 事件必须在 innerHTML 写入后绑定；两层折叠互不干扰（模块级 + 章节级）
    bindFold(el("aiMod"), el("aiHd"));
    Array.prototype.forEach.call(box.querySelectorAll(".ai-sec"), function (sec) {
      bindFold(sec, sec.querySelector(".ai-sec-hd"));
    });
  }

  /* 章节摘要：取首段 / 首条要点去标签后截断，用于折叠入口的一行摘要 */
  function briefOf(s) {
    var raw = arr(s.paragraphs)[0] || arr(s.bullets)[0] || "";
    var txt = String(raw).replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
    return txt.length > 44 ? txt.slice(0, 44) + "…" : txt;
  }
  /* 单个章节块：高亮样式 + 可独立折叠的标题入口 */
  function sectionBlock(s, i) {
    var id = "aiSec" + i;
    var brief = briefOf(s);
    return '<div class="ai-sec open" id="' + id + '">' +
      '<h4 class="ai-sec-hd" id="' + id + 'Hd" role="button" tabindex="0" aria-expanded="true" ' +
        'aria-controls="' + id + 'Bd" title="展开 / 收起本章节">' +
        '<span class="t">' + esc(s.title || ("章节 " + (i + 1))) + "</span>" +
        // 摘要仅在收起状态显示（展开后与首段内容重复）
        (brief ? '<span class="sm">' + esc(brief) + "</span>" : "") +
        '<span class="caret" aria-hidden="true"></span>' +
      "</h4>" +
      '<div class="ai-sec-bd" id="' + id + 'Bd">' +
        arr(s.paragraphs).map(function (p) { return "<p>" + p + "</p>"; }).join("") +
        (arr(s.bullets).length
          ? "<ul>" + arr(s.bullets).map(function (b) { return "<li>" + b + "</li>"; }).join("") + "</ul>"
          : "") +
      "</div></div>";
  }

  /* ================= 启动（置于末尾，确保所有状态变量已初始化） ================= */
  try { boot(); } catch (e) { fatal(e); }
})();
