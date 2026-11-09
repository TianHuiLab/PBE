#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Calibrate per-unit power models and predict battery state.

The calibration input is the minute-level component power proxy emitted by
``s3_run.py``.  The simplified forecast entry accepts only ``time/end/step`` and
a task plan; current telemetry is fetched by a provider and solar power comes
from the historical polynomial, an eclipse-only provider, or a trusted
orbit/power module.  Fit values
describe observed branch power, not independently measured unit energy, and
the battery prediction remains bounded bookkeeping rather than flight-grade
ephemeris/temperature/SOH validation.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from components import evaluate_components
from power_outlier_filter import filter_power_values


SCHEMA_VERSION = "1.0"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_MODEL_NAME = "unit_power_models.json"
DEFAULT_REPORT_NAME = "unit_power_model_report.md"
VALID_GRID_STATES = {"缺测", "未映射", "分钟内切换"}


class UnitPowerModelError(ValueError):
    """Raised when calibration or prediction inputs violate the contract."""


@dataclass(frozen=True)
class CurrentPowerSnapshot:
    """One exact-minute telemetry state used to initialize a forecast."""

    time: pd.Timestamp
    battery_voltage_V: float
    bus_voltage_V: float
    total_load_W: float
    phase: str
    unit_power_W: dict[str, float]
    missing_unit_power_W: dict[str, str]
    provenance: dict


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise UnitPowerModelError(message)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: str | Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _atomic_write_frame(frame: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(path)


def _json_safe(value: Any) -> Any:
    """Convert NumPy/Pandas scalars and reject non-finite JSON numbers."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _parse_time(value: Any, timezone_name: str = DEFAULT_TIMEZONE) -> pd.Timestamp:
    if value is None or value == "":
        raise UnitPowerModelError("时间字段不能为空")
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise UnitPowerModelError(f"无法解析时间: {value!r}") from exc
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(timezone_name)
    else:
        stamp = stamp.tz_convert(timezone_name)
    return stamp


def _read_json(path: str | Path) -> dict:
    path = Path(path)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnitPowerModelError(f"无法读取 JSON: {path}") from exc
    _require(isinstance(result, dict), f"JSON 根节点必须是对象: {path}")
    return result


def _load_component_specs(path: str | Path, satellite_id: str | None = None) -> tuple[dict, list[dict]]:
    config = _read_json(path)
    _require(isinstance(config.get("components"), list), "部件配置必须包含 components 列表")
    if satellite_id is not None:
        _require(config.get("satellite_id") == satellite_id,
                 "部件配置 satellite_id 与请求不一致")
    specs = []
    seen = set()
    for raw in config["components"]:
        _require(isinstance(raw, dict), "部件定义必须是对象")
        unit_id = raw.get("id")
        _require(isinstance(unit_id, str) and re.fullmatch(r"[a-z][a-z0-9_]*", unit_id),
                 "部件 id 必须为小写字母开头的字母、数字或下划线")
        _require(unit_id not in seen, f"重复部件 id: {unit_id}")
        seen.add(unit_id)
        _require(isinstance(raw.get("enabled"), bool), f"{unit_id} 缺少 enabled")
        if raw["enabled"] and raw.get("role") == "load":
            specs.append(dict(raw))
    _require(bool(specs), "配置中没有已启用的 load 部件")
    return config, specs


def _robust_stats(values: np.ndarray) -> dict:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values) & (values >= 0)]
    if len(values) == 0:
        return {"n": 0}
    q = np.quantile(values, [0, .01, .05, .1, .25, .5, .75, .9, .95, .99, 1])
    return {
        "n": int(len(values)),
        "min_W": _round(q[0]),
        "p01_W": _round(q[1]),
        "p05_W": _round(q[2]),
        "p10_W": _round(q[3]),
        "p25_W": _round(q[4]),
        "p50_W": _round(q[5]),
        "p75_W": _round(q[6]),
        "p90_W": _round(q[7]),
        "p95_W": _round(q[8]),
        "p99_W": _round(q[9]),
        "max_W": _round(q[10]),
        "mean_W": _round(float(np.mean(values))),
        "std_W": _round(float(np.std(values, ddof=1))) if len(values) > 1 else 0.0,
        "cv": _round(float(np.std(values, ddof=1) / np.mean(values))
                     if len(values) > 1 and np.mean(values) > 0 else None),
    }


def _two_means(values: np.ndarray, max_iter: int = 100) -> tuple[np.ndarray, np.ndarray] | None:
    """Deterministic 1-D two-means using several robust quantile starts."""
    values = np.asarray(values, dtype=float)
    if len(values) < 4 or np.ptp(values) <= 0:
        return None
    candidates = []
    starts = ((.10, .90), (.50, .95), (.50, .99),
              (.25, .75), (.05, .99), (.10, .99))
    for low_q, high_q in starts:
        centers = np.array(
            [np.quantile(values, low_q), np.quantile(values, high_q)],
            dtype=float)
        if np.unique(centers).size < 2:
            continue
        labels = np.zeros(len(values), dtype=int)
        valid = True
        for _ in range(max_iter):
            new_labels = (np.abs(values[:, None] - centers[None, :])
                          .argmin(axis=1).astype(int))
            if np.array_equal(new_labels, labels):
                break
            labels = new_labels
            new_centers = []
            for index in (0, 1):
                subset = values[labels == index]
                new_centers.append(float(np.median(subset)) if len(subset) else math.nan)
            if not np.all(np.isfinite(new_centers)):
                valid = False
                break
            centers = np.asarray(new_centers)
        if not valid or np.any(np.bincount(labels, minlength=2) == 0):
            continue
        order = np.argsort(centers)
        remap = {int(old): new for new, old in enumerate(order)}
        labels = np.array([remap[int(label)] for label in labels], dtype=int)
        centers = centers[order]
        # Median-centered within-cluster dispersion avoids selecting a split
        # that leaves a wide low-power plateau and a mostly empty outlier bin.
        within = sum(
            float(np.sum((values[labels == index]
                          - np.median(values[labels == index])) ** 2))
            for index in (0, 1))
        candidates.append((within, labels, centers))
    if not candidates:
        return None
    _, labels, centers = min(candidates, key=lambda item: item[0])
    return labels, centers


def _split_high_power_band(
    active_values: np.ndarray,
    source_count: int,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Find the best ratio-separated high tail without local two-means minima."""
    active_values = np.asarray(active_values, dtype=float)
    if len(active_values) < 10:
        return None
    transformed = np.log1p(active_values)
    order = np.argsort(transformed, kind="stable")
    sorted_values = transformed[order]
    cumulative = np.cumsum(sorted_values)
    cumulative_sq = np.cumsum(sorted_values ** 2)
    total = float(cumulative[-1])
    total_sq = float(cumulative_sq[-1])
    best = None
    for split in range(5, len(sorted_values) - 4):
        left_count = split
        right_count = len(sorted_values) - split
        if right_count / source_count < .002:
            continue
        left_sum = float(cumulative[split - 1])
        right_sum = total - left_sum
        left_sse = (
            float(cumulative_sq[split - 1]) - left_sum ** 2 / left_count
        )
        right_sse = (
            total_sq - float(cumulative_sq[split - 1])
            - right_sum ** 2 / right_count
        )
        left_center = left_sum / left_count
        right_center = right_sum / right_count
        left_power = float(np.expm1(left_center))
        right_power = float(np.expm1(right_center))
        ratio = (right_power / left_power
                 if left_power > 0 else 0.0)
        if ratio < 14.0:
            continue
        sse = max(0.0, left_sse + right_sse)
        if best is None or sse < best[0]:
            best = (sse, split, left_center, right_center)
    if best is None:
        return None
    _, split, left_center, right_center = best
    sorted_labels = np.zeros(len(sorted_values), dtype=int)
    sorted_labels[split:] = 1
    labels = np.empty(len(sorted_values), dtype=int)
    labels[order] = sorted_labels
    return labels, np.asarray([left_center, right_center], dtype=float)


def _detect_power_bands(values: np.ndarray) -> dict:
    """Detect a low/active band when separation is materially visible."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values) & (values >= 0)]
    if len(values) == 0:
        return {"kind": "unavailable", "bands": [], "active_mask": np.zeros(0, bool)}

    def band_summary(name, label, subset):
        q05, q50, q95 = np.quantile(subset, [.05, .5, .95])
        return {
            "id": name,
            "label": label,
            "n": int(len(subset)),
            "fraction": _round(len(subset) / len(values)),
            "power_p05_W": _round(q05),
            "power_p50_W": _round(q50),
            "power_p95_W": _round(q95),
        }

    fit_values = np.clip(values, 0, float(np.quantile(values, .999)))
    split = _two_means(fit_values)
    if split is not None:
        labels, centers = split
        low_values, high_values = values[labels == 0], values[labels == 1]
        low_fraction = len(low_values) / len(values)
        high_fraction = len(high_values) / len(values)
        separation = float(centers[1] - centers[0])
        min_separation = max(1.0, 0.10 * max(float(centers[1]), 1.0))
        valid = (low_fraction >= .001 and high_fraction >= .002
                 and separation >= min_separation)
        if valid:
            # A second split is evaluated in log-power space so that a
            # materially higher transient state is separated by ratio rather
            # than by the absolute scale of the unit.
            high_split = _split_high_power_band(high_values, len(values))
            if high_split is not None:
                high_labels, _ = high_split
                steady_values = high_values[high_labels == 0]
                transient_values = high_values[high_labels == 1]
                steady_median = float(np.median(steady_values))
                transient_median = float(np.median(transient_values))
                transient_fraction = len(transient_values) / len(values)
                ratio = (transient_median / steady_median
                         if steady_median > 0 else 0.0)
                if (len(steady_values) >= 5 and len(transient_values) >= 5
                        and transient_fraction >= .002 and ratio >= 14.0):
                    high_mask = np.zeros(len(values), dtype=bool)
                    active_indices = np.flatnonzero(labels == 1)
                    high_mask[active_indices[high_labels == 1]] = True
                    threshold = float(np.expm1(
                        (np.log1p(steady_median)
                         + np.log1p(transient_median)) / 2))
                    bands = [
                        band_summary("low", "低功率观测层", low_values),
                        band_summary(
                            "active", "稳态工作功率观测层",
                            steady_values),
                        band_summary(
                            "high", "高功率瞬态观测层",
                            transient_values),
                    ]
                    return {
                        "kind": "three_level",
                        "bands": bands,
                        "low_power_W": _round(
                            float(np.median(low_values))),
                        "active_power_W": _round(steady_median),
                        "high_power_W": _round(transient_median),
                        "active_threshold_W": _round(
                            float((centers[0] + centers[1]) / 2)),
                        "high_threshold_W": _round(threshold),
                        "active_mask": labels == 1,
                        "high_mask": high_mask,
                    }
            global_high = _split_high_power_band(values, len(values))
            if global_high is not None:
                global_labels, _ = global_high
                lower_values = values[global_labels == 0]
                transient_values = values[global_labels == 1]
                lower_split = _two_means(lower_values)
                if lower_split is not None:
                    lower_labels, lower_centers = lower_split
                    lower_low_values = lower_values[lower_labels == 0]
                    lower_steady_values = lower_values[lower_labels == 1]
                    low_median = float(np.median(lower_low_values))
                    steady_median = float(np.median(lower_steady_values))
                    transient_median = float(np.median(transient_values))
                    active_fraction = (
                        len(lower_steady_values) / len(values)
                    )
                    low_active_ratio = (
                        steady_median / low_median
                        if low_median > 0 else 0.0
                    )
                    high_active_ratio = (
                        transient_median / steady_median
                        if steady_median > 0 else 0.0
                    )
                    if (len(lower_low_values) >= 5
                            and len(lower_steady_values) >= 5
                            and len(transient_values) >= 5
                            and active_fraction >= .002
                            and low_active_ratio >= 1.9
                            and high_active_ratio >= 14.0):
                        active_mask = np.zeros(len(values), dtype=bool)
                        lower_indices = np.flatnonzero(global_labels == 0)
                        active_mask[
                            lower_indices[lower_labels == 1]] = True
                        high_mask = global_labels == 1
                        threshold = float(np.expm1(
                            (np.log1p(steady_median)
                             + np.log1p(transient_median)) / 2))
                        bands = [
                            band_summary(
                                "low", "低功率观测层", lower_low_values),
                            band_summary(
                                "active", "稳态工作功率观测层",
                                lower_steady_values),
                            band_summary(
                                "high", "高功率瞬态观测层",
                                transient_values),
                        ]
                        return {
                            "kind": "three_level",
                            "bands": bands,
                            "low_power_W": _round(low_median),
                            "active_power_W": _round(steady_median),
                            "high_power_W": _round(transient_median),
                            "active_threshold_W": _round(
                                float((lower_centers[0]
                                       + lower_centers[1]) / 2)),
                            "high_threshold_W": _round(threshold),
                            "active_mask": active_mask,
                            "high_mask": high_mask,
                        }
            bands = [
                band_summary("low", "低功率观测层", low_values),
                band_summary("active", "工作功率观测层", high_values),
            ]
            return {
                "kind": "two_level",
                "bands": bands,
                "low_power_W": _round(float(np.median(low_values))),
                "active_power_W": _round(float(np.median(high_values))),
                "high_power_W": None,
                "active_threshold_W": _round(float((centers[0] + centers[1]) / 2)),
                "high_threshold_W": None,
                "active_mask": labels == 1,
                "high_mask": None,
            }
    q05, q50, q95 = np.quantile(values, [.05, .5, .95])
    return {
        "kind": "constant",
        "bands": [{
            "id": "constant",
            "label": "单一功率观测层",
            "n": int(len(values)),
            "fraction": 1.0,
            "power_p05_W": _round(q05),
            "power_p50_W": _round(q50),
            "power_p95_W": _round(q95),
        }],
        "low_power_W": None,
        "active_power_W": _round(float(np.median(values))),
        "high_power_W": None,
        "active_threshold_W": None,
        "high_threshold_W": None,
        "active_mask": np.ones(len(values), dtype=bool),
        "high_mask": None,
    }


def _active_episodes(stamps: pd.Series, active: np.ndarray,
                     max_gap_minutes: float) -> list[tuple[np.ndarray, np.ndarray]]:
    times = pd.to_datetime(stamps, errors="coerce")
    active = np.asarray(active, dtype=bool)
    episodes = []
    current = []
    previous = None
    for index in np.flatnonzero(active):
        stamp = times.iloc[int(index)]
        if previous is not None and (stamp - previous).total_seconds() > max_gap_minutes * 60:
            if current:
                episodes.append(np.asarray(current, dtype=int))
                current = []
        current.append(int(index))
        previous = stamp
    if current:
        episodes.append(np.asarray(current, dtype=int))
    result = []
    for indices in episodes:
        if len(indices) < 2:
            continue
        episode_times = times.iloc[indices]
        elapsed_hours = ((episode_times - episode_times.iloc[0]).dt.total_seconds()
                         .to_numpy(dtype=float) / 3600)
        result.append((indices, elapsed_hours))
    return result


def _fit_polynomial(values: np.ndarray, stamps: pd.Series, active: np.ndarray,
                    max_gap_minutes: float, degree: int, min_samples: int,
                    runtime_clip_W: tuple[float, float] | None = None) -> dict:
    episodes = _active_episodes(stamps, active, max_gap_minutes)
    usable = [episode for episode in episodes if len(episode[0]) >= 2]
    total = int(sum(len(indices) for indices, _ in usable))
    clip = None
    if runtime_clip_W is not None:
        clip = (float(runtime_clip_W[0]), float(runtime_clip_W[1]))
    base = {
        "degree": int(degree),
        "episodes": int(len(usable)),
        "active_samples": total,
        "time_unit": "hours_since_active_episode_start",
        "selected": False,
        "runtime_clip_W": list(clip) if clip is not None else None,
    }
    if len(usable) < 2 or total < min_samples:
        base["reason"] = "运行片段或有效样本不足，不能检验跨片段泛化"
        return base

    x_all = np.concatenate([elapsed for _, elapsed in usable])
    y_all = np.concatenate([values[indices].astype(float) for indices, _ in usable])
    episode_vector = np.concatenate([
        np.full(len(indices), episode_id, dtype=int)
        for episode_id, (indices, _) in enumerate(usable)
    ])
    squared_errors_raw, squared_errors_clipped, squared_errors_const = [], [], []
    endpoint_raw, endpoint_clipped, endpoint_const = [], [], []
    fold_count = min(5, len(usable))
    for fold in range(fold_count):
        test = episode_vector % fold_count == fold
        train = ~test
        if test.sum() < 2 or train.sum() < max(3, degree + 1):
            continue
        try:
            fit = np.polynomial.Polynomial.fit(
                x_all[train], y_all[train], degree).convert()
        except (np.linalg.LinAlgError, ValueError):
            continue
        prediction_raw = fit(x_all[test])
        prediction = (np.clip(prediction_raw, clip[0], clip[1])
                      if clip is not None else prediction_raw)
        baseline = float(np.median(y_all[train]))
        squared_errors_raw.extend(
            ((prediction_raw - y_all[test]) ** 2).tolist())
        squared_errors_clipped.extend(
            ((prediction - y_all[test]) ** 2).tolist())
        squared_errors_const.extend(
            ((baseline - y_all[test]) ** 2).tolist())
        for offset in (0, -1):
            endpoint_raw.append(
                float(prediction_raw[offset] - y_all[test][offset]))
            endpoint_clipped.append(
                float(prediction[offset] - y_all[test][offset]))
            endpoint_const.append(
                float(baseline - y_all[test][offset]))
    if not squared_errors_clipped:
        base["reason"] = "分块交叉验证无可用折"
        return base

    raw_rmse = float(np.sqrt(np.mean(squared_errors_raw)))
    poly_rmse = float(np.sqrt(np.mean(squared_errors_clipped)))
    const_rmse = float(np.sqrt(np.mean(squared_errors_const)))
    improvement = (1 - poly_rmse / const_rmse) if const_rmse > 0 else 0.0
    final_fit = np.polynomial.Polynomial.fit(x_all, y_all, degree).convert()
    support_min = float(np.min(x_all))
    support_max = float(np.max(x_all))
    support_raw = final_fit(np.linspace(support_min, support_max, 100))
    support_predictions = (np.clip(support_raw, clip[0], clip[1])
                           if clip is not None else support_raw)
    differences = np.diff(support_predictions)
    tolerance = max(1e-9, 1e-6 * max(
        float(np.max(np.abs(support_predictions))), 1.0))
    monotonic = bool(
        np.all(differences >= -tolerance)
        or np.all(differences <= tolerance)
    )
    endpoint_poly_mae = float(np.mean(np.abs(endpoint_clipped)))
    endpoint_const_mae = float(np.mean(np.abs(endpoint_const)))
    endpoint_tolerance = 1.10
    endpoint_guard = (
        endpoint_poly_mae <= endpoint_const_mae * endpoint_tolerance + 1e-9
    )
    selected = (improvement >= .10 and poly_rmse < const_rmse
                and np.all(np.isfinite(support_predictions))
                and float(np.min(support_predictions)) >= 0
                and monotonic and endpoint_guard)
    failures = []
    if improvement < .10 or poly_rmse >= const_rmse:
        failures.append("多项式在留出运行片段上的截断后误差改善不足 10%")
    if not monotonic:
        failures.append("支撑区间内预测非单调，端点稳定性不足")
    if not endpoint_guard:
        failures.append("片段首末位置误差超过恒定基线 10% 容差")
    if not np.all(np.isfinite(support_predictions)):
        failures.append("支撑区间内存在非有限预测")
    elif float(np.min(support_predictions)) < 0:
        failures.append("支撑区间内出现负功率")
    base.update({
        "selected": bool(selected),
        "coefficients_W_per_hour_power": [_round(value, 9) for value in final_fit.coef],
        "support_hours": [_round(support_min), _round(support_max)],
        "monotonic_within_support": monotonic,
        "endpoint_guard_passed": bool(endpoint_guard),
        "endpoint_mae_tolerance": endpoint_tolerance,
        "endpoint_polynomial_mae_W": _round(endpoint_poly_mae),
        "endpoint_constant_mae_W": _round(endpoint_const_mae),
        "cross_validation": {
            "method": "episode-grouped-deterministic-folds",
            "folds": fold_count,
            "raw_polynomial_rmse_W": _round(raw_rmse),
            "polynomial_rmse_W": _round(poly_rmse),
            "clipped_polynomial_rmse_W": _round(poly_rmse),
            "constant_rmse_W": _round(const_rmse),
            "relative_improvement": _round(improvement),
        },
        "extrapolation_policy": "clamp_to_support",
        "runtime_clip_policy": (
            "active_band_p05_p95" if clip is not None else
            "no_clip"
        ),
    })
    if not selected:
        base["reason"] = "；".join(failures)
    return base


def _mode_models(frame: pd.DataFrame, unit_id: str, power_column: str,
                 upper_limit_W: float | None = None) -> dict:
    mode_column = f"mode_{unit_id}"
    if mode_column not in frame:
        return {}
    modes = {}
    for mode_name, group in frame[[mode_column, power_column]].groupby(
            mode_column, dropna=False, sort=True):
        if pd.isna(mode_name) or str(mode_name) in VALID_GRID_STATES:
            continue
        values = pd.to_numeric(group[power_column], errors="coerce")
        valid_mask = np.isfinite(values) & (values >= 0)
        if upper_limit_W is not None:
            valid_mask &= values <= float(upper_limit_W)
        values = values[valid_mask].to_numpy(dtype=float)
        if len(values) == 0:
            continue
        modes[str(mode_name)] = {
            "n": int(len(values)),
            "power_p05_W": _round(float(np.quantile(values, .05))),
            "power_p50_W": _round(float(np.median(values))),
            "power_p95_W": _round(float(np.quantile(values, .95))),
            "support": "normal" if len(values) >= 5 else "sparse",
        }
    return modes


def _apply_shared_power_band_groups(models: dict, config: dict) -> None:
    """Transfer a missing high band from a configured sibling power feed.

    Some physical units expose paired or redundant feeds. A whole-day holdout
    can contain every high-power episode for one feed while the sibling has
    only steady-state observations. The group prior is used only when the
    local model has no high band; local evidence always wins.
    """
    groups = config.get("shared_power_band_groups", [])
    _require(isinstance(groups, list), "shared_power_band_groups 必须为列表")
    for raw_group in groups:
        _require(isinstance(raw_group, dict), "共享功率层组必须是对象")
        group_id = raw_group.get("id")
        members = raw_group.get("members")
        _require(
            isinstance(group_id, str) and bool(group_id),
            "共享功率层组缺少 id",
        )
        _require(
            isinstance(members, list)
            and len(members) >= 2
            and len(set(members)) == len(members)
            and all(isinstance(item, str) and item for item in members),
            f"共享功率层组 {group_id} 的 members 必须至少包含两个不同单机",
        )
        missing = [item for item in members if item not in models]
        _require(
            not missing,
            f"共享功率层组 {group_id} 含未建模单机: {missing}",
        )
        sources = [
            (item, models[item])
            for item in members
            if models[item].get("high_power_W") is not None
        ]
        if not sources:
            continue
        source_bands = []
        for source_id, source in sources:
            band = next(
                (
                    item
                    for item in source.get("power_bands", [])
                    if item.get("id") == "high"
                ),
                None,
            )
            _require(
                band is not None,
                f"{source_id} 有 high_power_W 但缺少 high 功率层",
            )
            source_bands.append(band)
        high_thresholds = [
            float(model["high_threshold_W"])
            for _, model in sources
            if model.get("high_threshold_W") is not None
        ]
        _require(bool(high_thresholds), f"共享功率层组 {group_id} 缺少高功率阈值")
        high_threshold = float(np.median(high_thresholds))
        power_p05 = float(np.median([
            float(band["power_p05_W"]) for band in source_bands]))
        power_p50 = float(np.median([
            float(band["power_p50_W"]) for band in source_bands]))
        power_p95 = float(np.median([
            float(band["power_p95_W"]) for band in source_bands]))
        for target_id in members:
            target = models[target_id]
            if (target.get("high_power_W") is not None
                    or not target.get("usable_for_prediction")):
                continue
            target.setdefault("power_bands", []).append({
                "id": "high",
                "label": "同组高功率观测层",
                "n": 0,
                "fraction": 0.0,
                "power_p05_W": _round(power_p05),
                "power_p50_W": _round(power_p50),
                "power_p95_W": _round(power_p95),
                "support": "shared_power_band_group",
            })
            target["high_power_W"] = _round(power_p50)
            target["high_threshold_W"] = _round(high_threshold)
            target["shared_power_band_source"] = {
                "group_id": group_id,
                "source_units": [source_id for source_id, _ in sources],
            }
            reason = raw_group.get("reason")
            target.setdefault("warnings", []).append(
                f"本机训练样本未形成 high 层；按共享单机组 {group_id} "
                f"从 {[source_id for source_id, _ in sources]} 迁移高功率先验"
                + (f"（{reason}）" if reason else "")
            )


def calibrate_unit_models(
        component_csv: str | Path,
        components_config: str | Path,
        *,
        satellite_id: str = "SCS-01-01",
        timezone_name: str = DEFAULT_TIMEZONE,
        max_gap_minutes: float = 5.0,
        min_samples: int = 30,
        polynomial_degree: int = 2,
        exclude_dates: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Fit every enabled ``role=load`` component from minute power proxies."""
    component_csv = Path(component_csv)
    components_config = Path(components_config)
    config, specs = _load_component_specs(components_config, satellite_id)
    _require(max_gap_minutes > 0, "max_gap_minutes 必须为正数")
    _require(min_samples >= 3, "min_samples 至少为 3")
    _require(polynomial_degree in (1, 2, 3), "polynomial_degree 仅支持 1、2、3")

    frame = pd.read_csv(component_csv)
    _require("t" in frame, "部件分钟 CSV 缺少 t 列")
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    _require(not frame["t"].isna().any(), "部件分钟 CSV 含无效时间")
    _require(not frame["t"].duplicated().any(), "部件分钟 CSV 含重复时间")
    frame = frame.sort_values("t").reset_index(drop=True)
    _require(not frame.empty, "部件分钟 CSV 为空")
    source_rows = int(len(frame))
    source_time_start = frame["t"].iloc[0].isoformat()
    source_time_end = frame["t"].iloc[-1].isoformat()
    excluded_dates = sorted({
        str(value).strip()
        for value in (exclude_dates or [])
        if str(value).strip()
    })
    for value in excluded_dates:
        try:
            pd.Timestamp(value).date()
        except (TypeError, ValueError) as error:
            raise UnitPowerModelError(
                f"排除日期格式无效: {value}") from error
    if excluded_dates:
        date_text = frame["t"].dt.strftime("%Y-%m-%d")
        excluded_mask = date_text.isin(excluded_dates)
        frame = frame.loc[~excluded_mask].reset_index(drop=True)
    else:
        excluded_mask = pd.Series(False, index=range(source_rows))
    _require(not frame.empty, "排除留出日期后部件分钟 CSV 为空")
    stamps = frame["t"]

    models = {}
    for spec in specs:
        unit_id = spec["id"]
        power_column = f"P_{unit_id}_W"
        warnings = []
        if power_column not in frame:
            models[unit_id] = {
                "name": spec.get("name", unit_id),
                "model_type": "unavailable",
                "usable_for_prediction": False,
                "warnings": [f"{power_column} 不在分钟 CSV 中"],
                "source": {"power_column": power_column},
            }
            continue
        numeric = pd.to_numeric(frame[power_column], errors="coerce")
        raw_valid_mask = np.isfinite(numeric) & (numeric >= 0)
        filtered_numeric, outlier_filter = filter_power_values(
            numeric,
            upper_limit_W=spec.get("max_power_W"),
        )
        valid_mask = filtered_numeric.notna()
        valid_values = filtered_numeric.loc[valid_mask].to_numpy(dtype=float)
        missing_fraction = float((~valid_mask).mean())
        if len(valid_values) < min_samples:
            models[unit_id] = {
                "name": spec.get("name", unit_id),
                "model_type": "unavailable",
                "usable_for_prediction": False,
                "source": {"power_column": power_column},
                "valid_samples": int(len(valid_values)),
                "missing_fraction": _round(missing_fraction),
                "stats": _robust_stats(valid_values),
                "outlier_filter": outlier_filter,
                "warnings": [
                    f"有效样本 {len(valid_values)} < {min_samples}",
                    *(
                        [f"已过滤 {outlier_filter['outlier_samples']} 个"
                         "超过配置筛选上限的遥测野值"]
                        if outlier_filter["outlier_samples"] else []
                    ),
                ],
            }
            continue

        band = _detect_power_bands(valid_values)
        valid_indices = np.flatnonzero(valid_mask.to_numpy())
        valid_stamps = stamps.iloc[valid_indices].reset_index(drop=True)
        active_band = next(
            (item for item in band["bands"] if item["id"] == "active"),
            band["bands"][0],
        )
        runtime_clip = (
            float(active_band["power_p05_W"]),
            float(active_band["power_p95_W"]),
        )
        high_mask = band.get("high_mask")
        steady_active_mask = band["active_mask"]
        if high_mask is not None:
            steady_active_mask = band["active_mask"] & ~high_mask
        modes = _mode_models(
            frame, unit_id, power_column,
            upper_limit_W=outlier_filter["upper_limit_W"],
        )
        if modes:
            polynomial = {
                "degree": int(polynomial_degree),
                "episodes": 0,
                "active_samples": int(len(valid_values)),
                "time_unit": "hours_since_active_episode_start",
                "selected": False,
                "reason": "显式状态模型优先，未用多项式覆盖",
            }
        else:
            polynomial = _fit_polynomial(
                valid_values, valid_stamps, steady_active_mask,
                max_gap_minutes, polynomial_degree, min_samples,
                runtime_clip_W=runtime_clip)
            if high_mask is not None:
                polynomial["candidate_selected"] = bool(
                    polynomial.get("selected"))
                polynomial["selected"] = False
                polynomial["reason"] = (
                    "检测到分离的高功率状态；改用 active/high 分层恒定模型，"
                    "不将高瞬态混入稳态时间多项式")
        if modes:
            model_type = "mode_constant"
        elif polynomial.get("selected"):
            model_type = "polynomial"
        else:
            model_type = "constant"

        if band["kind"] in {"two_level", "three_level"}:
            warnings.append("检测到低功率层与工作功率层；任务计划必须明确 on/mode")
        if band["kind"] == "three_level":
            warnings.append(
                f"检测到 active/high 双状态；"
                f"active 层取稳态功率，高功率任务必须显式使用 "
                f"power_level='high'（阈值约 "
                f"{band['high_threshold_W']:.3f} W）")
        if not modes:
            warnings.append("未建立经确认的同机状态映射；按任务计划的 on/mode 选择功率")
        if any(item["support"] == "sparse" for item in modes.values()):
            warnings.append("至少一个显式状态只有少于 5 个样本")
        stats = _robust_stats(valid_values)
        if outlier_filter["outlier_samples"]:
            warnings.append(
                f"已过滤 {outlier_filter['outlier_samples']} 个超过配置筛选上限"
                f"（{outlier_filter['upper_limit_W']} W）的遥测野值"
            )
        if stats.get("p99_W") and stats["max_W"] > 5 * stats["p99_W"]:
            warnings.append("存在远高于 p99 的短时峰值；预测不使用未解释峰值做外推")

        model = {
            "name": spec.get("name", unit_id),
            "model_type": model_type,
            "usable_for_prediction": True,
            "source": {
                "power_column": power_column,
                "telemetry_basis": spec.get("plugin", spec.get("model")),
                "source_codes": [value for key, value in spec.items()
                                 if key.endswith("_code") and isinstance(value, str)],
            },
            "observations": {
                "valid_samples": int(len(valid_values)),
                "raw_valid_samples": int(raw_valid_mask.sum()),
                "missing_fraction": _round(missing_fraction),
                "outlier_samples": int(outlier_filter["outlier_samples"]),
                "time_start": stamps.iloc[0].isoformat(),
                "time_end": stamps.iloc[-1].isoformat(),
            },
            "stats": stats,
            "outlier_filter": outlier_filter,
            "power_bands": band["bands"],
            "active_power_W": band["active_power_W"],
            "low_power_W": band["low_power_W"],
            "high_power_W": band.get("high_power_W"),
            "active_threshold_W": band["active_threshold_W"],
            "high_threshold_W": band.get("high_threshold_W"),
            "modes": modes,
            "polynomial": polynomial,
            "warnings": warnings,
            "evidence_level": ("observed_minute_proxy_mode_constant" if modes else
                               "observed_minute_proxy_polynomial" if polynomial.get("selected") else
                               "observed_minute_proxy_banded_constant"
                               if band["kind"] == "three_level" else
                               "observed_minute_proxy_constant"),
        }
        models[unit_id] = model

    _apply_shared_power_band_groups(models, config)
    config_hash = sha256_file(components_config)
    data_hash = sha256_file(component_csv)
    model_types = {}
    for model in models.values():
        key = model["model_type"]
        model_types[key] = model_types.get(key, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": satellite_id,
        "timezone": timezone_name,
        "model_code": {
            "file": Path(__file__).name,
            "sha256": sha256_file(Path(__file__).resolve()),
        },
        "created_from": {
            "component_csv": str(component_csv),
            "component_csv_sha256": data_hash,
            "components_config": str(components_config),
            "components_config_sha256": config_hash,
            "source_time_start": source_time_start,
            "source_time_end": source_time_end,
            "time_start": stamps.iloc[0].isoformat(),
            "time_end": stamps.iloc[-1].isoformat(),
        },
        "training_split": {
            "method": "exclude_whole_dates",
            "excluded_dates": excluded_dates,
            "source_rows": source_rows,
            "retained_rows": int(len(frame)),
            "excluded_rows": int(excluded_mask.sum()),
        },
        "calibration_contract": {
            "input": "enabled role=load component power columns from energy_v2_components.csv",
            "power_basis": "minute-mean voltage times minute-mean current proxy unless configured otherwise",
            "missing_policy": "missing/non-finite/negative/outlier samples are excluded, never treated as zero",
            "outlier_policy": (
                "configured max_power_W is a documented screening limit; "
                "without it, a wide robust power-tail guard removes "
                "implausible telemetry spikes before state and model fitting"),
            "band_policy": (
                "deterministic 1-D two-means for low/active; a log-power "
                "second split creates active/high only with >=0.2% source "
                "support, >=5 samples and >=14x median separation"),
            "polynomial_policy": (
                "degree 1-3 candidate; episode-grouped holdout uses the same "
                "active-band p05-p95 runtime clipping as deployment; selection "
                "also requires >=10% RMSE improvement, monotonic support and "
                "endpoint error no more than 10% worse than the constant "
                "baseline"),
            "off_policy": "off defaults to 0 W; low/idle uses the observed low-power band when present",
            "shared_power_band_policy": (
                "configured paired feeds may borrow a missing high band from "
                "a sibling feed in the same physical unit; local high-band "
                "evidence always takes precedence"),
            "date_split_policy": (
                "explicit whole dates may be excluded before fitting; "
                "excluded dates are never replaced or backfilled"),
        },
        "model_counts": model_types,
        "models": models,
    }


def load_excluded_dates(path: str | Path) -> list[str]:
    """Read a validation-split JSON or one YYYY-MM-DD date per line."""
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        values = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        values = document.get("dates") if isinstance(document, dict) else None
        if not isinstance(values, list):
            raise UnitPowerModelError(
                "排除日期 JSON 必须包含 dates 数组")
    return sorted({
        str(value).strip() for value in values if str(value).strip()
    })


def _load_model_document(path_or_doc: str | Path | dict,
                         satellite_id: str | None = None) -> dict:
    doc = _read_json(path_or_doc) if not isinstance(path_or_doc, dict) else path_or_doc
    _require(doc.get("schema_version") == SCHEMA_VERSION,
             f"模型 schema_version 必须为 {SCHEMA_VERSION}")
    _require(isinstance(doc.get("models"), dict), "模型文件缺少 models 对象")
    if satellite_id is not None:
        _require(doc.get("satellite_id") == satellite_id,
                 "模型 satellite_id 与请求不一致")
    return doc


def _normalize_soc(value: Any) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             "battery_soc 必须为数值")
    number = float(value)
    if 1 < number <= 100:
        number /= 100
    _require(0 <= number <= 1, "battery_soc 必须在 [0,1] 或 [0,100] 内")
    return number


