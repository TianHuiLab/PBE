#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库连接测试脚本
用于验证能否成功连接到三体星ClickHouse数据库

使用方法:
    python "04_数据库连接测试脚本.py"
"""

import sys
import os
import re


def _config():
    """连接配置：环境变量优先，回退到内网默认值；口令不设默认值（见 ch_http 说明）。"""
    return {
        'host': os.environ.get('CH_HOST', '10.200.56.174'),
        'port': int(os.environ.get('CH_PORT', '9000')),
        'user': os.environ.get('CH_USER', 'default'),
        'password': os.environ.get('CH_PASSWORD', ''),
        'database': os.environ.get('CH_DB', 'chdb_3body'),
        'table': os.environ.get('CH_TABLE', 'master_satellite_01'),
        'secure': os.environ.get('CH_NATIVE_SECURE', '').lower() in ('1', 'true', 'yes'),
    }


def test_connection():
    """测试数据库连接"""
    print("=" * 60)
    print("三体星01星 ClickHouse数据库连接测试")
    print("=" * 60)
    print()

    # 数据库配置
    config = _config()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', config['table']):
        print('指定表名不是合法标识符')
        return False
    if (config['password'] and not config['secure'] and
            os.environ.get('CH_ALLOW_PLAINTEXT', '').lower() not in ('1', 'true', 'yes')):
        print('携带口令的原生明文连接已禁用；请使用 TLS 端点或加密隧道')
        return False

    print("连接配置:")
    for k, v in config.items():
        # 口令一律脱敏，不打印明文
        shown = ('******' if v else '(未设置，请配置 CH_PASSWORD)') if k == 'password' else v
        print(f"  {k}: {shown}")
    print()

    # 1. 检查clickhouse-driver是否安装
    print("步骤1: 检查clickhouse-driver包...")
    try:
        from clickhouse_driver import connect
        print("  ✅ clickhouse-driver 已安装")
    except ImportError:
        print("  ❌ clickhouse-driver 未安装")
        print("  请运行: pip install clickhouse-driver")
        return False

    # 2. 测试网络连接
    print("\n步骤2: 测试网络连接...")
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((config['host'], config['port']))
        sock.close()

        if result == 0:
            print(f"  ✅ 网络连通: {config['host']}:{config['port']}")
        else:
            print(f"  ❌ 网络不通: {config['host']}:{config['port']}")
            print("  可能原因:")
            print("    - 不在内网环境")
            print("    - 防火墙阻止")
            print("    - 数据库服务未启动")
            return False
    except Exception as e:
        print(f"  ❌ 网络测试失败: {e}")
        return False

    # 3. 测试数据库连接
    print("\n步骤3: 测试数据库连接...")
    try:
        conn = connect(
            host=config['host'],
            port=config['port'],
            user=config['user'],
            password=config['password'],
            database=config['database'],
            secure=config['secure'],
            settings={'connect_timeout': 10}
        )
        print(f"  ✅ 数据库连接成功")
        print(f"  数据库: {config['database']}")
    except Exception as e:
        print(f"  ❌ 数据库连接失败: {e}")
        return False

    # 4. 查询测试
    print("\n步骤4: 测试数据查询...")
    try:
        cursor = conn.cursor()

        # 查询表列表
        cursor.execute("SHOW TABLES")
        tables = cursor.fetchall()
        print(f"  ✅ 查询成功，数据库中有 {len(tables)} 张表")

        if tables:
            print(f"\n  表列表:")
            for i, table in enumerate(tables[:10], 1):  # 只显示前10个
                print(f"    {i}. {table[0]}")
            if len(tables) > 10:
                print(f"    ... (共 {len(tables)} 张表)")

        # 只检查指定表的结构，避免对任意大表做全量 COUNT。
        if config['table'] in {row[0] for row in tables}:
            cursor.execute(f"DESCRIBE TABLE `{config['table']}`")
            columns = cursor.fetchall()
            print(f"\n  指定表 '{config['table']}' 的字段数: {len(columns)}")
        else:
            print(f"\n  指定表 '{config['table']}' 不在当前数据库的表列表中")

        conn.close()
        print("\n" + "=" * 60)
        print("✅ 所有测试通过！数据库可以正常访问。")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"  ❌ 查询测试失败: {e}")
        conn.close()
        return False


def print_usage():
    """打印使用说明"""
    print("""
如果测试失败，请按以下步骤排查:

1. 确认网络环境:
   - 必须在能够访问 10.200.56.174 的内网环境中运行
   - 检查是否需要VPN连接

2. 安装依赖:
   pip install clickhouse-driver pandas numpy

3. 检查防火墙:
   - 确认9000端口未被防火墙阻止

4. 联系数据库管理员:
   - 确认数据库服务是否正常运行
   - 确认用户权限是否正确

正式评估请按 README 运行 s1_fetch.py → s3_run.py → gen_charts.py。
""")


if __name__ == "__main__":
    success = test_connection()

    if not success:
        print("\n" + "=" * 60)
        print("❌ 连接测试失败")
        print("=" * 60)
        print_usage()
        sys.exit(1)
    else:
        print("\n下一步:")
        print('  正式评估请按 README 运行 s1_fetch.py → s3_run.py → gen_charts.py')
        sys.exit(0)
