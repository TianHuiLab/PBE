#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reusable interval quality checks for stage-two telemetry and labels."""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


REASON_ORDER = (
    "duplicate",
    "gap",
    "phase_transition",
    "frozen_sensor",
    "missing",
    "range_violation",
)

PHASE_ALIASES = {
    "sun": "sun",
    "日照": "sun",
    "阳照": "sun",
    "光照区": "sun",
    "eclipse": "eclipse",
    "地影": "eclipse",
    "阴影": "eclipse",
    "阴影区": "eclipse",
}


def _phase(value) -> str:
    return PHASE_ALIASES.get(str(value or "").strip().lower(), "unknown")


def _normalized_times(frame: pd.DataFrame) -> pd.Series:
    if "t" not in frame:
        raise ValueError("区间质检输入缺少 t 列")
    times = pd.to_datetime(frame["t"], errors="coerce")
    if times.isna().any():
        raise ValueError("区间质检输入含无效时间")
    if times.empty:
        raise ValueError("区间质检输入为空")
    return times.reset_index(drop=True)


def _frozen_row_mask(
        frame: pd.DataFrame,
        columns: Iterable[str],
        min_samples: int,
) -> pd.Series:
    mask = pd.Series(False, index=frame.index)
    if min_samples < 2:
        return mask
    selected = frame.loc[:, list(columns)].copy()
    for column in selected:
        selected[column] = pd.to_numeric(selected[column], errors="coerce")
        selected.loc[selected[column].notna(), column] = (
            selected.loc[selected[column].notna(), column].round(12))
    changed = selected.ne(selected.shift()).any(axis=1)
    run_id = changed.cumsum()
    run_size = run_id.groupby(run_id).transform("size")
    return run_size.ge(int(min_samples))


def analyze_intervals(
        frame: pd.DataFrame,
        *,
        ranges: dict[str, tuple[float, float]],
        max_gap_seconds: float = 90.0,
        phase_column: str = "phase",
        frozen_columns: Iterable[str] | None = None,
        frozen_min_samples: int = 10,
) -> tuple[pd.DataFrame, dict]:
    """Classify adjacent telemetry intervals without deleting raw values."""
    if max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds 必须为正数")
    if not ranges:
        raise ValueError("区间质检至少需要一个量程")
    required = ["t", *ranges]
    if phase_column and phase_column in frame:
        required.append(phase_column)
    missing_columns = sorted(set(required) - set(frame.columns))
    if missing_columns:
        raise ValueError(f"区间质检输入缺少列: {missing_columns}")
    if len(frame) < 2:
        raise ValueError("区间质检至少需要两个时间点")

    source = frame.reset_index(drop=True).copy()
    source["t"] = _normalized_times(source)
    source["_duplicate_timestamp"] = source["t"].duplicated(keep=False)
    source = source.sort_values("t", kind="stable").reset_index(drop=True)
    duplicate_times = source.pop("_duplicate_timestamp")

    frozen_columns = list(frozen_columns or ranges)
    frozen = _frozen_row_mask(
        source, frozen_columns, int(frozen_min_samples))

    phase_values = (
        source[phase_column].map(_phase)
        if phase_column and phase_column in source
        else pd.Series("unknown", index=source.index)
    )
    left = source.iloc[:-1].reset_index(drop=True)
    right = source.iloc[1:].reset_index(drop=True)
    duration_seconds = (
        right["t"] - left["t"]).dt.total_seconds().to_numpy(dtype=float)
    duplicate = (
        duplicate_times.iloc[:-1].to_numpy(dtype=bool)
        | duplicate_times.iloc[1:].to_numpy(dtype=bool)
    )
    gap = (duration_seconds <= 0) | (
        duration_seconds > float(max_gap_seconds))
    phase_transition = (
        phase_values.iloc[:-1].to_numpy()
        != phase_values.iloc[1:].to_numpy()
    ) | (phase_values.iloc[:-1].to_numpy() == "unknown")
    frozen_interval = (
        frozen.iloc[:-1].to_numpy(dtype=bool)
        | frozen.iloc[1:].to_numpy(dtype=bool)
    )

    missing = np.zeros(len(left), dtype=bool)
    range_violation = np.zeros(len(left), dtype=bool)
    field_counts = {}
    for column, (lower, upper) in ranges.items():
        values = pd.to_numeric(source[column], errors="coerce").to_numpy(
            dtype=float)
        finite = np.isfinite(values)
        column_missing = ~finite[:-1] | ~finite[1:]
        column_outside = (
            (finite[:-1] & ((values[:-1] < float(lower))
                            | (values[:-1] > float(upper))))
            | (finite[1:] & ((values[1:] < float(lower))
                             | (values[1:] > float(upper))))
        )
        missing |= column_missing
        range_violation |= column_outside
        field_counts[column] = int(
            (finite & ((values < float(lower)) | (values > float(upper))))
            .sum())

    reason_flags = {
        "duplicate": duplicate,
        "gap": gap,
        "phase_transition": phase_transition,
        "frozen_sensor": frozen_interval,
        "missing": missing,
        "range_violation": range_violation,
    }
    reasons = []
    valid = []
    for index in range(len(left)):
        current = [
            name for name in REASON_ORDER
            if reason_flags[name][index]
        ]
        reasons.append("|".join(current))
        valid.append(not current)

    intervals = pd.DataFrame({
        "row_start": np.arange(len(left), dtype=int),
        "row_end": np.arange(1, len(source), dtype=int),
        "t_start": left["t"],
        "t_end": right["t"],
        "duration_seconds": duration_seconds,
        "quality_reasons": reasons,
        "valid": valid,
    })
    for column in ranges:
        intervals[f"{column}_start"] = pd.to_numeric(
            left[column], errors="coerce").to_numpy(dtype=float)
        intervals[f"{column}_end"] = pd.to_numeric(
            right[column], errors="coerce").to_numpy(dtype=float)
        intervals[f"{column}_mean"] = (
            intervals[f"{column}_start"]
            + intervals[f"{column}_end"]
        ) / 2.0

    reason_counts = {
        name: int(flag.sum())
        for name, flag in reason_flags.items()
        if int(flag.sum())
    }
    summary = {
        "qc_status": "pass" if bool(intervals["valid"].all()) else "fail",
        "n_rows": int(len(source)),
        "n_intervals": int(len(intervals)),
        "valid_intervals": int(intervals["valid"].sum()),
        "invalid_intervals": int((~intervals["valid"]).sum()),
        "valid_seconds": float(
            intervals.loc[intervals["valid"], "duration_seconds"].sum()),
        "reason_counts": reason_counts,
        "range_violation_fields": {
            field: count for field, count in field_counts.items() if count
        },
        "duplicate_timestamps": int(duplicate_times.sum()),
        "max_gap_seconds": float(max_gap_seconds),
        "frozen_min_samples": int(frozen_min_samples),
    }
    return intervals, summary