def derive_current_snapshot(
        wide: pd.DataFrame,
        requested_time: Any,
        specs: list[dict],
        *,
        task_unit_ids: set[str] | list[str] | tuple[str, ...],
        bus_voltage_code: str,
        load_current_code: str,
        battery_voltage_code: str | None = None,
        battery_voltage_V: float | None = None,
        phase_code: str | None = None,
        provenance: dict | None = None,
        timezone_name: str = DEFAULT_TIMEZONE,
) -> CurrentPowerSnapshot:
    """Derive the exact-minute state needed by the simplified forecast entry."""
    _require(isinstance(wide, pd.DataFrame) and not wide.empty,
             "当前遥测宽表为空")
    _require(not wide.index.duplicated().any(), "当前遥测宽表含重复时间")
    requested = _parse_time(requested_time, timezone_name)
    index = pd.to_datetime(wide.index, errors="coerce")
    _require(not index.isna().any(), "当前遥测宽表含无效时间")
    if index.tz is None:
        index = index.tz_localize(timezone_name)
    else:
        index = index.tz_convert(timezone_name)
    local = wide.copy()
    local.index = index
    local = local.sort_index()
    _require(requested in local.index,
             f"time 精确分钟没有遥测记录: {requested.isoformat()}")
    row = local.loc[requested]

    def numeric(code: str, label: str) -> float:
        _require(code in row.index, f"当前遥测缺少 {label}({code})")
        value = pd.to_numeric(pd.Series([row[code]]), errors="coerce").iloc[0]
        _require(pd.notna(value) and math.isfinite(float(value)) and float(value) >= 0,
                 f"当前遥测 {label}({code}) 缺测或非负校验失败")
        return float(value)

    bus_voltage = numeric(bus_voltage_code, "母线电压")
    load_current = numeric(load_current_code, "负载电流")
    total_load = bus_voltage * load_current
    if battery_voltage_code:
        battery_voltage = numeric(battery_voltage_code, "蓄电池电压")
    else:
        _require(battery_voltage_V is not None
                 and math.isfinite(float(battery_voltage_V))
                 and float(battery_voltage_V) > 0,
                 "缺少蓄电池电压或有效 battery_voltage_V")
        battery_voltage = float(battery_voltage_V)

    one = local.loc[[requested]]
    load_series = pd.Series([total_load], index=one.index)
    bus_series = pd.Series([bus_voltage], index=one.index)
    detail, _ = evaluate_components(
        one, bus_series, load_series, bus_voltage_code, specs)
    unit_power = {}
    missing = {}
    spec_ids = {spec["id"] for spec in specs}
    for unit_id in dict.fromkeys(str(value) for value in task_unit_ids):
        _require(unit_id in spec_ids, f"任务引用未知单机: {unit_id}")
        column = f"P_{unit_id}_W"
        value = detail[column].iloc[0]
        if pd.isna(value) or not math.isfinite(float(value)) or float(value) < 0:
            missing[unit_id] = "missing_or_invalid_current_power"
        else:
            unit_power[unit_id] = float(value)
    _require(not missing,
             "任务单机当前功率缺测，不能无依据推导背景负载: "
             + ", ".join(sorted(missing)))

    phase = "未判定"
    if phase_code:
        _require(phase_code in row.index,
                 f"当前遥测缺少光照状态({phase_code})")
        raw_phase = row[phase_code]
        state_code = f"{phase_code}__state"
        if state_code in row.index and pd.notna(row[state_code]):
            raw_phase = row[state_code]
        if raw_phase == "__mixed__":
            raw_phase = None
        phase = "未判定" if pd.isna(raw_phase) else str(raw_phase)
    result_provenance = dict(provenance or {})
    result_provenance.update({
        "selected_time": requested.isoformat(),
        "time_column": "onboard_time_bj",
        "timezone": timezone_name,
        "phase_code": phase_code,
    })
    return CurrentPowerSnapshot(
        time=requested,
        battery_voltage_V=battery_voltage,
        bus_voltage_V=bus_voltage,
        total_load_W=float(total_load),
        phase=phase,
        unit_power_W=unit_power,
        missing_unit_power_W=missing,
        provenance=result_provenance,
    )


