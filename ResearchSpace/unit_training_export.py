#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export auditable per-unit power-model training data for each satellite."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from power_outlier_filter import filter_power_values


SCHEMA_VERSION = "1.0"
TIMEZONE = "Asia/Shanghai"
MAX_ACTIVE_GAP_MINUTES = 5.0
OUTPUT_COLUMNS = [
    "timestamp",
    "observed_power_W",
    "target_power_W",
    "valid_power",
    "active_label",
    "power_band",
    "episode_id",
    "elapsed_hours",
    "explicit_mode",
    "source_codes",
    "excluded_reason",
    "fit_role",
    "used_in_model_fit",
]


class UnitTrainingExportError(ValueError):
    """Raised when the source data cannot satisfy the export contract."""


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n",
    )


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    frame.to_csv(
        temporary,
        index=False,
        encoding="utf-8-sig",
        na_rep="",
        float_format="%.6f",
    )
    temporary.replace(path)


def _round(value, digits: int = 6):
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return round(numeric, digits)


def _frame_summary(values: pd.Series, prefix: str = "") -> dict:
    numeric = pd.to_numeric(values, errors="coerce")
    numeric = numeric[np.isfinite(numeric)]
    if numeric.empty:
        return {
            f"{prefix}p05_W": None,
            f"{prefix}p50_W": None,
            f"{prefix}p95_W": None,
            f"{prefix}mean_W": None,
        }
    return {
        f"{prefix}p05_W": _round(numeric.quantile(0.05)),
        f"{prefix}p50_W": _round(numeric.quantile(0.50)),
        f"{prefix}p95_W": _round(numeric.quantile(0.95)),
        f"{prefix}mean_W": _round(numeric.mean()),
    }


def _timestamp_text(values: pd.Series) -> pd.Series:
    return values.dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _mode_series(
    frame: pd.DataFrame,
    unit_id: str,
    index: pd.Index,
) -> pd.Series:
    column = f"mode_{unit_id}"
    if column not in frame:
        return pd.Series(pd.NA, index=index, dtype="string")
    values = frame[column].astype("string")
    invalid = values.isna() | values.str.strip().isin(
        {"", "缺测", "NaN", "nan", "None", "null"}
    )
    return values.mask(invalid, pd.NA)


def _active_episode_features(
    timestamps: pd.Series,
    active_mask: pd.Series,
    *,
    max_gap_minutes: float = MAX_ACTIVE_GAP_MINUTES,
) -> tuple[pd.Series, pd.Series, int]:
    episode_ids = pd.Series(pd.NA, index=timestamps.index, dtype="string")
    elapsed_hours = pd.Series(
        np.nan, index=timestamps.index, dtype=float
    )
    active_positions = np.flatnonzero(active_mask.fillna(False).to_numpy())
    if len(active_positions) == 0:
        return episode_ids, elapsed_hours, 0

    episode_number = 0
    previous_position = None
    episode_start = None
    for position in active_positions:
        timestamp = timestamps.iloc[int(position)]
        new_episode = previous_position is None
        if previous_position is not None:
            position_is_adjacent = int(position) == int(previous_position) + 1
            time_gap = (
                timestamp - timestamps.iloc[int(previous_position)]
            ).total_seconds() / 60
            new_episode = (
                not position_is_adjacent
                or time_gap > max_gap_minutes
            )
        if new_episode:
            episode_number += 1
            episode_start = timestamp
        episode_ids.iloc[int(position)] = f"ep{episode_number:04d}"
        elapsed_hours.iloc[int(position)] = (
            timestamp - episode_start
        ).total_seconds() / 3600
        previous_position = position
    return episode_ids, elapsed_hours, episode_number


