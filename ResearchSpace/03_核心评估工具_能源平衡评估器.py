#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卫星整星能源平衡定量评估工具
基于三体星遥测数据的实证分析

.. deprecated:: v2.0 (2026-10-21)
   本文件为 **v1 工具链**（模拟数据演示 + 1200W 口径 + 纯安时积分 SOC）。
   纯安时积分在突发式遥测下会累积漂移导致 SOC 虚高、能量不闭合，该问题及
   v2 的修正方法（TMD018 主负载口径、电压锚定 SOC、分流模型）见
   `09_评估框架设计_v2_真实数据实证.md`。正式评估请使用 v2 工具链::

       python s1_fetch.py      # ClickHouse → data_power_minute.csv
       python s3_run.py        # energy_balancer.py 评估 → out/
       python gen_charts.py    # 图表 + HTML 文档

   保留本文件仅作方法论对照与教学演示。

作者: Vibe Research
日期: 2026-10-19
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional

# 异常检测阈值（v1 演示口径；生产阈值应从电源设计文档取，勿直接沿用）
BATTERY_V_MIN = 22.0   # 蓄电池电压下限 (V)
BATTERY_V_MAX = 34.0   # 蓄电池电压上限 (V)
BUS_OUTLIER_STD = 3.0  # 母线电压异常判据（标准差倍数）