def _load_soc_points(curve: dict | None) -> np.ndarray | None:
    if curve is None:
        return None
    points = curve.get("points")
    _require(isinstance(points, list) and len(points) >= 2,
             "V-SOC 曲线 points 至少需要两个点")
    table = np.asarray(points, dtype=float)
    _require(np.all(np.isfinite(table)) and np.all(np.diff(table[:, 0]) > 0),
             "V-SOC 曲线电压点必须有限且严格递增")
    _require(np.all((table[:, 1] >= 0) & (table[:, 1] <= 1))
             and np.all(np.diff(table[:, 1]) >= 0),
             "V-SOC 曲线 SOC 点必须在 [0,1] 内且不减")
    return table


def _voltage_to_soc(voltage: float, table: np.ndarray | None) -> float | None:
    if table is None:
        return None
    return float(np.interp(float(voltage), table[:, 0], table[:, 1]))


def _soc_to_voltage(soc: float, table: np.ndarray | None) -> float | None:
    if table is None:
        return None
    return float(np.interp(float(soc), table[:, 1], table[:, 0]))


def _segments(raw: Any, name: str, timezone_name: str,
              require_power: bool = True) -> list[dict]:
    _require(isinstance(raw, list) and raw, f"{name} 必须为非空列表")
    result = []
    previous_end = None
    for index, segment in enumerate(raw):
        _require(isinstance(segment, dict), f"{name}[{index}] 必须为对象")
        start = _parse_time(segment.get("start"), timezone_name)
        end = _parse_time(segment.get("end"), timezone_name)
        _require(start < end, f"{name}[{index}] start 必须早于 end")
        if previous_end is not None:
            _require(start >= previous_end, f"{name} 不能重叠或乱序")
        previous_end = end
        item = {"start": start, "end": end}
        if require_power:
            power = segment.get("power_W")
            _require(isinstance(power, (int, float)) and not isinstance(power, bool)
                     and math.isfinite(float(power)) and float(power) >= 0,
                     f"{name}[{index}].power_W 必须为非负有限数")
            item["power_W"] = float(power)
        result.append(item)
    return result