def _load_windows(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise UnitTrainingExportError(f"验证窗口不存在: {path}")
    frame = pd.read_csv(path, usecols=["start", "end"])
    frame["start"] = pd.to_datetime(frame["start"], errors="coerce")
    frame["end"] = pd.to_datetime(frame["end"], errors="coerce")
    if frame[["start", "end"]].isna().any().any():
        raise UnitTrainingExportError(f"验证窗口含无效时间: {path}")
    if (frame["end"] < frame["start"]).any():
        raise UnitTrainingExportError(f"验证窗口终点早于起点: {path}")
    return frame.sort_values(["start", "end"]).reset_index(drop=True)


def _overlap_mask(timestamps: pd.Series, windows: pd.DataFrame) -> pd.Series:
    mask = pd.Series(False, index=timestamps.index)
    for row in windows.itertuples(index=False):
        mask |= timestamps.between(row.start, row.end, inclusive="both")
    return mask


def _profile_contract(profile_root: Path) -> dict:
    satellite_id = profile_root.name
    if satellite_id == "SCS-01-01":
        return {
            "satellite_id": satellite_id,
            "data_path": "out/energy_v2_components.csv",
            "config_path": "config/components.json",
            "model_path": "power_model/unit_power_holdout_models.json",
            "full_model_path": "power_model/unit_power_models.json",
            "window_path": "power_model/batch_validation_windows.csv",
            "holdout_summary_path": "power_model/validation_split.json",
            "has_independent_unit_holdout": True,
            "fit_role": "train",
            "holdout_policy": (
                "按第二阶段16个验证日期整组留出；留出日期全部进入 eval.csv，"
                "单机模型拟合不得使用这些日期。"
            ),
            "excluded_dates": [],
        }
    if satellite_id == "YJ-3D":
        return {
            "satellite_id": satellite_id,
            "data_path": "power_model/physical_unit_power_minute.csv",
            "config_path": "power_model/physical_unit_components.json",
            "model_path": "power_model/physical_unit_holdout_models.json",
            "full_model_path": "power_model/physical_unit_models.json",
            "window_path": "power_model/load_aware_comparison_windows.csv",
            "holdout_summary_path": (
                "power_model/unit_task_validation_summary.json"
            ),
            "has_independent_unit_holdout": True,
            "fit_role": "train",
            "holdout_policy": (
                "整体按任务验证日期留出；留出日期全部进入 eval.csv，"
                "其余有效样本进入 train.csv。"
            ),
            "excluded_dates": [],
        }
    raise UnitTrainingExportError(f"不支持的卫星: {satellite_id}")


def _load_profile(contract: dict, profile_root: Path) -> dict:
    data_path = profile_root / contract["data_path"]
    config_path = profile_root / contract["config_path"]
    model_path = profile_root / contract["model_path"]
    window_path = profile_root / contract["window_path"]
    for path in (data_path, config_path, model_path, window_path):
        if not path.is_file():
            raise UnitTrainingExportError(f"缺少输入文件: {path}")

    config = _read_json(config_path)
    model_document = _read_json(model_path)
    if config.get("satellite_id") != contract["satellite_id"]:
        raise UnitTrainingExportError("组件配置的 satellite_id 不匹配")
    if model_document.get("satellite_id") != contract["satellite_id"]:
        raise UnitTrainingExportError("模型文件的 satellite_id 不匹配")

    specs = [
        spec for spec in config.get("components", [])
        if spec.get("role") == "load" and spec.get("enabled", True)
    ]
    models = model_document.get("models", {})
    spec_ids = [spec["id"] for spec in specs]
    if len(spec_ids) != len(set(spec_ids)):
        raise UnitTrainingExportError("组件配置存在重复单机 ID")
    if set(spec_ids) != set(models):
        missing_models = sorted(set(spec_ids) - set(models))
        extra_models = sorted(set(models) - set(spec_ids))
        raise UnitTrainingExportError(
            f"配置与模型单机集合不一致；缺模型={missing_models}，"
            f"多模型={extra_models}"
        )

    if "holdout_summary_path" in contract:
        summary_path = profile_root / contract["holdout_summary_path"]
        if not summary_path.is_file():
            raise UnitTrainingExportError(f"缺少留出摘要: {summary_path}")
        summary = _read_json(summary_path)
        values = (
            summary["dates"]
            if "dates" in summary
            else summary["design"]["excluded_dates"]
        )
        excluded_dates = sorted({str(value) for value in values})
        contract["excluded_dates"] = excluded_dates
    else:
        excluded_dates = []

    header = pd.read_csv(data_path, nrows=0)
    required_power_columns = {
        model.get("source", {}).get("power_column", f"P_{unit_id}_W")
        for unit_id, model in models.items()
    }
    missing_columns = sorted(required_power_columns - set(header.columns))
    if missing_columns:
        raise UnitTrainingExportError(
            f"分钟数据缺少功率列: {missing_columns}"
        )
    if "t" not in header.columns:
        raise UnitTrainingExportError("分钟数据缺少 t 列")

    mode_columns = [
        f"mode_{unit_id}" for unit_id in spec_ids
        if f"mode_{unit_id}" in header.columns
    ]
    use_columns = ["t", *sorted(required_power_columns), *mode_columns]
    frame = pd.read_csv(data_path, usecols=use_columns)
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    if frame["t"].isna().any():
        raise UnitTrainingExportError("分钟数据含无效时间")
    if frame["t"].duplicated().any():
        raise UnitTrainingExportError("分钟数据含重复时间")
    frame = frame.sort_values("t").reset_index(drop=True)
    if frame.empty:
        raise UnitTrainingExportError("分钟数据为空")

    date_text = frame["t"].dt.strftime("%Y-%m-%d")
    if excluded_dates:
        holdout_mask = date_text.isin(excluded_dates)
    else:
        holdout_mask = pd.Series(False, index=frame.index)
    windows = _load_windows(window_path)
    overlap_mask = _overlap_mask(frame["t"], windows)
    return {
        "frame": frame,
        "specs": specs,
        "models": models,
        "holdout_mask": holdout_mask,
        "overlap_mask": overlap_mask,
        "windows": windows,
        "data_path": data_path,
        "config_path": config_path,
        "model_path": model_path,
        "window_path": window_path,
    }


def _unit_samples(
    profile: dict,
    spec: dict,
    model: dict,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    frame = profile["frame"]
    unit_id = spec["id"]
    power_column = model.get(
        "source", {}).get("power_column", f"P_{unit_id}_W"
    )
    observed = pd.to_numeric(frame[power_column], errors="coerce")
    finite = pd.Series(np.isfinite(observed), index=frame.index)
    negative = finite & (observed < 0)
    raw_valid = finite & (observed >= 0)
    configured_filter = model.get("outlier_filter", {})
    filtered, outlier_filter = filter_power_values(
        observed,
        upper_limit_W=configured_filter.get("upper_limit_W"),
    )
    valid = filtered.notna()
    outlier = raw_valid & ~valid
    missing = ~finite

    threshold = model.get("active_threshold_W")
    high_threshold = model.get("high_threshold_W")
    has_threshold = threshold is not None
    has_high_band = (
        high_threshold is not None
        and threshold is not None
        and float(high_threshold) > float(threshold)
    )
    active = pd.Series(pd.NA, index=frame.index, dtype="boolean")
    band = pd.Series(pd.NA, index=frame.index, dtype="string")
    if has_threshold:
        active.loc[valid] = observed.loc[valid] >= float(threshold)
        if has_high_band:
            high = valid & (observed >= float(high_threshold))
            steady_active = (
                valid
                & (observed >= float(threshold))
                & ~high
            )
            band.loc[high] = "high"
            band.loc[steady_active] = "active"
        else:
            high = pd.Series(False, index=frame.index)
            band.loc[valid & active.fillna(False)] = "active"
        band.loc[valid & ~active.fillna(False)] = "low"
    else:
        high = pd.Series(False, index=frame.index)
        band.loc[valid] = "unclassified_single_band"
    episode_ids, elapsed_hours, episode_count = _active_episode_features(
        frame["t"], active
    )

    reasons = pd.Series(pd.NA, index=frame.index, dtype="string")
    reasons.loc[missing] = "missing_or_non_numeric"
    reasons.loc[negative] = "negative_power"
    reasons.loc[outlier] = "above_power_limit"
    modes = _mode_series(frame, unit_id, frame.index)
    source_codes = ";".join(
        str(value) for value in model.get("source", {}).get(
            "source_codes", []
        )
    )

    base = pd.DataFrame({
        "timestamp": _timestamp_text(frame["t"]),
        "observed_power_W": observed,
        "target_power_W": observed.where(valid),
        "valid_power": valid.astype(bool),
        "active_label": active,
        "power_band": band,
        "episode_id": episode_ids,
        "elapsed_hours": elapsed_hours,
        "explicit_mode": modes,
        "source_codes": source_codes,
        "excluded_reason": reasons,
        "fit_role": pd.Series(pd.NA, index=frame.index, dtype="string"),
        "used_in_model_fit": pd.Series(
            pd.NA, index=frame.index, dtype="boolean"
        ),
    })

    train_mask = valid & ~profile["holdout_mask"]
    eval_mask = valid & profile["holdout_mask"]
    excluded_mask = ~valid
    base.loc[train_mask, "fit_role"] = "train"
    base.loc[train_mask, "used_in_model_fit"] = True
    base.loc[eval_mask, "fit_role"] = "holdout_eval"
    base.loc[eval_mask, "used_in_model_fit"] = False
    base.loc[excluded_mask, "fit_role"] = "excluded"
    base.loc[excluded_mask, "used_in_model_fit"] = False

    train = base.loc[train_mask, OUTPUT_COLUMNS].reset_index(drop=True)
    evaluation = base.loc[eval_mask, OUTPUT_COLUMNS].reset_index(drop=True)
    validation_overlap = base.loc[
        valid & profile["overlap_mask"], OUTPUT_COLUMNS
    ].reset_index(drop=True)
    excluded = base.loc[
        excluded_mask, OUTPUT_COLUMNS
    ].reset_index(drop=True)

    if len(train) and not train["valid_power"].all():
        raise UnitTrainingExportError(f"{unit_id}: train 含无效功率")
    if len(train) and train["target_power_W"].isna().any():
        raise UnitTrainingExportError(f"{unit_id}: train 标签含缺测")
    if len(evaluation) and not evaluation["valid_power"].all():
        raise UnitTrainingExportError(f"{unit_id}: eval 含无效功率")

    train_times = frame.loc[train_mask, "t"]
    eval_times = frame.loc[eval_mask, "t"]
    overlap_times = frame.loc[valid & profile["overlap_mask"], "t"]
    valid_power = observed.loc[valid]
    stats = {
        "source_rows": int(len(frame)),
        "valid_rows": int(valid.sum()),
        "raw_valid_rows": int(raw_valid.sum()),
        "excluded_rows": int(excluded_mask.sum()),
        "missing_or_non_numeric_rows": int(missing.sum()),
        "negative_power_rows": int(negative.sum()),
        "above_power_limit_rows": int(outlier.sum()),
        "zero_power_rows": int((valid & (observed == 0)).sum()),
        "train_rows": int(train_mask.sum()),
        "eval_rows": int(eval_mask.sum()),
        "validation_overlap_rows": int(len(validation_overlap)),
        "active_rows": (
            int(active.fillna(False).sum()) if has_threshold else None
        ),
        "low_rows": (
            int((valid & ~active.fillna(False)).sum())
            if has_threshold else None
        ),
        "high_power_rows": int(high.sum()) if has_high_band else None,
        "active_episodes": int(episode_count) if has_threshold else None,
        "missing_fraction_source": (
            _round(excluded_mask.mean())
        ),
        "source_time_start": frame["t"].iloc[0].isoformat(),
        "source_time_end": frame["t"].iloc[-1].isoformat(),
        "train_time_start": (
            train_times.iloc[0].isoformat() if len(train_times) else None
        ),
        "train_time_end": (
            train_times.iloc[-1].isoformat() if len(train_times) else None
        ),
        "eval_time_start": (
            eval_times.iloc[0].isoformat() if len(eval_times) else None
        ),
        "eval_time_end": (
            eval_times.iloc[-1].isoformat() if len(eval_times) else None
        ),
        "validation_overlap_time_start": (
            overlap_times.iloc[0].isoformat()
            if len(overlap_times) else None
        ),
        "validation_overlap_time_end": (
            overlap_times.iloc[-1].isoformat()
            if len(overlap_times) else None
        ),
        **_frame_summary(valid_power),
    }
    return train, evaluation, validation_overlap, {
        "excluded": excluded,
        "stats": stats,
        "outlier_filter": outlier_filter,
    }


def _unit_metadata(
    contract: dict,
    spec: dict,
    model: dict,
    stats: dict,
    outlier_filter: dict,
) -> dict:
    unit_id = spec["id"]
    has_threshold = model.get("active_threshold_W") is not None
    if unit_id.startswith("heater_"):
        channel_group = "heater_main_channel"
    elif unit_id.startswith("pl") and unit_id[2:3].isdigit():
        channel_group = "yj_pl_channel"
    elif contract["satellite_id"] == "SCS-01-01":
        channel_group = "scs_load_branch"
    else:
        channel_group = "load_channel"
    warnings = list(model.get("warnings", []))
    if not has_threshold:
        warnings.append(
            "未检测到 active/idle 分离阈值；active_label 与 episode_id 留空，"
            "不把恒定功率层伪造成开关状态"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": contract["satellite_id"],
        "unit_id": unit_id,
        "name": spec.get("name", model.get("name", unit_id)),
        "category": spec.get("category", "load"),
        "channel_group": channel_group,
        "source": model.get("source", {}),
        "model": {
            "model_type": model.get("model_type"),
            "usable_for_prediction": model.get("usable_for_prediction"),
            "evidence_level": model.get("evidence_level"),
            "active_power_W": model.get("active_power_W"),
            "low_power_W": model.get("low_power_W"),
            "high_power_W": model.get("high_power_W"),
            "active_threshold_W": model.get("active_threshold_W"),
            "high_threshold_W": model.get("high_threshold_W"),
            "physical_branches": model.get("physical_branches", []),
            "aggregation_mode": model.get("aggregation_mode"),
            "power_bands": model.get("power_bands", []),
            "modes": model.get("modes", {}),
            "polynomial": model.get("polynomial", {}),
            "outlier_filter": outlier_filter,
            "warnings": warnings,
        },
        "sample_contract": {
            "timezone": TIMEZONE,
            "timestamp_format": "ISO-8601 with +08:00",
            "observed_power_definition": (
                "分钟功率代理；非有限、负值或超过公共过滤上限的野值排除"
            ),
            "target_definition": (
                "target_power_W 等于可用 observed_power_W；"
                "缺测标签不填充为 0"
            ),
            "active_definition": (
                "仅当模型有 active_threshold_W 时，"
                "observed_power_W >= 阈值记为 active；"
                "若存在 high_threshold_W，则高功率瞬态仍属于 active，"
                "并在 power_band 中单列为 high；否则 active_label 为空"
            ),
            "power_band_definition": (
                "有 active 阈值时为 active/low；检测到分离高功率层时增加 "
                "high。无阈值为 unclassified_single_band。"
                "low 不等同于物理 off，high 也不等于独立硬件故障"
            ),
            "episode_definition": (
                "连续有效 active 样本组成片段；非 active 样本或时间间隔"
                f"超过 {MAX_ACTIVE_GAP_MINUTES:g} 分钟时重开片段"
            ),
            "elapsed_hours_definition": (
                "active 片段起点至当前样本的小时数；非 active 样本为空"
            ),
            "fit_role_definition": (
                "train=用于单机模型拟合；holdout_eval=云尖留出日期；"
                "excluded=缺测或负值"
            ),
        },
        "counts": stats,
        "artifacts": {
            "train": "train.csv",
            "eval": "eval.csv",
            "validation_overlap": "validation_overlap.csv",
            "excluded": "excluded_samples.csv",
        },
    }


def _sample_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Per-unit power-model training row",
        "type": "object",
        "required": [
            "timestamp",
            "observed_power_W",
            "target_power_W",
            "valid_power",
            "active_label",
            "power_band",
            "episode_id",
            "elapsed_hours",
            "source_codes",
            "fit_role",
        ],
        "properties": {
            "timestamp": {"type": "string", "format": "date-time"},
            "observed_power_W": {"type": "number", "minimum": 0},
            "target_power_W": {"type": "number", "minimum": 0},
            "valid_power": {"const": True},
            "active_label": {
                "type": ["boolean", "null"],
                "description": "仅在有 active_threshold_W 时定义",
            },
            "power_band": {
                "type": ["string", "null"],
                "enum": [
                    "active",
                    "high",
                    "low",
                    "unclassified_single_band",
                    None,
                ],
            },
            "episode_id": {"type": ["string", "null"]},
            "elapsed_hours": {
                "type": ["number", "null"],
                "minimum": 0,
            },
            "explicit_mode": {"type": ["string", "null"]},
            "source_codes": {"type": "string"},
            "excluded_reason": {
                "type": ["string", "null"],
                "enum": [
                    "missing_or_non_numeric",
                    "negative_power",
                    "above_power_limit",
                    None,
                ],
            },
            "fit_role": {
                "enum": ["train", "holdout_eval", "excluded"]
            },
            "used_in_model_fit": {"type": "boolean"},
        },
    }


def _model_support(contract: dict, specs: list[dict], models: dict) -> dict:
    units = []
    for spec in specs:
        unit_id = spec["id"]
        model = models[unit_id]
        units.append({
            "unit_id": unit_id,
            "name": spec.get("name", model.get("name", unit_id)),
            "model_type": model.get("model_type"),
            "telemetry_basis": model.get("source", {}).get(
                "telemetry_basis"
            ),
            "power_column": model.get("source", {}).get("power_column"),
            "source_codes": model.get("source", {}).get("source_codes", []),
            "has_active_threshold": (
                model.get("active_threshold_W") is not None
            ),
            "has_high_power_band": (
                model.get("high_threshold_W") is not None
            ),
            "active_threshold_W": model.get("active_threshold_W"),
            "high_threshold_W": model.get("high_threshold_W"),
            "active_power_W": model.get("active_power_W"),
            "high_power_W": model.get("high_power_W"),
            "low_power_W": model.get("low_power_W"),
            "explicit_modes": sorted(model.get("modes", {})),
            "physical_branches": model.get("physical_branches", []),
            "aggregation_mode": model.get("aggregation_mode"),
            "outlier_filter": model.get("outlier_filter", {}),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": contract["satellite_id"],
        "n_units": len(units),
        "n_units_with_active_threshold": sum(
            item["has_active_threshold"] for item in units
        ),
        "n_units_without_active_threshold": sum(
            not item["has_active_threshold"] for item in units
        ),
        "n_units_with_high_power_band": sum(
            item["has_high_power_band"] for item in units
        ),
        "active_label_policy": (
            "无阈值的单机不推断 active/idle；active_label 与片段字段留空"
        ),
        "units": units,
    }


def _readme(contract: dict, manifest_seed: dict) -> str:
    satellite_id = contract["satellite_id"]
    dates = contract["excluded_dates"]
    holdout_text = (
        "有独立整日期留出。留出日期为：\n\n"
        + "\n".join(f"- `{value}`" for value in dates)
        if dates
        else "没有独立的单机时间留出集。`eval.csv` 为空表；"
        "`validation_overlap.csv` 是训练期重叠诊断，不得称为留出验证。"
    )
    independent_evidence = (
        "整日留出日期未进入 `train.csv`，`eval.csv` 中的样本未参与当前模型拟合；"
        "这支持单机模型的日期组样本外验证。"
        if dates else
        "没有独立单机时间留出，不能据此声称样本外泛化已经验证。"
    )
    physical_boundary = (
        "本数据包的`unit_id`是物理单机；`plN`的同机A/B支路"
        "已按`ab_topology.json`先过滤后求和，设备数量不重复计算。"
        if satellite_id == "YJ-3D" else
        "本数据包的`unit_id`对应卫星Profile中的已启用电能支路。"
    )
    return f"""# {satellite_id} 单机功率模型训练数据

## 1. 目录与用途

- `units/<unit_id>/train.csv`：用于单机模型拟合的有效样本。
- `units/<unit_id>/eval.csv`：独立留出日期的有效样本；无独立留出时为空表。
- `units/<unit_id>/validation_overlap.csv`：与第二阶段验证窗口重叠的有效样本，
  用 `used_in_model_fit` 标明是否已参与拟合。
- `units/<unit_id>/excluded_samples.csv`：缺测、非有限值、负功率或超筛选上限样本。
- `units/<unit_id>/metadata.json`：单机来源、模型、阈值、样本统计和证据边界。
- `model_support.json`：所有单机的任务/状态可建模性摘要。
- `manifest.json`：输入、代码、单机文件和 SHA-256 校验值。

本数据包只包含 `{satellite_id}`。另一颗卫星的数据在其自身的
`unit_training/` 目录中，不共享训练文件或索引。

## 2. 数据字段

| 字段 | 定义 |
|---|---|
| `timestamp` | 北京时间，ISO-8601，显式带 `+08:00` |
| `observed_power_W` | 分钟功率代理，单位 W |
| `target_power_W` | 监督学习标签，等于可用观测功率；缺测不填 0 |
| `valid_power` | 有效功率标记；三个训练/验证 CSV 中均为 `true` |
| `active_label` | 有阈值时，稳态 active 和高功率瞬态均为 `true`；无阈值时为空 |
| `power_band` | 观测功率层：`low`、`active`、`high` 或 `unclassified_single_band` |
| `episode_id` | 连续 active 片段标识；无阈值或非 active 时为空 |
| `elapsed_hours` | active 片段起点后的小时数 |
| `explicit_mode` | 配置存在同机状态列时保留的显式状态 |
| `source_codes` | 构造该单机功率代理的原始遥测点代码 |
| `excluded_reason` | `missing_or_non_numeric`、`negative_power` 或 `above_power_limit` |
| `fit_role` | `train`、`holdout_eval` 或 `excluded` |
| `used_in_model_fit` | 该行是否已参与当前单机模型拟合 |

`low` 是观测到的低功率聚类，不是物理下电真值。没有确认状态映射或阈值的通道
不会伪造 `active/idle`。真正的 `off` 只能由任务计划显式给出。

## 3. 切分规则

{holdout_text}

## 4. 数量与完整性

- 单机数：`{manifest_seed["n_units"]}`
- 有 active 阈值：`{manifest_seed["n_units_with_active_threshold"]}`
- 无 active 阈值：`{manifest_seed["n_units_without_active_threshold"]}`
- 含高功率瞬态层：`{manifest_seed["n_units_with_high_power_band"]}`
- 源分钟行数：`{manifest_seed["n_source_rows"]}`
- 有效样本合计（跨单机计数）：`{manifest_seed["n_valid_rows"]}`
- 排除样本合计（跨单机计数）：`{manifest_seed["n_excluded_rows"]}`

所有计数按“单机 × 分钟”计算，因此不同单机对同一分钟分别计数。缺测、负值和
超筛选上限样本保留在 `excluded_samples.csv`，不会用零填充后混入训练。

## 5. 证据边界

- 单机功率可能是支路电压乘电流的分钟代理，尚不等同于独立硬件功率真值。
- 某些支路可能重叠，单机功率之和未必与整星总负载闭合。
- {physical_boundary}
- 配置中的 `max_power_W` 优先作为筛选上限；未配置时公共过滤器只剔除以
  p99 为基准的极端尾部野值。配置值是建模包络，不等同于铭牌额定功率；
  两类剔除均保留在 `excluded_samples.csv`。
- 功率层与阈值来自本项目既有校准模型，不在导出过程中重新发明阈值。
- `validation_overlap.csv` 只用于诊断训练窗口与第二阶段测试窗口的重叠程度。
- {independent_evidence}
"""


def write_unit_training(profile_root, output_dir=None) -> dict:
    root = Path(profile_root).resolve()
    contract = _profile_contract(root)
    profile = _load_profile(contract, root)
    output = (
        Path(output_dir)
        if output_dir is not None
        else root / "unit_training"
    ).resolve()
    units_dir = output / "units"
    schema_dir = output / "schema"
    units_dir.mkdir(parents=True, exist_ok=True)
    schema_dir.mkdir(parents=True, exist_ok=True)

    manifest_units = []
    support = _model_support(contract, profile["specs"], profile["models"])
    support_by_id = {
        item["unit_id"]: item for item in support["units"]
    }
    total_valid = 0
    total_excluded = 0
    for spec in profile["specs"]:
        unit_id = spec["id"]
        model = profile["models"][unit_id]
        train, evaluation, overlap, extra = _unit_samples(
            profile, spec, model
        )
        excluded = extra["excluded"]
        stats = extra["stats"]
        total_valid += stats["valid_rows"]
        total_excluded += stats["excluded_rows"]
        unit_relative = Path("units") / unit_id
        unit_dir = output / unit_relative
        _atomic_csv(unit_dir / "train.csv", train)
        _atomic_csv(unit_dir / "eval.csv", evaluation)
        _atomic_csv(unit_dir / "validation_overlap.csv", overlap)
        _atomic_csv(unit_dir / "excluded_samples.csv", excluded)
        metadata = _unit_metadata(
            contract, spec, model, stats, extra["outlier_filter"])
        _atomic_json(unit_dir / "metadata.json", metadata)
        artifact_names = [
            "train.csv",
            "eval.csv",
            "validation_overlap.csv",
            "excluded_samples.csv",
            "metadata.json",
        ]
        manifest_units.append({
            "unit_id": unit_id,
            "name": support_by_id[unit_id]["name"],
            "path": unit_relative.as_posix(),
            "channel_group": metadata["channel_group"],
            "model_type": model.get("model_type"),
            "has_active_threshold": (
                model.get("active_threshold_W") is not None
            ),
            "has_high_power_band": (
                model.get("high_threshold_W") is not None
            ),
            "counts": stats,
            "sha256": {
                name: _sha256_file(unit_dir / name)
                for name in artifact_names
            },
        })

    _atomic_json(schema_dir / "sample.schema.json", _sample_schema())
    _atomic_json(output / "model_support.json", support)
    manifest_seed = {
        "n_units": len(manifest_units),
        "n_units_with_active_threshold": (
            support["n_units_with_active_threshold"]
        ),
        "n_units_without_active_threshold": (
            support["n_units_without_active_threshold"]
        ),
        "n_units_with_high_power_band": (
            support["n_units_with_high_power_band"]
        ),
        "n_source_rows": sum(
            item["counts"]["source_rows"] for item in manifest_units
        ),
        "n_valid_rows": total_valid,
        "n_excluded_rows": total_excluded,
    }
    _atomic_text(
        output / "README.md",
        _readme(contract, manifest_seed),
    )

    code_root = Path(__file__).resolve().parent
    input_paths = [
        profile["data_path"],
        profile["config_path"],
        profile["model_path"],
        profile["window_path"],
    ]
    if "full_model_path" in contract:
        input_paths.append(root / contract["full_model_path"])
    if "holdout_summary_path" in contract:
        input_paths.append(root / contract["holdout_summary_path"])
    code_paths = [
        code_root / "unit_training_export.py",
        code_root / "unit_power_model.py",
        code_root / "components.py",
        code_root / "power_outlier_filter.py",
        code_root / "branch_aggregation.py",
        code_root / "physical_unit_model.py",
    ]
    root_artifacts = [
        "README.md",
        "model_support.json",
        "schema/sample.schema.json",
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact": "per_unit_power_model_training_data",
        "satellite_id": contract["satellite_id"],
        "generated_at": datetime.now(
            ZoneInfo(TIMEZONE)
        ).isoformat(timespec="seconds"),
        **manifest_seed,
        "has_independent_unit_holdout": (
            contract["has_independent_unit_holdout"]
        ),
        "holdout_policy": contract["holdout_policy"],
        "excluded_dates": contract["excluded_dates"],
        "n_validation_windows": int(len(profile["windows"])),
        "timezone": TIMEZONE,
        "target_definition": (
            "observed_power_W; only finite non-negative values are labels"
        ),
        "missing_policy": (
            "missing, non-finite and negative samples are excluded with "
            "reasons; missing labels are never replaced by zero"
        ),
        "units": manifest_units,
        "inputs": [
            {
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "sha256": _sha256_file(path),
            }
            for path in input_paths
        ],
        "code": [
            {
                "path": path.name,
                "sha256": _sha256_file(path),
            }
            for path in code_paths
        ],
        "root_artifacts": {
            name: _sha256_file(output / name)
            for name in root_artifacts
        },
    }
    manifest_path = output / "manifest.json"
    _atomic_json(manifest_path, manifest)
    return {
        "root": output,
        "manifest": manifest_path,
        "model_support": output / "model_support.json",
        "readme": output / "README.md",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    paths = write_unit_training(
        args.profile_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps({
        "root": str(paths["root"]),
        "manifest": str(paths["manifest"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
