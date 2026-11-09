#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared profile contracts, split metadata, and evaluation summaries."""
from __future__ import annotations

import csv
from datetime import datetime
import json
import math
from pathlib import Path
import random
import statistics
from zoneinfo import ZoneInfo


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


def cluster_bootstrap_mean_ci(
        values, *, dates, samples=BOOTSTRAP_SAMPLES,
        seed=BOOTSTRAP_SEED, confidence=.95):
    """Bootstrap dates, retaining all same-date cases in each resample."""
    if len(values) != len(dates):
        raise ValueError("指标与日期长度不一致")
    grouped = {}
    for value, date in zip(values, dates):
        numbers = _finite_numbers([value])
        date_text = str(date or "").strip()
        if not numbers or not date_text:
            continue
        grouped.setdefault(date_text, []).append(numbers[0])
    if not grouped:
        raise ValueError("bootstrap 输入不能为空")
    if samples < 1:
        raise ValueError("bootstrap samples 必须为正整数")
    if not 0 < confidence < 1:
        raise ValueError("confidence 必须在 (0,1) 内")
    date_keys = sorted(grouped)
    date_means = {
        date: statistics.fmean(grouped[date])
        for date in date_keys
    }
    rng = random.Random(int(seed))
    boot = []
    for _ in range(int(samples)):
        selected = [
            date_keys[rng.randrange(len(date_keys))]
            for _ in range(len(date_keys))
        ]
        boot.append(statistics.fmean(date_means[date] for date in selected))
    tail = (1 - confidence) / 2
    mean = statistics.fmean(date_means.values())
    return {
        "mean": mean,
        "lower": _quantile(boot, tail),
        "upper": _quantile(boot, 1 - tail),
        "confidence": float(confidence),
        "samples": int(samples),
        "seed": int(seed),
        "sampling_unit": "date",
        "n_dates": len(date_keys),
    }


def _metric_summary(values, dates):
    numbers = _finite_numbers(values)
    valid_pairs = []
    for value, date in zip(values, dates):
        finite = _finite_numbers([value])
        if finite and date not in (None, ""):
            valid_pairs.append((finite[0], str(date)))
    if not numbers:
        return {
            "n_cases": 0,
            "effective_dates": 0,
            "window_equal_mean": None,
            "window_median": None,
            "window_p95": None,
            "date_equal_mean": None,
            "date_cluster_bootstrap": None,
        }
    by_date = {}
    for value, date in valid_pairs:
        by_date.setdefault(date, []).append(value)
    ci = cluster_bootstrap_mean_ci(
        [value for value, _ in valid_pairs],
        dates=[date for _, date in valid_pairs],
    )
    return {
        "n_cases": len(numbers),
        "effective_dates": len(by_date),
        "window_equal_mean": statistics.fmean(numbers),
        "window_median": statistics.median(numbers),
        "window_p95": _quantile(numbers, .95),
        "date_equal_mean": statistics.fmean(
            statistics.fmean(group) for group in by_date.values()),
        "date_cluster_bootstrap": ci,
    }


def load_profile_contract(profile_root):
    """Load the satellite-specific adapter for the common stage-two pipeline."""
    root = Path(profile_root).resolve()
    path = root / "config" / "stage2_profile.json"
    if not path.is_file():
        raise FileNotFoundError(f"缺少第二阶段 Profile 契约: {path}")
    contract = json.loads(path.read_text(encoding="utf-8"))
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("stage2_profile.json schema_version 不受支持")
    if contract.get("satellite_id") != root.name:
        raise ValueError("stage2_profile.json satellite_id 与目录不一致")
    primary = contract.get("primary")
    if not isinstance(primary, dict):
        raise ValueError("stage2_profile.json 缺少 primary 对象")
    for key in ("evaluation_mode", "background_policy", "unit_model_split"):
        if not primary.get(key):
            raise ValueError(f"stage2_profile.json primary 缺少 {key}")
    return contract


def primary_profile_path(profile_root, key):
    """Resolve a declared primary model input without satellite-specific names."""
    root = Path(profile_root).resolve()
    contract = load_profile_contract(root)
    relative = contract["primary"].get(key)
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"stage2_profile.json primary 缺少 {key}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"stage2_profile.json {key} 越出卫星目录") from error
    if not path.is_file():
        raise FileNotFoundError(f"第二阶段主模型文件不存在: {path}")
    return path


