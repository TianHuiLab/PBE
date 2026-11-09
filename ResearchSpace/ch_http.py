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
SCHEME = os.environ.get('CH_SCHEME', 'http').lower()
ALLOW_PLAINTEXT = os.environ.get('CH_ALLOW_PLAINTEXT', '').lower() in ('1', 'true', 'yes')


class ClickHouseError(Exception):
    pass


def _request(sql, database, user, password, host, port, fmt, scheme,
             allow_plaintext, params, max_execution_time):
    if scheme not in ('http', 'https'):
        raise ClickHouseError('CH_SCHEME 只能是 http 或 https')
    if scheme == 'http' and password and not allow_plaintext:
        raise ClickHouseError('携带口令的明文 HTTP 已禁用；配置 HTTPS 或显式设置 CH_ALLOW_PLAINTEXT=1')
    options = {'database': database, 'default_format': fmt,
               'max_execution_time': str(max_execution_time)}
    for name, value in (params or {}).items():
        if not name.isidentifier():
            raise ValueError(f'无效查询参数名: {name}')
        options['param_' + name] = str(value)
    url = f'{scheme}://{host}:{port}/?' + urllib.parse.urlencode(options)
    return urllib.request.Request(url, data=sql.encode('utf-8'), headers={
        'X-ClickHouse-User': user, 'X-ClickHouse-Key': password})


def _raise_http_error(error):
    detail = error.read().decode('utf-8', 'replace')[:2000]
    raise ClickHouseError(f'HTTP {error.code}: {detail}') from error


def query_iter(sql, database=DB, user=USER, password=PASSWORD, host=HOST,
               port=PORT, timeout=60, scheme=SCHEME,
               allow_plaintext=ALLOW_PLAINTEXT, params=None, max_execution_time=60):
    """逐行解析 JSONEachRow；调用方须完整迭代或关闭生成器。"""
    req = _request(sql, database, user, password, host, port, 'JSONEachRow',
                   scheme, allow_plaintext, params, max_execution_time)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp:
                if line.strip():
                    yield json.loads(line)
    except urllib.error.HTTPError as error:
        _raise_http_error(error)
    except (urllib.error.URLError, OSError) as error:
        raise ClickHouseError(f'连接失败: {error}') from error


def query(sql, database=DB, user=USER, password=PASSWORD, host=HOST, port=PORT,
          timeout=60, fmt='JSONEachRow', scheme=SCHEME,
          allow_plaintext=ALLOW_PLAINTEXT, params=None, max_execution_time=60):
    """执行 SQL；JSONEachRow 兼容旧列表接口，大结果优先使用 query_iter。"""
    if fmt == 'JSONEachRow':
        return list(query_iter(sql, database, user, password, host, port, timeout,
                               scheme, allow_plaintext, params, max_execution_time))
    req = _request(sql, database, user, password, host, port, fmt, scheme,
                   allow_plaintext, params, max_execution_time)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
    except urllib.error.HTTPError as error:
        _raise_http_error(error)
    except (urllib.error.URLError, OSError) as error:
        raise ClickHouseError(f'连接失败: {error}') from error
    return json.loads(body) if fmt == 'JSON' else body


if __name__ == '__main__':
    print("== SHOW DATABASES ==")
    try:
        rows = query("SHOW DATABASES", fmt='TabSeparated')
        print(rows)
    except ClickHouseError as e:
        print("ERR", e)
