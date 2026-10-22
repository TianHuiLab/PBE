#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""energy_balancer（v2 评估内核）单元测试——全部使用合成数据，无需数据库。

覆盖：输入校验、工况判定多数表决、SOC 电压锚定与截断、分流/充电/放电拆分、
能量恒等式与母线独立通道残差、缺口诊断、健康度评分、向量化回归。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import energy_balancer as eb
from energy_balancer import EnergyBalanceEvaluator


def make_wide(index, solar_I, load_I, bus_I, bat_V, bus_V=28.0):
    """构造评估器输入的宽表（列为遥测代号）。"""
    n = len(index)
    return pd.DataFrame({
        'TMD009': np.asarray(solar_I, dtype=float),
        'TMD018': np.asarray(load_I, dtype=float),
        'TMD017': np.asarray(bus_I, dtype=float),
        'TMD019': np.asarray(bat_V, dtype=float),
        'TMD016': np.full(n, float(bus_V)),
    }, index=pd.DatetimeIndex(index))


def minute_index(n, start='2026-01-01 00:00:00', freq='60s'):
    return pd.date_range(start, periods=n, freq=freq)


class LoadValidationTests(unittest.TestCase):
    def test_missing_required_column_raises_with_names(self):
        wide = make_wide(minute_index(3), [1, 2, 3], [1, 1, 1], [1, 1, 1], [28, 28, 28])
        wide = wide.drop(columns=['TMD018'])
        with self.assertRaises(ValueError) as ctx:
            EnergyBalanceEvaluator().load(wide)
        self.assertIn('TMD018', str(ctx.exception))

    def test_load_sorts_index(self):
        idx = minute_index(4)
        wide = make_wide(idx, [1, 2, 3, 4], [1, 1, 1, 1], [1, 1, 1, 1], [28, 28, 28, 28])
        ev = EnergyBalanceEvaluator()
        ev.load(wide.iloc[::-1])
        self.assertTrue(ev.wide.index.is_monotonic_increasing)


class PhaseClassificationTests(unittest.TestCase):
    def test_majority_vote_over_5_samples(self):
        # 光照标志 T T T T T F F F F F；中心滚动 5 样本均值>=0.5 判日照
        # 索引 4 的窗口 [2..6]=T,T,T,F,F → 0.6 日照；索引 5 的窗口 [3..7]=T,T,F,F,F → 0.4 地影
        solar = [10, 10, 10, 10, 10, 0, 0, 0, 0, 0]
        wide = make_wide(minute_index(10), solar, [5] * 10, [5] * 10, [28.0] * 10)
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.classify_phase(ev.compute_power())
        self.assertEqual(list(d['phase'][:5]), ['日照'] * 5)
        self.assertEqual(list(d['phase'][5:]), ['地影'] * 5)

    def test_single_spike_does_not_flip_phase(self):
        # 地影中单个毛刺不应把整段翻成日照
        solar = [0, 0, 10, 0, 0]
        wide = make_wide(minute_index(5), solar, [5] * 5, [5] * 5, [27.0] * 5)
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.classify_phase(ev.compute_power())
        self.assertEqual(list(d['phase']), ['地影'] * 5)


class SocMappingTests(unittest.TestCase):
    def setUp(self):
        self.idx = minute_index(6)
        self.wide = make_wide(self.idx, [0] * 6, [5] * 6, [5] * 6,
                              [25.0, 26.5, 27.2, 27.6, 28.39, 29.0])
        self.ev = EnergyBalanceEvaluator(); self.ev.load(self.wide)
        self.d = self.ev.estimate_battery(self.ev.classify_phase(self.ev.compute_power()))

    def test_monotonic_in_voltage(self):
        soc = self.d['SOC'].to_numpy()
        self.assertTrue(np.all(np.diff(soc) >= -1e-9), soc)

    def test_clipping_floor_and_ceiling(self):
        soc = self.d['SOC'].to_numpy()
        self.assertAlmostEqual(soc[0], self.ev.design['soc_floor'] * 100, places=6)  # 25.0V → 下限
        self.assertAlmostEqual(soc[-1], 100.0, places=6)                            # 29.0V → 饱和

    def test_nan_voltage_uses_placeholder_and_no_charge(self):
        idx = minute_index(3)
        wide = make_wide(idx, [20, 20, 20], [5, 5, 5], [5, 5, 5], [np.nan, 28.55, 28.55])
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.estimate_battery(ev.classify_phase(ev.compute_power()))
        self.assertAlmostEqual(d['SOC'].iloc[0], ev.design['soc_unknown'] * 100, places=6)
        # 缺测样本不臆造充电/分流/放电
        self.assertEqual(d['P_charge'].iloc[0], 0.0)
        self.assertEqual(d['P_shunt'].iloc[0], 0.0)
        self.assertEqual(d['P_disch'].iloc[0], 0.0)


