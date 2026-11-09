#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Solar-array power providers for the future-task forecast.

An orbit/attitude/power algorithm is loaded as a trusted local Python module.
The module exports ``build_profile(start, end, step_seconds[, snapshot])`` and
returns records with ``start``, ``end`` and ``power_W``.
"""

from __future__ import annotations

import importlib.util
import inspect
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from unit_power_model import (DEFAULT_TIMEZONE, UnitPowerModelError,
                              _compact_time, _require)


class EclipseOnlySolarProvider:
    """Zero solar input only when the current telemetry says shadow."""

    source = "eclipse_only_zero"

    def profile(self, *, start: pd.Timestamp, end: pd.Timestamp,
                step_seconds: int, snapshot: Any,
                phase_segments: list[dict] | None = None) -> list[dict]:
        _require(getattr(snapshot, "phase", None) in {"阴影区", "地影"},
                 "当前不在阴影区，不能使用 eclipse-only 太阳阵功率方案")
        _require(phase_segments is not None and all(
            str(item.get("phase")) in {"eclipse", "阴影", "地影", "阴影区"}
            for item in phase_segments),
            "eclipse-only 方案要求 phase_segments 全部为 eclipse")
        _require(start < end, "太阳阵功率窗口无效")
        return [{"start": start, "end": end, "power_W": 0.0}]


class ModuleSolarPowerProvider:
    """Load an external orbit/attitude/solar-power profile builder."""

    source = "external_solar_module"

    def __init__(self, module_file: str | Path):
        path = Path(module_file).resolve()
        _require(path.is_file(), f"太阳阵功率模块不存在: {path}")
        spec = importlib.util.spec_from_file_location(
            f"forecast_solar_provider_{path.stem}", path)
        _require(spec is not None and spec.loader is not None,
                 f"无法加载太阳阵功率模块: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        builder = getattr(module, "build_profile", None)
        _require(callable(builder),
                 "太阳阵功率模块必须导出 build_profile(start, end, step_seconds)")
        self.path = path
        self.builder = builder
        self.parameters = inspect.signature(builder).parameters

    def profile(self, *, start: pd.Timestamp, end: pd.Timestamp,
                step_seconds: int, snapshot: Any,
                phase_segments: list[dict] | None = None) -> list[dict]:
        kwargs = {
            "start": start,
            "end": end,
            "step_seconds": step_seconds,
        }
        if "snapshot" in self.parameters:
            kwargs["snapshot"] = snapshot
        if "phase_segments" in self.parameters:
            kwargs["phase_segments"] = phase_segments
        raw = self.builder(**kwargs)
        if isinstance(raw, pd.DataFrame):
            records = raw.to_dict(orient="records")
        else:
            records = raw
        _require(isinstance(records, list) and records,
                 "太阳阵功率模块必须返回非空记录列表或 DataFrame")
        result = []
        for index, item in enumerate(records):
            _require(isinstance(item, dict)
                     and {"start", "end", "power_W"} <= set(item),
                     f"太阳阵功率记录[{index}]缺少 start/end/power_W")
            _require(isinstance(item["power_W"], (int, float))
                     and not isinstance(item["power_W"], bool)
                     and float(item["power_W"]) >= 0,
                     f"太阳阵功率记录[{index}].power_W 必须为非负数")
            result.append({
                "start": item["start"],
                "end": item["end"],
                "power_W": float(item["power_W"]),
            })
        return result


class PolynomialSolarPowerProvider:
    """Evaluate a historical solar-array polynomial by phase."""

    source = "historical_polynomial"

    def __init__(self, model: dict, model_path: str | Path | None = None):
        _require(isinstance(model, dict), "太阳阵模型必须为对象")
        _require(model.get("model_type") in {
            "polynomial", "constant", "load_aware_linear"},
            "太阳阵模型类型必须为 polynomial、constant 或 load_aware_linear")
        self.model = dict(model)
        self.bus_transfer_efficiency = float(
            self.model.get("bus_transfer_efficiency", 1.0))
        _require(np.isfinite(self.bus_transfer_efficiency)
                 and 0 < self.bus_transfer_efficiency <= 1,
                 "bus_transfer_efficiency 必须在 (0,1]")
        margin = self.model.get("load_following_charge_margin_W")
        _require(margin is None or (
            isinstance(margin, (int, float))
            and not isinstance(margin, bool)
            and np.isfinite(float(margin))
            and float(margin) >= 0),
            "load_following_charge_margin_W 必须为非负有限数")
        self.load_following_charge_margin_W = (
            float(margin) if margin is not None else None)
        self.warnings = []
        self.path = Path(model_path).resolve() if model_path else None
        if self.path is not None:
            self.model_file_sha256 = hashlib.sha256(
                self.path.read_bytes()).hexdigest()
        else:
            self.model_file_sha256 = None

    def profile(self, *, start: pd.Timestamp, end: pd.Timestamp,
                step_seconds: int, snapshot: Any,
                phase_segments: list[dict],
                load_segments: list[dict] | None = None) -> list[dict]:
        _require(start < end, "太阳阵功率窗口无效")
        _require(isinstance(phase_segments, list) and phase_segments,
                 "太阳阵多项式需要 phase_segments")
        self.warnings = []
        normalized = []
        for index, raw in enumerate(phase_segments):
            segment_start = _compact_time(
                raw.get("start"), start, DEFAULT_TIMEZONE,
                f"phase_segments[{index}].start")
            segment_end = _compact_time(
                raw.get("end"), start, DEFAULT_TIMEZONE,
                f"phase_segments[{index}].end")
            phase = str(raw.get("phase", "")).strip().lower()
            if phase in {"sun", "日照", "阳照", "阳照区"}:
                phase = "sun"
            elif phase in {"eclipse", "地影", "阴影", "阴影区"}:
                phase = "eclipse"
            else:
                raise UnitPowerModelError(
                    f"phase_segments[{index}].phase 必须为 sun/eclipse")
            _require(segment_start < segment_end,
                     f"phase_segments[{index}] start 必须早于 end")
            normalized.append({
                "start": segment_start,
                "end": segment_end,
                "phase": phase,
            })
        _require(normalized[0]["start"] <= start
                 and normalized[-1]["end"] >= end,
                 "phase_segments 未覆盖完整预测窗口")

        coefficients = np.asarray(
            self.model.get("coefficients_W_per_hour_power", []), dtype=float)
        if self.model["model_type"] == "polynomial":
            _require(len(coefficients) >= 2
                     and np.all(np.isfinite(coefficients)),
                     "太阳阵多项式系数无效")
        if self.model["model_type"] == "load_aware_linear":
            feature_model = self.model.get("feature_model", {})
            feature_names = feature_model.get("features", [])
            feature_coefficients = np.asarray(
                feature_model.get("coefficients", []), dtype=float)
            allowed_features = {
                "elapsed_hours", "load_W",
                "task_unit_power_W", "active_unit_count"}
            _require(feature_names and set(feature_names) <= allowed_features
                     and len(feature_coefficients) == len(feature_names)
                     and np.all(np.isfinite(feature_coefficients)),
                     "负载感知太阳阵模型系数无效")
            _require(load_segments is not None and load_segments,
                     "负载感知太阳阵模型需要未来负载段")
            normalized_loads = []
            for index, item in enumerate(load_segments):
                load_start = pd.Timestamp(item.get("start"))
                load_end = pd.Timestamp(item.get("end"))
                if load_start.tzinfo is None:
                    load_start = load_start.tz_localize(DEFAULT_TIMEZONE)
                if load_end.tzinfo is None:
                    load_end = load_end.tz_localize(DEFAULT_TIMEZONE)
                _require(load_start < load_end,
                         f"load_segments[{index}] start 必须早于 end")
                normalized_loads.append({
                    **item,
                    "start": load_start,
                    "end": load_end,
                    "power_W": float(item["power_W"]),
                    "task_unit_power_W": float(
                        item.get("task_unit_power_W", 0.0)),
                    "active_unit_count": float(
                        item.get("active_unit_count", 0.0)),
                })
        else:
            feature_model = None
            normalized_loads = []
        support_low, support_high = self.model.get("support_hours", [0, 0])
        result = []
        for segment in normalized:
            seg_start = max(segment["start"], start)
            seg_end = min(segment["end"], end)
            if seg_start >= seg_end:
                continue
            if segment["phase"] == "eclipse":
                result.append({
                    "start": seg_start,
                    "end": seg_end,
                    "power_W": 0.0,
                    "phase": "eclipse",
                })
                continue
            cursor = seg_start
            while cursor < seg_end:
                step_end = min(
                    cursor + pd.Timedelta(seconds=step_seconds), seg_end)
                midpoint = cursor + (step_end - cursor) / 2
                elapsed_hours = (
                    midpoint - segment["start"]).total_seconds() / 3600
                clipped = min(max(elapsed_hours, float(support_low)),
                              float(support_high))
                if clipped != elapsed_hours:
                    self.warnings.append(
                        "sun polynomial elapsed time exceeds training support; "
                        "clamped for extrapolation safety")
                if self.model["model_type"] == "load_aware_linear":
                    load_item = next((
                        item for item in normalized_loads
                        if item["start"] <= midpoint < item["end"]), None)
                    _require(load_item is not None,
                             f"未来负载段未覆盖 {midpoint.isoformat()}")
                    available_features = {
                        "elapsed_hours": clipped,
                        "load_W": load_item["power_W"],
                        "task_unit_power_W":
                            load_item["task_unit_power_W"],
                        "active_unit_count":
                            load_item["active_unit_count"],
                    }
                    feature_values = np.asarray(
                        [available_features[name]
                         for name in feature_names], dtype=float)
                    power = (
                        float(feature_model.get("intercept", 0.0))
                        + float(np.dot(feature_coefficients, feature_values)))
                elif self.model["model_type"] == "polynomial":
                    power = float(np.polynomial.polynomial.polyval(
                        clipped, coefficients))
                else:
                    power = float(self.model["constant_power_W"])
                power = max(0.0, power) * self.bus_transfer_efficiency
                result.append({
                    "start": cursor,
                    "end": step_end,
                    "power_W": power,
                    "phase": "sun",
                    "elapsed_hours": clipped,
                })
                cursor = step_end
        if self.bus_transfer_efficiency != 1.0:
            self.warnings.append(
                "sun power scaled by empirical bus transfer efficiency "
                f"{self.bus_transfer_efficiency:.6f}")
        self.warnings = list(dict.fromkeys(self.warnings))
        return result