def integrate_checked_soc(
        frame: pd.DataFrame,
        *,
        initial_soc: float,
        capacity_Ah: float,
        ranges: dict[str, tuple[float, float]],
        current_column: str = "battery_current_mean",
        voltage_column: str = "battery_voltage_mean",
        max_gap_seconds: float = 90.0,
        frozen_min_samples: int = 10,
) -> dict:
    """Return raw and QC-clean SOC diagnostics plus an optional main label."""
    if not math.isfinite(float(initial_soc)) or not 0 <= float(initial_soc) <= 1:
        raise ValueError("initial_soc 必须在 [0,1]")
    if not math.isfinite(float(capacity_Ah)) or float(capacity_Ah) <= 0:
        raise ValueError("capacity_Ah 必须为正数")
    if current_column not in ranges or voltage_column not in ranges:
        raise ValueError("SOC 质检量程必须包含电流和电压列")

    source = frame.reset_index(drop=True).copy()
    source["t"] = _normalized_times(source)
    source = source.sort_values("t", kind="stable").reset_index(drop=True)
    intervals, quality = analyze_intervals(
        source,
        ranges=ranges,
        max_gap_seconds=max_gap_seconds,
        frozen_columns=(current_column, voltage_column),
        frozen_min_samples=frozen_min_samples,
    )

    soc_raw = [float(initial_soc)]
    soc_valid = [float(initial_soc)]
    capacity = float(capacity_Ah)
    for row in intervals.itertuples(index=False):
        seconds = float(row.duration_seconds)
        current = float(getattr(row, f"{current_column}_mean"))
        raw_integrable = (
            math.isfinite(seconds)
            and 0 < seconds <= float(max_gap_seconds)
            and math.isfinite(current)
        )
        raw_delta = (
            -current * seconds / 3600.0 / capacity
            if raw_integrable else 0.0
        )
        valid_delta = (
            -current * seconds / 3600.0 / capacity
            if bool(row.valid) and math.isfinite(current) else 0.0
        )
        soc_raw.append(min(1.0, max(0.0, soc_raw[-1] + raw_delta)))
        soc_valid.append(min(1.0, max(0.0, soc_valid[-1] + valid_delta)))

    qc_pass = quality["qc_status"] == "pass"
    series = pd.DataFrame({
        "t": source["t"],
        "soc_raw_pct": np.asarray(soc_raw) * 100.0,
        "soc_valid_intervals_pct": np.asarray(soc_valid) * 100.0,
        "valid": [True] + intervals["valid"].astype(bool).tolist(),
        "quality_reasons": [""] + intervals["quality_reasons"].tolist(),
    })
    return {
        "qc_status": "pass" if qc_pass else "fail",
        "target_soc_pct": (
            float(soc_valid[-1]) * 100.0 if qc_pass else None),
        "target_soc_pct_raw": float(soc_raw[-1]) * 100.0,
        "target_soc_pct_valid_intervals_only": (
            float(soc_valid[-1]) * 100.0),
        "quality": quality,
        "series": series,
    }


