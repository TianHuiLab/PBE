#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三体星01星 能源平衡定量评估工具
从ClickHouse数据库读取遥测数据并进行分析

.. deprecated:: v2.0 (2026-10-21)
   本文件为 **v1 工具链**：内嵌一份简化的 SatelliteEnergyBalancer（1200W 口径、
   纯安时积分 SOC），该方法论问题已在 `09_评估框架设计_v2_真实数据实证.md` 中
   记录（SOC 虚高、能量不闭合）。正式评估请使用 v2 工具链::

       python s1_fetch.py      # ClickHouse → data_power_minute.csv
       python s3_run.py        # energy_balancer.py 评估 → out/
       python gen_charts.py    # 图表 + HTML 文档

   保留本文件仅作数据库结构探索参考；其数据库探索能力亦可由 q1/q2 + ch_http 替代。

使用说明：
    1. 确保环境可以访问 10.200.56.174:9000
    2. 安装依赖: pip install clickhouse-driver pandas numpy
    3. 配置口令: 环境变量 CH_PASSWORD（或同目录 .env，见 .env.example）
    4. 运行: python3 05_数据库分析工具_ClickHouse能源分析器.py

依赖:
    pip install clickhouse-driver pandas numpy
"""

import os
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta

try:
    from clickhouse_driver import connect
    CLICKHOUSE_AVAILABLE = True
except ImportError:
    CLICKHOUSE_AVAILABLE = False
    print("⚠️  请先安装clickhouse-driver: pip install clickhouse-driver")


class ClickHouseEnergyAnalyzer:
    """基于ClickHouse数据库的卫星能源平衡分析器"""

    def __init__(self,
                 host: str = None,
                 port: int = None,
                 user: str = None,
                 password: str = None,
                 database: str = None):
        """
        初始化数据库连接

        Parameters:
        -----------
        host : str
            ClickHouse节点IP（默认取环境变量 CH_HOST 或 10.200.56.174）
        port : int
            ClickHouse原生端口（默认取环境变量 CH_PORT 或 9000）
        user : str
            用户名（默认取环境变量 CH_USER 或 default）
        password : str
            口令（默认取环境变量 CH_PASSWORD；无默认口令，见 ch_http.py 说明）
        database : str
            数据库名称（默认取环境变量 CH_DB 或 chdb_3body.master_satellite_01）
        """
        if not CLICKHOUSE_AVAILABLE:
            raise ImportError("clickhouse-driver未安装，请运行: pip install clickhouse-driver")

        self.host = host or os.environ.get('CH_HOST', '10.200.56.174')
        self.port = port or int(os.environ.get('CH_PORT', '9000'))
        self.user = user or os.environ.get('CH_USER', 'default')
        self.password = password if password is not None else os.environ.get('CH_PASSWORD', '')
        self.database = database or os.environ.get(
            'CH_DB', 'chdb_3body') + '.master_satellite_01'
        self.conn = None
        self.telemetry_data = None

        # 设计参数（需要根据实际设计文档修正）
        # ⚠ v1 口径：太阳阵 1200W 已被 v2 实证报告（10 号文档）修正为 ~900W，
        #   此处保留仅为兼容旧演示，勿用于正式结论。
        self.design_params = {
            'solar_panel_rated_power': 200.0,
            'solar_array_rated_power': 1200.0,
            'battery_capacity': 100.0,
            'bus_voltage_nominal': 28.0,
            'battery_soc_initial': 0.95,
        }

    def connect_db(self) -> bool:
        """连接ClickHouse数据库"""
        try:
            print(f"正在连接ClickHouse数据库...")
            print(f"  节点: {self.host}:{self.port}")
            print(f"  数据库: {self.database}")

            self.conn = connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                settings={'connect_timeout': 10, 'receive_timeout': 60}
            )
            print("✅ 数据库连接成功！")
            return True

        except Exception as e:
            print(f"❌ 数据库连接失败: {e}")
            return False

    def close_db(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            print("数据库连接已关闭")

    def explore_database(self):
        """探索数据库结构，查看所有表"""
        if not self.conn:
            if not self.connect_db():
                return

        cursor = self.conn.cursor()

        print("\n" + "=" * 60)
        print("数据库表结构探索")
        print("=" * 60)

        # 查看所有表
        cursor.execute("SHOW TABLES")
        tables = cursor.fetchall()
        print(f"\n📊 数据库中共有 {len(tables)} 张表：\n")
        for i, table in enumerate(tables, 1):
            print(f"  {i:3d}. {table[0]}")

        # 查看每张表的结构和数据量
        print(f"\n{'='*60}")
        print("各表详细信息：")
        print(f"{'='*60}\n")

        for table_info in tables:
            table_name = table_info[0]
            try:
                # 查看表结构
                cursor.execute(f"DESCRIBE TABLE {table_name}")
                columns = cursor.fetchall()

                # 查看数据量
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                count = cursor.fetchone()[0]

                # 查看时间范围（如果有时间列）
                time_col = None
                for col in columns:
                    if any(kw in col[0].lower() for kw in ['time', 'timestamp', 'datetime', 'date']):
                        time_col = col[0]
                        break

                print(f"📋 表名: {table_name}")
                print(f"   数据量: {count:,} 条")

                if time_col:
                    cursor.execute(f"SELECT MIN({time_col}), MAX({time_col}) FROM {table_name}")
                    time_range = cursor.fetchone()
                    print(f"   时间范围: {time_range[0]} ~ {time_range[1]}")

                print(f"   字段列表:")
                for col in columns:
                    print(f"     - {col[0]} ({col[1]})")
                print()

            except Exception as e:
                print(f"📋 表名: {table_name}")
                print(f"   ❌ 查询失败: {e}\n")

        cursor.close()

    def load_telemetry_data(self, table_name: str = None,
                            time_start: str = None,
                            time_end: str = None,
                            limit: int = None) -> pd.DataFrame:
        """
        从数据库加载遥测数据

        Parameters:
        -----------
        table_name : str
            表名，如果为None则自动查找能源相关表
        time_start : str
            起始时间，格式如 '2026-01-01 00:00:00'
        time_end : str
            结束时间
        limit : int
            限制返回行数

        Returns:
        --------
        pd.DataFrame
            遥测数据
        """
        if not self.conn:
            if not self.connect_db():
                return pd.DataFrame()

        cursor = self.conn.cursor()

        # 如果没指定表名，自动查找
        if table_name is None:
            cursor.execute("SHOW TABLES")
            tables = cursor.fetchall()

            # 查找包含遥测数据的表
            energy_tables = []
            for t in tables:
                tname = t[0].lower()
                if any(kw in tname for kw in ['telemetry', '遥测', 'power', 'energy', 'eps', 'satellite']):
                    energy_tables.append(t[0])

            if energy_tables:
                table_name = energy_tables[0]
                print(f"\n🔍 自动检测到能源相关表: {table_name}")
            else:
                print("⚠️  未找到能源相关表，请手动指定表名")
                self.explore_database()
                return pd.DataFrame()

        # 构建查询
        query = f"SELECT * FROM {table_name}"
        conditions = []

        if time_start:
            # 需要找到时间列名
            cursor.execute(f"DESCRIBE TABLE {table_name}")
            columns = cursor.fetchall()
            time_col = None
            for col in columns:
                if any(kw in col[0].lower() for kw in ['time', 'timestamp', 'datetime']):
                    time_col = col[0]
                    break

            if time_col:
                conditions.append(f"{time_col} >= '{time_start}'")
            if time_end:
                conditions.append(f"{time_col} <= '{time_end}'")

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        if limit:
            query += f" LIMIT {limit}"

        print(f"\n正在查询数据...")
        print(f"SQL: {query[:200]}...")

        try:
            # 使用pandas读取
            cursor.execute(query)
            columns = [col[0] for col in cursor.description]
            data = cursor.fetchall()

            df = pd.DataFrame(data, columns=columns)
            print(f"✅ 成功加载 {len(df):,} 条数据，共 {len(df.columns)} 列")

            # 显示前几行
            print(f"\n数据预览（前5行）:")
            print(df.head())

            self.telemetry_data = df
            return df

        except Exception as e:
            print(f"❌ 数据查询失败: {e}")
            return pd.DataFrame()

    def query_energy_params(self, param_codes: List[str] = None,
                            table_name: str = None) -> pd.DataFrame:
        """
        查询指定能源参数的数据

        Parameters:
        -----------
        param_codes : List[str]
            遥测代号列表，如 ['TMD003', 'TMD016', 'TMD019']
        table_name : str
            表名

        Returns:
        --------
        pd.DataFrame
        """
        if not self.conn:
            if not self.connect_db():
                return pd.DataFrame()

        # 默认查询所有能源相关参数
        if param_codes is None:
            param_codes = [
                # 太阳阵
                'TMD003', 'TMD004', 'TMD005', 'TMD006', 'TMD007', 'TMD008',  # 帆板电压
                'TMD009',  # 总电流
                'TMD010', 'TMD011', 'TMD012', 'TMD013', 'TMD014', 'TMD015',  # 帆板电流
                # 母线
                'TMD016', 'TMD017', 'TMD018',
                # 蓄电池
                'TMD019', 'TMD080',
            ]

        cursor = self.conn.cursor()

        # 查找包含这些参数的表
        if table_name is None:
            cursor.execute("SHOW TABLES")
            tables = cursor.fetchall()
            for t in tables:
                tname = t[0].lower()
                if any(kw in tname for kw in ['telemetry', '遥测', 'power', 'energy']):
                    table_name = t[0]
                    break

        # 查找参数列名
        cursor.execute(f"DESCRIBE TABLE {table_name}")
        columns = cursor.fetchall()
        col_names = [c[0] for c in columns]

        # 构建查询
        select_cols = ['*']  # 默认全选
        # 查找参数列
        param_cols = []
        for param in param_codes:
            for col in col_names:
                if param.lower() in col.lower():
                    param_cols.append(col)
                    break

        if param_cols:
            select_cols = list(set(param_cols))

        query = f"SELECT {', '.join(select_cols)} FROM {table_name} LIMIT 10000"
        print(f"\n查询能源参数: {param_codes[:5]}...")

        try:
            cursor.execute(query)
            columns = [col[0] for col in cursor.description]
            data = cursor.fetchall()
            df = pd.DataFrame(data, columns=columns)
            print(f"✅ 查询到 {len(df)} 条数据")
            return df
        except Exception as e:
            print(f"❌ 查询失败: {e}")
            return pd.DataFrame()


class SatelliteEnergyBalancer:
    """卫星能源平衡评估器（与之前相同，此处简化）"""

    def __init__(self, satellite_id: str = "01星"):
        self.satellite_id = satellite_id
        self.telemetry_data = None
        self.metrics = {}

        self.design_params = {
            'solar_panel_rated_power': 200.0,
            'solar_array_rated_power': 1200.0,
            'battery_capacity': 100.0,
            'bus_voltage_nominal': 28.0,
            'battery_soc_initial': 0.95,
        }

    def load_telemetry_data(self, data: pd.DataFrame):
        """加载遥测数据"""
        self.telemetry_data = data.copy()
        print(f"已加载 {len(data)} 条遥测数据")

    def calculate_solar_power(self) -> pd.DataFrame:
        """计算太阳阵发电功率"""
        df = self.telemetry_data.copy()

        # 尝试找到太阳阵电压电流列
        # 遥测代号映射：A~F 板电压 TMD003~TMD008，电流 TMD010~TMD015
        # （TMD009 是太阳阵总电流，不是帆板电流，勿混用）
        panels = ['A', 'B', 'C', 'D', 'E', 'F']
        total_power = pd.Series(0.0, index=df.index)

        for panel in panels:
            idx = panels.index(panel)
            v_code = f'tmd{3 + idx:03d}'    # TMD003..TMD008
            i_code = f'tmd{10 + idx:03d}'   # TMD010..TMD015
            # 查找电压列
            v_col = None
            i_col = None
            for col in df.columns:
                if v_col is None and (v_code in col.lower()
                                      or f'v_panel_{panel.lower()}' in col.lower()):
                    v_col = col
                if i_col is None and (i_code in col.lower()
                                      or f'i_panel_{panel.lower()}' in col.lower()):
                    i_col = col

            if v_col and i_col:
                try:
                    panel_power = pd.to_numeric(df[v_col], errors='coerce') * pd.to_numeric(df[i_col], errors='coerce')
                    total_power += panel_power.fillna(0)
                except Exception:
                    pass

        df['P_solar_total'] = total_power
        return df

    def calculate_metrics(self) -> Dict:
        """计算评估指标"""
        if self.telemetry_data is None:
            return {}

        df = self.telemetry_data.copy()
        metrics = {}

        # 尝试计算发电功率
        solar_df = self.calculate_solar_power()
        if 'P_solar_total' in solar_df.columns:
            metrics['avg_solar_power'] = solar_df['P_solar_total'].mean()
            metrics['max_solar_power'] = solar_df['P_solar_total'].max()

        # 尝试计算母线参数
        for col in df.columns:
            if 'tmd016' in col.lower():  # 母线电压
                bus_voltage = pd.to_numeric(df[col], errors='coerce')
                metrics['bus_voltage_avg'] = bus_voltage.mean()
                metrics['bus_voltage_std'] = bus_voltage.std()
            if 'tmd017' in col.lower():  # 母线电流
                bus_current = pd.to_numeric(df[col], errors='coerce')
                metrics['bus_current_avg'] = bus_current.mean()

        if 'bus_voltage_avg' in metrics and 'bus_current_avg' in metrics:
            metrics['avg_load_power'] = metrics['bus_voltage_avg'] * metrics['bus_current_avg']

        self.metrics = metrics
        return metrics

    def generate_report(self) -> str:
        """生成评估报告"""
        if not self.metrics:
            self.calculate_metrics()

        report = f"""# 三体星{self.satellite_id} 能源系统评估报告

