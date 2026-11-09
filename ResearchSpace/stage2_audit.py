#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic audit metrics for satellite second-stage forecast results."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
from zoneinfo import ZoneInfo

from stage2_cases import load_representative_case
from stage2_framework import (
    audit_metric_from_summary,
    load_profile_contract,
    load_validation_split,
    primary_profile_path,
)


SCHEMA_VERSION = "1.0"
BOOTSTRAP_SEED = 20261104
BOOTSTRAP_SAMPLES = 10000
SOC_SATURATION_EPS = 1e-6


def _finite_numbers(values):
    numbers = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numbers.append(number)
    return numbers


def _quantile(values, probability):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("分位数输入不能为空")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * float(probability)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def bootstrap_mean_ci(values, *, samples=BOOTSTRAP_SAMPLES,
                      seed=BOOTSTRAP_SEED, confidence=.95):
    """Return a deterministic percentile bootstrap interval for the mean."""
    numbers = _finite_numbers(values)
    if not numbers:
        raise ValueError("bootstrap 输入不能为空")
    if samples < 1:
        raise ValueError("bootstrap samples 必须为正整数")
    if not 0 < confidence < 1:
        raise ValueError("confidence 必须在 (0,1) 内")
    rng = random.Random(int(seed))
    size = len(numbers)
    means = []
    for _ in range(int(samples)):
        means.append(sum(numbers[rng.randrange(size)] for _ in range(size)) / size)
    tail = (1 - confidence) / 2
    return {
        "mean": statistics.fmean(numbers),
        "lower": _quantile(means, tail),
        "upper": _quantile(means, 1 - tail),
        "confidence": float(confidence),
        "samples": int(samples),
        "seed": int(seed),
    }


def _weighted_mean(values, weights):
    pairs = []
    for value, weight in zip(values, weights):
        try:
            number = float(value)
            mass = float(weight)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and math.isfinite(mass) and mass >= 0:
            pairs.append((number, mass))
    denominator = sum(weight for _, weight in pairs)
    return None if denominator <= 0 else sum(
        value * weight for value, weight in pairs) / denominator


def summarize_metric(values, *, dates, durations=None, energy_weights=None,
                     samples=BOOTSTRAP_SAMPLES, seed=BOOTSTRAP_SEED):
    """Summarize a metric at window level and with duration/energy masses."""
    numbers = _finite_numbers(values)
    if not numbers:
        raise ValueError("指标序列不能为空")
    if not (len(values) == len(dates)
            == (len(durations) if durations is not None else len(values))
            == (len(energy_weights) if energy_weights is not None else len(values))):
        raise ValueError("指标、日期、时长和能量权重长度不一致")
    effective_dates = sorted({
        str(date) for date, value in zip(dates, values)
        if value is not None and date not in (None, "")})
    ci = bootstrap_mean_ci(numbers, samples=samples, seed=seed)
    return {
        "n_windows": len(numbers),
        "effective_dates": len(effective_dates),
        "date_values": effective_dates,
        "window_equal": {
            "mean": statistics.fmean(numbers),
            "median": statistics.median(numbers),
            "p95": _quantile(numbers, .95),
            "bootstrap_95_ci": [ci["lower"], ci["upper"]],
            "bootstrap": ci,
        },
        "duration_weighted_mean": (
            _weighted_mean(values, durations)
            if durations is not None else None),
        "energy_weighted_mean": (
            _weighted_mean(values, energy_weights)
            if energy_weights is not None else None),
    }


def _soc_group(rows, observed_key, predicted_key, error_key):
    errors = []
    zero_count = 0
    ceiling_count = 0
    for row in rows:
        try:
            error = abs(float(row[error_key]))
            predicted = float(row[predicted_key])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(error) or not math.isfinite(predicted):
            continue
        errors.append(error)
        zero_count += int(error < 1e-12)
        ceiling_count += int(predicted >= 1 - SOC_SATURATION_EPS)
    return {
        "n_windows": len(errors),
        "mae_pp": statistics.fmean(errors) if errors else None,
        "p95_abs_pp": _quantile(errors, .95) if errors else None,
        "zero_error_count": zero_count,
        "predicted_ceiling_count": ceiling_count,
    }


def partition_soc_saturation(rows, *, observed_key, predicted_key, error_key):
    """Partition windows by the observed reference reaching 100% SOC."""
    saturated = []
    unsaturated = []
    for row in rows:
        try:
            observed = float(row[observed_key])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(observed):
            continue
        (saturated if observed >= 1 - SOC_SATURATION_EPS
         else unsaturated).append(row)
    return {
        "criterion": (
            "observed_target_soc >= 1 - 1e-6；"
            "预测触顶单独计数，不改变分组"),
        "all": _soc_group(rows, observed_key, predicted_key, error_key),
        "observed_saturated": _soc_group(
            saturated, observed_key, predicted_key, error_key),
        "observed_non_saturated": _soc_group(
            unsaturated, observed_key, predicted_key, error_key),
    }