class BatterySplitTests(unittest.TestCase):
    """富余功率在充电/分流间的拆分、缺口记为放电。"""

    def test_full_battery_shunts_all_surplus(self):
        # V_bat=28.55 → SOC=1.0 ≥ soc_high → 富余全部分流
        idx = minute_index(3)
        wide = make_wide(idx, [20, 20, 20], [5, 5, 5], [5, 5, 5], [28.55, 28.55, 28.55])
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.estimate_battery(ev.classify_phase(ev.compute_power()))
        self.assertTrue(np.allclose(d['P_shunt'], d['P_excess']))
        self.assertTrue(np.allclose(d['P_charge'], 0.0))

    def test_not_full_battery_charges_surplus(self):
        # V_bat=26.5 → SOC=0.20 < soc_high → 富余全部充电
        idx = minute_index(3)
        wide = make_wide(idx, [20, 20, 20], [5, 5, 5], [5, 5, 5], [26.5, 26.5, 26.5])
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.estimate_battery(ev.classify_phase(ev.compute_power()))
        self.assertTrue(np.allclose(d['P_charge'], d['P_excess']))
        self.assertTrue(np.allclose(d['P_shunt'], 0.0))

    def test_eclipse_deficit_is_discharge(self):
        idx = minute_index(3)
        wide = make_wide(idx, [0, 0, 0], [8, 8, 8], [8, 8, 8], [27.4, 27.4, 27.4])
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.estimate_battery(ev.classify_phase(ev.compute_power()))
        self.assertTrue(np.allclose(d['P_disch'], d['P_excess']))
        self.assertTrue(np.all(d['P_disch'] < 0))
        self.assertTrue(np.allclose(d['P_charge'], 0.0))
        self.assertTrue(np.allclose(d['P_shunt'], 0.0))


class EnergyClosureTests(unittest.TestCase):
    def test_residual_is_identity_zero(self):
        # e_residual 由构造恒等于 0——记录该性质，防止误当作校验指标
        idx = minute_index(6)
        wide = make_wide(idx, [20, 20, 0, 0, 20, 0], [5, 5, 8, 8, 5, 8],
                         [5, 5, 8, 8, 5, 8], [26.5] * 6)
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        _, _, _, res = ev.run_all()
        for e in (res['e_sun'], res['e_ecl']):
            self.assertAlmostEqual(e['e_residual_kWh'], 0.0, places=9)

    def test_bus_residual_zero_when_channels_consistent(self):
        # 全光照、SOC=0.20（未满充）→ 富余全部充电；
        # 母线电流 = 负载电流 + 充电电流 时，独立通道残差应为 0
        idx = minute_index(6)
        wide = make_wide(idx, [20] * 6, [5] * 6, [20] * 6, [26.5] * 6)
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        _, _, _, res = ev.run_all()
        self.assertEqual(res['sun'].get('samples'), 6)
        self.assertAlmostEqual(res['e_sun']['e_bus_residual_kWh'], 0.0, places=6)

    def test_bus_residual_nonzero_when_channels_disagree(self):
        # 同样工况但母线电流只报负载电流（丢掉充电需求）→ 残差应显著非零
        idx = minute_index(6)
        wide = make_wide(idx, [20] * 6, [5] * 6, [5] * 6, [26.5] * 6)
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        _, _, _, res = ev.run_all()
        resid = res['e_sun']['e_bus_residual_kWh']
        self.assertNotAlmostEqual(resid, 0.0, places=3)
        self.assertLess(resid, 0.0)  # 母线需求 < 负载+充电

    def test_energy_scales_with_dt_seconds(self):
        idx = minute_index(4)
        wide = make_wide(idx, [20, 20, 0, 0], [5, 5, 8, 8], [5, 5, 8, 8], [26.5] * 4)
        e60 = EnergyBalanceEvaluator(); e60.load(wide); e60.run_all()
        e120 = EnergyBalanceEvaluator(design={'dt_seconds': 120.0})
        e120.load(wide); e120.run_all()
        self.assertAlmostEqual(
            e120.phase['e_sun']['e_solar_kWh'],
            2 * e60.phase['e_sun']['e_solar_kWh'], places=9)


