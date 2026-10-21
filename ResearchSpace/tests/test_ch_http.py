#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ch_http（ClickHouse HTTP 客户端）单元测试——mock urllib，无需真实数据库。

覆盖：JSONEachRow 解析、TabSeparated/JSON 透传、口令走请求头不进 URL、
HTTP 错误与连接错误的异常转换、.env 加载与环境变量优先级。
"""
import importlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ch_http


class _Resp(io.BytesIO):
    """最小 urllib 响应替身（支持 with 语句）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class QueryParsingTests(unittest.TestCase):
    def test_jsoneachrow_parsing(self):
        body = b'{"a": 1, "b": "x"}\n{"a": 2, "b": "y"}\n'
        with mock.patch.object(ch_http.urllib.request, 'urlopen',
                               return_value=_Resp(body)):
            rows = ch_http.query("SELECT 1")
        self.assertEqual(rows, [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])

    def test_tabseparated_passthrough(self):
        with mock.patch.object(ch_http.urllib.request, 'urlopen',
                               return_value=_Resp(b"default\nsystem\n")):
            out = ch_http.query("SHOW DATABASES", fmt='TabSeparated')
        self.assertEqual(out, "default\nsystem\n")

    def test_json_format(self):
        with mock.patch.object(ch_http.urllib.request, 'urlopen',
                               return_value=_Resp(b'{"meta": [], "data": []}')):
            out = ch_http.query("SELECT 1", fmt='JSON')
        self.assertEqual(out, {"meta": [], "data": []})


class CredentialHandlingTests(unittest.TestCase):
    def _capture_request(self, body=b'{"ok": 1}\n'):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['req'] = req
            return _Resp(body)

        with mock.patch.object(ch_http.urllib.request, 'urlopen', fake_urlopen):
            ch_http.query("SELECT 1", password='s3cret!')
        return captured['req']

    def test_password_not_in_url(self):
        req = self._capture_request()
        self.assertNotIn('s3cret', req.full_url)
        self.assertNotIn('password', req.full_url)
        self.assertNotIn('user=', req.full_url)

    def test_password_sent_via_headers(self):
        req = self._capture_request()
        self.assertEqual(req.get_header('X-clickhouse-user'), 'default')
        self.assertEqual(req.get_header('X-clickhouse-key'), 's3cret!')

    def test_database_and_format_in_url(self):
        req = self._capture_request()
        self.assertIn('database=chdb_3body', req.full_url)
        self.assertIn('default_format=JSONEachRow', req.full_url)


class ErrorHandlingTests(unittest.TestCase):
    def test_http_error_raises_clickhouse_error(self):
        err = HTTPError('http://x/', 403, 'Forbidden', {}, io.BytesIO(b'denied'))
        with mock.patch.object(ch_http.urllib.request, 'urlopen', side_effect=err):
            with self.assertRaises(ch_http.ClickHouseError) as ctx:
                ch_http.query("SELECT 1")
        self.assertIn('403', str(ctx.exception))
        self.assertIn('denied', str(ctx.exception))

    def test_connection_error_raises_clickhouse_error(self):
        with mock.patch.object(ch_http.urllib.request, 'urlopen',
                               side_effect=URLError('no route')):
            with self.assertRaises(ch_http.ClickHouseError) as ctx:
                ch_http.query("SELECT 1")
        self.assertIn('连接失败', str(ctx.exception))


class EnvFileTests(unittest.TestCase):
    def test_env_file_loaded_without_overwriting(self):
        with mock.patch.dict(os.environ), \
             mock.patch.object(ch_http, 'ENV_FILE',
                               Path(__file__).parent / '_no_such.env'):
            os.environ.pop('CH_TEST_VAR', None)
            ch_http._load_env_file()
            self.assertIsNone(os.environ.get('CH_TEST_VAR'))

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / '.env'
            env_path.write_text(
                '# comment\nCH_TEST_VAR=from_file\nCH_TEST_QUOTED="q"\n',
                encoding='utf-8')
            os.environ['CH_TEST_VAR'] = 'from_env'   # 已存在 → 不被覆盖
            os.environ.pop('CH_TEST_QUOTED', None)
            with mock.patch.object(ch_http, 'ENV_FILE', env_path):
                ch_http._load_env_file()
            self.assertEqual(os.environ['CH_TEST_VAR'], 'from_env')
            self.assertEqual(os.environ['CH_TEST_QUOTED'], 'q')

    def test_module_constants_come_from_env(self):
        with mock.patch.dict(os.environ, {
                'CH_HOST': '127.0.0.1', 'CH_PORT': '9999',
                'CH_USER': 'tester', 'CH_PASSWORD': 'pw', 'CH_DB': 'chdb_test'}):
            reloaded = importlib.reload(ch_http)
        self.assertEqual((reloaded.HOST, reloaded.PORT), ('127.0.0.1', 9999))
        self.assertEqual((reloaded.USER, reloaded.PASSWORD), ('tester', 'pw'))
        self.assertEqual(reloaded.DB, 'chdb_test')
        importlib.reload(ch_http)  # 恢复默认状态


if __name__ == '__main__':
    unittest.main()
