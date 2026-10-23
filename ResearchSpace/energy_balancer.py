#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三体星(SCS-01-01) 整星能源平衡定量评估框架 (v2.2)
基于 ClickHouse chdb_3body.telemetry_raw_01 真实遥测。

v2.1 口径变更（用户决策 2026-10-22，依据 S3R 拓扑实证）：
  发电功率改用**母线电压 × 母线电流**（TMD016 × TMD017），不再单独建模分流：
    1) 电源拓扑为 S3R（顺序开关分流调节，遥测大表/知识 YAML 证实：太阳阵
       直接耦合母线，S3R 分流级在 PCU 内部消耗富余、不流经母线测点）；
    2) TMD017 是"太阳阵经 S3R 送入母线的单向电流"测点——实证：地影时
       TMD017≈−0.1 A（≈0）而负载电流 TMD018≈9 A，日照时 TMD017−TMD018
       中位 +7.1 A（=充电电流）；
    3) 故 P_solar := V_bus × I_bus 即"实际送达母线的发电"，分流已被 S3R
       在测点上游扣除——富余全部为充电，无需 SOC 判定拆分；
    4) 分流不再是模型量，改为**诊断量** P_shunt_diag = (I_solar_panel −
       I_bus) × V_bus（帆板级 TMD009 与母线级 TMD017 的差额，S3R 分流+
       PCU 变换损耗的等效值），用于独立交叉校验；
    5) 日照/地影判定仍用帆板级电流 TMD009（光照工况判据）——不可改用
       I_bus：满充分流时 I_bus 很小但仍是日照。

v2.2 口径变更（用户决策 2026-10-23）：
  V-SOC 主曲线由通用锂电假设曲线切换为**三体星蓄电池设计放电表**
  （10A 恒流放电，22.82–29.31V ↔ 10%–100% SOC，65Ah 设计构型）——
  该表经用户确认为 SCS-01-01 设计值；切换后 SOC/DoD/健康度结论按
  新曲线刷新（敏感性实测见 13 号报告附录 A.7）。

其余 v2 特性保留：逐样本电压锚定 SOC、稳健百分位指标、分工况能量收支、
恒等式/独立残差分离（见 energy() docstring）。

配置外置（单一事实源，config/ 目录，JSON 无需额外依赖）：
  - design_params.json    设计/模型参数（容量、额定功率、阈值、dt 等）
  - v_soc_curve.json      电压→SOC 校准曲线（三体星设计表·已激活）
  - telemetry_params.json 遥测代号 ↔ 逻辑名映射
