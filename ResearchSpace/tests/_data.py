"""Helpers for tests that require external telemetry or training data."""
from pathlib import Path
import unittest


def require_local_data(*paths):
    """仅在本地遥测或模型输入文件齐全时运行集成用例。"""
    resolved = tuple(Path(path).resolve() for path in paths)
    missing = [str(path) for path in resolved if not path.is_file()]
    reason = (
        "需要本地遥测、训练数据或报告输入；GitHub CI 不包含这些文件"
        if missing else ""
    )
    return unittest.skipUnless(not missing, reason)