class SatelliteEnergyBalancer:
    """卫星能源平衡评估器"""

    def __init__(self, satellite_id: str = "01星"):
        """
        初始化能源平衡评估器

        Parameters:
        -----------
        satellite_id : str
            卫星编号，如"01星"、"02星"
        """
        self.satellite_id = satellite_id
        self.telemetry_data = None
        self.metrics = {}

        # 设计参数（需根据实际设计文档补充）
        self.design_params = {
            'solar_panel_rated_power': 200.0,  # 单帆板额定功率(W)
            'solar_array_rated_power': 1200.0,  # 太阳阵总额定功率(W)
            'battery_capacity': 100.0,  # 蓄电池容量(Ah)
            'bus_voltage_nominal': 28.0,  # 母线标称电压(V)
            'battery_soc_initial': 0.95,  # 初始SOC
        }

    def load_telemetry_data(self, data: pd.DataFrame):
        """
        加载遥测数据

        Parameters:
        -----------
        data : pd.DataFrame
            遥测数据，列名应为遥测代号（如TMD003, TMD004等）
        """
        self.telemetry_data = data.copy()
        print(f"已加载 {len(data)} 条遥测数据")

    def calculate_solar_power(self) -> pd.DataFrame:
        """
        计算太阳阵发电功率

        Returns:
        --------
        pd.DataFrame
            包含各帆板功率和总功率的数据
        """
        if self.telemetry_data is None:
            raise ValueError("请先加载遥测数据")

        df = self.telemetry_data.copy()

        # 太阳阵各帆板电压和电流
        panels = ['A', 'B', 'C', 'D', 'E', 'F']
        voltage_cols = {f'V_panel_{p}': f'TMD{str(i).zfill(3)}'
                       for i, p in zip(range(3, 9), panels)}
        current_cols = {f'I_panel_{p}': f'TMD{str(i).zfill(3)}'
                       for i, p in zip(range(10, 16), panels)}

        # 计算各帆板功率
        for panel in panels:
            v_col = f'V_panel_{panel}'
            i_col = f'I_panel_{panel}'
            p_col = f'P_panel_{panel}'

            if voltage_cols[v_col] in df.columns and current_cols[i_col] in df.columns:
                df[p_col] = df[voltage_cols[v_col]] * df[current_cols[i_col]]
            else:
                df[p_col] = np.nan

        # 计算总发电功率
        panel_power_cols = [f'P_panel_{p}' for p in panels]
        df['P_solar_total'] = df[panel_power_cols].sum(axis=1)

        # 总电流法验证（如果有总电流数据）
        if 'TMD009' in df.columns:
            df['P_solar_alt'] = df['TMD016'] * df['TMD009']  # 母线电压 × 总电流

        self.solar_power = df
        return df

    def calculate_load_power(self) -> pd.DataFrame:
        """
        计算各分系统负载功率

        Returns:
        --------
        pd.DataFrame
            包含各分系统功率和总功率的数据
        """
        if self.telemetry_data is None:
            raise ValueError("请先加载遥测数据")

        df = self.telemetry_data.copy()

        # 母线电压和电流
        if 'TMD016' in df.columns and 'TMD017' in df.columns:
            df['P_load_total'] = df['TMD016'] * df['TMD017']
        else:
            df['P_load_total'] = np.nan

        # 姿控系统功率（反作用轮 + 陀螺）
        wheel_currents = ['TMD045', 'TMD046', 'TMD047', 'TMD048']
        gyro_current = 'TMD067'

        attitude_currents = [c for c in wheel_currents + [gyro_current]
                            if c in df.columns]
        if attitude_currents and 'TMD016' in df.columns:
            df['P_attitude'] = df['TMD016'] * df[attitude_currents].sum(axis=1)
        else:
            df['P_attitude'] = np.nan

        # 测控系统功率
        if all(c in df.columns for c in ['TMD049', 'TMD050', 'TMD051', 'TMD052']):
            df['P_TT&C'] = (df['TMD049'] * df['TMD050'] +
                           df['TMD051'] * df['TMD052'])
            # 补充测控数传和相控阵
            if 'TMD055' in df.columns and 'TMD056' in df.columns:
                df['P_TT&C'] += df['TMD055'] * df['TMD056']
            if 'TMD053' in df.columns and 'TMD054' in df.columns:
                df['P_TT&C'] += df['TMD053'] * df['TMD054']
        else:
            df['P_TT&C'] = np.nan

        # 通信系统功率（星间微波 + 星间激光）
        comm_currents = ['TMD031', 'TMD032', 'TMD033', 'TMD034',
                        'TMD043', 'TMD044']
        comm_currents = [c for c in comm_currents if c in df.columns]
        if comm_currents and 'TMD016' in df.columns:
            df['P_comm'] = df['TMD016'] * df[comm_currents].sum(axis=1)
        else:
            df['P_comm'] = np.nan

        # 计算系统功率（智算机 + 路由器）
        compute_currents = ['TMD025', 'TMD026', 'TMD027', 'TMD028',
                           'TMD029', 'TMD030']
        compute_currents = [c for c in compute_currents if c in df.columns]
        if compute_currents and 'TMD016' in df.columns:
            df['P_compute'] = df['TMD016'] * df[compute_currents].sum(axis=1)
        else:
            df['P_compute'] = np.nan

        # 科学载荷功率
        if 'TMD066' in df.columns and 'TMD016' in df.columns:
            df['P_payload'] = df['TMD016'] * df['TMD066']
        else:
            df['P_payload'] = np.nan

        # 电推进系统功率
        if 'TMD023' in df.columns and 'TMD024' in df.columns:
            df['P_propulsion'] = df['TMD016'] * (df['TMD023'] + df['TMD024'])
        else:
            df['P_propulsion'] = np.nan

        self.load_power = df
        return df

    def calculate_battery_power(self) -> pd.DataFrame:
        """
        计算蓄电池功率和状态

        Returns:
        --------
        pd.DataFrame
            包含蓄电池功率、SOC等状态的数据
        """
        if not hasattr(self, 'solar_power') or not hasattr(self, 'load_power'):
            self.calculate_solar_power()
            self.calculate_load_power()

        df = self.solar_power.copy()
        df['P_load_total'] = self.load_power['P_load_total']

        # 蓄电池功率（功率平衡）
        df['P_battery'] = df['P_solar_total'] - df['P_load_total']

        # 蓄电池电压
        if 'TMD019' in self.telemetry_data.columns:
            df['V_battery'] = self.telemetry_data['TMD019']
        else:
            df['V_battery'] = np.nan

        # 蓄电池电流
        df['I_battery'] = df['P_battery'] / df['V_battery']

        # 充放电状态
        df['battery_status'] = df['I_battery'].apply(
            lambda x: '充电' if x < -0.1 else ('放电' if x > 0.1 else '浮充')
            if not np.isnan(x) else '未知'
        )

        # SOC估计（安时积分法）
        # ⚠ dt 按 1 秒采样假设；接入分钟级/原始 3s 数据前必须改为实际采样间隔，
        #   否则 SOC 积分会差 60 倍（v2 已改用电压锚定，见 deprecated 说明）
        df['SOC'] = self.design_params['battery_soc_initial']
        dt = 1.0 / 3600.0  # 假设1秒采样，转换为小时

        for i in range(1, len(df)):
            if not np.isnan(df.loc[df.index[i], 'I_battery']):
                delta_soc = df.loc[df.index[i], 'I_battery'] * dt / self.design_params['battery_capacity']
                df.loc[df.index[i], 'SOC'] = df.loc[df.index[i-1], 'SOC'] + delta_soc

        # 截断到 [0,1]，防止积分漂移越界（纯积分无校正时的已知缺陷）
        df['SOC'] = df['SOC'].clip(0.0, 1.0)

        # DoD（放电深度）
        df['DoD'] = 1 - df['SOC']

        self.battery_state = df
        return df

    def calculate_metrics(self) -> Dict:
        """
        计算能源平衡评估指标

        Returns:
        --------
        Dict
            包含各项性能指标的字典
        """
        if not hasattr(self, 'battery_state'):
            self.calculate_battery_power()

        df = self.battery_state.copy()
        metrics = {}

        # 1. 功率裕度（P_load≈0 时除法产生 inf，替换为 NaN 避免污染均值）
        with np.errstate(divide='ignore', invalid='ignore'):
            df['power_margin'] = (df['P_solar_total'] - df['P_load_total']) / df['P_load_total'] * 100
        df['power_margin'] = df['power_margin'].replace([np.inf, -np.inf], np.nan)
        # 供 detect_anomalies 使用（此前该列算完即弃，告警永远不触发）
        self.battery_state['power_margin'] = df['power_margin']
        metrics['avg_power_margin'] = df['power_margin'].mean()
        metrics['min_power_margin'] = df['power_margin'].min()
        metrics['max_power_margin'] = df['power_margin'].max()

        # 2. 发电系统指标
        metrics['avg_solar_power'] = df['P_solar_total'].mean()
        metrics['max_solar_power'] = df['P_solar_total'].max()
        metrics['solar_utilization'] = metrics['avg_solar_power'] / self.design_params['solar_array_rated_power'] * 100

        # 3. 负载系统指标
        metrics['avg_load_power'] = df['P_load_total'].mean()
        metrics['max_load_power'] = df['P_load_total'].max()

        # 4. 蓄电池状态指标
        metrics['avg_SOC'] = df['SOC'].mean() * 100
        metrics['min_SOC'] = df['SOC'].min() * 100
        metrics['max_DoD'] = df['DoD'].max() * 100

        # 5. 母线稳定度
        if 'TMD016' in df.columns:
            bus_voltage = df['TMD016']
            metrics['bus_voltage_avg'] = bus_voltage.mean()
            metrics['bus_voltage_stability'] = (bus_voltage.max() - bus_voltage.min()) / bus_voltage.mean() * 100

        # 6. 系统效率
        total_energy_in = (df['P_solar_total'] + df['P_battery'].apply(lambda x: max(0, x))).sum()
        metrics['system_efficiency'] = df['P_load_total'].sum() / total_energy_in * 100

        self.metrics = metrics
        return metrics

    def evaluate_health(self) -> Dict:
        """
        评估能源系统健康度

        Returns:
        --------
        Dict
            健康度评分和建议
        """
        if not self.metrics:
            self.calculate_metrics()

        scores = {}

        # 1. 功率裕度评分
        if self.metrics['avg_power_margin'] >= 20:
            scores['power_margin_score'] = 100
        elif self.metrics['avg_power_margin'] >= 10:
            scores['power_margin_score'] = 70
        else:
            scores['power_margin_score'] = 40

        # 2. 蓄电池状态评分
        if self.metrics['max_DoD'] <= 30:
            scores['battery_score'] = 100
        elif self.metrics['max_DoD'] <= 40:
            scores['battery_score'] = 70
        else:
            scores['battery_score'] = 40

        # 3. 母线稳定度评分
        if self.metrics.get('bus_voltage_stability', 100) <= 3:
            scores['bus_stability_score'] = 100
        elif self.metrics.get('bus_voltage_stability', 100) <= 5:
            scores['bus_stability_score'] = 70
        else:
            scores['bus_stability_score'] = 40

        # 4. 太阳阵利用率评分
        if self.metrics['solar_utilization'] >= 60:
            scores['solar_utilization_score'] = 100
        elif self.metrics['solar_utilization'] >= 40:
            scores['solar_utilization_score'] = 70
        else:
            scores['solar_utilization_score'] = 40

        # 综合健康度评分（加权平均）
        weights = {
            'power_margin_score': 0.35,
            'battery_score': 0.30,
            'bus_stability_score': 0.15,
            'solar_utilization_score': 0.20
        }

        health_score = sum(scores[k] * weights[k] for k in weights.keys())

        # 健康等级
        if health_score >= 90:
            health_level = '优秀'
        elif health_score >= 75:
            health_level = '良好'
        elif health_score >= 60:
            health_level = '一般'
        else:
            health_level = '较差'

        # 生成建议
        recommendations = []
        if scores['power_margin_score'] < 70:
            recommendations.append("功率裕度不足，建议优化负载调度或检查太阳阵性能")
        if scores['battery_score'] < 70:
            recommendations.append("蓄电池放电深度过大，建议关注蓄电池健康状态")
        if scores['bus_stability_score'] < 70:
            recommendations.append("母线电压波动较大，建议检查电源调节器")
        if scores['solar_utilization_score'] < 70:
            recommendations.append("太阳阵利用率偏低，建议检查对日指向或帆板性能")

        return {
            'health_score': health_score,
            'health_level': health_level,
            'sub_scores': scores,
            'recommendations': recommendations
        }

    def detect_anomalies(self, threshold_std: float = BUS_OUTLIER_STD) -> List[Dict]:
        """
        异常检测

        Parameters:
        -----------
        threshold_std : float
            异常检测阈值（标准差倍数）

        Returns:
        --------
        List[Dict]
            异常事件列表
        """
        if self.telemetry_data is None:
            raise ValueError("请先加载遥测数据")

        anomalies = []

        # 检测母线电压异常
        if 'TMD016' in self.telemetry_data.columns:
            bus_voltage = self.telemetry_data['TMD016']
            mean_v = bus_voltage.mean()
            std_v = bus_voltage.std()

            outlier_mask = (bus_voltage < mean_v - threshold_std * std_v) | \
                          (bus_voltage > mean_v + threshold_std * std_v)
            if outlier_mask.any():
                anomalies.append({
                    'type': '母线电压异常',
                    'count': outlier_mask.sum(),
                    'severity': '高' if outlier_mask.sum() > len(bus_voltage) * 0.01 else '中'
                })

        # 检测蓄电池电压异常（阈值见模块常量 BATTERY_V_MIN/MAX）
        if 'TMD019' in self.telemetry_data.columns:
            bat_voltage = self.telemetry_data['TMD019']
            # 简单阈值检测
            if (bat_voltage < BATTERY_V_MIN).any() or (bat_voltage > BATTERY_V_MAX).any():
                anomalies.append({
                    'type': '蓄电池电压越限',
                    'count': ((bat_voltage < BATTERY_V_MIN) | (bat_voltage > BATTERY_V_MAX)).sum(),
                    'severity': '高'
                })

        # 检测功率异常（power_margin 由 calculate_metrics 写入 battery_state）
        if hasattr(self, 'battery_state'):
            power_margin = self.battery_state.get('power_margin', pd.Series())
            if len(power_margin) > 0:
                if (power_margin < 0).any():
                    anomalies.append({
                        'type': '功率裕度不足',
                        'count': (power_margin < 0).sum(),
                        'severity': '高' if (power_margin < -10).any() else '中'
                    })

        return anomalies

    def generate_report(self) -> str:
        """
        生成能源评估报告

        Returns:
        --------
        str
            Markdown格式的报告
        """
        if not self.metrics:
            self.calculate_metrics()

        health = self.evaluate_health()
        anomalies = self.detect_anomalies()

        report = f"""# 三体星{self.satellite_id}能源系统评估报告

## 1. 能源健康度评分

**综合评分：{health['health_score']:.1f}分（{health['health_level']}）**

### 分项评分
- 功率裕度：{health['sub_scores']['power_margin_score']:.0f}分
- 蓄电池状态：{health['sub_scores']['battery_score']:.0f}分
- 母线稳定度：{health['sub_scores']['bus_stability_score']:.0f}分
- 太阳阵利用率：{health['sub_scores']['solar_utilization_score']:.0f}分

## 2. 能量平衡分析

### 发电系统
- 平均发电功率：{self.metrics['avg_solar_power']:.1f} W
- 最大发电功率：{self.metrics['max_solar_power']:.1f} W
- 太阳阵利用率：{self.metrics['solar_utilization']:.1f}%

### 负载系统
- 平均负载功率：{self.metrics['avg_load_power']:.1f} W
- 最大负载功率：{self.metrics['max_load_power']:.1f} W

### 功率裕度
- 平均功率裕度：{self.metrics['avg_power_margin']:.1f}%
- 最小功率裕度：{self.metrics['min_power_margin']:.1f}%
- 评估结论：{'正常' if self.metrics['avg_power_margin'] > 20 else '预警' if self.metrics['avg_power_margin'] > 0 else '异常'}

## 3. 蓄电池状态

- 平均SOC：{self.metrics['avg_SOC']:.1f}%
- 最低SOC：{self.metrics['min_SOC']:.1f}%
- 最大DoD：{self.metrics['max_DoD']:.1f}%
- 评估结论：{'健康' if self.metrics['max_DoD'] < 30 else '关注' if self.metrics['max_DoD'] < 40 else '劣化'}

## 4. 配电系统

- 母线电压平均值：{self.metrics.get('bus_voltage_avg', 0):.2f} V
- 母线电压稳定度：{self.metrics.get('bus_voltage_stability', 0):.2f}%
- 系统效率：{self.metrics.get('system_efficiency', 0):.1f}%
- 评估结论：{'正常' if self.metrics.get('bus_voltage_stability', 100) < 5 else '波动'}

## 5. 异常与预警

"""
        if anomalies:
            for i, anomaly in enumerate(anomalies, 1):
                report += f"{i}. **{anomaly['type']}**：出现{anomaly['count']}次，严重程度：{anomaly['severity']}\n"
        else:
            report += "无异常事件\n"

        report += "\n## 6. 建议措施\n\n"
        if health['recommendations']:
            for i, rec in enumerate(health['recommendations'], 1):
                report += f"{i}. {rec}\n"
        else:
            report += "能源系统运行正常，建议继续保持当前运行模式\n"

        report += f"""
---
**报告生成时间**：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}
**评估数据量**：{len(self.telemetry_data)} 条遥测数据
"""

        return report