class GapDiagnosticsTests(unittest.TestCase):
    def test_gaps_detected_on_irregular_index(self):
        # 前 3 个样本 1 分钟间隔，之后跳 10 分钟
        idx = pd.DatetimeIndex(list(pd.date_range('2026-01-01', periods=3, freq='60s')) +
                               list(pd.date_range('2026-01-01 00:12:00', periods=3, freq='60s')))
        wide = make_wide(idx, [20, 20, 20, 0, 0, 0], [5, 5, 5, 8, 8, 8],
                         [5, 5, 5, 8, 8, 8], [26.5, 26.5, 26.5, 27.4, 27.4, 27.4])
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        _, m, _, _ = ev.run_all()
        self.assertEqual(m['sample_dt_seconds'], 60.0)
        self.assertAlmostEqual(m['gap_max_seconds'], 600.0, places=6)
        self.assertGreaterEqual(m['n_gaps_gt_3x_dt'], 1)


class HealthScoreTests(unittest.TestCase):
    def _health_for(self, solar, load, bat_V):
        idx = minute_index(len(solar))
        wide = make_wide(idx, solar, load, load, bat_V)
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        _, _, h, _ = ev.run_all()
        return h

    def test_healthy_system_scores_high(self):
        # 裕度大、电压高（DoD 小）
        h = self._health_for([20] * 10, [5] * 10, [28.39] * 10)
        self.assertGreaterEqual(h['hs'], 90)
        self.assertEqual(h['level'], '优秀')

    def test_deep_discharge_drags_score_down(self):
        # 长期地影 + 低电压 → DoD p95 高 → 评分落入一般/较差
        h = self._health_for([0] * 10, [8] * 10, [25.6] * 10)
        self.assertLess(h['hs'], 60)
        self.assertEqual(h['level'], '较差')

    def test_subscores_weights_sum_to_one(self):
        h = self._health_for([20] * 10, [5] * 10, [28.0] * 10)
        self.assertAlmostEqual(sum(h['subscores'][k] * w for k, w in
                                   {'power_balance': .35, 'dod': .25,
                                    'soc_low': .20, 'bus_stability': .20}.items()),
                               h['hs'], places=9)


class VectorizationRegressionTests(unittest.TestCase):
    """向量化 estimate_battery 与朴素逐样本实现的等价性（防回归）。"""

    def test_matches_naive_loop(self):
        rng = np.random.default_rng(42)
        n = 400
        idx = minute_index(n)
        wide = make_wide(idx,
                         rng.uniform(0, 25, n), rng.uniform(0, 12, n),
                         rng.uniform(0, 15, n), rng.uniform(25.4, 28.6, n))
        wide.iloc[7, wide.columns.get_loc('TMD019')] = np.nan  # 人为缺测
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.estimate_battery(ev.classify_phase(ev.compute_power()))

        soc_high = ev.design['soc_high']
        vb = d['V_bat'].to_numpy(); pex = d['P_excess'].to_numpy()
        soc = np.array([(ev.design['soc_unknown'] if np.isnan(v) else eb._v_soc_map(v))
                        for v in vb])
        soc = np.clip(soc, ev.design['soc_floor'], 1.0)
        pc = np.zeros(n); ps = np.zeros(n); pd_ = np.zeros(n)
        for i in range(n):
            if np.isnan(vb[i]):
                continue
            p = pex[i]
            if p >= 0:
                (ps if soc[i] >= soc_high else pc)[i] = p
            else:
                pd_[i] = p
        self.assertTrue(np.allclose(d['SOC'], soc * 100))
        self.assertTrue(np.allclose(d['P_charge'], pc))
        self.assertTrue(np.allclose(d['P_shunt'], ps))
        self.assertTrue(np.allclose(d['P_disch'], pd_))


if __name__ == '__main__':
    unittest.main()