def _segment_value(segments: list[dict], stamp: pd.Timestamp, name: str) -> float:
    for segment in segments:
        if segment["start"] <= stamp < segment["end"]:
            return float(segment["power_W"])
    raise UnitPowerModelError(f"{name} 在 {stamp.isoformat()} 未覆盖预测窗口")


def _model_power(model: dict, task: dict, elapsed_hours: float,
                 warnings: list[str], unit_id: str) -> float:
    if "power_W" in task:
        power = task["power_W"]
        _require(isinstance(power, (int, float)) and not isinstance(power, bool)
                 and math.isfinite(float(power)) and float(power) >= 0,
                 f"{unit_id} task.power_W 必须为非负有限数")
        if task.get("_source") != "restore_current":
            warnings.append(f"{unit_id}: 使用任务显式 power_W，未使用拟合值")
        return float(power)

    mode = task.get("mode", task.get("state", "active"))
    levels = task.get("power_level")
    if mode in {"off", "关机", "下电"}:
        return 0.0
    modes = model.get("modes", {})
    if mode in modes:
        if modes[mode].get("support") == "sparse":
            warnings.append(f"{unit_id}: mode={mode!r} 仅有稀疏观测支撑")
        return float(modes[mode]["power_p50_W"])
    if mode in {"idle", "low", "待机", "空闲", "低功率"}:
        low = model.get("low_power_W")
        _require(low is not None, f"{unit_id} 没有低功率观测层，不能预测 idle")
        return float(low)
    if levels:
        for band in model.get("power_bands", []):
            if levels == band["id"] or levels == band["label"]:
                return float(band["power_p50_W"])
        raise UnitPowerModelError(f"{unit_id} 不存在 power_level={levels!r}")
    if mode not in {"active", "on", "工作", "开机", "运行"}:
        warnings.append(f"{unit_id}: mode={mode!r} 无已确认映射，按 active 处理")

    polynomial = model.get("polynomial", {})
    if polynomial.get("selected"):
        low, high = polynomial["support_hours"]
        clipped = min(max(elapsed_hours, float(low)), float(high))
        if clipped != elapsed_hours:
            warnings.append(f"{unit_id}: 运行时长超出拟合支撑，已截断到 [{low}, {high}] h")
        coefficients = np.asarray(
            polynomial["coefficients_W_per_hour_power"], dtype=float)
        raw_power = float(np.polynomial.polynomial.polyval(
            clipped, coefficients))
        runtime_clip = polynomial.get("runtime_clip_W")
        if (isinstance(runtime_clip, (list, tuple))
                and len(runtime_clip) == 2):
            lower, upper = float(runtime_clip[0]), float(runtime_clip[1])
        else:
            lower = float(model.get("stats", {}).get("p05_W") or 0.0)
            upper = float(
                model.get("stats", {}).get("p99_W") or raw_power)
        power = min(max(raw_power, lower), upper)
        if power != raw_power:
            warnings.append(
                f"{unit_id}: 多项式原始预测 {raw_power:.3f} W "
                f"已按运行时区间 [{lower:.3f}, {upper:.3f}] W 截断")
        return float(power)

    active = model.get("active_power_W")
    _require(active is not None, f"{unit_id} 缺少可用工作功率")
    return float(active)


def _model_empirical_spread_W(model: dict, task: dict) -> float:
    """Return a non-negative empirical half-range for energy bookkeeping."""
    def half_range(item: dict) -> float:
        center = item.get("power_p50_W")
        if center is None:
            return 0.0
        lower = item.get("power_p05_W", center)
        upper = item.get("power_p95_W", center)
        return max(float(upper) - float(center), float(center) - float(lower))

    if "power_W" in task:
        return 0.0
    mode = task.get("mode", task.get("state", "active"))
    levels = task.get("power_level")
    if mode in {"off", "关机", "下电"}:
        return 0.0
    if mode in model.get("modes", {}):
        return half_range(model["modes"][mode])
    polynomial = model.get("polynomial", {})
    if (polynomial.get("selected") and not levels
            and mode not in {"idle", "low", "待机", "空闲", "低功率"}):
        return float(polynomial["cross_validation"]["polynomial_rmse_W"])
    for band in model.get("power_bands", []):
        if ((mode in {"idle", "low", "待机", "空闲", "低功率"}
             and band["id"] == "low")
                or (levels and levels in {band["id"], band["label"]})
                or (not levels and band["id"] == "active")):
            return half_range(band)
    if model.get("power_bands"):
        return half_range(model["power_bands"][0])
    return 0.0


def _validate_tasks(raw_tasks: Any, models: dict, timezone_name: str,
                    window_start: pd.Timestamp,
                    window_end: pd.Timestamp) -> list[dict]:
    _require(isinstance(raw_tasks, list), "tasks 必须为列表")
    tasks = []
    by_unit: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for index, task in enumerate(raw_tasks):
        _require(isinstance(task, dict), f"tasks[{index}] 必须为对象")
        unit_id = task.get("unit_id")
        _require(unit_id in models, f"tasks[{index}] 引用了未知单机: {unit_id!r}")
        model = models[unit_id]
        _require(model.get("usable_for_prediction"),
                 f"{unit_id} 没有可用于预测的功率模型")
        start = _parse_time(task.get("start"), timezone_name)
        end = _parse_time(task.get("end"), timezone_name)
        _require(start < end, f"tasks[{index}] start 必须早于 end")
        _require(end > window_start and start < window_end,
                 f"tasks[{index}] 完全位于预测窗口外")
        preview = {key: value for key, value in task.items()
                   if key not in {"start", "end"}}
        preview["start"] = max(start, window_start)
        preview["end"] = min(end, window_end)
        preview["_unit_id"] = unit_id
        preview["_original_start"] = start
        tasks.append(preview)
        by_unit.setdefault(unit_id, []).append((start, end))
    for unit_id, intervals in by_unit.items():
        intervals = sorted(intervals)
        for previous, current in zip(intervals, intervals[1:]):
            _require(current[0] >= previous[1],
                     f"{unit_id} 的同一单机任务不能重叠")
    tasks.sort(key=lambda item: (item["start"], item["_unit_id"]))
    return tasks


