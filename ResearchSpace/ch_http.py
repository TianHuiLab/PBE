#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小型 ClickHouse HTTP 客户端（仅用 stdlib urllib，通过 HTTP 8123 接口访问）。
用于在无 clickhouse-driver 环境下查询集群。

凭据配置（按优先级，均不写死在本文件中）：
  1. 环境变量 CH_HOST / CH_PORT / CH_USER / CH_PASSWORD / CH_DB；
  2. 同目录 .env 文件（KEY=VALUE 每行一条，见 .env.example；.env 已被 .gitignore 排除）；
  3. 非敏感默认值（主机/端口/用户/库名）。口令无默认值——未配置时查询会因认证
     失败报错，这是有意的：避免把生产口令留在源码里。
"""
import urllib.request
import urllib.parse
import urllib.error
import json
import os
from pathlib import Path

# .env 路径提取为模块常量，便于测试注入
ENV_FILE = Path(__file__).resolve().parent / '.env'


def _load_env_file():
    """读取 .env（KEY=VALUE 每行一条），不覆盖已存在的环境变量。"""
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

HOST = os.environ.get('CH_HOST', '10.200.56.174')
PORT = int(os.environ.get('CH_PORT', '8123'))
USER = os.environ.get('CH_USER', 'default')
PASSWORD = os.environ.get('CH_PASSWORD', '')  # 无默认口令，见模块 docstring
DB = os.environ.get('CH_DB', 'chdb_3body')


class ClickHouseError(Exception):
    pass


def query(sql, database=DB, user=USER, password=PASSWORD, host=HOST, port=PORT,
          timeout=60, fmt='JSONEachRow'):
    """执行 SQL，返回解析后的结果。fmt 支持 JSONEachRow / JSON / TabSeparated。

    口令经 X-ClickHouse-User / X-ClickHouse-Key 请求头传递，不进入 URL，
    避免被代理、访问日志或错误回显记录。
    """
    base = f"http://{host}:{port}/"
    params = {
        'database': database,
        'default_format': fmt,
        'max_execution_time': '60',
    }
    url = base + '?' + urllib.parse.urlencode(params)
    data = sql.encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={
        'X-ClickHouse-User': user,
        'X-ClickHouse-Key': password,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:2000]
        raise ClickHouseError(f"HTTP {e.code}: {detail}")
    except Exception as e:
        raise ClickHouseError(f"连接失败: {e}")

    if fmt == 'JSONEachRow':
        rows = []
        for line in body.strip().splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    elif fmt == 'JSON':
        return json.loads(body)
    else:
        return body


if __name__ == '__main__':
    print("== SHOW DATABASES ==")
    try:
        rows = query("SHOW DATABASES", fmt='TabSeparated')
        print(rows)
    except ClickHouseError as e:
        print("ERR", e)