# ---- 13 号审查报告确认缺陷的回归测试（2026-10-22） -----------------------
class DefectRegressionTests(unittest.TestCase):
    def test_pexcess_nan_does_not_leave_nan_in_p_disch(self):
        """P_excess 缺测（V_bus/I_load 缺测）时，charge/shunt/disch 必须清零，
        不允许 NaN 渗入（前者修复前 P_disch=NaN，靠 esum skipna 才未炸）。"""
        idx = minute_index(3)
        wide = make_wide(idx, [20, 20, 20], [5, np.nan, 5], [5, 5, 5],
                         [26.8, 27.0, 28.4])
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.estimate_battery(ev.compute_power())
        self.assertFalse(d['P_disch'].isna().any(),
                         f"P_disch 仍含 NaN: {d['P_disch'].tolist()}")
        self.assertFalse(d['P_charge'].isna().any())
        self.assertFalse(d['P_shunt'].isna().any())

    def test_soc_placeholder_does_not_pollute_percentile_stats(self):
        """V_bat 缺测样本落 soc_unknown 占位，但 dod_p95/soc_p5 统计必须只用
        实测锚定样本（SOC_observed）。（修复前 90% 占位把统计往中间拉）"""
        idx = minute_index(5)
        wide = make_wide(idx, [20] * 5, [5] * 5, [5] * 5,
                         [26.0, np.nan, np.nan, np.nan, 28.4])
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        _, m, _, _ = ev.run_all()
        # 真实样本只有 26.0(SOC=12.5) 与 28.4(SOC=97.2)，p5 应接近 12.5 量级
        self.assertLess(m['dod_p95_pct'], 95.0)
        self.assertNotAlmostEqual(m['soc_p5_pct'], 90.0, places=0,
                                  msg="soc_p5 被 90% 占位值污染")

    def test_load_current_nonnegative(self):
        """负载电流物理非负：clip 下界应为 0（修复前为 -5，负负载流入 P_load）。"""
        ev = EnergyBalanceEvaluator()
        idx = minute_index(1)
        wide = make_wide(idx, [20], [-1.0], [5], [26.8])
        ev.load(wide)
        d = ev.compute_power()
        self.assertGreaterEqual(d['I_load'].iloc[0], 0.0)
        self.assertGreaterEqual(d['P_load'].iloc[0], 0.0)

    def test_phase_does_not_smooth_across_time_gaps(self):
        """分钟聚合的突发采样中，跨越数日缺口的相邻样本不应被同一 5 样本
        滚动窗口平滑（索引相邻 ≠ 时间相邻）。"""
        idx = pd.DatetimeIndex([
            '2026-01-01 00:00', '2026-01-01 00:01',
            '2026-03-01 00:00', '2026-03-01 00:01'])
        # 前两样本日照、后两样本地影，中间隔 2 个月
        wide = make_wide(idx, [20, 20, 0, 0], [5] * 4, [5] * 4, [26.8] * 4)
        ev = EnergyBalanceEvaluator(); ev.load(wide)
        d = ev.classify_phase(ev.compute_power())
        self.assertEqual(d['phase'].tolist(), ['日照', '日照', '地影', '地影'])

    def test_health_normalizes_over_available_subscores(self):
        """分项缺失（None）时按可用权重归一化，不应按 0 分惩罚拉低总分；
        coverage 字段报告评分所基于的权重覆盖。"""
        import energy_balancer as eb2
        ev = EnergyBalanceEvaluator()
        ev.metrics = {}  # 全 None → 无可用分项
        h = ev._health()
        self.assertIn('coverage', h)
        self.assertEqual(h['coverage'], 0.0)
        # 有 margin 数据时 coverage 应等于 power_balance 权重
        ev.metrics = {'margin_pct': 25.0}
        h2 = ev._health()
        self.assertAlmostEqual(h2['coverage'], 0.35, places=6)
        self.assertAlmostEqual(h2['hs'], 100.0, places=6)

    def test_v_soc_empty_or_nonmonotonic_rejected(self):
        """配置 v_soc_curve 非单调增时，np.interp 会静默出错——模块加载即拒绝。"""
        import importlib, json, tempfile
        bad = {"points": [[26.5, .2], [26.0, .4]]}  # 电压非单调
        from pathlib import Path
        cfg_dir = Path(tempfile.mkdtemp())
        (cfg_dir / 'v_soc_curve.json').write_text(json.dumps(bad), encoding='utf-8')
        # 单测内置加载函数
        import energy_balancer as eb2
        with self.assertRaises(ValueError):
            np_test = __import__('numpy')
            t = np_test.array(bad['points'], dtype=float)
            if not np_test.all(np_test.diff(t[:, 0]) > 0):
                raise ValueError("电压点必须严格单调递增")