文件缺失时回退到模块内 DEFAULT_*（与配置文件保持一致），并在 stderr 提示。
"""
import json
import sys
from pathlib import Path

import pandas as pd
import numpy as np

CONFIG_DIR = Path(__file__).resolve().parent / 'config'

# ---- 内置默认值（与 config/*.json 保持一致；配置文件优先） --------------------
DEFAULT_DESIGN = {
    'battery_capacity_Ah': 65.0,
    'battery_capacity_min_Ah': 60.3,  # 设计下限（放电深度限制要求）；P4 容量/SOH 低于此值即违规
    'solar_array_Pm': 900.0,
    'bus_nominal_V': 28.0,
    'eta_charge': 0.97,
    'eta_discharge': 0.97,
    'soc_floor': 0.05,         # SOC 下限截断
    'soc_unknown': 0.9,        # V_bat 缺测时的 SOC 占位
    'sun_I_threshold_A': 1.0,  # 日照判据电流阈值（对帆板级 TMD009）
    'dt_seconds': 60.0,        # 名义采样间隔（分钟聚合数据）；能量积分按此折算
    # 注：v2.1 起 soc_high（分流阈值）已移除——分流由 S3R 测点隐含扣除，
    # 不再需要 SOC 判定（见模块 docstring 第 3 条）。
}
# (V_bat, SOC) 三体星设计放电表（10A 恒流放电，7s13p/65Ah 构型，用户 2026-10-23 确认激活）
DEFAULT_V_SOC_TABLE = np.array([
    (22.82, 0.10), (23.90, 0.20), (24.49, 0.30), (25.01, 0.40), (25.65, 0.50),
    (26.32, 0.60), (26.90, 0.70), (27.60, 0.80), (28.23, 0.90), (29.31, 1.0),
])
DEFAULT_COLS = dict(
    solar_I='TMD009', bus_V='TMD016', bus_I='TMD017',
    load_I='TMD018', bat_V='TMD019',
)


def _load_json(name, default):
    """读取 config/ 下 JSON；缺失时回退默认值并提示（不静默）。"""
    path = CONFIG_DIR / name
    if not path.is_file():
        print(f"[energy_balancer] 未找到 {path}，使用内置默认参数", file=sys.stderr)
        return default
    with path.open(encoding='utf-8') as f:
        return json.load(f)


def _load_design():
    cfg = _load_json('design_params.json', None)
    if cfg is None:
        return dict(DEFAULT_DESIGN)
    d = dict(DEFAULT_DESIGN)
    d.update({k: v for k, v in cfg.items() if not k.startswith('_')})
    return d


def _load_v_soc_table():
    cfg = _load_json('v_soc_curve.json', None)
    table = DEFAULT_V_SOC_TABLE if cfg is None else np.array(cfg['points'], dtype=float)
    # np.interp 要求采样横坐标单调非降，否则结果静默错误——配置即代码，须校验
    if len(table) < 2 or not np.all(np.diff(table[:, 0]) > 0):
        raise ValueError("v_soc_curve 电压点必须严格单调递增")
    if not np.all(np.diff(table[:, 1]) >= 0):
        raise ValueError("v_soc_curve SOC 点必须单调非降")
    return table


def _load_cols():
    cfg = _load_json('telemetry_params.json', None)
    if cfg is None:
        return dict(DEFAULT_COLS)
    cols = dict(DEFAULT_COLS)
    cols.update({k: v['code'] for k, v in cfg.get('columns', {}).items()})
    return cols


DESIGN = _load_design()
V_SOC_TABLE = _load_v_soc_table()
TELEMETRY_COLS = _load_cols()


def _v_soc_map(v):
    """电压→SOC 分段线性插值（饱和截断）。"""
    vs = V_SOC_TABLE[:, 0]; socs = V_SOC_TABLE[:, 1]
    v = np.clip(v, vs[0], vs[-1])
    return float(np.interp(v, vs, socs))


class EnergyBalanceEvaluator:
    def __init__(self, design=None, cols=None):
        d = dict(DESIGN); d.update(design or {})
        self.design = d
        c = dict(TELEMETRY_COLS); c.update(cols or {})
        self.cols = c
        self.wide = None
        self.df = None
        self.metrics = None
        self.health = None

    # ------------------------------------------------------------------ 数据层
    def load(self, wide):
        """加载宽表（index 为时间、列为遥测代号），并校验必需列齐全。"""
        missing = [f"{logical}={code}" for logical, code in self.cols.items()
                   if code not in wide.columns]
        if missing:
            raise ValueError(
                "宽表缺少必需遥测列: " + ", ".join(missing) +
                f"；实际列: {list(wide.columns)[:20]}...")
        self.wide = wide.sort_index()

    def _clip(self, s, lo, hi):
        return pd.to_numeric(s, errors='coerce').clip(lower=lo, upper=hi)

    # ---------------------------------------------------------------- 物理量层
    def compute_power(self):
        """计算瞬时功率物理量（全向量化）。

        口径（v2.1，S3R 拓扑）：
          P_solar     = V_bus × I_bus(TMD017)   —— 实际送达母线的发电；
                        分流已在 S3R 测点上游扣除，故富余即充电
          P_shunt_diag= (I_solar − I_bus) × V_bus —— 诊断量：帆板级(TMD009)与
                        母线级(TMD017)的功率差额 ≈ S3R 分流+PCU 损耗
          P_load      = V_bus × I_load(TMD018)   —— 主负载
          P_excess    = P_solar − P_load          —— + 富余(充电) / − 缺口(放电)
          I_solar(TMD009) 保留：日照/地影工况判据（光照本身，不受分流影响）
        """
        w = self.wide
        Vb = self._clip(w[self.cols['bus_V']], 10, 45.0)
        d = pd.DataFrame(index=w.index)
        d['V_bus'] = Vb
        d['V_bat'] = self._clip(w[self.cols['bat_V']], 10, 45.0)
        d['I_solar'] = self._clip(w[self.cols['solar_I']], 0, 100)   # 帆板级总电流（工况判据）
        d['I_bus'] = self._clip(w[self.cols['bus_I']], -5, 100)      # S3R 送入母线的太阳阵电流
        d['P_solar'] = d['I_bus'] * d['V_bus']          # v2.1 发电口径：母线电压×母线电流
        d['P_shunt_diag'] = (d['I_solar'] - d['I_bus']) * d['V_bus']  # 诊断：被 S3R 分流/PCU 损耗
        d['I_load'] = self._clip(w[self.cols['load_I']], 0, 100)     # 负载电流物理非负
        d['P_load'] = d['I_load'] * d['V_bus']          # 主负载（实际消耗）
        d['P_excess'] = d['P_solar'] - d['P_load']      # + 富余 / - 缺口
        return d

    # ---------------------------------------------------------------- 工况层
    def classify_phase(self, d):
        """日照/地影判定：I_solar ≥ 阈值 的样本做 5 样本中心滚动**多数表决**
        （布尔均值 ≥ 0.5，抗单点毛刺；非中值滤波——09 号文档旧表述"滚动中值"
        已按实现修正）。        """
        sun = d['I_solar'] >= self.design['sun_I_threshold_A']
        # 在时间缺口（>3×dt_seconds）处先切段，避免跨越数日的样本被同一
        # 5 样本窗口平滑（突发采样下索引相邻 ≠ 时间相邻）。
        dt = self.design['dt_seconds']
        if isinstance(d.index, pd.DatetimeIndex) and len(d) >= 2:
            gaps = d.index.to_series().diff().dt.total_seconds()
            seg_id = (gaps > 3 * dt).fillna(False).cumsum()
            roll = sun.groupby(seg_id.to_numpy()).transform(
                lambda s: s.rolling(5, center=True, min_periods=1).mean())
        else:
            roll = sun.rolling(5, center=True, min_periods=1).mean()
        d['phase'] = np.where(roll >= 0.5, '日照', '地影')
        return d

    # ---------------------------------------------------------------- 储能层
    def estimate_battery(self, d):
        """逐样本电压锚定 SOC + 充放拆分（全向量化，可接连续遥测）。

        v2.1 简化（S3R 拓扑）：分流不再建模——母线电流测点已在 S3R 上游
        扣除分流，故：
          富余 P_excess ≥ 0 → 全部计充电（不管 SOC 是否已满：满充后 S3R
                             会自动分流，母线电流随之下降，充电自然趋 0）；
          缺口 P_excess < 0 → 全部计放电。

        缺测处理（重要）：
          - V_bat 缺测：SOC 落 soc_unknown 占位，但从 SOC 派生的所有量
            （charge/disch）清零，不臆造；
          - P_excess 缺测（上游 V_bus/I_load/I_bus 任一缺测）：charge/disch
            也清零，避免把 NaN 传进能量统计的 skipna 求和形成"静默 0"假象。
        """
        vb = d['V_bat'].to_numpy(dtype=float)
        pex = d['P_excess'].to_numpy(dtype=float)
        n = len(d)
        vs = V_SOC_TABLE[:, 0]; socs = V_SOC_TABLE[:, 1]
        known = ~np.isnan(vb)
        soc = np.full(n, self.design['soc_unknown'])
        soc[known] = np.interp(np.clip(vb[known], vs[0], vs[-1]), vs, socs)
        soc = np.clip(soc, self.design['soc_floor'], 1.0)

        surplus = pex >= 0
        p_charge = np.where(surplus, pex, 0.0)
        p_disch = np.where(~surplus, pex, 0.0)
        # 缺测样本不臆造充放；P_excess 缺测一并清零（否则 NaN 渗入 P_disch）
        invalid = ~known | np.isnan(pex)
        p_charge[invalid] = 0.0; p_disch[invalid] = 0.0

        d['SOC'] = soc * 100.0
        d['DoD'] = (1 - soc) * 100.0
        # 显式记录 SOC 是否来自实测电压锚定（统计层据此剔除占位样本）
        d['SOC_observed'] = known
        d['P_charge'] = p_charge
        d['P_disch'] = p_disch
        return d

    # ---------------------------------------------------------------- 统计层
    def phase_summary(self, d, phase):
        sub = d[d['phase'] == phase]
        if len(sub) == 0:
            return {}
        q = lambda s: pd.to_numeric(sub[s], errors='coerce').dropna()
        out = {'samples': int(len(sub))}
        for key in ['P_solar', 'P_load', 'P_excess', 'P_shunt_diag', 'I_load', 'I_bus']:
            s = q(key)
            if len(s):
                out[key + '_mean'] = float(s.mean())
                out[key + '_p50'] = float(s.median())
        for key in ['V_bus', 'V_bat']:
            s = q(key)
            if len(s):
                out[key + '_mean'] = float(s.mean())
                out[key + '_p5'] = float(s.quantile(.05))
                out[key + '_p95'] = float(s.quantile(.95))
        # SOC 仅统计实测（V_bat 锚定）样本，剔除 soc_unknown 占位
        if 'SOC' in sub:
            if 'SOC_observed' in sub.columns:
                obs = sub[sub['SOC_observed']]['SOC']
            else:
                obs = sub['SOC']
            if len(obs):
                out['soc_p5'] = float(obs.quantile(.05))
                out['soc_p50'] = float(obs.median())
                out['soc_min'] = float(obs.min())
                out['soc_n_observed'] = int(len(obs))
        return out

    def energy(self, d, phase):
        """工况能量收支（kWh）。每样本按 design['dt_seconds'] 名义间隔折算，
        只计有观测的样本、不对缺口外推（与 gen_charts 同原则）。

        两个量的区别（重要，v2.1 口径）：
          e_residual     = e_solar - e_load - e_batnet
                           恒等于 0（因 P_charge+P_disch ≡ P_excess 且
                           P_excess ≡ P_solar−P_load 由构造保证），
                           **不具备模型校验能力**，仅保留作记账恒等式；
          e_shunt_diag   = Σ[(I_solar−I_bus)×V_bus]dt，帆板级(TMD009)与
                           母线级(TMD017)两个**独立测点**的功率差，物理上
                           应 ≥0（S3R 分流 + PCU 变换损耗的等效值），
                           是 v2.1 下有判别力的交叉校验量。持续为负说明
                           电流测点间存在系统偏差（需查测点拓扑）。
        """
        sub = d[d['phase'] == phase]
        if len(sub) == 0:
            return dict(e_solar=0, e_load=0, e_batnet=0, e_shunt_diag=0,
                        e_charge=0, e_disch=0, e_residual=0, e_shunt_diag_kWh=0,
                        e_solar_panel_kWh=0)
        def esum(col):
            return float(pd.to_numeric(sub[col], errors='coerce').sum())
        dt = self.design['dt_seconds'] / 3600.0 / 1000.0   # kWh per sample
        e_solar = esum('P_solar')*dt; e_load = esum('P_load')*dt
        e_charge = esum('P_charge')*dt; e_disch = esum('P_disch')*dt
        e_shunt_diag = esum('P_shunt_diag')*dt
        # 帆板级发电（诊断口径）：I_solar × V_bus
        e_solar_panel = float((pd.to_numeric(sub['I_solar'], errors='coerce') *
                               pd.to_numeric(sub['V_bus'], errors='coerce')).sum())*dt
        e_batnet = e_charge + e_disch          # 电池净获电（+充/-放）
        e_residual = e_solar - e_load - e_batnet   # 恒等式（≡0）
        return dict(e_solar_kWh=e_solar, e_load_kWh=e_load,
                    e_batnet_kWh=e_batnet, e_shunt_diag_kWh=e_shunt_diag,
                    e_charge_kWh=e_charge, e_disch_kWh=e_disch,
                    e_residual_kWh=e_residual, e_solar_panel_kWh=e_solar_panel)

    # ---------------------------------------------------------------- 汇总层
    def _gap_diagnostics(self, d):
        """采样间隔与缺口诊断（突发式遥测的质量信号，不改变能量口径）。"""
        idx = d.index
        if not isinstance(idx, pd.DatetimeIndex) or len(idx) < 2:
            return None, None
        gaps = np.diff(idx.values).astype('timedelta64[s]').astype(float)
        dt = self.design['dt_seconds']
        return float(np.nanmax(gaps)), int((gaps > 3 * dt).sum())

    def run_all(self):
        d = self.compute_power()
        d = self.classify_phase(d)
        d = self.estimate_battery(d)
        res = dict(df=d)
        res['sun'] = self.phase_summary(d, '日照')
        res['ecl'] = self.phase_summary(d, '地影')
        res['e_sun'] = self.energy(d, '日照')
        res['e_ecl'] = self.energy(d, '地影')
        self.df = d
        self.phase = res
        # 综合指标
        sun, ecl = res['sun'], res['ecl']
        margin = None; dod_p95 = None; soc_p5 = None; soc_min = None; vbus_stab = None
        if sun:
            pl = sun.get('P_load_mean')
            if pl and pl > 0:
                margin = (sun.get('P_solar_mean', 0) - pl) / pl * 100
        if 'SOC' in d.columns:
            # 仅用实测锚定样本统计，避免 soc_unknown 占位值污染分位数
            observed = d[d['SOC_observed']] if 'SOC_observed' in d.columns else d
            if len(observed):
                dod_p95 = float(observed['DoD'].quantile(.95))  # DoD already in %
                soc_p5 = float(observed['SOC'].quantile(.05))
                soc_min = float(observed['SOC'].min())
        vb = pd.to_numeric(d['V_bus'], errors='coerce').dropna()
        if len(vb) >= 2:  # 至少两个有效样本才能计算 p95-p5 展宽
            vbus_stab = (vb.quantile(.95) - vb.quantile(.05)) / vb.median() * 100
        gap_max, n_gaps = self._gap_diagnostics(d)
        self.metrics = dict(
            n_samples=int(len(d)),
            time_span_days=float((d.index.max()-d.index.min()).total_seconds()/86400),
            sun_ratio=float((d['phase']=='日照').mean()),
            sample_dt_seconds=self.design['dt_seconds'],
            gap_max_seconds=gap_max,
            n_gaps_gt_3x_dt=n_gaps,
            margin_pct=round(margin,1) if margin is not None else None,
            dod_p95_pct=round(dod_p95,1) if dod_p95 is not None else None,
            soc_p5_pct=round(soc_p5,1) if soc_p5 is not None else None,
            soc_absolute_min_pct=round(soc_min,1) if soc_min is not None else None,
            vbus_stability_pct=round(vbus_stab,2) if vbus_stab is not None else None,
        )
        self.health = self._health()
        return d, self.metrics, self.health, res

    def _score(self, val, warn, crit, reverse=False):
        if val is None:
            return None
        if reverse:
            return 100.0 if val <= warn else (40.0 if val >= crit else 70.0)
        return 100.0 if val >= warn else (40.0 if val <= crit else 70.0)

    def _health(self):
        m = self.metrics
        th = self.design.get('health_thresholds', {})
        w = self.design.get('health_weights',
                           {'power_balance': .35, 'dod': .25,
                            'soc_low': .20, 'bus_stability': .20})
        ss = dict(
            power_balance=self._score(m.get('margin_pct'), *th.get('margin_pct', (20, 0))),
            dod=self._score(m.get('dod_p95_pct'), *th.get('dod_p95_pct', (30, 50)), reverse=True),
            soc_low=self._score(m.get('soc_p5_pct'), *th.get('soc_p5_pct', (50, 30))),
            bus_stability=self._score(m.get('vbus_stability_pct'), *th.get('vbus_stability_pct', (3, 5)), reverse=True),
        )
        # 仅对可得分项归一化加权——分项缺失（缺数据）时不再按 0 分惩罚，
        # 并报告评分覆盖率，便于判断结论的可靠程度。
        avail = [k for k in ss if ss[k] is not None]
        wsum = sum(w[k] for k in avail)
        hs = (sum(ss[k] * w[k] for k in avail) / wsum) if wsum else 0.0
        lvl = ('优秀' if hs >= 90 else '良好' if hs >= 75 else '一般' if hs >= 60 else '较差')
        return dict(hs=hs, level=lvl, subscores=ss,
                    coverage=round(wsum, 3))
