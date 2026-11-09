#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三体星整星能源平衡定量评估框架 (v2.3)
按卫星配置读取 ClickHouse 宽表遥测；01/02 星使用独立配置和输出目录。

当前设计边界（用户提供供配电方案 5.3.3）：不调节母线 + S³R 顺序开关分流，
蓄电池电压钳位母线；光照期优先供负载，其次充电，最后分流；阴影期由
蓄电池供电，日照短时不足时太阳阵与电池联合供电。PCDU 内充电支路相对
TMD017 电流测点的位置尚未由连接图确认。

2026-11-02 口径更新（用户提供）：TMD017 改释为“太阳阵侧母线电流”——
地影期≈0、光照期母线功率≈六板端原值功率和（诊断）；太阳阵电压遥测
TMD003–008 判为不可用，依赖电压的诊断（六板端原值功率和、帆板端 ΣV×I−TMD017）
不再计入正文口径（相关图表已从清单移除）。光照期太阳阵功率暂定 =
太阳阵侧母线功率 + 蓄电池充电功率（= U_bus×(I017−I018) 正值），该叠加对
充电功率存在重复计数，属暂定口径，详见 framework_documentation 文档
“太阳阵功率(暂定3)”列与边界说明。

保留历史字段名以兼容现有输出：
  P_solar = TMD016 × TMD017，即太阳阵侧母线功率（TMD017 测点折算）；
  P_excess = P_solar − P_load，只是两个测点的代数差额；
  P_charge/P_disch 是该差额的正负拆分，不是独立电池支路测量或充放电判向；
  P_shunt_diag = (TMD009 − TMD017) × TMD016 是跨测点诊断，若充电支路
  位于 TMD017 测点之前，差额可能包含该支路，不能当作 S³R 损耗。
日照/地影优先按配置的状态遥测判定；状态缺测时以 TMD009 回退。

v2.2 口径变更（用户决策 2026-10-23）：
  V-SOC 主曲线由通用锂电假设曲线切换为**三体星蓄电池设计放电表**
  （10A 恒流放电，22.82–29.31V ↔ 10%–100% SOC，65Ah 设计构型）——
  该表经用户确认为 SCS-01-01 设计值；SCS-01-02 尚未单独确认，
  沿用时 SOC/DoD/健康度仅为条件试算（敏感性实测见 13 号报告附录 A.7）。

其余 v2 特性保留：逐样本电压锚定 SOC、稳健百分位指标、分工况能量收支、
记账恒等式与差额诊断分离（见 energy() docstring）。

v2.3：TMK5315 优先判定日照/地影，缺测时由 TMD009 分钟极值回退；SOC 缺测保留
NaN；综合评分只称为观测样本评分，不能推断整个稀疏观测时期。

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
    'sun_I_threshold_A': 1.0,  # TMK5315 缺测时的电流回退阈值（帆板级 TMD009）
    'dt_seconds': 60.0,        # 名义采样间隔（分钟聚合数据）；能量积分按此折算
    'interval_max_gap_seconds': 10.0,  # 原始相邻采样积分的最大间隔
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


def _load_json(name, default, config_dir=None):
    """读取 config/ 下 JSON；缺失时回退默认值并提示（不静默）。"""
    path = Path(CONFIG_DIR if config_dir is None else config_dir) / name
    if not path.is_file():
        print(f"[energy_balancer] 未找到 {path}，使用内置默认参数", file=sys.stderr)
        return default
    with path.open(encoding='utf-8') as f:
        return json.load(f)


def _load_design(config_dir=None):
    cfg = _load_json('design_params.json', None, config_dir)
    if cfg is None:
        return dict(DEFAULT_DESIGN)
    d = dict(DEFAULT_DESIGN)
    d.update({k: v for k, v in cfg.items() if not k.startswith('_')})
    return d


def _load_v_soc_table(config_dir=None):
    cfg = _load_json('v_soc_curve.json', None, config_dir)
    table = DEFAULT_V_SOC_TABLE if cfg is None else np.array(cfg['points'], dtype=float)
    # np.interp 要求采样横坐标单调非降，否则结果静默错误——配置即代码，须校验
    if table.ndim != 2 or table.shape[1] != 2 or len(table) < 2:
        raise ValueError('v_soc_curve points 必须是至少两个 [电压, SOC] 数值对')
    if not np.isfinite(table).all() or np.any((table[:, 1] < 0) | (table[:, 1] > 1)):
        raise ValueError('v_soc_curve 不允许非有限值，SOC 必须在 [0,1]')
    if not np.all(np.diff(table[:, 0]) > 0):
        raise ValueError("v_soc_curve 电压点必须严格单调递增")
    if not np.all(np.diff(table[:, 1]) >= 0):
        raise ValueError("v_soc_curve SOC 点必须单调非降")
    return table