def predict_battery_state(
        model_document: str | Path | dict,
        request: str | Path | dict,
        *,
        v_soc_curve: str | Path | dict | None = None,
        design_params: str | Path | dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Predict per-step load and battery SOC through a task-defined window."""
    model_reference = model_document
    request_reference = request
    request = _read_json(request) if not isinstance(request, dict) else request
    _require(request.get("schema_version") == SCHEMA_VERSION,
             f"预测请求 schema_version 必须为 {SCHEMA_VERSION}")
    satellite_id = request.get("satellite_id")
    _require(isinstance(satellite_id, str) and satellite_id,
             "预测请求缺少 satellite_id")
    model_document = _load_model_document(model_document, satellite_id)
    models = model_document["models"]
    timezone_name = request.get("timezone", model_document.get("timezone", DEFAULT_TIMEZONE))
    warnings = []

    current = request.get("current_state")
    prediction = request.get("prediction")
    assumptions = request.get("assumptions", {})
    _require(isinstance(current, dict), "缺少 current_state 对象")
    _require(isinstance(prediction, dict), "缺少 prediction 对象")
    _require(isinstance(assumptions, dict), "assumptions 必须为对象")
    start = _parse_time(prediction.get("start"), timezone_name)
    end = _parse_time(prediction.get("end"), timezone_name)
    _require(start < end, "prediction.start 必须早于 prediction.end")
    step_seconds = prediction.get("step_seconds", 60)
    _require(isinstance(step_seconds, int) and not isinstance(step_seconds, bool)
             and 1 <= step_seconds <= 3600,
             "prediction.step_seconds 必须在 1..3600 秒内")

    curve = (_read_json(v_soc_curve) if isinstance(v_soc_curve, (str, Path))
             else v_soc_curve)
    design = (_read_json(design_params) if isinstance(design_params, (str, Path))
              else design_params or {})
    soc_points = _load_soc_points(curve)
    current_time = _parse_time(current.get("time", start), timezone_name)
    if current_time > start:
        warnings.append("current_state.time 晚于 prediction.start；按给定的当前状态向未来外推")

    if "battery_soc" in current:
        initial_soc = _normalize_soc(current["battery_soc"])
        soc_source = "telemetry_battery_soc"
    elif "battery_voltage_V" in current:
        voltage = current["battery_voltage_V"]
        _require(isinstance(voltage, (int, float)) and math.isfinite(float(voltage)),
                 "battery_voltage_V 必须为有限数值")
        initial_soc = _voltage_to_soc(float(voltage), soc_points)
        _require(initial_soc is not None, "只有电池电压时必须提供 V-SOC 曲线")
        soc_source = "battery_voltage_mapped_by_v_soc_curve"
    else:
        raise UnitPowerModelError("current_state 必须提供 battery_soc 或 battery_voltage_V")
    initial_soc = min(max(initial_soc, 0.0), 1.0)

    capacity_Ah = assumptions.get(
        "battery_capacity_Ah", current.get("battery_capacity_Ah",
                                          design.get("battery_capacity_Ah")))
    nominal_voltage_V = assumptions.get(
        "nominal_voltage_V", design.get("bus_nominal_V"))
    _require(isinstance(capacity_Ah, (int, float)) and not isinstance(capacity_Ah, bool)
             and float(capacity_Ah) > 0, "缺少正的 battery_capacity_Ah")
    _require(isinstance(nominal_voltage_V, (int, float))
             and not isinstance(nominal_voltage_V, bool)
             and float(nominal_voltage_V) > 0, "缺少正的 nominal_voltage_V")
    eta_charge = assumptions.get("eta_charge", design.get("eta_charge", 1.0))
    eta_discharge = assumptions.get("eta_discharge", design.get("eta_discharge", 1.0))
    soc_floor = assumptions.get("soc_floor", design.get("soc_floor", 0.05))
    _require(isinstance(eta_charge, (int, float)) and 0 < float(eta_charge) <= 1,
             "eta_charge 必须在 (0,1] 内")
    _require(isinstance(eta_discharge, (int, float)) and 0 < float(eta_discharge) <= 1,
             "eta_discharge 必须在 (0,1] 内")
    _require(isinstance(soc_floor, (int, float)) and 0 <= float(soc_floor) < 1,
             "soc_floor 必须在 [0,1) 内")
    _require(initial_soc >= float(soc_floor),
             "初始 SOC 低于给定 soc_floor")

    current_load = current.get("load_power_W")
    if current_load is not None:
        _require(isinstance(current_load, (int, float))
                 and not isinstance(current_load, bool)
                 and math.isfinite(float(current_load))
                 and float(current_load) >= 0,
                 "current_state.load_power_W 必须为非负有限数")
    background = current.get("background_load_W")
    if background is None and current_load is not None:
        current_units = current.get("current_unit_power_W", {})
        _require(isinstance(current_units, dict),
                 "current_unit_power_W 必须为对象")
        for unit_id, value in current_units.items():
            _require(unit_id in models, f"current_unit_power_W 引用了未知单机: {unit_id}")
            _require(isinstance(value, (int, float)) and math.isfinite(float(value))
                     and float(value) >= 0,
                     f"current_unit_power_W[{unit_id}] 必须为非负有限数")
        background = float(current_load) - float(sum(current_units.values()))
        _require(background >= 0,
                 "当前总负载小于当前单机功率和，无法建立非负背景负载")
        warnings.append("background_load_W 由当前负载减去 current_unit_power_W 推导")
    _require(isinstance(background, (int, float)) and math.isfinite(float(background))
             and float(background) >= 0,
             "必须提供非负 current_state.background_load_W，或可推导的当前负载与单机功率")

    background_segments = prediction.get("background_load_segments")
    if background_segments is None:
        background_segments = [{
            "start": start.isoformat(), "end": end.isoformat(),
            "power_W": float(background),
        }]
    background_segments = _segments(
        background_segments, "prediction.background_load_segments", timezone_name)
    solar_segments = _segments(
        prediction.get("solar_segments"), "prediction.solar_segments", timezone_name)
    solar_load_following_margin_W = prediction.get(
        "solar_load_following_charge_margin_W")
    _require(solar_load_following_margin_W is None or (
        isinstance(solar_load_following_margin_W, (int, float))
        and not isinstance(solar_load_following_margin_W, bool)
        and math.isfinite(float(solar_load_following_margin_W))
        and float(solar_load_following_margin_W) >= 0),
        "prediction.solar_load_following_charge_margin_W 必须为非负有限数")
    if solar_load_following_margin_W is not None:
        warnings.append(
            "太阳阵到母线功率按总负载加"
            f"{float(solar_load_following_margin_W):.3f} W充电余量封顶")
    for name, segments in [
            ("background_load_segments", background_segments),
            ("solar_segments", solar_segments)]:
        _require(segments[0]["start"] <= start and segments[-1]["end"] >= end,
                 f"{name} 未覆盖完整预测窗口")

    tasks = _validate_tasks(request.get("tasks", []), models,
                            timezone_name, start, end)
    boundaries = {start, end}
    for segment in background_segments + solar_segments:
        if segment["end"] > start and segment["start"] < end:
            boundaries.add(max(segment["start"], start))
            boundaries.add(min(segment["end"], end))
    for task in tasks:
        boundaries.add(task["start"])
        boundaries.add(task["end"])
    boundaries = sorted(boundaries)
    intervals = []
    max_delta = pd.Timedelta(seconds=step_seconds)
    for left, right in zip(boundaries, boundaries[1:]):
        cursor = left
        while cursor < right:
            step_end = min(cursor + max_delta, right)
            intervals.append((cursor, step_end))
            cursor = step_end

    capacity_Wh = float(capacity_Ah) * float(nominal_voltage_V)
    floor_Wh = float(soc_floor) * capacity_Wh
    energy_Wh = initial_soc * capacity_Wh
    unit_energy_Wh = {unit_id: 0.0 for unit_id in models}
    unit_spread_squared_Wh2 = {unit_id: 0.0 for unit_id in models}
    total_load_Wh = total_solar_Wh = charge_Wh = discharge_Wh = unserved_Wh = 0.0
    rows = []
    minimum_soc = initial_soc
    minimum_time = start
    floor_limited_seconds = 0.0
    ceiling_limited_seconds = 0.0
    for left, right in intervals:
        duration_s = (right - left).total_seconds()
        if duration_s <= 0:
            continue
        sample_time = left
        background_W = _segment_value(
            background_segments, sample_time, "background_load_segments")
        solar_uncapped_W = _segment_value(
            solar_segments, sample_time, "solar_segments")
        active = [task for task in tasks
                  if task["start"] <= sample_time < task["end"]]
        powers = {}
        for task in active:
            unit_id = task["_unit_id"]
            if unit_id in powers:
                raise UnitPowerModelError(f"{unit_id} 在 {sample_time.isoformat()} 存在重叠任务")
            if "elapsed_offset_seconds" in task:
                offset = task["elapsed_offset_seconds"]
                _require(isinstance(offset, (int, float)) and float(offset) >= 0,
                         f"{unit_id} elapsed_offset_seconds 必须为非负数值")
            else:
                offset = 0.0
            elapsed_hours = ((sample_time - task["_original_start"]).total_seconds()
                             + float(offset)) / 3600
            powers[unit_id] = _model_power(model_document["models"][unit_id],
                                           task, elapsed_hours, warnings, unit_id)
        unit_load_W = sum(powers.values())
        active_unit_count = len(powers)
        total_load_W = background_W + unit_load_W
        solar_W = solar_uncapped_W
        if solar_load_following_margin_W is not None:
            solar_W = min(
                solar_W,
                total_load_W + float(solar_load_following_margin_W))
        load_step_Wh = total_load_W * duration_s / 3600
        solar_step_Wh = solar_W * duration_s / 3600
        total_load_Wh += load_step_Wh
        total_solar_Wh += solar_step_Wh
        for unit_id, power in powers.items():
            unit_energy_Wh[unit_id] += power * duration_s / 3600
            task = next(item for item in active if item["_unit_id"] == unit_id)
            spread_W = _model_empirical_spread_W(
                model_document["models"][unit_id], task)
            unit_spread_squared_Wh2[unit_id] += (
                spread_W * duration_s / 3600) ** 2

        soc_start = energy_Wh / capacity_Wh
        net_Wh = solar_step_Wh - load_step_Wh
        charge_bus_Wh = discharge_Wh_step = 0.0
        if net_Wh >= 0:
            requested = net_Wh * float(eta_charge)
            accepted = min(requested, capacity_Wh - energy_Wh)
            energy_Wh += accepted
            charge_bus_Wh = accepted / float(eta_charge)
            charge_Wh += charge_bus_Wh
            if requested - accepted > 1e-9:
                ceiling_limited_seconds += duration_s
        else:
            requested = -net_Wh / float(eta_discharge)
            available = max(0.0, energy_Wh - floor_Wh)
            accepted = min(requested, available)
            energy_Wh -= accepted
            discharge_Wh_step = accepted
            discharge_Wh += accepted
            if requested - accepted > 1e-9:
                unserved_Wh += requested - accepted
                floor_limited_seconds += duration_s
        soc_end = energy_Wh / capacity_Wh
        if soc_end < minimum_soc:
            minimum_soc = soc_end
            minimum_time = right
        row = {
            "t_start": left.isoformat(),
            "t_end": right.isoformat(),
            "duration_s": duration_s,
            "solar_power_W": solar_W,
            "solar_power_uncapped_W": solar_uncapped_W,
            "solar_load_following_charge_margin_W": (
                solar_load_following_margin_W),
            "background_load_W": background_W,
            "unit_load_W": unit_load_W,
            "active_unit_count": active_unit_count,
            "task_unit_power_W": unit_load_W,
            "total_load_W": total_load_W,
            "net_power_W": net_Wh * 3600 / duration_s,
            "battery_charge_bus_W": charge_bus_Wh * 3600 / duration_s,
            "battery_discharge_stored_W": discharge_Wh_step * 3600 / duration_s,
            "soc_start": soc_start,
            "soc_end": soc_end,
            "battery_voltage_V": _soc_to_voltage(soc_end, soc_points),
        }
        for unit_id in models:
            row[f"P_{unit_id}_W"] = powers.get(unit_id, 0.0)
        rows.append(row)

    timeline = pd.DataFrame(rows)
    end_soc = energy_Wh / capacity_Wh
    end_voltage = _soc_to_voltage(end_soc, soc_points)
    target_energy_kWh = energy_Wh / 1000
    remaining_Ah = end_soc * float(capacity_Ah)
    available_Ah = max(0.0, end_soc - float(soc_floor)) * float(capacity_Ah)
    unit_spread_kWh = {
        unit_id: math.sqrt(value) / 1000
        for unit_id, value in unit_spread_squared_Wh2.items()
        if value > 1e-18
    }
    aggregate_spread_kWh = math.sqrt(
        sum(value ** 2 for value in unit_spread_kWh.values()))
    if unserved_Wh > 1e-6:
        status = "completed_with_shortfall"
    elif warnings:
        status = "completed_with_warnings"
    else:
        status = "completed"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": satellite_id,
        "status": status,
        "timezone": timezone_name,
        "provenance": {
            "model_file": str(model_reference)
            if isinstance(model_reference, (str, Path)) else None,
            "model_file_sha256": sha256_file(model_reference)
            if isinstance(model_reference, (str, Path)) else None,
            "request_file": str(request_reference)
            if isinstance(request_reference, (str, Path)) else None,
            "request_file_sha256": sha256_file(request_reference)
            if isinstance(request_reference, (str, Path)) else None,
            "model_code_sha256": model_document.get("model_code", {}).get("sha256"),
        },
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_hours": _round((end - start).total_seconds() / 3600),
            "step_seconds_max": step_seconds,
        },
        "initial_state": {
            "time": current_time.isoformat(),
            "soc": _round(initial_soc),
            "soc_source": soc_source,
            "battery_voltage_V": current.get("battery_voltage_V"),
            "remaining_Ah": _round(initial_soc * float(capacity_Ah)),
            "remaining_energy_nominal_kWh": _round(initial_soc * capacity_Wh / 1000),
        },
        "target_end_state": {
            "time": end.isoformat(),
            "soc": _round(end_soc),
            "remaining_Ah": _round(remaining_Ah),
            "remaining_energy_nominal_kWh": _round(target_energy_kWh),
            "available_to_soc_floor_Ah": _round(available_Ah),
            "available_to_soc_floor_nominal_kWh": _round(
                max(0.0, end_soc - float(soc_floor)) * capacity_Wh / 1000),
            "battery_voltage_from_v_soc_V": _round(end_voltage),
            "soc_half_spread_from_power_models": _round(
                aggregate_spread_kWh * 1000 / capacity_Wh),
        },
        "minimum_state": {
            "soc": _round(minimum_soc),
            "time": minimum_time.isoformat(),
            "remaining_Ah": _round(minimum_soc * float(capacity_Ah)),
        },
        "energy": {
            "solar_to_bus_kWh": _round(total_solar_Wh / 1000),
            "load_kWh": _round(total_load_Wh / 1000),
            "battery_charge_bus_kWh": _round(charge_Wh / 1000),
            "battery_discharge_stored_kWh": _round(discharge_Wh / 1000),
            "unserved_energy_at_floor_kWh": _round(unserved_Wh / 1000),
            "model_power_empirical_spread_kWh": _round(aggregate_spread_kWh),
            "unit_energy_kWh": {
                unit_id: _round(value / 1000)
                for unit_id, value in unit_energy_Wh.items() if value > 1e-9
            },
            "unit_model_power_empirical_spread_kWh": {
                unit_id: _round(value)
                for unit_id, value in unit_spread_kWh.items()
            },
        },
        "limits": {
            "floor_limited_seconds": _round(floor_limited_seconds),
            "ceiling_limited_seconds": _round(ceiling_limited_seconds),
        },
        "assumptions": {
            "battery_capacity_Ah": _round(float(capacity_Ah)),
            "nominal_voltage_V": _round(float(nominal_voltage_V)),
            "nominal_energy_kWh": _round(capacity_Wh / 1000),
            "eta_charge": _round(float(eta_charge)),
            "eta_discharge": _round(float(eta_discharge)),
            "soc_floor": _round(float(soc_floor)),
            "battery_energy_basis": "rated capacity x nominal voltage; SOC is coulomb/energy bookkeeping",
        },
        "warnings": list(dict.fromkeys(warnings)),
        "evidence": {
            "power_model_status": "data-fitted minute proxies",
            "battery_model_status": "bookkeeping prediction, not independent in-orbit validation",
            "solar_input_status": "caller-provided piecewise solar power",
            "solar_bus_power_constraint": (
                "P_solar_to_bus <= total_load + "
                f"{float(solar_load_following_margin_W):.3f} W"
                if solar_load_following_margin_W is not None
                else "unconstrained"),
            "background_load_status": "caller-provided or derived from current telemetry",
            "not_modeled": [
                "temperature-dependent battery capacity",
                "charge/discharge hysteresis and rate dependence",
                "solar-array incidence and MPPT/S3R operating point",
                "unlisted loads not included in background_load",
                "measurement-boundary overlap among unit power branches",
            ],
        },
    }
    return timeline, _json_safe(summary)


def _compact_time(value: Any, base: pd.Timestamp, timezone_name: str,
                  label: str) -> pd.Timestamp:
    if isinstance(value, str) and re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
        date = base.strftime("%Y-%m-%d")
        return _parse_time(f"{date}T{value}", timezone_name)
    result = _parse_time(value, timezone_name)
    _require(result.date() == base.date(),
             f"{label} 跨日时必须使用完整日期时间")
    return result


GENERIC_POWER_MODES = {
    "active", "on", "工作", "开机", "运行",
    "idle", "low", "待机", "空闲", "低功率",
    "off", "关机", "下电",
}


def _validate_task_mode(unit_id: str, mode: str, model: dict,
                        allow_restore: bool) -> None:
    if allow_restore and mode == "restore":
        return
    allowed = GENERIC_POWER_MODES | set(model.get("modes", {}))
    _require(mode in allowed,
             f"{unit_id} 未支持模式 {mode!r}；可用模式: {sorted(allowed)}")


def _normalize_phase_segments(prediction: dict, start: pd.Timestamp,
                              end: pd.Timestamp, timezone_name: str) -> list[dict]:
    raw_segments = prediction.get("phase_segments")
    _require(isinstance(raw_segments, list) and raw_segments,
             "prediction.phase_segments 必须为非空列表")
    result = []
    for index, raw in enumerate(raw_segments):
        _require(isinstance(raw, dict), f"phase_segments[{index}] 必须为对象")
        segment_start = _compact_time(
            raw.get("start"), start, timezone_name,
            f"phase_segments[{index}].start")
        segment_end = _compact_time(
            raw.get("end"), start, timezone_name,
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
        if result:
            _require(segment_start == result[-1]["end"],
                     "phase_segments 必须连续、有序且不能重叠")
        else:
            _require(segment_start == start,
                     "phase_segments 必须从 time 开始")
        result.append({
            "start": segment_start,
            "end": segment_end,
            "phase": phase,
        })
    _require(result[-1]["end"] == end,
             "phase_segments 必须完整覆盖到 prediction.end")
    return result


def _normalize_task_plan(request: dict, models: dict, timezone_name: str
                         ) -> tuple[pd.Timestamp, pd.Timestamp, int,
                                    list[dict], list[dict], list[str]]:
    _require(isinstance(request, dict), "预测计划必须为 JSON 对象")
    _require("current_state" not in request,
             "简化入口不再接收 current_state；SOC 由模型自动查询")
    start = _parse_time(request.get("time"), timezone_name)
    _require(start.second == 0 and start.microsecond == 0,
             "time 必须为整分钟")
    prediction = request.get("prediction")
    _require(isinstance(prediction, dict), "缺少 prediction 对象")
    for legacy_field in ("solar_segments", "background_load_segments"):
        _require(legacy_field not in prediction,
                 f"prediction.{legacy_field} 已移除，"
                 "该输入由模型自动计算或由功率模块提供")
    end = _parse_time(prediction.get("end"), timezone_name)
    _require(start < end, "time 必须早于 prediction.end")
    step_seconds = prediction.get("step_seconds", 60)
    _require(isinstance(step_seconds, int) and not isinstance(step_seconds, bool)
             and 1 <= step_seconds <= 3600,
             "prediction.step_seconds 必须在 1..3600 秒内")
    raw_tasks = prediction.get("tasks", [])
    _require(isinstance(raw_tasks, list), "prediction.tasks 必须为列表")
    phase_segments = _normalize_phase_segments(
        prediction, start, end, timezone_name)
    normalized = []
    for index, raw in enumerate(raw_tasks):
        _require(isinstance(raw, dict), f"tasks[{index}] 必须为对象")
        _require("mode" not in raw,
                 f"tasks[{index}] 的 mode 有歧义；请改用 run_mode/after_mode")
        unit_value = raw.get("unit")
        units_value = raw.get("units")
        _require((unit_value is None) != (units_value is None),
                 f"tasks[{index}] 必须且只能提供 unit 或 units 之一")
        if unit_value is not None:
            _require(isinstance(unit_value, str) and unit_value,
                     f"tasks[{index}].unit 必须为非空字符串")
            units = [unit_value]
        else:
            if isinstance(units_value, str):
                units = [units_value]
            else:
                _require(isinstance(units_value, list) and units_value,
                         f"tasks[{index}].units 必须为非空列表或字符串")
                units = list(units_value)
            _require(all(isinstance(unit_id, str) and unit_id for unit_id in units),
                     f"tasks[{index}].units 含无效单机 ID")
        units = list(dict.fromkeys(units))
        _require(units, f"tasks[{index}] 没有单机")
        for unit_id in units:
            _require(unit_id in models, f"tasks[{index}] 引用未知单机: {unit_id}")
            _require(models[unit_id].get("usable_for_prediction"),
                     f"{unit_id} 没有可用于预测的功率模型")
        task_start = _compact_time(
            raw.get("start"), start, timezone_name, f"tasks[{index}].start")
        task_end = _compact_time(
            raw.get("end"), start, timezone_name, f"tasks[{index}].end")
        _require(task_start < task_end, f"tasks[{index}] start 必须早于 end")
        _require(task_start >= start and task_end <= end,
                 f"tasks[{index}] 必须位于 [time, end) 内")
        run_mode = raw.get("run_mode", "active")
        after_mode = raw.get("after_mode", "restore")
        _require(isinstance(run_mode, str) and run_mode,
                 f"tasks[{index}].run_mode 必须为非空字符串")
        _require(isinstance(after_mode, str) and after_mode,
                 f"tasks[{index}].after_mode 必须为非空字符串")
        for unit_id in units:
            _validate_task_mode(
                unit_id, run_mode, models[unit_id], allow_restore=False)
            _validate_task_mode(
                unit_id, after_mode, models[unit_id], allow_restore=True)
        normalized.append({
            "name": raw.get("name"),
            "start": task_start,
            "end": task_end,
            "units": units,
            "run_mode": run_mode,
            "after_mode": after_mode,
            "power_level": raw.get("power_level"),
            "index": index,
        })
    unit_ids = sorted({unit_id for task in normalized
                       for unit_id in task["units"]})
    for unit_id in unit_ids:
        intervals = sorted((task["start"], task["end"])
                           for task in normalized if unit_id in task["units"])
        for previous, current in zip(intervals, intervals[1:]):
            _require(current[0] >= previous[1],
                     f"{unit_id} 的任务窗口不能重叠")
    return start, end, step_seconds, phase_segments, normalized, unit_ids


def _expand_override_tasks(start: pd.Timestamp, end: pd.Timestamp,
                           tasks: list[dict], unit_ids: list[str],
                           current_unit_power_W: dict[str, float]) -> list[dict]:
    expanded = []
    for unit_id in unit_ids:
        own = sorted((task for task in tasks if unit_id in task["units"]),
                     key=lambda task: task["start"])
        cursor = start
        for index, task in enumerate(own):
            if cursor < task["start"]:
                after_mode = "restore" if index == 0 else own[index - 1]["after_mode"]
                if after_mode == "restore":
                    expanded.append({
                        "unit_id": unit_id,
                        "start": cursor,
                        "end": task["start"],
                        "power_W": current_unit_power_W[unit_id],
                        "_source": "restore_current",
                    })
                else:
                    expanded.append({
                        "unit_id": unit_id,
                        "start": cursor,
                        "end": task["start"],
                        "mode": after_mode,
                        "_source": "after_mode",
                    })
            entry = {
                "unit_id": unit_id,
                "start": task["start"],
                "end": task["end"],
                "mode": task["run_mode"],
                "_source": "run_mode",
            }
            if task.get("power_level"):
                entry["power_level"] = task["power_level"]
            expanded.append(entry)
            cursor = task["end"]
        if cursor < end:
            after_mode = own[-1]["after_mode"] if own else "restore"
            if after_mode == "restore":
                expanded.append({
                    "unit_id": unit_id,
                    "start": cursor,
                    "end": end,
                    "power_W": current_unit_power_W[unit_id],
                    "_source": "restore_current",
                })
            else:
                expanded.append({
                    "unit_id": unit_id,
                    "start": cursor,
                    "end": end,
                    "mode": after_mode,
                    "_source": "after_mode",
                })
    return sorted(expanded, key=lambda task: (task["start"], task["unit_id"]))


def forecast_from_plan(
        request: str | Path | dict,
        model_document: str | Path | dict,
        snapshot_provider: Any,
        *,
        v_soc_curve: str | Path | dict,
        design_params: str | Path | dict | None = None,
        solar_provider: Any | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Run the simplified ``time/end/step/tasks`` forecast contract."""
    request_reference = request
    request = _read_json(request) if not isinstance(request, dict) else request
    model_reference = model_document
    model_document = _load_model_document(model_document)
    models = model_document["models"]
    timezone_name = request.get("timezone", model_document.get(
        "timezone", DEFAULT_TIMEZONE))
    start, end, step_seconds, phase_segments, tasks, unit_ids = _normalize_task_plan(
        request, models, timezone_name)
    _require(hasattr(snapshot_provider, "snapshot"),
             "snapshot_provider 必须提供 snapshot(time, unit_ids)")
    snapshot = snapshot_provider.snapshot(start, tuple(unit_ids))
    _require(all(hasattr(snapshot, field) for field in (
        "time", "battery_voltage_V", "bus_voltage_V", "total_load_W",
        "phase", "unit_power_W", "missing_unit_power_W", "provenance")),
        "遥测快照 provider 必须返回 CurrentPowerSnapshot 兼容对象")
    _require(snapshot.time == start,
             "遥测快照时间必须等于 time 精确分钟")
    missing = [unit_id for unit_id in unit_ids
               if unit_id not in snapshot.unit_power_W]
    _require(not missing,
             "任务单机当前功率缺测: " + ", ".join(missing))
    background_load_W = snapshot.total_load_W - sum(
        snapshot.unit_power_W[unit_id] for unit_id in unit_ids)
    _require(background_load_W >= -1e-9,
             "当前总负载小于任务单机当前功率之和，无法建立背景负载")
    background_load_W = max(0.0, background_load_W)
    solar_warnings = []
    solar_load_following_margin_W = None
    expanded_tasks = _expand_override_tasks(
        start, end, tasks, unit_ids, snapshot.unit_power_W)
    load_segments = None
    if solar_provider is not None and "load_segments" in inspect.signature(
            solar_provider.profile).parameters:
        preliminary_request = {
            "schema_version": SCHEMA_VERSION,
            "satellite_id": model_document["satellite_id"],
            "timezone": timezone_name,
            "current_state": {
                "time": start.isoformat(),
                "battery_voltage_V": snapshot.battery_voltage_V,
                "background_load_W": background_load_W,
            },
            "prediction": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "step_seconds": step_seconds,
                "solar_segments": [{
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "power_W": 0.0,
                }],
            },
            "tasks": expanded_tasks,
            "assumptions": request.get("assumptions", {}),
        }
        preliminary_timeline, _ = predict_battery_state(
            model_document, preliminary_request,
            v_soc_curve=v_soc_curve, design_params=design_params)
        load_segments = [{
            "start": row.t_start,
            "end": row.t_end,
            "power_W": row.total_load_W,
            "task_unit_power_W": row.task_unit_power_W,
            "active_unit_count": row.active_unit_count,
        } for row in preliminary_timeline.itertuples()]

    if solar_provider is None:
        _require(all(segment["phase"] == "eclipse"
                     for segment in phase_segments),
                 "phase_segments 含阳照区；必须提供太阳阵功率 provider")
        solar_segments = [{
            "start": segment["start"].isoformat(),
            "end": segment["end"].isoformat(),
            "power_W": 0.0,
        } for segment in phase_segments]
        solar_source = "eclipse_only_zero"
    else:
        _require(hasattr(solar_provider, "profile"),
                 "solar_provider 必须提供 profile(start, end, step_seconds, snapshot)")
        profile_kwargs = {
            "start": start,
            "end": end,
            "step_seconds": step_seconds,
            "snapshot": snapshot,
            "phase_segments": phase_segments,
        }
        if load_segments is not None:
            profile_kwargs["load_segments"] = load_segments
        raw_profile = solar_provider.profile(**profile_kwargs)
        if isinstance(raw_profile, pd.DataFrame):
            records = raw_profile.to_dict(orient="records")
        else:
            records = raw_profile
        solar_segments = [{
            "start": str(item["start"]),
            "end": str(item["end"]),
            "power_W": float(item["power_W"]),
        } for item in records]
        solar_segments = _segments(
            solar_segments, "solar_provider", timezone_name)
        solar_segments = [{
            "start": item["start"].isoformat(),
            "end": item["end"].isoformat(),
            "power_W": item["power_W"],
        } for item in solar_segments]
        solar_source = getattr(
            solar_provider, "source", "solar_power_provider")
        warnings_list = getattr(solar_provider, "warnings", [])
        if warnings_list:
            solar_warnings.extend(str(item) for item in warnings_list)
        margin = getattr(
            solar_provider, "load_following_charge_margin_W", None)
        if margin is not None:
            solar_load_following_margin_W = float(margin)

    internal_request = {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": model_document["satellite_id"],
        "timezone": timezone_name,
        "current_state": {
            "time": start.isoformat(),
            "battery_voltage_V": snapshot.battery_voltage_V,
            "background_load_W": background_load_W,
        },
        "prediction": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step_seconds": step_seconds,
            "solar_segments": solar_segments,
            "solar_load_following_charge_margin_W":
                solar_load_following_margin_W,
        },
        "tasks": expanded_tasks,
        "assumptions": request.get("assumptions", {}),
    }
    timeline, summary = predict_battery_state(
        model_document, internal_request, v_soc_curve=v_soc_curve,
        design_params=design_params)
    summary["schema_version"] = "1.1"
    if solar_warnings:
        summary["warnings"].extend(solar_warnings)
        if summary["status"] == "completed":
            summary["status"] = "completed_with_warnings"
    summary["input_contract"] = {
        "schema_version": "1.1-simplified",
        "user_inputs": ["time", "prediction.end", "prediction.step_seconds",
                        "prediction.phase_segments", "prediction.tasks"],
        "task_semantics": (
            "run_mode applies on [start,end); after_mode applies from end until "
            "the next task for that unit or prediction end; omitted after_mode "
            "restores the current telemetry-derived power"),
    }
    summary["derived_state"] = {
        "snapshot_time": snapshot.time.isoformat(),
        "battery_voltage_V": snapshot.battery_voltage_V,
        "bus_voltage_V": snapshot.bus_voltage_V,
        "total_load_W": snapshot.total_load_W,
        "phase": snapshot.phase,
        "current_task_unit_power_W": snapshot.unit_power_W,
        "background_load_W": background_load_W,
        "solar_source": solar_source,
        "solar_bus_transfer_efficiency": (
            float(solar_provider.bus_transfer_efficiency)
            if solar_provider is not None
            and hasattr(solar_provider, "bus_transfer_efficiency")
            else None),
        "solar_load_following_charge_margin_W":
            solar_load_following_margin_W,
        "solar_provider_file": (
            str(Path(solar_provider.path))
            if solar_provider is not None
            and getattr(solar_provider, "path", None) else None),
        "solar_provider_file_sha256": (
            sha256_file(solar_provider.path)
            if solar_provider is not None
            and getattr(solar_provider, "path", None) else None),
        "phase_segments": [{
            "start": segment["start"].isoformat(),
            "end": segment["end"].isoformat(),
            "phase": segment["phase"],
        } for segment in phase_segments],
    }
    summary["evidence"]["solar_input_status"] = (
        "当前精确分钟遥测为阴影区，eclipse-only 方案按 0 W"
        if solar_source == "eclipse_only_zero"
        else f"太阳阵功率模块: {solar_source}")
    if solar_source == "eclipse_only_zero":
        summary["warnings"].append(
            "eclipse-only 方案假定 [time,end) 全程位于阴影区；"
            "跨出阴影前应使用定轨模块核对")
        if summary["status"] == "completed":
            summary["status"] = "completed_with_warnings"
    summary["evidence"]["background_load_status"] = (
        "由精确分钟遥测的总负载减去任务单机当前功率推导")
    summary["warnings"].append(
        "背景负载按 time 时刻的遥测快照保持恒定；"
        "未列入任务的热控等负载变化未单独建模")
    if summary["status"] == "completed":
        summary["status"] = "completed_with_warnings"
    summary["evidence"]["not_modeled"] = [
        item for item in summary["evidence"].get("not_modeled", [])
        if item != "unlisted loads not included in background_load"
    ]
    summary["evidence"]["not_modeled"].append(
        "unlisted load changes: background load is held constant at snapshot")
    summary["snapshot"] = snapshot.provenance
    summary["provenance"]["request_file"] = (
        str(request_reference) if isinstance(request_reference, (str, Path)) else None)
    summary["provenance"]["request_file_sha256"] = (
        sha256_file(request_reference)
        if isinstance(request_reference, (str, Path)) else None)
    summary["provenance"]["model_file"] = (
        str(model_reference) if isinstance(model_reference, (str, Path)) else None)
    summary["provenance"]["model_file_sha256"] = (
        sha256_file(model_reference)
        if isinstance(model_reference, (str, Path)) else None)
    return timeline, _json_safe(summary)


