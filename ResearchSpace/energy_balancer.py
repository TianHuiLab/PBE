#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三体星(SCS-01-01) 整星能源平衡定量评估框架 (v2)
基于 ClickHouse chdb_3body.telemetry_raw_01 真实遥测。

v2 改进（依据真实数据诊断）：
  1) 主负载功率改用"负载电流 TMD018"(真实负载)；"母线电流 TMD017"作为
     总线需求口径做交叉校验（见 energy() 的 e_bus_residual，这是唯一
     非恒等式的闭合校验）；
  2) 蓄电池 SOC 采用**逐样本电压锚定**（V_bat → SOC 分段线性映射）。
     注：早期 docstring 所称"电压锚定+地影段安时积分混合估计"未落地，
     自 v2b 起即为纯电压锚定（09 号文档 §5 佐证）；纯安时积分因突发采样
     缺口会累积漂移，已废止（03 号 v1 工具仅留作方法论对照）；
  3) 增加分流(shunt)模型：电压锚定 SOC ≥ soc_high 后，富余发电计为分流，
     不再无限计入充电；
  4) 指标采用稳健百分位(p5-p95)与中位数，抗遥测异常毛刺；
  5) 评估分"日照段 / 地影段"两工况分别给出能量收支与储能状态。

配置外置（单一事实源，config/ 目录，JSON 无需额外依赖）：
  - design_params.json    设计/模型参数（容量、额定功率、阈值、dt 等）
  - v_soc_curve.json      电压→SOC 校准曲线（未标定，见文件内 calibration_status）
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
    'battery_capacity_Ah': 100.0,
    'solar_array_Pm': 900.0,
    'bus_nominal_V': 28.0,
    'eta_charge': 0.97,
    'eta_discharge': 0.97,
    'soc_high': 0.95,          # 分流阈值：SOC>=95% 视为满充，多余能量分流
    'soc_floor': 0.05,         # SOC 下限截断
    'soc_unknown': 0.9,        # V_bat 缺测时的 SOC 占位
    'sun_I_threshold_A': 1.0,  # 日照判据电流阈值
    'dt_seconds': 60.0,        # 名义采样间隔（分钟聚合数据）；能量积分按此折算
}
DEFAULT_V_SOC_TABLE = np.array([  # (V_bat, SOC) 充电后静置参考
    (25.50, 0.05), (26.50, 0.20), (27.20, 0.40), (27.60, 0.55),
    (27.90, 0.70), (28.10, 0.82), (28.25, 0.90), (28.39, 0.97), (28.55, 1.0),
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
        w = self.wide
        Vb = self._clip(w[self.cols['bus_V']], 10, 45.0)
        d = pd.DataFrame(index=w.index)
        d['V_bus'] = Vb
        d['V_bat'] = self._clip(w[self.cols['bat_V']], 10, 45.0)
        d['I_solar'] = self._clip(w[self.cols['solar_I']], 0, 100)
        d['P_solar'] = d['I_solar'] * d['V_bus']
        d['I_bus'] = self._clip(w[self.cols['bus_I']], -5, 100)
        d['P_bus'] = d['I_bus'] * d['V_bus']          # 母线需求口径（交叉校验）
        d['I_load'] = self._clip(w[self.cols['load_I']], 0, 100)  # 负载电流物理非负
        d['P_load'] = d['I_load'] * d['V_bus']         # 主负载（实际消耗）
        d['P_excess'] = d['P_solar'] - d['P_load']     # + 富余 / - 缺口
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
        """逐样本电压锚定 SOC + 分流模型（全向量化，可接连续遥测）。

        分流逻辑：富余 P_excess>0 时，若电压锚定 SOC ≥ soc_high 则计分流，
        否则计充电；缺口 P_excess<0 计放电。

        缺测处理（重要）：
          - V_bat 缺测：SOC 落 soc_unknown 占位，但从 SOC 派生的所有量
            （charge/shunt/disch）清零，不臆造；
          - P_excess 缺测（上游 V_bus/I_load/I_solar 任一缺测）：charge/shunt/disch
            也清零，避免把 NaN 传进能量统计的 skipna 求和形成"静默 0"假象。
        """
        soc_high = self.design['soc_high']
        vb = d['V_bat'].to_numpy(dtype=float)
        pex = d['P_excess'].to_numpy(dtype=float)
        n = len(d)
        vs = V_SOC_TABLE[:, 0]; socs = V_SOC_TABLE[:, 1]
        known = ~np.isnan(vb)
        soc = np.full(n, self.design['soc_unknown'])
        soc[known] = np.interp(np.clip(vb[known], vs[0], vs[-1]), vs, socs)
        soc = np.clip(soc, self.design['soc_floor'], 1.0)

        surplus = pex >= 0
        full = soc >= soc_high
        p_charge = np.where(surplus & ~full, pex, 0.0)
        p_shunt = np.where(surplus & full, pex, 0.0)
        p_disch = np.where(~surplus, pex, 0.0)
        # 缺测样本不臆造充放/分流；P_excess 缺测一并清零（否则 NaN 渗入 P_disch）
        invalid = ~known | np.isnan(pex)
        p_charge[invalid] = 0.0; p_shunt[invalid] = 0.0; p_disch[invalid] = 0.0

        d['SOC'] = soc * 100.0
        d['DoD'] = (1 - soc) * 100.0
        # 显式记录 SOC 是否来自实测电压锚定（统计层据此剔除占位样本）
        d['SOC_observed'] = known
        d['P_charge'] = p_charge
        d['P_shunt'] = p_shunt
        d['P_disch'] = p_disch
        return d

    # ---------------------------------------------------------------- 统计层
    def phase_summary(self, d, phase):
        sub = d[d['phase'] == phase]
        if len(sub) == 0:
            return {}
        q = lambda s: pd.to_numeric(sub[s], errors='coerce').dropna()
        out = {'samples': int(len(sub))}
        for key in ['P_solar', 'P_load', 'P_bus', 'P_excess', 'P_shunt', 'I_load']:
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

        两个残差的区别（重要）：
          e_residual      = e_solar - e_load - e_batnet - e_shunt
                           恒等于 0（因 P_charge+P_disch+P_shunt ≡ P_excess 由
                           构造保证），**不具备模型校验能力**，仅保留作记账恒等式；
          e_bus_residual  = e_bus - e_load - e_charge
                           用 TMD017 母线需求这一**独立遥测通道**校验负载+充电
                           拆分，是唯一有判别力的闭合校验。其绝对值/方向需结合
                           测量点拓扑解释（充电/分流电流是否流经 TMD017 测点），
                           当前与"含充电"叙事存在偏差，见 09 号文档 §6 待查项。
        """
        sub = d[d['phase'] == phase]
        if len(sub) == 0:
            return dict(e_solar=0, e_load=0, e_bus=0, e_batnet=0, e_shunt=0,
                        e_charge=0, e_disch=0, e_residual=0, e_bus_residual=0)
        def esum(col):
            return float(pd.to_numeric(sub[col], errors='coerce').sum())
        dt = self.design['dt_seconds'] / 3600.0 / 1000.0   # kWh per sample
        e_solar = esum('P_solar')*dt; e_load = esum('P_load')*dt
        e_bus = esum('P_bus')*dt
        e_charge = esum('P_charge')*dt; e_disch = esum('P_disch')*dt
        e_shunt = esum('P_shunt')*dt
        e_batnet = e_charge + e_disch          # 电池净获电（+充/-放）
        e_residual = e_solar - e_load - e_batnet - e_shunt   # 恒等式（≡0）
        e_bus_residual = e_bus - e_load - e_charge           # 独立通道校验
        return dict(e_solar_kWh=e_solar, e_load_kWh=e_load, e_bus_kWh=e_bus,
                    e_batnet_kWh=e_batnet, e_shunt_kWh=e_shunt,
                    e_charge_kWh=e_charge, e_disch_kWh=e_disch,
                    e_residual_kWh=e_residual, e_bus_residual_kWh=e_bus_residual)

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