def _load_cols(config_dir=None):
    cfg = _load_json('telemetry_params.json', None, config_dir)
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
    def __init__(self, design=None, cols=None, v_soc_table=None,
                 phase_code=None, phase_labels=('阳照区', '阴影区'),
                 battery_direction=None):
        d = dict(DESIGN); d.update(design or {})
        self.design = d
        c = dict(TELEMETRY_COLS); c.update(cols or {})
        self.cols = c
        self.v_soc_table = V_SOC_TABLE if v_soc_table is None else v_soc_table
        self.phase_code = phase_code
        self.phase_labels = phase_labels
        self.battery_direction = battery_direction or {}
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
        numeric = pd.to_numeric(s, errors='coerce')
        return numeric.where(np.isfinite(numeric)).clip(lower=lo, upper=hi)

    # ---------------------------------------------------------------- 物理量层
    def compute_power(self):
        """计算瞬时功率物理量（全向量化）。

        历史字段名保留，物理解释按待核测点处理：
          P_solar     = V_bus × I_bus(TMD017)   —— 太阳阵侧母线功率
                        （2026-11-02 口径：TMD017=太阳阵侧母线电流，
                        地影≈0、光照≈六板端原值功率和；母线电流已含充电支路）
          P_shunt_diag= (I_solar − I_bus) × V_bus —— 诊断量：帆板级(TMD009)与
                        母线级(TMD017)的功率差额，不能直接量化分流或损耗
          P_load      = V_bus × I_load(TMD018)   —— 主负载
          P_excess    = P_solar − P_load          —— 两测点差额，非电池功率
          I_solar(TMD009) 保留：状态缺测时的工况回退判据及帆板级诊断
        """
        w = self.wide
        Vb = self._clip(w[self.cols['bus_V']], 10, 45.0)
        d = pd.DataFrame(index=w.index)
        d['V_bus'] = Vb
        d['V_bat'] = self._clip(w[self.cols['bat_V']], 10, 45.0)
        d['I_solar'] = self._clip(w[self.cols['solar_I']], 0, 100)   # 帆板级总电流（工况回退判据）
        d['I_bus'] = self._clip(w[self.cols['bus_I']], -5, 100)      # TMD017，充电分支相对位置待核
        d['P_solar'] = d['I_bus'] * d['V_bus']          # 历史字段：TMD017 测点功率折算
        d['P_shunt_diag'] = (d['I_solar'] - d['I_bus']) * d['V_bus']  # 测点边界差诊断，不能直接当作分流或损耗
        d['I_load'] = self._clip(w[self.cols['load_I']], 0, 100)     # 负载电流物理非负
        d['P_load'] = d['I_load'] * d['V_bus']          # 主负载（实际消耗）
        d['P_excess'] = d['P_solar'] - d['P_load']      # 测点代数差额，非电池净功率
        return d

    # ---------------------------------------------------------------- 工况层
    def classify_phase(self, d):
        """光照状态为主判据；缺测时用电流极值回退。

        缺少极值的旧输入才沿用分段 5 样本多数表决，且不能把缺测判成地影。
        """
        threshold = self.design['sun_I_threshold_A']
        solar_code = self.cols['solar_I']
        min_col, max_col = f'{solar_code}__mn', f'{solar_code}__mx'
        if min_col in self.wide and max_col in self.wide:
            low = pd.to_numeric(self.wide[min_col], errors='coerce')
            high = pd.to_numeric(self.wide[max_col], errors='coerce')
            valid = np.isfinite(low) & np.isfinite(high) & (low <= high)
            d['phase'] = np.select(
                [valid & (low >= threshold), valid & (high < threshold)],
                ['日照', '地影'], default='未判定')
            return self._apply_phase_flag(d)
        known = d['I_solar'].notna()
        sun = d['I_solar'] >= threshold
        # 旧输入在时间缺口处切段，避免跨越数日平滑。
        dt = self.design['dt_seconds']
        if isinstance(d.index, pd.DatetimeIndex) and len(d) >= 2:
            gaps = d.index.to_series().diff().dt.total_seconds()
            seg_id = (gaps > 3 * dt).fillna(False).cumsum()
            roll = sun.groupby(seg_id.to_numpy()).transform(
                lambda s: s.rolling(5, center=True, min_periods=1).mean())
        else:
            roll = sun.rolling(5, center=True, min_periods=1).mean()
        d['phase'] = np.where(~known, '未判定', np.where(roll >= 0.5, '日照', '地影'))
        return self._apply_phase_flag(d)

    def _apply_phase_flag(self, d):
        solar_code = self.cols['solar_I']
        fallback_phase = d['phase'].copy()
        d['phase_source'] = np.where(d['phase'].eq('未判定'), '未判定', f'{solar_code}_回退')
        d['phase_conflict'] = False
        if not self.phase_code:
            return d
        state_col = f'{self.phase_code}__state'
        state = (self.wide[state_col].astype('string').str.strip()
                 if state_col in self.wide else pd.Series(pd.NA, index=d.index, dtype='string'))
        if self.phase_code in self.wide:
            numeric = pd.to_numeric(self.wide[self.phase_code], errors='coerce')
            state = state.mask(state.isna() & numeric.eq(1), self.phase_labels[0])
            state = state.mask(state.isna() & numeric.eq(0), self.phase_labels[1])
            low_col, high_col = f'{self.phase_code}__mn', f'{self.phase_code}__mx'
            if low_col in self.wide and high_col in self.wide:
                low = pd.to_numeric(self.wide[low_col], errors='coerce')
                high = pd.to_numeric(self.wide[high_col], errors='coerce')
                state = state.mask(state.isna() & low.eq(0) & high.eq(1), '__mixed__')
            state = state.mask(state.isna() & numeric.notna(), '__invalid_numeric__')
        sun_flag = state.isin([self.phase_labels[0], '1'])
        eclipse_flag = state.isin([self.phase_labels[1], '0'])
        d['phase_conflict'] = ((sun_flag & fallback_phase.eq('地影')) |
                               (eclipse_flag & fallback_phase.eq('日照')))
        mixed = state.eq('__mixed__').fillna(False)
        invalid = (state.notna() & state.ne('') &
                   ~sun_flag & ~eclipse_flag & ~mixed).fillna(False)
        for mask, phase, source in (
            (sun_flag, '日照', self.phase_code),
            (eclipse_flag, '地影', self.phase_code),
            (mixed, '未判定', f'{self.phase_code}_切换'),
            (invalid, '未判定', f'{self.phase_code}_异常'),
        ):
            d.loc[mask, 'phase'] = phase
            d.loc[mask, 'phase_source'] = source
        return d

    # ---------------------------------------------------------------- 储能层
    def estimate_battery(self, d):
        """逐样本电压锚定 SOC + 充放拆分（全向量化，可接连续遥测）。

        P_charge/P_disch 为历史兼容字段，仅把 P_excess 按正负拆分。
        设计方案说明 PCDU 充电控制与不调节母线耦合，但 TMD017 是否覆盖
        充电支路待核；此拆分不能判定实际充电/放电功率或方向。

        缺测处理（重要）：
          - V_bat 缺测：SOC/DoD 保持 NaN；功率差额仍可独立计算；
          - P_excess 缺测：charge/disch 保持 NaN，能量层排除该样本。
        """
        vb = d['V_bat'].to_numpy(dtype=float)
        pex = d['P_excess'].to_numpy(dtype=float)
        n = len(d)
        vs = self.v_soc_table[:, 0]; socs = self.v_soc_table[:, 1]
        known = np.isfinite(vb)
        soc = np.full(n, np.nan)
        soc[known] = np.interp(np.clip(vb[known], vs[0], vs[-1]), vs, socs)
        soc[known] = np.clip(soc[known], self.design['soc_floor'], 1.0)

        surplus = pex >= 0
        p_charge = np.where(surplus, pex, 0.0)
        p_disch = np.where(~surplus, pex, 0.0)
        invalid = ~np.isfinite(pex)
        p_charge[invalid] = np.nan; p_disch[invalid] = np.nan

        d['SOC'] = soc * 100.0
        d['DoD'] = (1 - soc) * 100.0
        # 显式记录 SOC 是否来自实测电压锚定。
        d['SOC_observed'] = known
        d['P_charge'] = p_charge
        d['P_disch'] = p_disch
        return d

    def assign_battery_switch_direction(self, d):
        """按已指定的开关语义赋放电正号；断开不等同于充电。"""
        spec = self.battery_direction
        d['battery_switch_state'] = ''
        d['battery_direction_rule'] = '未判定'
        d['I_battery_switch_signed_A'] = np.nan
        d['P_battery_terminal_switch_W'] = np.nan
        d['battery_direction_conflict'] = False
        if not spec:
            return d
        switch_code = spec['switch_code']
        state_col = f'{switch_code}__state'
        state = (self.wide[state_col].astype('string').str.strip()
                 if state_col in self.wide else pd.Series(pd.NA, index=d.index, dtype='string'))
        d['battery_switch_state'] = state.fillna('')
        on = state.eq(spec['discharge_state']).fillna(False)
        d.loc[on, 'battery_direction_rule'] = '放电（开关规则）'
        current_code = spec['current_code']
        current = (pd.to_numeric(self.wide[current_code], errors='coerce')
                   if current_code in self.wide else pd.Series(np.nan, index=d.index))
        limit = float(spec.get('current_limit_A', 100))
        valid = np.isfinite(current) & current.ge(0) & current.le(limit)
        min_col, max_col = f'{current_code}__mn', f'{current_code}__mx'
        if min_col in self.wide and max_col in self.wide:
            low = pd.to_numeric(self.wide[min_col], errors='coerce')
            high = pd.to_numeric(self.wide[max_col], errors='coerce')
            valid &= np.isfinite(low) & np.isfinite(high) & low.ge(0) & high.le(limit) & low.le(high)
        signed = current.where(on & valid)
        d['I_battery_switch_signed_A'] = signed
        d['P_battery_terminal_switch_W'] = signed * d['V_bat']
        d['battery_direction_conflict'] = (on & d['I_load'].notna() & d['I_bus'].notna() &
                                           d['I_load'].lt(d['I_bus']))
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
        # SOC 仅统计实测（V_bat 锚定）样本。
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

        e_solar/e_batnet/e_charge/e_disch 均为历史字段名；其中 e_solar 是
        TMD017 测点折算能量，后三者是与负载测点差额的拆分，非实测电池电量。
        两类诊断量的区别：
          e_residual     = e_solar - e_load - e_batnet
                           恒等于 0（因 P_charge+P_disch ≡ P_excess 且
                           P_excess ≡ P_solar−P_load 由构造保证），
                           **不具备模型校验能力**，仅保留作记账恒等式；
          e_shunt_diag   = Σ[(I_solar−I_bus)×V_bus]dt，帆板级(TMD009)与
                           TMD017 的测点差额。负值值得检查测点拓扑、
                           时序与噪声；正值可能含充电支路，不能直接解释为分流或损耗。
        """
        phase_rows = d[d['phase'] == phase]
        valid = np.isfinite(phase_rows[['P_solar', 'P_load', 'P_excess']]).all(axis=1)
        sub = phase_rows.loc[valid]
        excluded = int((~valid).sum())
        if len(sub) == 0:
            return dict(e_solar_kWh=0.0, e_load_kWh=0.0, e_batnet_kWh=0.0,
                        e_shunt_diag_kWh=0.0, e_charge_kWh=0.0, e_disch_kWh=0.0,
                        e_residual_kWh=0.0, e_solar_panel_kWh=0.0,
                        n_energy_samples=0, n_excluded_power_samples=excluded)
        def esum(col):
            return float(pd.to_numeric(sub[col], errors='coerce').sum())
        dt = self.design['dt_seconds'] / 3600.0 / 1000.0   # kWh per sample
        e_solar = esum('P_solar')*dt; e_load = esum('P_load')*dt
        e_charge = esum('P_charge')*dt; e_disch = esum('P_disch')*dt
        e_shunt_diag = esum('P_shunt_diag')*dt
        # 帆板级发电（诊断口径）：I_solar × V_bus
        e_solar_panel = float((pd.to_numeric(sub['I_solar'], errors='coerce') *
                               pd.to_numeric(sub['V_bus'], errors='coerce')).sum())*dt
        e_batnet = e_charge + e_disch          # 历史字段：两测点能量差，非电池净获电
        e_residual = e_solar - e_load - e_batnet   # 恒等式（≡0）
        return dict(e_solar_kWh=e_solar, e_load_kWh=e_load,
                    e_batnet_kWh=e_batnet, e_shunt_diag_kWh=e_shunt_diag,
                    e_charge_kWh=e_charge, e_disch_kWh=e_disch,
                    e_residual_kWh=e_residual, e_solar_panel_kWh=e_solar_panel,
                    n_energy_samples=int(len(sub)), n_excluded_power_samples=excluded)

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
        d = self.assign_battery_switch_direction(d)
        res = dict(df=d)
        res['sun'] = self.phase_summary(d, '日照')
        res['ecl'] = self.phase_summary(d, '地影')
        res['e_sun'] = self.energy(d, '日照')
        res['e_ecl'] = self.energy(d, '地影')
        res['unknown'] = self.phase_summary(d, '未判定')
        res['e_unknown'] = self.energy(d, '未判定')
        self.df = d
        self.phase = res
        # 综合指标
        sun, ecl = res['sun'], res['ecl']
        margin = None; dod_p95 = None; soc_p5 = None; soc_min = None; vbus_stab = None
        if sun:
            pl = sun.get('P_load_mean')
            ps = sun.get('P_solar_mean')
            if pl and pl > 0 and ps is not None:
                margin = (ps - pl) / pl * 100
        if 'SOC' in d.columns:
            # 仅用实测锚定样本统计，避免缺测污染分位数。
            observed = d[d['SOC_observed']] if 'SOC_observed' in d.columns else d
            if len(observed):
                dod_p95 = float(observed['DoD'].quantile(.95))  # DoD already in %
                soc_p5 = float(observed['SOC'].quantile(.05))
                soc_min = float(observed['SOC'].min())
        vb = pd.to_numeric(d['V_bus'], errors='coerce').dropna()
        if len(vb) >= 2:  # 至少两个有效样本才能计算 p95-p5 展宽
            vbus_stab = (vb.quantile(.95) - vb.quantile(.05)) / vb.median() * 100
        gap_max, n_gaps = self._gap_diagnostics(d)
        span_seconds = ((d.index.max()-d.index.min()).total_seconds()
                        if len(d) >= 2 else 0.0)
        classified = int(d['phase'].isin(['日照', '地影']).sum())
        unknown = int((d['phase'] == '未判定').sum())
        negative_shunt = int((d['P_shunt_diag'] < 0).sum())
        self.metrics = dict(
            n_samples=int(len(d)),
            time_span_days=float(span_seconds/86400),
            calendar_minute_coverage=(float(len(d) / (span_seconds / self.design['dt_seconds'] + 1))
                                      if len(d) else 0.0),
            n_phase_classified=classified,
            n_phase_unknown=unknown,
            n_phase_conflict=int(d['phase_conflict'].sum()),
            n_battery_switch_discharge=int(d['battery_direction_rule'].eq('放电（开关规则）').sum()),
            n_battery_switch_current_valid=int(d['I_battery_switch_signed_A'].notna().sum()),
            n_battery_direction_conflict=int(d['battery_direction_conflict'].sum()),
            phase_source_counts={str(k): int(v) for k, v in
                                 d['phase_source'].value_counts().items()},
            phase_known_ratio=float(classified / len(d)) if len(d) else 0.0,
            sun_ratio=float((d['phase']=='日照').sum() / classified) if classified else None,
            sample_dt_seconds=self.design['dt_seconds'],
            gap_max_seconds=gap_max,
            n_gaps_gt_3x_dt=n_gaps,
            n_negative_shunt_diag=negative_shunt,
            margin_pct=round(margin,1) if margin is not None else None,
            dod_p95_pct=round(dod_p95,1) if dod_p95 is not None else None,
            soc_p5_pct=round(soc_p5,1) if soc_p5 is not None else None,
            soc_absolute_min_pct=round(soc_min,1) if soc_min is not None else None,
            vbus_stability_pct=round(float(vbus_stab),2) if vbus_stab is not None else None,
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
        hs = (sum(ss[k] * w[k] for k in avail) / wsum) if wsum else None
        lvl = ('无法评分' if hs is None else
               '优秀' if hs >= 90 else '良好' if hs >= 75 else
               '一般' if hs >= 60 else '较差')
        return dict(hs=hs, level=lvl, subscores=ss,
                    coverage=round(wsum, 3), scope='观测样本',
                    period_assessment=None,
                    measurement_validation='缺少独立电池支路功率测量')