def capped_energy(
        frame: pd.DataFrame,
        *,
        value_column: str,
        cap_W: float,
        max_gap_seconds: float = 90.0,
) -> dict:
    """Integrate raw and capped power with the training winsorization cap."""
    if cap_W < 0 or not math.isfinite(float(cap_W)):
        raise ValueError("cap_W 必须为非负有限数")
    if "t" not in frame or value_column not in frame:
        raise ValueError("功率积分输入缺少 t 或目标列")
    source = frame.reset_index(drop=True).copy()
    source["t"] = _normalized_times(source)
    if len(source) < 2:
        raise ValueError("功率积分至少需要两个时间点")
    if source["t"].duplicated().any():
        raise ValueError("功率积分输入含重复时间")
    source = source.sort_values("t", kind="stable").reset_index(drop=True)
    values = pd.to_numeric(
        source[value_column], errors="coerce").to_numpy(dtype=float)
    raw_positive = np.where(np.isfinite(values), np.maximum(values, 0.0), np.nan)
    capped = np.where(
        np.isfinite(raw_positive),
        np.minimum(raw_positive, float(cap_W)),
        np.nan,
    )
    all_seconds = source["t"].diff().dt.total_seconds().to_numpy(dtype=float)
    seconds = all_seconds[1:]
    usable = (
        np.isfinite(seconds)
        & (seconds > 0)
        & (seconds <= float(max_gap_seconds))
        & np.isfinite(raw_positive[:-1])
        & np.isfinite(raw_positive[1:])
    )
    if not usable.any():
        raise ValueError("功率积分没有可用的连续区间")
    mean_raw = (raw_positive[:-1] + raw_positive[1:]) / 2.0
    mean_capped = (capped[:-1] + capped[1:]) / 2.0
    primary_Wh = float(
        np.sum(mean_capped[usable] * seconds[usable] / 3600.0))
    raw_Wh = float(
        np.sum(mean_raw[usable] * seconds[usable] / 3600.0))
    above_cap = np.isfinite(values) & (values > float(cap_W))
    excess_Wh = float(
        np.sum((mean_raw[usable] - mean_capped[usable])
               * seconds[usable] / 3600.0))
    missing_samples = int((~np.isfinite(values)).sum())
    invalid_intervals = int((~usable).sum())
    return {
        "primary_energy_kWh": primary_Wh / 1000.0,
        "raw_energy_kWh": raw_Wh / 1000.0,
        "excess_energy_kWh": excess_Wh / 1000.0,
        "above_cap_count": int(above_cap.sum()),
        "above_cap_fraction": float(above_cap.mean()),
        "max_W": float(np.nanmax(values)) if np.isfinite(values).any() else None,
        "cap_W": float(cap_W),
        "missing_samples": missing_samples,
        "invalid_intervals": invalid_intervals,
        "qc_status": (
            "pass"
            if missing_samples == 0 and invalid_intervals == 0
            else "fail"
        ),
    }


def integrate_checked_energy(
        frame: pd.DataFrame,
        *,
        value_column: str,
        ranges: dict[str, tuple[float, float]],
        max_gap_seconds: float = 90.0,
        frozen_min_samples: int = 10,
) -> dict:
    """Integrate a raw power label only when every source interval passes QC."""
    if value_column not in frame:
        raise ValueError(f"功率积分输入缺少 {value_column}")
    source = frame.reset_index(drop=True).copy()
    source["t"] = _normalized_times(source)
    source = source.sort_values("t", kind="stable").reset_index(drop=True)
    intervals, quality = analyze_intervals(
        source,
        ranges=ranges,
        max_gap_seconds=max_gap_seconds,
        frozen_columns=tuple(ranges),
        frozen_min_samples=frozen_min_samples,
    )
    values = pd.to_numeric(
        source[value_column], errors="coerce").to_numpy(dtype=float)
    raw_Wh = 0.0
    valid_Wh = 0.0
    for row in intervals.itertuples(index=False):
        start = int(row.row_start)
        end = int(row.row_end)
        seconds = float(row.duration_seconds)
        if not (
            math.isfinite(seconds)
            and 0 < seconds <= float(max_gap_seconds)
            and math.isfinite(values[start])
            and math.isfinite(values[end])
        ):
            continue
        interval_Wh = (
            (values[start] + values[end]) / 2.0
            * seconds / 3600.0
        )
        raw_Wh += interval_Wh
        if bool(row.valid):
            valid_Wh += interval_Wh
    qc_pass = quality["qc_status"] == "pass"
    return {
        "qc_status": "pass" if qc_pass else "fail",
        "energy_kWh": valid_Wh / 1000.0 if qc_pass else None,
        "energy_kWh_raw": raw_Wh / 1000.0,
        "energy_kWh_valid_intervals_only": valid_Wh / 1000.0,
        "quality": quality,
    }