def bcr_outlier_audit(values, cap_W):
    """Quantify BCR-output samples beyond the training/QC cap."""
    numbers = _finite_numbers(values)
    cap = float(cap_W)
    if not numbers:
        raise ValueError("BCR 审计输入不能为空")
    if not math.isfinite(cap) or cap < 0:
        raise ValueError("BCR 上限必须为非负有限数")
    total = sum(max(value, 0.0) for value in numbers)
    excess = sum(max(value - cap, 0.0) for value in numbers)
    above = [value for value in numbers if value > cap]
    return {
        "n_valid": len(numbers),
        "cap_W": cap,
        "min_W": min(numbers),
        "median_W": statistics.median(numbers),
        "p95_W": _quantile(numbers, .95),
        "p99_W": _quantile(numbers, .99),
        "max_W": max(numbers),
        "above_cap_count": len(above),
        "above_cap_fraction": len(above) / len(numbers),
        "excess_energy_fraction": None if total <= 0 else excess / total,
        "total_observed_energy_proxy": total,
        "excess_energy_proxy": excess,
    }


def solar_structure_audit(model, contract=None):
    """Record whether the solar model separates availability and load pull."""
    contract = contract or {}
    features = list(model.get("feature_model", {}).get("features", []))
    coefficients = list(
        model.get("feature_model", {}).get("coefficients", []))
    load_coefficient = None
    for feature, coefficient in zip(features, coefficients):
        if feature in {"load_W", "predicted_total_load_W"}:
            try:
                load_coefficient = float(coefficient)
            except (TypeError, ValueError):
                load_coefficient = None
            break
    required = list(contract.get("required_features", []))
    missing = [feature for feature in required if feature not in features]
    return {
        "model_type": model.get("model_type"),
        "features": features,
        "load_coefficient": load_coefficient,
        "load_following_sensitivity": bool(
            load_coefficient is not None and abs(load_coefficient) >= 0.8),
        "required_features": required,
        "missing_features": missing,
        "status": (
            str(contract.get("status"))
            if missing or contract.get("status")
            else "structural_features_available"
        ),
        "policy": (
            "当前模型只能作为负载感知代理；缺失可用发电特征时，"
            "不得把负载跟随系数解释为MPPT物理增益。"
        ),
    }


def _active_threshold(model):
    threshold = model.get("active_threshold_W")
    if threshold is not None:
        return float(threshold)
    active = model.get("active_power_W")
    return max(abs(float(active)) * .05, 1e-6) if active is not None else None