def load_validation_split(profile_root):
    root = Path(profile_root).resolve()
    path = root / "power_model" / "validation_split.json"
    if not path.is_file():
        raise FileNotFoundError(f"缺少验证日期清单: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("validation_split.json schema_version 不受支持")
    if (
        not isinstance(document.get("satellite_id"), str)
        or not document["satellite_id"].strip()
    ):
        raise ValueError("validation_split.json 缺少 satellite_id")
    dates = document.get("dates")
    if (
        not isinstance(dates, list)
        or not dates
        or any(not isinstance(item, str) or not item for item in dates)
        or dates != sorted(set(dates))
    ):
        raise ValueError("validation_split.json dates 必须是非空有序唯一字符串列表")
    return document


def write_validation_split(profile_root, *, satellite_id, dates, source):
    root = Path(profile_root).resolve()
    normalized = sorted({str(item) for item in dates if str(item)})
    if not normalized:
        raise ValueError("验证日期不能为空")
    path = root / "power_model" / "validation_split.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": satellite_id,
        "generated_at": datetime.now(
            ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "split_unit": "date",
        "dates": normalized,
        "source": str(source),
    }
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)
    return path


def _normal_phase(value):
    text = str(value or "").strip().lower()
    if text in {"sun", "日照", "阳照", "光照区"}:
        return "sun"
    if text in {"eclipse", "地影", "阴影", "阴影区"}:
        return "eclipse"
    return "unknown"


def _case_date(case):
    window = case.get("label", {}).get("window", {})
    return str(window.get("start", ""))[:10]


def _case_row(case):
    evaluation = case.get("prediction", {}).get("evaluation", {})
    label = case.get("label", {})
    observed = label.get("observed", {})
    metadata = case.get("metadata", {})
    observed_soc = observed.get("target_soc_pct")
    try:
        saturated = float(observed_soc) >= 100.0 - SOC_SATURATION_EPS
    except (TypeError, ValueError):
        saturated = False
    quality = label.get("label_quality", {})
    soc_qc_status = quality.get(
        "soc_qc_status", quality.get("qc_status", "pass"))
    solar_qc_status = quality.get(
        "solar_qc_status", quality.get("qc_status", "pass"))
    load_qc_status = quality.get(
        "load_qc_status", quality.get("qc_status", "pass"))
    soc_qc_clean = soc_qc_status == "pass"
    solar_qc_clean = solar_qc_status == "pass"
    load_qc_clean = load_qc_status == "pass"
    non_failing_statuses = {"pass", "not_applicable"}
    metadata_qc = str(metadata.get("qc_status", "pass"))
    return {
        "case_id": str(case.get("case_id", "")),
        "date": _case_date(case),
        "phase": _normal_phase(metadata.get("phase")),
        "season": metadata.get("season"),
        "duration_band": metadata.get("duration_band"),
        "load_band": metadata.get("load_band"),
        "task_count": metadata.get("task_count"),
        "observed_target_soc_pct": observed_soc,
        "soc_error_pp": evaluation.get("target_soc_error_pp"),
        "remaining_Ah_error": evaluation.get("remaining_Ah_error"),
        "solar_energy_relative_error_pct": evaluation.get(
            "solar_energy_relative_error_pct"),
        "load_energy_relative_error_pct": evaluation.get(
            "load_energy_relative_error_pct"),
        "soc_error_raw_pp": evaluation.get(
            "target_soc_error_raw_pp",
            evaluation.get("target_soc_error_pp")),
        "remaining_Ah_error_raw": evaluation.get(
            "remaining_Ah_error_raw",
            evaluation.get("remaining_Ah_error")),
        "solar_energy_relative_error_raw_pct": evaluation.get(
            "solar_energy_relative_error_raw_pct",
            evaluation.get("solar_energy_relative_error_pct")),
        "load_energy_relative_error_raw_pct": evaluation.get(
            "load_energy_relative_error_raw_pct",
            evaluation.get("load_energy_relative_error_pct")),
        "saturated": saturated,
        "qc_clean": (
            metadata_qc in non_failing_statuses
            and soc_qc_status in non_failing_statuses
            and solar_qc_status in non_failing_statuses
            and load_qc_status in non_failing_statuses
        ),
        "soc_qc_clean": soc_qc_clean,
        "solar_qc_clean": solar_qc_clean,
        "load_qc_clean": load_qc_clean,
        "qc_status": (
            metadata.get("qc_status")
            or quality.get("qc_status")
            or "pass"
        ),
        "soc_qc_status": soc_qc_status,
        "solar_qc_status": solar_qc_status,
        "load_qc_status": load_qc_status,
        "quality_reasons": quality.get("quality_reasons", {}),
    }