**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**数据来源**: ClickHouse数据库 ({self.design_params.get('database', 'chdb_3body.master_satellite_01')})

## 1. 发电系统

"""
        if 'avg_solar_power' in self.metrics:
            report += f"""- 平均发电功率: {self.metrics['avg_solar_power']:.2f} W
- 最大发电功率: {self.metrics['max_solar_power']:.2f} W
- 太阳阵利用率: {self.metrics['avg_solar_power'] / self.design_params['solar_array_rated_power'] * 100:.1f}%
"""
        else:
            report += "- 发电功率数据暂不可用\n"

        report += "\n## 2. 配电系统\n"
        if 'bus_voltage_avg' in self.metrics:
            report += f"""- 母线平均电压: {self.metrics['bus_voltage_avg']:.2f} V
- 母线电压标准差: {self.metrics.get('bus_voltage_std', 0):.3f} V
"""
        if 'bus_current_avg' in self.metrics:
            report += f"- 母线平均电流: {self.metrics['bus_current_avg']:.2f} A\n"
        if 'avg_load_power' in self.metrics:
            report += f"- 平均负载功率: {self.metrics['avg_load_power']:.2f} W\n"

        report += "\n## 3. 评估结论\n"
        if 'avg_solar_power' in self.metrics and 'avg_load_power' in self.metrics:
            margin = (self.metrics['avg_solar_power'] - self.metrics['avg_load_power']) / self.metrics['avg_load_power'] * 100
            report += f"- 功率裕度: {margin:.1f}%\n"
            if margin > 20:
                report += "- 状态: ✅ 能源系统运行正常\n"
            elif margin > 0:
                report += "- 状态: ⚠️ 功率裕度偏低，建议关注\n"
            else:
                report += "- 状态: ❌ 功率不足，蓄电池处于放电状态\n"

        return report


def main():
    """主函数：演示完整的数据库连接和分析流程"""
    from pathlib import Path
    print("=" * 60)
    print("三体星01星 能源平衡定量评估工具（v1 · 已 deprecated，正式评估请用 s3_run.py）")
    print("=" * 60)

    # 1. 初始化分析器（口令等一律走环境变量/默认值，不在此硬编码）
    analyzer = ClickHouseEnergyAnalyzer()

    # 2. 连接数据库
    if not analyzer.connect_db():
        print("\n❌ 无法连接数据库，请检查:")
        print("   - 网络是否可达（是否在内网环境）")
        print("   - 防火墙设置")
        print("   - 数据库服务是否运行")
        print("   - 口令是否已配置（环境变量 CH_PASSWORD 或 .env）")
        return

    # 3. 探索数据库结构
    analyzer.explore_database()

    # 4. 加载遥测数据
    print("\n" + "=" * 60)
    print("加载遥测数据")
    print("=" * 60)

    # 尝试加载数据（自动查找能源相关表）
    telemetry_df = analyzer.load_telemetry_data(
        time_start='2026-01-01 00:00:00',
        time_end='2026-01-02 00:00:00',
        limit=10000
    )

    if telemetry_df.empty:
        print("⚠️  未加载到数据")
        analyzer.close_db()
        return

    # 5. 查询能源参数
    print("\n" + "=" * 60)
    print("查询能源关键参数")
    print("=" * 60)

    energy_df = analyzer.query_energy_params()

    # 6. 能源平衡评估
    print("\n" + "=" * 60)
    print("运行能源平衡评估")
    print("=" * 60)

    evaluator = SatelliteEnergyBalancer(satellite_id="01星")

    # 合并数据
    if not energy_df.empty:
        combined_df = pd.concat([telemetry_df, energy_df], axis=1).drop_duplicates()
    else:
        combined_df = telemetry_df

    evaluator.load_telemetry_data(combined_df)
    metrics = evaluator.calculate_metrics()

    # 7. 生成报告
    print("\n" + "=" * 60)
    print("生成评估报告")
    print("=" * 60)

    report = evaluator.generate_report()
    print(report)

    # 保存报告与数据（输出到本脚本所在目录的 out/，不再硬编码绝对路径）
    outdir = Path(__file__).resolve().parent / 'out'
    outdir.mkdir(parents=True, exist_ok=True)
    output_path = outdir / 'energy_report_from_db.md'
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n✅ 报告已保存至: {output_path}")

    # 保存数据到CSV
    csv_path = outdir / 'telemetry_data.csv'
    combined_df.to_csv(csv_path, index=False)
    print(f"✅ 遥测数据已保存至: {csv_path}")

    # 8. 关闭连接
    analyzer.close_db()

    print("\n" + "=" * 60)
    print("分析完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