def render_markdown_report(model_document: dict, example_summary: dict | None = None) -> str:
    models = model_document["models"]
    counts = model_document.get("model_counts", {})
    source_csv_name = Path(
        model_document.get("created_from", {}).get("component_csv", "component CSV")
    ).name
    satellite_label = {
        "SCS-01-01": "三体01星",
        "SCS-01-02": "三体02星",
        "YJ-3D": "云尖3D星",
    }.get(model_document["satellite_id"], model_document["satellite_id"])
    if model_document["satellite_id"] == "YJ-3D":
        solar_target_rule = (
            "- 云尖3D太阳阵目标采用直接代理：`TMEA003×TMEA007`，不使用方案B；"
            "输出再乘模型中的经验母线传输效率，并按总负载加充电余量封顶。")
        solar_target_boundary = (
            "- 太阳阵目标采用`TMEA003×TMEA007`母线电压条件代理，"
            "再按经验母线传输效率折算并施加负荷跟随上界；"
            "两者都不是太阳阵端独立测量。")
    else:
        solar_target_rule = (
            "- 01星历史目标采用方案B：`P_load(TMD016×TMD018)+P_charge_est`。")
        solar_target_boundary = (
            "- 太阳阵目标采用用户确认的方案B，`P_charge_est`继续受TMD017测点边界影响，"
            "不是独立阵列或充电功率测量。")
    lines = [
        f"# {satellite_label}单机功率模型与出阴影期蓄电池预测",
        "",
        f"> 模型版本 `{model_document['schema_version']}`；卫星 `{model_document['satellite_id']}`；"
        f"校准窗口 `{model_document['created_from']['time_start']}` 至 "
        f"`{model_document['created_from']['time_end']}`。",
        "",
        "## 1. 模型口径",
        "",
        f"校准数据来自 `{source_csv_name}` 的同分钟单机功率代理。"
        "仅有支路电流时，它是“母线电压 × 支路电流”的等价功率；有独立电压/电流对时，"
        "它是“分钟平均电压 × 分钟平均电流”。缺失、非有限和负值不按零处理。"
        "已知 `max_power_W` 时，超过配置筛选上限的点直接排除；否则公共过滤器"
        "只去除极端功率尾部野值。所有剔除数量和原始最大值保存在模型 JSON。",
        "",
        "每个单机先检测经验功率层；若 active 层内部还存在按比值明显分离的高功率瞬态，"
        "则拆成 `low/active/high` 三层并优先采用分层恒定功率；"
        "当前高功率分层要求中位功率至少为稳态层的 14 倍。"
        "剩余时间多项式使用运行片段分组留出，并在验证中施加与部署完全相同的 "
        "active 层 p05–p95 截断。只有截断后 RMSE 至少改善 10%、支撑区间单调且"
        "首末误差不超过恒定基线 10% 时才会选为默认模型。",
        "",
        "当配置中有同机状态映射时，优先按状态拟合恒功率；没有确认映射的单机由未来任务计划"
        "明确给出 `on/off/active/idle` 或 `power_level`。高功率瞬态使用 "
        "`power_level: \"high\"`；状态量本身不重复计入负载。",
        "",
        "## 2. 单机模型结果",
        "",
        "| 单机 ID | 名称 | 模型 | 工作功率 W | 高功率层 W | 低功率层 W | 样本 | p95 W | 说明 |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for unit_id, model in models.items():
        stats = model.get("stats", {})
        active = model.get("active_power_W")
        if model.get("polynomial", {}).get("selected"):
            coefficient = model["polynomial"]["coefficients_W_per_hour_power"][0]
            active_text = f"P(0)={coefficient:.3f}"
        else:
            active_text = "—" if active is None else f"{active:.3f}"
        low = model.get("low_power_W")
        high = model.get("high_power_W")
        note = "；".join(model.get("warnings", [])) or model.get("evidence_level", "")
        lines.append(
            f"| `{unit_id}` | {model.get('name', unit_id)} | `{model.get('model_type')}` | "
            f"{active_text} | {'—' if high is None else f'{high:.3f}'} | "
            f"{'—' if low is None else f'{low:.3f}'} | "
            f"{model.get('observations', {}).get('valid_samples', 0)} | "
            f"{stats.get('p95_W') if stats.get('p95_W') is not None else '—'} | {note} |")
    lines.extend([
        "",
        f"模型数量：`{json.dumps(counts, ensure_ascii=False, sort_keys=True)}`。"
        "低功率层是观测聚类，不等同于物理下电；只有任务计划明确 `off` 时才按 0 W 计。",
        "高功率层是同一 active 状态内按 log 功率比值识别的瞬态观测层；"
        "`active` 默认只使用稳态中位数，不能用它代表全部 active 负载。",
        "",
        "### 多项式细节",
        "",
    ])
    selected = [item for item in models.items()
                if item[1].get("polynomial", {}).get("selected")]
    if selected:
        for unit_id, model in selected:
            polynomial = model["polynomial"]
            cv = polynomial["cross_validation"]
            lines.append(
                f"- `{unit_id}`：系数（按小时幂次升序）"
                f"`{polynomial['coefficients_W_per_hour_power']}`，支撑 "
                f"`{polynomial['support_hours']} h`，原始留出 RMSE "
                f"`{cv.get('raw_polynomial_rmse_W')}` W，运行时截断 "
                f"`{polynomial.get('runtime_clip_W')}` W 后 RMSE "
                f"`{cv['polynomial_rmse_W']}` W，恒定模型 "
                f"`{cv['constant_rmse_W']}` W；首末 MAE "
                f"`{polynomial.get('endpoint_polynomial_mae_W')}` W；"
                f"单调性 `{polynomial.get('monotonic_within_support')}`。")
    else:
        lines.append("- 本批数据没有单机达到跨运行片段 10% 的误差改善门槛，因此全部保留恒功率模型。")
    lines.extend([
        "",
        "## 3. 预测输入",
        "",
        "业务入口按“时间窗口 + 单机任务”配置。SOC、当前负载、背景负载、"
        "任务单机当前功率和日周期太阳阵功率由模型自动补齐。",
        "",
        "```json",
        "{",
        '  "time": "2025-12-29T05:21:00+08:00",',
        '  "prediction": {',
        '    "end": "2025-12-29T05:45:00+08:00",',
        '    "step_seconds": 60,',
        '    "phase_segments": [',
        '      {"start": "05:21", "end": "05:45", "phase": "eclipse"}',
        "    ],",
        '    "tasks": [',
        '      {"start": "05:24", "end": "05:29", "unit": "compute_nx1_f"},',
        '      {"start": "05:30", "end": "05:35", "unit": "microwave_2a",',
        '       "run_mode": "active", "after_mode": "off"}',
        "    ]",
        "  }",
        "}",
        "```",
        "",
        "规则：",
        "",
        "- `time`必须是整分钟；模型精确查询该分钟的 `TMD016/018/019` 和任务单机功率测点。",
        "- `TMD019`与项目V-SOC曲线自动计算初始SOC；`TMD016×TMD018`自动计算当前总负载。",
        "- 只减去任务单机当前功率得到背景负载；未列入任务的单机和非单机负载均留在背景负载中。",
        "- `run_mode`对`[start,end)`生效，省略时为`active`。",
        "- `after_mode`从`end`持续到该单机下一任务开始或预测结束，省略时为`restore`，"
        "即恢复`time`时刻的当前遥测功率。",
        "- `unit`和`units`二选一；同一窗口内同模式的单机放在`units`列表，避免重复配置。",
        "- 任务只写`HH:MM[:SS]`时日期继承`time`；跨日必须写完整日期时间。",
        "- `phase_segments`必须连续、有序、无重叠，并完整覆盖`[time,end)`；"
        "`phase`可取`sun/eclipse`或阳照/阴影别名。",
        "- 阴影片段太阳阵功率固定为0 W；阳照片段使用历史多项式模型。",
        solar_target_rule,
        "",
        "## 4. 预测输出",
        "",
        "JSON 输出给出：目标时刻 SOC、剩余 Ah、按额定容量×标称电压折算的剩余 kWh、"
        "最低 SOC、到 SOC 下限的可用 Ah、太阳能/负载/充放电能量、各单机任务耗能、"
        "由单机模型 p05–p95 半范围或多项式交叉验证 RMSE 近似合成的经验功率散布、"
        "触底/触顶时长、假设和证据边界。CSV 时间线给出每个步长的负载、净功率、SOC 和单机功率。",
        "",
        "电池记账模型为：",
        "",
        "```text",
        "E_next = E + eta_charge × max(P_solar - P_load, 0) × dt",
        "E_next = E - max(P_load - P_solar, 0) / eta_discharge × dt",
        "SOC = E / (capacity_Ah × nominal_voltage_V)",
        "```",
        "",
        "能量和 SOC 在 `soc_floor` 与 100% 处截断；超过可用电量时记录未供能量。"
        "这是用于任务规划的簿记预测，不是电池容量、温度、滞回或 SOH 的在轨验证。",
        "",
    ])
    if example_summary:
        target = example_summary["target_end_state"]
        minimum = example_summary["minimum_state"]
        lines.extend([
            "## 5. 已落盘样例结果",
            "",
            f"示例使用 `{example_summary['window']['start']}` 至 "
            f"`{example_summary['window']['end']}`。模型自动查询当前遥测并推导"
            f"当前总负载`{example_summary.get('derived_state', {}).get('total_load_W', 0):.3f} W`、"
            f"背景负载`{example_summary.get('derived_state', {}).get('background_load_W', 0):.3f} W`；"
            f"太阳阵源`{example_summary.get('derived_state', {}).get('solar_source', '')}`按"
            "`phase_segments`生成功率。",
            "",
            f"- 初始 SOC：`{example_summary['initial_state']['soc']:.4f}`；"
            f"来源 `{example_summary['initial_state']['soc_source']}`。",
            f"- 目标时刻 SOC：`{target['soc']:.4f}`；剩余 "
            f"`{target['remaining_Ah']:.3f} Ah` / "
            f"`{target['remaining_energy_nominal_kWh']:.3f} kWh`；"
            f"单机模型经验功率散布约 "
            f"`±{target.get('soc_half_spread_from_power_models', 0):.4f} SOC`。",
            f"- 最低 SOC：`{minimum['soc']:.4f}`，时刻 `{minimum['time']}`。",
            f"- 期间负载 `{example_summary['energy']['load_kWh']:.3f} kWh`，"
            f"电池储存侧放电 `{example_summary['energy']['battery_discharge_stored_kWh']:.3f} kWh`。",
            "",
            "样例只验证任务计划和计算闭环，不代表真实未来任务的成功执行或飞行级精度。",
            "",
        ])
    lines.extend([
        "## 6. 复现命令",
        "",
        "```powershell",
        "python unit_power_model.py calibrate `",
        "  --component-csv satellites/SCS-01-01/out/energy_v2_components.csv `",
        "  --components-config satellites/SCS-01-01/config/components.json `",
        "  --out-json satellites/SCS-01-01/power_model/unit_power_models.json `",
        "  --out-report satellites/SCS-01-01/power_model/unit_power_model_report.md `",
        "  --out-summary-csv satellites/SCS-01-01/power_model/unit_power_model_summary.csv",
        "",
        "python solar_power_calibration.py `",
        "  --result-csv satellites/SCS-01-01/out/energy_v2_result.csv `",
        "  --satellite-id SCS-01-01 `",
        "  --out satellites/SCS-01-01/power_model/solar_power_model.json `",
        "  --out-report satellites/SCS-01-01/power_model/solar_power_model_report.md",
        "",
        "$env:CH_ALLOW_PLAINTEXT='1' # 仅在该项目已授权的本地只读连接环境使用",
        "python unit_power_model.py forecast `",
        "  --models satellites/SCS-01-01/power_model/unit_power_models.json `",
        "  --request satellites/SCS-01-01/power_model/example_prediction_input.json `",
        "  --profile-dir satellites/SCS-01-01/config `",
        "  --transport http `",
        "  --solar-model satellites/SCS-01-01/power_model/solar_power_model.json `",
        "  --out-json satellites/SCS-01-01/power_model/example_prediction_output.json `",
        "  --out-csv satellites/SCS-01-01/power_model/example_prediction_timeline.csv",
        "```",
        "",
        "## 7. 证据边界",
        "",
        "- 单机功率测点可能与其他支路重叠，当前 25 路负载支路和并未与 `TMD018` 总负载闭合。",
        "- 除科学载荷外，本批配置没有已确认的单机状态到功率支路映射；未来任务必须明确单机工况。",
        "- 背景负载由当前总负载减去任务单机当前功率得到，并在窗口中按恒定值保持；"
        "未列入任务的热控等负载变化尚未建模。",
        "- 阳照片段多项式只使用片段内经历小时建模；温度、太阳入射角、姿态、"
        "S3R工作点和阵列老化未显式建模。",
        solar_target_boundary,
        "- 多项式超出历史运行片段支撑时长时按边界截断，再按 active 层的 "
        "p05–p95 做运行截断；离线验证使用同一规则。",
        "- `active/high` 是基于观测功率比值的统计分层，不等同于硬件状态真值；"
        "高功率任务必须显式提供 `power_level: \"high\"`。",
        "- 电池端真实净功率、充放电方向和母线损耗仍需独立测量与拓扑核验。",
        "- 经验功率散布取单机 p05–p95 半范围或多项式留出 RMSE 的平方和近似，"
        "不是概率置信区间；不同单机误差相关性和背景负载误差均未量化。",
    ])
    return "\n".join(lines) + "\n"


def model_summary_frame(model_document: dict) -> pd.DataFrame:
    rows = []
    for unit_id, model in model_document["models"].items():
        stats = model.get("stats", {})
        polynomial = model.get("polynomial", {})
        cv = polynomial.get("cross_validation", {})
        outlier_filter = model.get("outlier_filter", {})
        rows.append({
            "unit_id": unit_id,
            "name": model.get("name", unit_id),
            "model_type": model.get("model_type"),
            "usable_for_prediction": model.get("usable_for_prediction", False),
            "active_power_W": model.get("active_power_W"),
            "low_power_W": model.get("low_power_W"),
            "high_power_W": model.get("high_power_W"),
            "active_threshold_W": model.get("active_threshold_W"),
            "high_threshold_W": model.get("high_threshold_W"),
            "n_valid": model.get("observations", {}).get("valid_samples", 0),
            "missing_fraction": model.get("observations", {}).get("missing_fraction"),
            "p50_W": stats.get("p50_W"),
            "p95_W": stats.get("p95_W"),
            "max_W": stats.get("max_W"),
            "outlier_method": outlier_filter.get("method"),
            "outlier_limit_W": outlier_filter.get("upper_limit_W"),
            "outlier_samples": outlier_filter.get("outlier_samples"),
            "raw_max_W": outlier_filter.get("raw_max_W"),
            "power_bands": len(model.get("power_bands", [])),
            "explicit_modes": len(model.get("modes", {})),
            "polynomial_selected": polynomial.get("selected", False),
            "polynomial_episodes": polynomial.get("episodes"),
            "polynomial_cv_improvement": cv.get("relative_improvement"),
            "warnings": " | ".join(model.get("warnings", [])),
        })
    return pd.DataFrame(rows)


def _write_models_and_report(models: dict, out_json: Path, out_report: Path,
                             example_summary: dict | None = None) -> None:
    _atomic_write_text(
        out_json, json.dumps(_json_safe(models), ensure_ascii=False,
                             indent=2, allow_nan=False))
    _atomic_write_text(
        out_report, render_markdown_report(models, example_summary))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    calibrate = sub.add_parser("calibrate", help="从部件分钟 CSV 拟合单机功率模型")
    calibrate.add_argument("--component-csv", type=Path, required=True)
    calibrate.add_argument("--components-config", type=Path, required=True)
    calibrate.add_argument("--satellite-id", default="SCS-01-01")
    calibrate.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    calibrate.add_argument("--max-gap-minutes", type=float, default=5.0)
    calibrate.add_argument("--min-samples", type=int, default=30)
    calibrate.add_argument("--polynomial-degree", type=int, default=2)
    calibrate.add_argument(
        "--exclude-dates-file", type=Path,
        help="每行一个 YYYY-MM-DD；用于整日分组留出")
    calibrate.add_argument("--out-json", type=Path, required=True)
    calibrate.add_argument("--out-report", type=Path, required=True)
    calibrate.add_argument("--out-summary-csv", type=Path,
                           help="可选：输出单机模型摘要 CSV")
    calibrate.add_argument("--example-summary", type=Path,
                           help="可选：把已生成预测摘要写入模型报告")

    predict = sub.add_parser("predict", help="按未来任务计划预测出阴影期电池状态")
    predict.add_argument("--models", type=Path, required=True)
    predict.add_argument("--request", type=Path, required=True)
    predict.add_argument("--v-soc-curve", type=Path)
    predict.add_argument("--design-params", type=Path)
    predict.add_argument("--out-json", type=Path, required=True)
    predict.add_argument("--out-csv", type=Path, required=True)

    report = sub.add_parser("report", help="只渲染已有模型和可选预测摘要的报告")
    report.add_argument("--models", type=Path, required=True)
    report.add_argument("--example-summary", type=Path)
    report.add_argument("--out-report", type=Path, required=True)

    forecast = sub.add_parser(
        "forecast", help="按 time/end/step/tasks 自动取当前遥测并预测")
    forecast.add_argument("--models", type=Path, required=True)
    forecast.add_argument("--request", type=Path, required=True)
    forecast.add_argument(
        "--profile-dir", type=Path,
        default=Path(__file__).resolve().parent / "satellites" /
        "SCS-01-01" / "config")
    forecast.add_argument("--v-soc-curve", type=Path)
    forecast.add_argument("--design-params", type=Path)
    forecast.add_argument("--transport", choices=("native", "http"),
                          default="http")
    forecast.add_argument(
        "--solar-module", type=Path,
        help="可信本地模块，导出 build_profile(start,end,step_seconds[,snapshot])")
    forecast.add_argument(
        "--solar-model", type=Path,
        help="历史太阳阵多项式模型；默认使用模型库同目录 solar_power_model.json")
    forecast.add_argument("--out-json", type=Path, required=True)
    forecast.add_argument("--out-csv", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "calibrate":
        exclude_dates = []
        if args.exclude_dates_file:
            exclude_dates = load_excluded_dates(args.exclude_dates_file)
        document = calibrate_unit_models(
            args.component_csv, args.components_config,
            satellite_id=args.satellite_id,
            timezone_name=args.timezone,
            max_gap_minutes=args.max_gap_minutes,
            min_samples=args.min_samples,
            polynomial_degree=args.polynomial_degree,
            exclude_dates=exclude_dates)
        example_summary = (_read_json(args.example_summary)
                           if args.example_summary else None)
        _write_models_and_report(document, args.out_json, args.out_report,
                                 example_summary)
        if args.out_summary_csv:
            _atomic_write_frame(
                model_summary_frame(document), args.out_summary_csv)
        print(f"已保存 {len(document['models'])} 个单机模型: {args.out_json}")
        print(json.dumps(document["model_counts"], ensure_ascii=False))
    elif args.command == "predict":
        timeline, summary = predict_battery_state(
            args.models, args.request, v_soc_curve=args.v_soc_curve,
            design_params=args.design_params)
        _atomic_write_text(
            args.out_json, json.dumps(summary, ensure_ascii=False,
                                      indent=2, allow_nan=False))
        _atomic_write_frame(timeline, args.out_csv)
        print(f"目标时刻 SOC={summary['target_end_state']['soc']:.4f}; "
              f"剩余={summary['target_end_state']['remaining_Ah']:.3f} Ah")
    elif args.command == "report":
        document = _load_model_document(args.models)
        example_summary = (_read_json(args.example_summary)
                           if args.example_summary else None)
        _atomic_write_text(
            args.out_report,
            render_markdown_report(document, example_summary))
        print(f"已保存模型报告: {args.out_report}")
    else:
        from future_forecast_runtime import ClickHouseSnapshotProvider
        from solar_power_provider import (EclipseOnlySolarProvider,
                                          ModuleSolarPowerProvider,
                                          PolynomialSolarPowerProvider)

        profile_dir = Path(args.profile_dir)
        v_soc_curve = args.v_soc_curve or profile_dir / "v_soc_curve.json"
        design_params = args.design_params or profile_dir / "design_params.json"
        snapshot_provider = ClickHouseSnapshotProvider(
            profile_dir, transport=args.transport)
        solar_model_path = (args.solar_model
                            or Path(args.models).parent / "solar_power_model.json")
        if args.solar_module:
            solar_provider = ModuleSolarPowerProvider(args.solar_module)
        elif solar_model_path.is_file():
            solar_provider = PolynomialSolarPowerProvider(
                _read_json(solar_model_path), model_path=solar_model_path)
        else:
            solar_provider = EclipseOnlySolarProvider()
        timeline, summary = forecast_from_plan(
            args.request, args.models, snapshot_provider,
            v_soc_curve=v_soc_curve, design_params=design_params,
            solar_provider=solar_provider)
        _atomic_write_text(
            args.out_json, json.dumps(summary, ensure_ascii=False,
                                      indent=2, allow_nan=False))
        _atomic_write_frame(timeline, args.out_csv)
        print(f"目标时刻 SOC={summary['target_end_state']['soc']:.4f}; "
              f"剩余={summary['target_end_state']['remaining_Ah']:.3f} Ah; "
              f"背景负载={summary['derived_state']['background_load_W']:.3f} W")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