def demo():
    """演示函数：生成模拟数据并运行评估"""
    print("=" * 60)
    print("卫星整星能源平衡定量评估工具 - 演示版")
    print("=" * 60)
    print()

    # 创建评估器
    evaluator = SatelliteEnergyBalancer(satellite_id="01星")

    # 生成模拟遥测数据（1小时，1Hz采样）
    n_samples = 3600
    time = np.arange(n_samples)

    # 模拟轨道周期（约90分钟）
    orbit_period = 5400  # 秒
    sunlight_ratio = 0.65  # 光照期占比

    # 模拟太阳阵电压（光照期）
    sunlight_mask = (time % orbit_period) < (orbit_period * sunlight_ratio)
    V_panel_A = 30.0 * sunlight_mask + np.random.normal(0, 0.5, n_samples)
    V_panel_A = np.clip(V_panel_A, 0, 35)

    # 模拟太阳阵电流（光照期）
    I_panel_A = 5.0 * sunlight_mask + np.random.normal(0, 0.2, n_samples)
    I_panel_A = np.clip(I_panel_A, 0, 7)

    # 创建遥测数据
    telemetry = pd.DataFrame({
        'timestamp': time,
        'TMD003': V_panel_A,  # 太阳阵A电压
        'TMD010': I_panel_A,  # 太阳阵A电流
        'TMD016': 28.0 + np.random.normal(0, 0.3, n_samples),  # 母线电压
        'TMD017': 10.0 + np.random.normal(0, 1.0, n_samples),  # 母线电流
        'TMD019': 28.0 + np.random.normal(0, 1.0, n_samples),  # 蓄电池电压
        'TMD009': I_panel_A * 6,  # 太阳阵总电流（假设6个帆板）
        'TMD045': 1.0 + np.random.normal(0, 0.1, n_samples),  # 飞轮1
        'TMD046': 1.0 + np.random.normal(0, 0.1, n_samples),  # 飞轮2
        'TMD047': 1.0 + np.random.normal(0, 0.1, n_samples),  # 飞轮3
        'TMD048': 1.0 + np.random.normal(0, 0.1, n_samples),  # 飞轮4
        'TMD067': 0.5 + np.random.normal(0, 0.05, n_samples),  # 陀螺
    })

    print("1. 加载遥测数据")
    evaluator.load_telemetry_data(telemetry)
    print()

    print("2. 计算发电功率")
    solar_power = evaluator.calculate_solar_power()
    print(f"   平均发电功率: {solar_power['P_solar_total'].mean():.1f} W")
    print(f"   最大发电功率: {solar_power['P_solar_total'].max():.1f} W")
    print()

    print("3. 计算负载功率")
    load_power = evaluator.calculate_load_power()
    print(f"   平均负载功率: {load_power['P_load_total'].mean():.1f} W")
    print(f"   最大负载功率: {load_power['P_load_total'].max():.1f} W")
    print()

    print("4. 计算蓄电池状态")
    battery_state = evaluator.calculate_battery_power()
    print(f"   平均SOC: {battery_state['SOC'].mean()*100:.1f}%")
    print(f"   最低SOC: {battery_state['SOC'].min()*100:.1f}%")
    print(f"   最大DoD: {battery_state['DoD'].max()*100:.1f}%")
    print()

    print("5. 计算性能指标")
    metrics = evaluator.calculate_metrics()
    print(f"   平均功率裕度: {metrics['avg_power_margin']:.1f}%")
    print(f"   太阳阵利用率: {metrics['solar_utilization']:.1f}%")
    print(f"   系统效率: {metrics.get('system_efficiency', 0):.1f}%")
    print()

    print("6. 健康度评估")
    health = evaluator.evaluate_health()
    print(f"   综合评分: {health['health_score']:.1f}分（{health['health_level']}）")
    print()

    print("7. 异常检测")
    anomalies = evaluator.detect_anomalies()
    if anomalies:
        print(f"   发现 {len(anomalies)} 个异常事件:")
        for anomaly in anomalies:
            print(f"   - {anomaly['type']}: {anomaly['count']}次")
    else:
        print("   未发现异常")
    print()

    print("8. 生成评估报告")
    report = evaluator.generate_report()
    print("   报告已生成（部分预览）:")
    print()
    print(report[:1000] + "...")
    print()

    # 保存报告（输出到本脚本所在目录，不再硬编码绝对路径）
    from pathlib import Path
    output_path = Path(__file__).resolve().parent / 'demo_report.md'
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"完整报告已保存至: {output_path}")
    print()
    print("=" * 60)
    print("演示完成！")
    print("=" * 60)


if __name__ == "__main__":
    demo()