def _subset_summary(rows, *, raw=False):
    dates = [row["date"] for row in rows]
    soc_key = "soc_error_raw_pp" if raw else "soc_error_pp"
    remaining_key = (
        "remaining_Ah_error_raw" if raw else "remaining_Ah_error")
    solar_key = (
        "solar_energy_relative_error_raw_pct"
        if raw else "solar_energy_relative_error_pct")
    load_key = (
        "load_energy_relative_error_raw_pct"
        if raw else "load_energy_relative_error_pct")

    def values(key, clean_key):
        return [
            abs(float(row[key]))
            if row[key] is not None
            and (raw or row[clean_key])
            else None
            for row in rows
        ]

    def signed_values(key, clean_key):
        return [
            row[key]
            if row[key] is not None
            and (raw or row[clean_key])
            else None
            for row in rows
        ]
    metrics = {
        "soc_abs_error_pp": _metric_summary(
            values(soc_key, "soc_qc_clean"), dates,
        ),
        "remaining_Ah_abs_error": _metric_summary(
            values(remaining_key, "soc_qc_clean"), dates,
        ),
        "solar_energy_abs_relative_error_pct": _metric_summary(
            values(solar_key, "solar_qc_clean"), dates,
        ),
        "load_energy_abs_relative_error_pct": _metric_summary(
            values(load_key, "load_qc_clean"), dates,
        ),
        "soc_signed_error_pp": _metric_summary(
            signed_values(soc_key, "soc_qc_clean"), dates),
    }
    return {
        "n_cases": len(rows),
        "effective_dates": len({row["date"] for row in rows if row["date"]}),
        "case_ids": [row["case_id"] for row in rows],
        "metrics": metrics,
    }


def summarize_cases(cases, *, satellite_id, primary=None):
    case_ids = [case.get("case_id") for case in cases]
    if not cases:
        raise ValueError("评测案例不能为空")
    if any(not value for value in case_ids):
        raise ValueError("每个评测案例必须有 case_id")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("评测案例 case_id 重复")
    rows = [_case_row(case) for case in cases]
    clean = [row for row in rows if row["qc_clean"]]
    subsets = {
        "all": _subset_summary(rows),
        "raw": _subset_summary(rows, raw=True),
        "qc_soc": _subset_summary(
            [row for row in rows if row["soc_qc_clean"]]),
        "qc_solar": _subset_summary(
            [row for row in rows if row["solar_qc_clean"]]),
        "qc_load": _subset_summary(
            [row for row in rows if row["load_qc_clean"]]),
        "qc_clean": _subset_summary(clean),
        "sun": _subset_summary([row for row in rows if row["phase"] == "sun"]),
        "eclipse": _subset_summary(
            [row for row in rows if row["phase"] == "eclipse"]),
        "saturated": _subset_summary(
            [row for row in rows if row["saturated"]]),
        "non_saturated": _subset_summary(
            [row for row in rows if not row["saturated"]]),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": satellite_id,
        "generated_at": datetime.now(
            ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "primary": primary or {},
        "subsets": subsets,
        "cases": rows,
    }


def write_evaluation_summary(
        output_dir, cases, *, satellite_id, primary=None):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    summary = summarize_cases(
        cases, satellite_id=satellite_id, primary=primary)
    summary_path = root / "evaluation_summary.json"
    temp = summary_path.with_name(summary_path.name + ".tmp")
    temp.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(summary_path)

    cases_path = root / "evaluation_cases.csv"
    fields = [
        "case_id", "date", "phase", "season", "duration_band",
        "load_band", "task_count", "observed_target_soc_pct",
        "soc_error_pp", "remaining_Ah_error",
        "solar_energy_relative_error_pct",
        "load_energy_relative_error_pct",
        "soc_error_raw_pp", "remaining_Ah_error_raw",
        "solar_energy_relative_error_raw_pct",
        "load_energy_relative_error_raw_pct",
        "saturated", "qc_clean", "soc_qc_clean",
        "solar_qc_clean", "load_qc_clean", "qc_status",
        "soc_qc_status", "solar_qc_status", "load_qc_status",
        "quality_reasons",
    ]
    temp = cases_path.with_name(cases_path.name + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary["cases"])
    temp.replace(cases_path)
    return summary_path, cases_path


def audit_metric_from_summary(metric):
    """Translate the shared metric record to the historical audit schema."""
    if metric is None or metric.get("n_cases", 0) == 0:
        return None
    return {
        "n_windows": metric["n_cases"],
        "effective_dates": metric["effective_dates"],
        "window_equal": {
            "mean": metric["window_equal_mean"],
            "median": metric["window_median"],
            "p95": metric["window_p95"],
            "date_equal_mean": metric["date_equal_mean"],
            "bootstrap_95_ci": [
                metric["date_cluster_bootstrap"]["lower"],
                metric["date_cluster_bootstrap"]["upper"],
            ],
            "bootstrap_date_cluster_95_ci": [
                metric["date_cluster_bootstrap"]["lower"],
                metric["date_cluster_bootstrap"]["upper"],
            ],
            "bootstrap": metric["date_cluster_bootstrap"],
        },
        "duration_weighted_mean": None,
        "energy_weighted_mean": None,
    }