def ab_coactivity(rows, models):
    """Compare active-state minutes for matching PL_n A/B channels."""
    channels = defaultdict(dict)
    for unit_id, model in models.items():
        match = re.fullmatch(r"pl(\d+)_([ab])", str(unit_id))
        if match:
            channels[int(match.group(1))][match.group(2).upper()] = (
                unit_id, model)
    pair_specs = []
    for channel, banks in sorted(channels.items()):
        if set(banks) != {"A", "B"}:
            continue
        a_id, a_model = banks["A"]
        b_id, b_model = banks["B"]
        a_threshold = _active_threshold(a_model)
        b_threshold = _active_threshold(b_model)
        if a_threshold is None or b_threshold is None:
            continue
        pair_specs.append({
            "pl_channel": channel,
            "a_unit": a_id,
            "b_unit": b_id,
            "a_threshold_W": a_threshold,
            "b_threshold_W": b_threshold,
            "counts": {"both": 0, "a_only": 0, "b_only": 0, "neither": 0},
        })
    for row in rows:
        for pair in pair_specs:
            try:
                a_power = float(row[f"P_{pair['a_unit']}_W"])
                b_power = float(row[f"P_{pair['b_unit']}_W"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(a_power) and math.isfinite(b_power)
                    and a_power >= 0 and b_power >= 0):
                continue
            a_active = a_power >= pair["a_threshold_W"]
            b_active = b_power >= pair["b_threshold_W"]
            counts = pair["counts"]
            if a_active and b_active:
                counts["both"] += 1
            elif a_active:
                counts["a_only"] += 1
            elif b_active:
                counts["b_only"] += 1
            else:
                counts["neither"] += 1
    pairs = []
    for pair in pair_specs:
        counts = pair.pop("counts")
        valid = sum(counts.values())
        if not valid:
            continue
        pairs.append({
            **pair,
            "valid_minutes": valid,
            "both_active_minutes": counts["both"],
            "a_only_minutes": counts["a_only"],
            "b_only_minutes": counts["b_only"],
            "neither_active_minutes": counts["neither"],
            "both_active_fraction": counts["both"] / valid,
            "either_active_fraction": (
                counts["both"] + counts["a_only"] + counts["b_only"]) / valid,
        })
    pairs.sort(key=lambda item: (
        -item["both_active_minutes"], item["pl_channel"]))
    return {
        "method": (
            "同编号PL_n A/B通道；阈值取active_threshold_W，"
            "无阈值时取active_power_W的5%且不小于1e-6 W"),
        "n_pairs": len(pairs),
        "pairs": pairs,
    }


def ab_topology_audit(coactivity, contract=None):
    """Attach an explicit aggregation mode to every comparable A/B pair."""
    contract = contract or {}
    allowed = set(contract.get(
        "allowed_aggregation_modes", ("sum", "or", "select", "unresolved")))
    approvals = {}
    for item in contract.get("pairs", []):
        try:
            channel = int(item["pl_channel"])
        except (KeyError, TypeError, ValueError):
            continue
        mode = str(item.get("aggregation_mode", "unresolved"))
        if mode not in allowed:
            raise ValueError(f"PL_{channel} A/B aggregation_mode 不受支持: {mode}")
        approvals[channel] = {
            "aggregation_mode": mode,
            "physical_unit": item.get("physical_unit"),
            "source": item.get("source"),
        }
    default_mode = str(contract.get(
        "default_aggregation_mode", "unresolved"))
    if default_mode not in allowed:
        raise ValueError("A/B 默认 aggregation_mode 不受支持")
    pairs = []
    for item in coactivity.get("pairs", []):
        channel = int(item["pl_channel"])
        approval = approvals.get(channel, {})
        mode = approval.get("aggregation_mode", default_mode)
        resolved = mode in {"sum", "or", "select"}
        pairs.append({
            "pl_channel": channel,
            "a_unit": item.get("a_unit"),
            "b_unit": item.get("b_unit"),
            "physical_unit": approval.get("physical_unit"),
            "aggregation_mode": mode,
            "resolved": resolved,
            "source": (
                approval.get("source")
                or contract.get("source_document")
            ),
        })
    unresolved = sum(not item["resolved"] for item in pairs)
    return {
        "status": str(contract.get("status", "unresolved")),
        "source_document": contract.get("source_document"),
        "policy": contract.get(
            "policy",
            "A/B拓扑未确认时不得自动求和、去重或选择单侧。"),
        "n_pairs": len(pairs),
        "n_resolved": len(pairs) - unresolved,
        "n_unresolved": unresolved,
        "blocks_task_aggregation": bool(unresolved),
        "pairs": pairs,
    }


def _parse_time(value):
    return datetime.fromisoformat(str(value))


def rolling_background_comparison(observations, windows, *,
                                  lookbacks=(30, 60),
                                  min_history_minutes=5):
    """Compare fixed and strictly causal rolling background estimates."""
    normalized = []
    for row in observations:
        try:
            stamp = _parse_time(row["t"])
            power = float(row["background_W"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(power) or power < 0:
            continue
        normalized.append({
            "t": stamp,
            "phase": str(row.get("phase", "")),
            "background_W": power,
        })
    normalized.sort(key=lambda row: row["t"])
    results = []
    for window in windows:
        start = _parse_time(window["start"])
        end = _parse_time(window["end"])
        phase = str(window.get("phase", ""))
        actual = [
            item["background_W"] for item in normalized
            if start <= item["t"] <= end
            and (not phase or not item["phase"] or item["phase"] == phase)]
        before_or_at = [
            item for item in normalized
            if item["t"] <= start
            and (not phase or not item["phase"] or item["phase"] == phase)]
        fixed_value = before_or_at[-1]["background_W"] if before_or_at else None
        observed_mean = statistics.fmean(actual) if actual else None
        models = {
            "fixed": {
                "predicted_background_W": fixed_value,
                "observed_background_mean_W": observed_mean,
                "signed_error_W": (
                    fixed_value - observed_mean
                    if fixed_value is not None and observed_mean is not None
                    else None),
                "absolute_error_W": (
                    abs(fixed_value - observed_mean)
                    if fixed_value is not None and observed_mean is not None
                    else None),
                "relative_error_pct": (
                    (fixed_value - observed_mean) / observed_mean * 100
                    if fixed_value is not None and observed_mean not in (None, 0)
                    else None),
            },
        }
        for lookback in lookbacks:
            history = [
                item["background_W"] for item in normalized
                if start - timedelta(minutes=int(lookback))
                <= item["t"] < start
                and (not phase or not item["phase"] or item["phase"] == phase)]
            enough = len(history) >= int(min_history_minutes)
            predicted = statistics.median(history) if enough else None
            models[f"rolling_{int(lookback)}m"] = {
                "predicted_background_W": predicted,
                "observed_background_mean_W": observed_mean,
                "signed_error_W": (
                    predicted - observed_mean
                    if predicted is not None and observed_mean is not None
                    else None),
                "absolute_error_W": (
                    abs(predicted - observed_mean)
                    if predicted is not None and observed_mean is not None
                    else None),
                "relative_error_pct": (
                    (predicted - observed_mean) / observed_mean * 100
                    if predicted is not None and observed_mean not in (None, 0)
                    else None),
                "history_minutes_available": len(history),
                "status": "ok" if enough else "insufficient_history",
            }
        results.append({
            "start": window["start"],
            "end": window["end"],
            "phase": phase,
            "observed_minutes": len(actual),
            "models": models,
        })
    model_names = ["fixed"] + [f"rolling_{int(value)}m" for value in lookbacks]
    summary = {}
    for name in model_names:
        evaluable = [
            item for item in results
            if item["models"][name]["absolute_error_W"] is not None]
        relative = [
            item["models"][name]["relative_error_pct"] for item in evaluable
            if item["models"][name]["relative_error_pct"] is not None]
        summary[name] = {
            "n_windows": len(evaluable),
            "mean_absolute_error_W": (
                statistics.fmean(
                    item["models"][name]["absolute_error_W"]
                    for item in evaluable)
                if evaluable else None),
            "mean_relative_error_pct": (
                statistics.fmean(relative) if relative else None),
        }
    return {
        "method": (
            "固定值取窗口起点或此前最近同工况值；滚动值只使用"
            "窗口开始前同工况分钟的中位数，不读取窗口内未来遥测"),
        "windows": results,
        "summary": summary,
    }


def hierarchical_background_comparison(
        observations,
        windows,
        *,
        rolling_lookback_minutes=60,
        min_history_minutes=5,
        previous_day_tolerance_minutes=15,
        min_previous_day_minutes=5,
):
    """Select rolling, previous-day, then fixed background without leakage."""
    normalized = []
    for row in observations:
        try:
            stamp = _parse_time(row["t"])
            power = float(row["background_W"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(power) or power < 0:
            continue
        normalized.append({
            "t": stamp,
            "phase": str(row.get("phase", "")),
            "background_W": power,
        })
    normalized.sort(key=lambda row: row["t"])
    results = []
    for window in windows:
        start = _parse_time(window["start"])
        end = _parse_time(window["end"])
        phase = str(window.get("phase", ""))
        matching = [
            item for item in normalized
            if not phase or not item["phase"] or item["phase"] == phase
        ]
        actual = [
            item["background_W"] for item in matching
            if start <= item["t"] <= end
        ]
        observed_mean = statistics.fmean(actual) if actual else None
        before_or_at = [
            item for item in matching if item["t"] <= start
        ]
        fixed = before_or_at[-1]["background_W"] if before_or_at else None
        rolling_history = [
            item["background_W"] for item in matching
            if start - timedelta(minutes=int(rolling_lookback_minutes))
            <= item["t"] < start
        ]
        previous_target = start - timedelta(days=1)
        previous_history = [
            item["background_W"] for item in matching
            if abs((item["t"] - previous_target).total_seconds())
            <= float(previous_day_tolerance_minutes) * 60
        ]
        if len(rolling_history) >= int(min_history_minutes):
            predicted = statistics.median(rolling_history)
            source = "rolling_60m"
        elif len(previous_history) >= int(min_previous_day_minutes):
            predicted = statistics.median(previous_history)
            source = "previous_day_same_phase"
        else:
            predicted = fixed
            source = "fixed"
        results.append({
            "start": window["start"],
            "end": window["end"],
            "phase": phase,
            "observed_minutes": len(actual),
            "observed_background_mean_W": observed_mean,
            "predicted_background_W": predicted,
            "selected_source": source,
            "rolling_history_minutes": len(rolling_history),
            "previous_day_history_minutes": len(previous_history),
            "fixed_background_W": fixed,
            "signed_error_W": (
                predicted - observed_mean
                if predicted is not None and observed_mean is not None
                else None),
            "absolute_error_W": (
                abs(predicted - observed_mean)
                if predicted is not None and observed_mean is not None
                else None),
        })
    evaluable = [
        item for item in results if item["absolute_error_W"] is not None]
    relative = [
        (item["predicted_background_W"]
         - item["observed_background_mean_W"])
        / item["observed_background_mean_W"] * 100
        for item in evaluable
        if item["observed_background_mean_W"] not in (None, 0)
    ]
    return {
        "method": (
            "优先窗口前60分钟同工况中位；历史不足时使用前一天"
            "同相位、同一时钟窗口前后容差内中位；再不足回退起点快照"),
        "orbit_phase_basis": (
            "未配置轨道周期时以同相位、同一时钟时刻作为代理；"
            "不声称已验证真实轨道相位"),
        "rolling_lookback_minutes": int(rolling_lookback_minutes),
        "previous_day_tolerance_minutes":
            int(previous_day_tolerance_minutes),
        "windows": results,
        "summary": {
            "n_windows": len(evaluable),
            "mean_absolute_error_W": (
                statistics.fmean(
                    item["absolute_error_W"] for item in evaluable)
                if evaluable else None),
            "mean_relative_error_pct": (
                statistics.fmean(relative) if relative else None),
            "selected_sources": dict(sorted(
                Counter(item["selected_source"] for item in results).items())),
        },
    }


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(profile_root, audit, *, input_paths, code_paths):
    """Build a stable manifest while hashing every declared input/code file."""
    root = Path(profile_root).resolve()

    def describe(path):
        resolved = Path(path).resolve()
        try:
            display = resolved.relative_to(root).as_posix()
        except ValueError:
            display = resolved.as_posix()
        return {
            "path": display,
            "sha256": sha256_file(resolved),
            "bytes": resolved.stat().st_size,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": audit["satellite_id"],
        "generated_at": datetime.now(
            ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "inputs": [describe(path) for path in input_paths],
        "code": [describe(path) for path in code_paths],
        "validation": audit.get("validation", {}),
        "model_selection": audit.get("model_selection", {}),
        "metrics": audit.get("metrics", {}),
        "metrics_context": audit.get("metrics_context", {}),
        "diagnostic_metrics": audit.get("diagnostic_metrics", {}),
        "diagnostic_metrics_context": audit.get(
            "diagnostic_metrics_context", {}),
        "data_coverage": audit.get("data_coverage"),
        "specialized_audits": {
            key: audit[key]
            for key in (
                "soc_saturation", "raw_soc_saturation", "background_load",
                "ab_coactivity", "ab_topology", "solar_structure",
                "primary_solar_structure",
                "bcr_outliers")
            if key in audit},
        "representative_case": audit.get("representative_case"),
        "evidence_boundaries": audit.get("evidence_boundaries", []),
    }


def _read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _load_primary_evaluation(profile_root, satellite_id):
    path = Path(profile_root) / "stage2_testset" / "evaluation_summary.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"缺少共享评测摘要，请先运行 stage2_testset.py: {path}")
    summary = _read_json(path)
    if summary.get("schema_version") != "1.0":
        raise ValueError("evaluation_summary.json schema_version 不受支持")
    if summary.get("satellite_id") != satellite_id:
        raise ValueError("evaluation_summary.json satellite_id 不一致")
    if not summary.get("subsets", {}).get("all"):
        raise ValueError("evaluation_summary.json 缺少 subsets.all")
    return path, summary


def _primary_metrics(summary):
    metrics = summary["subsets"]["all"]["metrics"]
    return {
        key: audit_metric_from_summary(
            metrics.get(key))
        for key in (
            "soc_abs_error_pp",
            "solar_energy_abs_relative_error_pct",
            "load_energy_abs_relative_error_pct",
        )
    }


def _evaluation_saturation(summary):
    rows = []
    for item in summary.get("cases", []):
        observed = item.get("observed_target_soc_pct")
        error = item.get("soc_error_pp")
        if observed is None or error is None or not item.get("soc_qc_clean"):
            continue
        observed_fraction = float(observed) / 100.0
        rows.append({
            "observed": observed_fraction,
            "predicted": observed_fraction + float(error) / 100.0,
            "error": float(error),
        })
    return partition_soc_saturation(
        rows,
        observed_key="observed",
        predicted_key="predicted",
        error_key="error",
    )


def _metric_from_rows(rows, *, value_key, duration_key, weight_key=None):
    values = []
    dates = []
    durations = []
    weights = []
    for row in rows:
        try:
            value = abs(float(row[value_key]))
            duration = float(row[duration_key])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(value) or not math.isfinite(duration):
            continue
        values.append(value)
        durations.append(duration)
        dates.append(str(row["start"])[:10])
        if weight_key:
            try:
                weight = abs(float(row[weight_key]))
            except (KeyError, TypeError, ValueError):
                weight = 0.0
            weights.append(weight if math.isfinite(weight) else 0.0)
        else:
            weights.append(duration)
    return summarize_metric(
        values, dates=dates, durations=durations, energy_weights=weights)


def _scs_audit(profile_root):
    contract = load_profile_contract(profile_root)
    model_dir = profile_root / "power_model"
    windows_path = model_dir / "batch_validation_windows.csv"
    summary_path = model_dir / "batch_validation_summary.json"
    solar_path = primary_profile_path(profile_root, "solar_model_path")
    unit_path = primary_profile_path(profile_root, "unit_model_path")
    evaluation_path, evaluation = _load_primary_evaluation(
        profile_root, "SCS-01-01")
    coverage_path = model_dir / "extended_data_coverage.json"
    windows = _read_csv(windows_path)
    summary = _read_json(summary_path)
    solar = _read_json(solar_path)
    units = _read_json(unit_path)
    coverage = _read_json(coverage_path) if coverage_path.is_file() else None
    diagnostic_metrics = {
        "soc_abs_error_pp": _metric_from_rows(
            windows, value_key="soc_error_pp",
            duration_key="duration_minutes",
            weight_key="load_obs_energy_kWh"),
        "solar_energy_abs_relative_error_pct": _metric_from_rows(
            windows, value_key="solar_energy_relative_error_pct",
            duration_key="duration_minutes",
            weight_key="solar_obs_proxy_energy_kWh"),
        "load_energy_abs_relative_error_pct": _metric_from_rows(
            windows, value_key="load_energy_relative_error_pct",
            duration_key="duration_minutes",
            weight_key="load_obs_energy_kWh"),
    }
    split = load_validation_split(profile_root)
    validation_dates = split["dates"]
    expected_dates = contract.get("split", {}).get("expected_dates")
    if expected_dates is not None and len(validation_dates) != int(expected_dates):
        raise ValueError(
            f"SCS validation split 应为 {expected_dates} 个日期，"
            f"实际 {len(validation_dates)}")
    input_paths = [
        windows_path, summary_path, solar_path, unit_path,
        evaluation_path,
        coverage_path,
        profile_root / "stage2_testset" / "evaluation_cases.csv",
        profile_root / "config" / "stage2_profile.json",
        profile_root / "power_model" / "validation_split.json",
        model_dir / "batch_validation_holdout_solar_model.json",
        model_dir / "batch_validation_training_excluded.csv",
        model_dir / "validation_solar_20251229_0539.json",
        model_dir / "validation_solar_20251229_0539_output.json",
        model_dir / "validation_solar_20251229_0539_timeline.csv",
        model_dir / "solar_validation_20251229_0539_summary.json",
        model_dir / "solar_power_model_holdout_20251229.json",
        model_dir / "unit_power_holdout_models.json",
        model_dir / "unit_power_holdout_report.md",
        model_dir / "unit_power_holdout_summary.csv",
        profile_root / "out" / "energy_v2_result.csv",
        profile_root / "out" / "energy_v2_components.csv",
        model_dir / "representative_case_scs" / "input.json",
        model_dir / "representative_case_scs" / "output.json",
        model_dir / "representative_case_scs" / "prediction_timeline.csv",
        model_dir / "representative_case_scs" / "observed_soc.csv",
        model_dir / "representative_case_scs" / "task_plan.json",
        profile_root / "unit_training" / "manifest.json",
    ]
    code_root = Path(__file__).resolve().parent
    code_paths = [
        code_root / "stage2_audit.py",
        code_root / "batch_validate_forecast.py",
        code_root / "solar_power_calibration.py",
        code_root / "unit_power_model.py",
        code_root / "gen_charts.py",
        code_root / "yj3d_report.py",
        code_root / "report_style.css",
        code_root / "stage2_cases.py",
        code_root / "stage2_coverage.py",
        code_root / "stage2_framework.py",
        code_root / "stage2_testset.py",
        code_root / "stage2_quality.py",
        code_root / "unit_training_export.py",
        code_root / "power_outlier_filter.py",
        code_root / "branch_aggregation.py",
        code_root / "physical_unit_model.py",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": "SCS-01-01",
        "validation": {
            "method": "date_grouped_holdout_frozen_snapshot",
            "n_windows": evaluation["subsets"]["all"]["n_cases"],
            "validation_dates": validation_dates,
            "effective_dates": len(validation_dates),
            "coverage": summary["coverage"],
            "unit_model_split": "date_grouped_holdout",
            "solar_model_split": "date_grouped_holdout",
        },
        "metrics": _primary_metrics(evaluation),
        "metrics_context": {
            **evaluation["primary"],
            "source": "stage2_testset/evaluation_summary.json",
        },
        "diagnostic_metrics": diagnostic_metrics,
        "diagnostic_metrics_context": {
            **contract["diagnostic"],
            "source": "power_model/batch_validation_windows.csv",
        },
        "data_coverage": coverage,
        "model_selection": {
            "solar_target": solar.get("target_definition", {}),
            "solar_model_type": solar.get("model_type"),
            "solar_selected_degree": solar.get("selected_degree"),
            "solar_cross_validation": solar.get("cross_validation", {}),
            "unit_model_counts": units.get("model_counts", {}),
            "unit_calibration_contract": units.get("calibration_contract", {}),
            "unit_training_split": units.get("training_split", {}),
        },
        "evidence_boundaries": [
            "目标SOC由TMD019与固定V-SOC曲线映射，不是独立硬件SOC基准",
            "太阳阵目标采用方案B代理，不是独立太阳阵测量",
            "主指标使用窗口起始冻结背景；单机和太阳阵均按验证日期整组留出",
            "历史逐分钟背景残差回放只作为上界/诊断值，不能与主指标混用",
            "25路负载支路未与TMD018总负载完全闭合",
        ],
        "representative_case": load_representative_case(profile_root),
        "input_files": [str(path.relative_to(profile_root)) for path in input_paths],
        "code_files": [str(path) for path in code_paths],
    }


def _window_ranges(windows, lookback_minutes):
    parsed = []
    for row in windows:
        start = _parse_time(row["start"])
        end = _parse_time(row["end"])
        parsed.append((row, start, end))
    if not parsed:
        return parsed
    earliest = min(start for _, start, _ in parsed) - timedelta(
        minutes=int(lookback_minutes))
    latest = max(end for _, _, end in parsed)
    return earliest, latest


def _yj_background_observations(
        profile_root, windows, unit_models, branch_models,
        unit_csv, *, lookback_minutes=1500):
    model_dir = profile_root / "power_model"
    lower, upper = _window_ranges(windows, lookback_minutes)
    task_models = {
        unit_id: model for unit_id, model in unit_models["models"].items()
        if model.get("active_threshold_W") is not None}
    task_power_by_time = {}
    unit_csv = Path(unit_csv)

    def unit_rows():
        with unit_csv.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                stamp = _parse_time(row["t"])
                if not lower <= stamp <= upper:
                    continue
                task_power = 0.0
                for unit_id, model in task_models.items():
                    try:
                        observed = float(row[f"P_{unit_id}_W"])
                        threshold = float(model["active_threshold_W"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not math.isfinite(observed) or observed < 0:
                        continue
                    upper_limit = (model.get("outlier_filter") or {}).get(
                        "upper_limit_W")
                    if (
                        upper_limit is not None
                        and observed > float(upper_limit)
                    ):
                        continue
                    if observed >= threshold:
                        task_power += float(model.get("active_power_W") or 0.0)
                    else:
                        task_power += float(model.get("low_power_W") or 0.0)
                task_power_by_time[row["t"]] = task_power
                yield row

    coactivity = ab_coactivity(unit_rows(), branch_models["models"])
    bus_by_time = {}
    with (profile_root / "data_power_minute.csv").open(
            encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            try:
                stamp = _parse_time(row["t"])
                bus = float(row["bus_measure_W_mean"])
            except (KeyError, TypeError, ValueError):
                continue
            if lower <= stamp <= upper and math.isfinite(bus):
                bus_by_time[row["t"]] = {
                    "phase": row.get("phase", ""),
                    "bus_W": bus,
                }
    observations = []
    for stamp in sorted(set(bus_by_time) & set(task_power_by_time)):
        bus = bus_by_time[stamp]
        observations.append({
            "t": stamp,
            "phase": bus["phase"],
            "background_W": max(bus["bus_W"] - task_power_by_time[stamp], 0.0),
        })
    return observations, coactivity, task_models


def _bcr_audit(profile_root, solar):
    path = profile_root / "bcr_output_minute.csv"
    values = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            try:
                value = float(row["bcr_out_W"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
    result = bcr_outlier_audit(
        values, cap_W=solar["training"]["winsor_cap_W"])
    result["source"] = path.name
    result["policy"] = (
        "主评测按所用留出模型的99.9%缩尾上限处理；"
        "原始尖峰单独保留为异常事件诊断")
    return result


def _yj_audit(profile_root):
    contract = load_profile_contract(profile_root)
    model_dir = profile_root / "power_model"
    evaluation_path, evaluation = _load_primary_evaluation(
        profile_root, "YJ-3D")
    unit_holdout_path = primary_profile_path(
        profile_root, "unit_model_path")
    branch_unit_path = primary_profile_path(
        profile_root, "branch_unit_model_path")
    physical_config_path = primary_profile_path(
        profile_root, "physical_config_path")
    unit_data_path = primary_profile_path(
        profile_root, "unit_data_path")
    solar_holdout_path = primary_profile_path(
        profile_root, "solar_model_path")
    rows_all = _read_csv(model_dir / "load_aware_comparison_results.csv")
    rows = [
        row for row in rows_all
        if row.get("variant") == "load_task_aware_BCR"]
    solar = _read_json(model_dir / "solar_power_model.json")
    solar_holdout = _read_json(solar_holdout_path)
    units = _read_json(unit_holdout_path)
    branch_units = _read_json(branch_unit_path)
    task_summary = _read_json(model_dir / "unit_task_validation_summary.json")
    battery = _read_json(model_dir / "battery_remaining_metrics_summary.json")
    windows = _read_csv(model_dir / "load_aware_comparison_windows.csv")
    background_observations, coactivity, task_models = (
        _yj_background_observations(
            profile_root, windows, units, branch_units, unit_data_path))
    topology_path = profile_root / contract.get(
        "topology_contract", "power_model/ab_topology.json")
    topology = _read_json(topology_path)
    ab_topology = ab_topology_audit(coactivity, topology)
    solar_structure = solar_structure_audit(
        solar, contract.get("bcr_structure"))
    primary_solar_structure = solar_structure_audit(
        solar_holdout, contract.get("bcr_structure"))
    background_windows = []
    by_time = {
        row["t"]: row for row in background_observations}
    for window in windows:
        start = window["start"]
        phase = by_time.get(start, {}).get("phase", "日照")
        background_windows.append({
            "start": start,
            "end": window["end"],
            "phase": phase,
        })
    background = rolling_background_comparison(
        background_observations, background_windows,
        lookbacks=(30, 60), min_history_minutes=5)
    hierarchical = hierarchical_background_comparison(
        background_observations,
        background_windows,
        rolling_lookback_minutes=60,
        min_history_minutes=5,
        previous_day_tolerance_minutes=15,
        min_previous_day_minutes=5,
    )
    background["hierarchical"] = hierarchical
    background["summary"]["hierarchical"] = {
        "n_windows": hierarchical["summary"]["n_windows"],
        "mean_absolute_error_W": hierarchical["summary"][
            "mean_absolute_error_W"],
        "mean_relative_error_pct": hierarchical["summary"][
            "mean_relative_error_pct"],
        "selected_sources": hierarchical["summary"]["selected_sources"],
    }
    background["reconstructed_task_channels"] = sorted(task_models)
    background["background_definition"] = (
        "max(TMEA003×TMEA004 母线测点功率 − 历史可变单机功率层, 0)；"
        "用于诊断，不等于独立测得的未计负载")
    saturation = _evaluation_saturation(evaluation)
    raw_saturation = partition_soc_saturation(
        rows, observed_key="observed_target_soc_current_anchored",
        predicted_key="predicted_target_soc",
        error_key="target_soc_error_pp")
    diagnostic_metrics = {
        key: audit_metric_from_summary(
            evaluation["subsets"]["raw"]["metrics"].get(key))
        for key in (
            "soc_abs_error_pp",
            "solar_energy_abs_relative_error_pct",
            "load_energy_abs_relative_error_pct",
        )
    }
    split = load_validation_split(profile_root)
    validation_dates = split["dates"]
    expected_dates = contract.get("split", {}).get("expected_dates")
    if expected_dates is not None and len(validation_dates) != int(expected_dates):
        raise ValueError(
            f"YJ validation split 应为 {expected_dates} 个日期，"
            f"实际 {len(validation_dates)}")
    input_paths = [
        evaluation_path,
        profile_root / "stage2_testset" / "evaluation_cases.csv",
        profile_root / "config" / "stage2_profile.json",
        profile_root / "power_model" / "validation_split.json",
        model_dir / "load_aware_comparison_results.csv",
        model_dir / "load_aware_comparison_summary.json",
        model_dir / "load_aware_comparison_windows.csv",
        model_dir / "unit_task_validation_summary.json",
        model_dir / "battery_remaining_metrics_summary.json",
        physical_config_path,
        unit_data_path,
        branch_unit_path,
        model_dir / "solar_power_model.json",
        profile_root / "bcr_output_minute.csv",
        profile_root / "data_power_minute.csv",
        profile_root / "unit_inventory.json",
        profile_root / "unit_power_summary.json",
        profile_root / "config" / "telemetry_params.json",
        profile_root / "config" / "assessment_params.json",
        topology_path,
        model_dir / "validation_sun_20260728_1622.json",
        model_dir / "validation_sun_20260728_1622_holdout_output.json",
        model_dir / "validation_sun_20260728_1622_holdout_timeline.csv",
        model_dir / "validation_sun_20260728_1622_summary.json",
        model_dir / "v_soc_curve.json",
        unit_holdout_path,
        solar_holdout_path,
        model_dir / "representative_case_yj" / "input.json",
        model_dir / "representative_case_yj" / "output.json",
        model_dir / "representative_case_yj" / "prediction_timeline.csv",
        model_dir / "representative_case_yj" / "observed_soc_current_integrated.csv",
        model_dir / "representative_case_yj" / "task_plan.json",
        profile_root / "unit_training" / "manifest.json",
    ]
    code_root = Path(__file__).resolve().parent
    code_paths = [
        code_root / "stage2_audit.py",
        code_root / "yj3d_power_forecast.py",
        code_root / "solar_power_calibration.py",
        code_root / "unit_power_model.py",
        code_root / "gen_charts.py",
        code_root / "yj3d_report.py",
        code_root / "report_style.css",
        code_root / "stage2_cases.py",
        code_root / "stage2_framework.py",
        code_root / "stage2_testset.py",
        code_root / "stage2_quality.py",
        code_root / "unit_training_export.py",
        code_root / "power_outlier_filter.py",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": "YJ-3D",
        "validation": {
            "method": "date_grouped_holdout_frozen_snapshot",
            "n_windows": evaluation["subsets"]["all"]["n_cases"],
            "validation_dates": validation_dates,
            "effective_dates": len(validation_dates),
            "excluded_dates": task_summary["design"]["excluded_dates"],
        },
        "metrics": _primary_metrics(evaluation),
        "metrics_context": {
            **evaluation["primary"],
            "source": "stage2_testset/evaluation_summary.json",
        },
        "diagnostic_metrics": diagnostic_metrics,
        "diagnostic_metrics_context": {
            "evaluation_mode": "raw_ungated_sensitivity",
            "usage": "保留越界电量、原始BCR尖峰和未通过SOC质检窗口的诊断值",
        },
        "soc_saturation": saturation,
        "raw_soc_saturation": raw_saturation,
        "background_load": background,
        "ab_coactivity": coactivity,
        "ab_topology": ab_topology,
        "solar_structure": solar_structure,
        "primary_solar_structure": primary_solar_structure,
        "bcr_outliers": {
            **_bcr_audit(profile_root, solar),
            "primary_model_cap_W":
                solar_holdout.get("training", {}).get("winsor_cap_W"),
            "primary_policy": "training_99p9_winsor_cap",
        },
        "model_selection": {
            "solar_target": solar.get("target_definition", {}),
            "solar_model_type": solar.get("model_type"),
            "solar_cross_validation": solar.get("cross_validation", {}),
            "solar_feature_model": solar.get("feature_model", {}),
            "task_variants": task_summary.get("by_variant", {}),
            "unit_calibration_contract": units.get("calibration_contract", {}),
            "absolute_v_soc_window": {
                "n_windows": battery.get("absolute_soc_reference_windows"),
                "metrics": battery.get("absolute_metrics"),
            },
        },
        "evidence_boundaries": [
            "多窗口目标SOC为当前锚定加TMEA006积分，只有1个绝对V-SOC端点窗口",
            "历史任务状态不等于可提前获得的任务计划",
            "BCR目标是MPPT/DC-DC输出侧功率，不是PV输入功率",
            "当前部署背景负载固定为起始快照；滚动模型仅为审计候选",
            "A/B供电通道物理拓扑和重复计量关系仍需单线图核准",
            "含越量程电池或母线测点的窗口不进入相应主指标；原始与有效区间条件值仅保留为诊断",
            "BCR主能量按所用留出模型的训练缩尾上限处理，原始尖峰另列",
        ],
        "representative_case": load_representative_case(profile_root),
        "input_files": [str(path.relative_to(profile_root)) for path in input_paths],
        "code_files": [str(path) for path in code_paths],
    }


def build_stage2_audit(profile_root, satellite_id=None):
    """Build the same audit schema for either satellite profile."""
    root = Path(profile_root).resolve()
    if not (root / "power_model").is_dir():
        raise ValueError(f"缺少第二阶段目录: {root / 'power_model'}")
    if satellite_id is None:
        satellite_id = root.name
    if satellite_id == "SCS-01-01":
        return _scs_audit(root)
    if satellite_id == "YJ-3D":
        return _yj_audit(root)
    raise ValueError(f"不支持的卫星: {satellite_id}")


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    temp.replace(path)


def write_stage2_outputs(profile_root, satellite_id=None):
    """Regenerate stage2_audit.json and stage2_manifest.json."""
    root = Path(profile_root).resolve()
    audit = build_stage2_audit(root, satellite_id=satellite_id)
    input_paths = [root / path for path in audit["input_files"]]
    code_paths = [Path(path) for path in audit["code_files"]]
    manifest = build_manifest(
        root, audit, input_paths=input_paths, code_paths=code_paths)
    audit_path = root / "power_model" / "stage2_audit.json"
    manifest_path = root / "power_model" / "stage2_manifest.json"
    _atomic_json(audit_path, audit)
    _atomic_json(manifest_path, manifest)
    return audit_path, manifest_path


def ensure_stage2_outputs(profile_root, satellite_id=None):
    """Create or refresh audit artifacts when any declared source is newer."""
    root = Path(profile_root).resolve()
    model_dir = root / "power_model"
    audit_path = model_dir / "stage2_audit.json"
    manifest_path = model_dir / "stage2_manifest.json"
    if not audit_path.is_file() or not manifest_path.is_file():
        return write_stage2_outputs(root, satellite_id=satellite_id)
    manifest = _read_json(manifest_path)
    output_mtime = min(audit_path.stat().st_mtime, manifest_path.stat().st_mtime)
    declared = manifest.get("inputs", []) + manifest.get("code", [])
    stale = False
    for item in declared:
        path = Path(item["path"])
        if not path.is_absolute():
            path = root / path
        if not path.is_file() or path.stat().st_mtime > output_mtime:
            stale = True
            break
    if stale:
        return write_stage2_outputs(root, satellite_id=satellite_id)
    return audit_path, manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--satellite-id")
    args = parser.parse_args()
    audit_path, manifest_path = write_stage2_outputs(
        args.profile_dir, satellite_id=args.satellite_id)
    print(json.dumps({
        "audit": str(audit_path),
        "manifest": str(manifest_path),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
