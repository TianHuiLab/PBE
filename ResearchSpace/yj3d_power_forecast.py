#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YJ-3D adapters for the 01-star power-forecast framework."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import ch_http as C
from branch_aggregation import aggregate_branch_power_frame
from s1_fetch import native_query_iter
from solar_power_calibration import calibrate_solar_power_model
from power_outlier_filter import filter_power_values
from unit_power_model import CurrentPowerSnapshot


def _range_upper(text: object, unit: str) -> float | None:
    """Return the largest numeric value carrying ``unit`` in a range string."""
    if not isinstance(text, str):
        return None
    pattern = rf"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*{re.escape(unit)}"
    values = [
        float(value) for value in re.findall(pattern, text)
        if math.isfinite(float(value))
    ]
    positive = [value for value in values if value > 0]
    return max(positive) if positive else None


def prepare_solar_target_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Build YJ-3D load and summed BCR-output solar target columns."""
    required = {
        "t", "phase", "bus_measure_W_mean",
        "battery_signed_trial_W_mean", "bcr_out_W",
    }
    if required - set(frame.columns):
        raise ValueError(f"云尖3D分钟数据缺少列: {sorted(required-set(frame.columns))}")
    result = frame.copy()
    result["t"] = pd.to_datetime(result["t"], errors="coerce")
    if result["t"].isna().any():
        raise ValueError("云尖3D分钟数据含无效时间")
    bus = pd.to_numeric(result["bus_measure_W_mean"], errors="coerce")
    battery = pd.to_numeric(result["battery_signed_trial_W_mean"], errors="coerce")
    direct = pd.to_numeric(result["bcr_out_W"], errors="coerce")
    result["P_load"] = bus
    result["P_solar_target"] = direct
    result["P_solar_diag"] = bus - battery
    result["P_charge_est"] = 0.0
    result["P_solar"] = direct
    result["phase"] = result["phase"].astype(str)
    return result[[
        "t", "phase", "P_load", "P_solar_target", "P_solar_diag",
        "P_solar", "P_charge_est"]]


def historical_task_state_features(
        unit_frame: pd.DataFrame,
        unit_model_document: dict,
) -> pd.DataFrame:
    """Build aggregate task-state features from historical unit power proxies."""
    if "t" not in unit_frame:
        raise ValueError("云尖3D单机分钟数据缺少 t 列")
    result = pd.DataFrame({
        "t": pd.to_datetime(unit_frame["t"], errors="coerce"),
    })
    if result["t"].isna().any() or result["t"].duplicated().any():
        raise ValueError("云尖3D单机分钟数据含无效或重复时间")
    active_columns = []
    task_power_columns = []
    for unit_id, model in unit_model_document.get("models", {}).items():
        threshold = model.get("active_threshold_W")
        power_column = f"P_{unit_id}_W"
        if threshold is None or power_column not in unit_frame:
            continue
        observed, _ = filter_power_values(
            unit_frame[power_column],
            upper_limit_W=(model.get("outlier_filter") or {}).get(
                "upper_limit_W"),
        )
        active = pd.Series(np.nan, index=unit_frame.index, dtype=float)
        valid = observed.notna()
        active.loc[valid] = (
            observed.loc[valid] >= float(threshold)).astype(float)
        state_power = pd.Series(np.nan, index=unit_frame.index, dtype=float)
        state_power.loc[active.eq(1)] = float(
            model.get("active_power_W") or 0.0)
        state_power.loc[active.eq(0)] = float(
            model.get("low_power_W") or 0.0)
        active_columns.append(active)
        task_power_columns.append(state_power)
    result["active_unit_count"] = (
        pd.concat(active_columns, axis=1).sum(axis=1, min_count=1)
        if active_columns else 0.0)
    result["task_unit_power_W"] = (
        pd.concat(task_power_columns, axis=1).sum(axis=1, min_count=1)
        if task_power_columns else 0.0)
    return result


def _bcr_output_query(bcr_config: dict, database: str, table: str) -> str:
    if not database.isidentifier() or not table.isidentifier():
        raise ValueError("云尖3D源库或表名称无效")
    aliases, valid_terms, power_terms = [], [], []
    channels = bcr_config.get("pv_channels", [])
    if len(channels) != 8:
        raise ValueError("云尖3D BCR输出通道必须为8路")
    for index, channel in enumerate(channels):
        voltage_code = str(channel.get("output_voltage", ""))
        current_code = str(channel.get("output_current", ""))
        if not voltage_code.isidentifier() or not current_code.isidentifier():
            raise ValueError(f"云尖3D BCR输出代号无效: {index}")
        voltage, current = f"bcr_v_{index}", f"bcr_i_{index}"
        aliases.extend((
            f"toFloat64OrNull({voltage_code}) AS {voltage}",
            f"toFloat64OrNull({current_code}) AS {current}",
        ))
        valid_terms.append(
            f"(isNotNull({voltage}) AND isNotNull({current}) "
            f"AND isFinite(ifNull({voltage},0)) "
            f"AND isFinite(ifNull({current},0)))")
        power_terms.append(
            f"if({voltage}*{current}>0,{voltage}*{current},0)")
    ok_all = "(" + " AND ".join(valid_terms) + ")"
    total_power = "(" + "+".join(power_terms) + ")"
    aliases.extend((
        f"{ok_all} AS ok_bcr_out",
        f"{total_power} AS bcr_out_row_W",
    ))
    return (
        "WITH " + ",".join(aliases) + " SELECT "
        "toStartOfMinute(timestamp) AS minute_utc,"
        "count() AS n_source_rows,"
        "countIf(ok_bcr_out) AS n_valid_rows,"
        "avgIf(bcr_out_row_W,ok_bcr_out) AS bcr_out_W "
        f"FROM {database}.{table} "
        "WHERE timestamp>=toDateTime64({start:String},3,'UTC') "
        "AND timestamp<toDateTime64({end:String},3,'UTC') "
        "GROUP BY minute_utc ORDER BY minute_utc")


def fetch_bcr_output_minutes(
        profile_dir: str | Path,
        output: str | Path,
        *,
        query_rows=None,
        chunk_days: int = 3,
) -> pd.DataFrame:
    """Fetch minute means of summed same-row BCR output power."""
    profile_dir, output = Path(profile_dir), Path(output)
    if chunk_days < 1:
        raise ValueError("chunk_days 必须为正整数")
    cfg = json.loads(
        (profile_dir / "telemetry_params.json").read_text(encoding="utf-8"))
    assessment = json.loads(
        (profile_dir / "assessment_params.json").read_text(encoding="utf-8"))
    bcr = json.loads(
        (profile_dir / "boundary_candidates.json").read_text(encoding="utf-8"))
    sql = _bcr_output_query(
        bcr, cfg["source_database"], cfg["source_table"])
    start = pd.Timestamp(
        assessment["assessment_start_beijing"]).tz_localize(
            "Asia/Shanghai").tz_convert("UTC")
    end = pd.Timestamp(
        assessment["assessment_end_exclusive_beijing"]).tz_localize(
            "Asia/Shanghai").tz_convert("UTC")
    query = query_rows or native_query_iter
    rows = []
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=chunk_days), end)
        rows.extend(query(
            sql, params={
                "start": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                "end": stop.strftime("%Y-%m-%d %H:%M:%S"),
            }, timeout=180, max_execution_time=180))
        cursor = stop
    if not rows:
        raise ValueError("云尖3D BCR输出分钟查询为空")
    frame = pd.DataFrame(rows)
    frame["minute_utc"] = pd.to_datetime(
        frame["minute_utc"], errors="coerce", utc=True)
    if frame["minute_utc"].isna().any():
        raise ValueError("云尖3D BCR输出分钟含无效UTC时间")
    if frame["minute_utc"].duplicated().any():
        raise ValueError("云尖3D BCR输出分钟含重复时间")
    frame["bcr_out_W"] = pd.to_numeric(
        frame["bcr_out_W"], errors="coerce")
    frame["n_valid_rows"] = pd.to_numeric(
        frame["n_valid_rows"], errors="coerce").fillna(0).astype(int)
    frame = frame.loc[
        frame["bcr_out_W"].notna() & frame["n_valid_rows"].gt(0)].copy()
    frame = frame.sort_values("minute_utc").reset_index(drop=True)
    frame["minute_beijing"] = frame["minute_utc"].dt.tz_convert(
        "Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M:%S")
    frame = frame[[
        "minute_utc", "minute_beijing",
        "n_source_rows", "n_valid_rows", "bcr_out_W"]]
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(output)
    return frame


def calibrate_load_aware_solar_model(
        result_csv: str | Path,
        *,
        target_definition: dict,
        max_gap_minutes: float = 5.0,
        feature_policy: str | None = None,
) -> dict:
    """Fit a time-linear BCR model with future load/task-state features."""
    path = Path(result_csv)
    baseline = calibrate_solar_power_model(
        path, satellite_id="YJ-3D",
        target_definition=target_definition,
        target_column="P_solar_target")
    frame = pd.read_csv(path)
    required = {
        "t", "phase", "P_load", "P_solar_target",
        "task_unit_power_W", "active_unit_count",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"负载感知太阳阵训练数据缺少列: {sorted(missing)}")
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    if frame["t"].isna().any() or frame["t"].duplicated().any():
        raise ValueError("负载感知太阳阵训练时间含无效或重复值")
    frame = frame.sort_values("t").reset_index(drop=True)
    phase = frame["phase"].astype(str).str.strip().map({
        "日照": "sun", "阳照": "sun", "阳照区": "sun", "sun": "sun",
        "地影": "eclipse", "阴影": "eclipse", "阴影区": "eclipse",
        "eclipse": "eclipse",
    })
    target = pd.to_numeric(frame["P_solar_target"], errors="coerce")
    load = pd.to_numeric(frame["P_load"], errors="coerce")
    task_power = pd.to_numeric(frame["task_unit_power_W"], errors="coerce")
    active_count = pd.to_numeric(
        frame["active_unit_count"], errors="coerce")
    valid = (
        phase.eq("sun") & np.isfinite(target) & target.ge(0)
        & np.isfinite(load) & load.ge(0)
        & np.isfinite(task_power) & task_power.ge(0)
        & np.isfinite(active_count) & active_count.ge(0))
    sun = phase.eq("sun")
    episode = pd.Series(np.nan, index=frame.index, dtype=float)
    gaps = frame["t"].diff().dt.total_seconds().fillna(np.inf) / 60
    starts = sun & (
        ~sun.shift(fill_value=False) | gaps.gt(max_gap_minutes))
    episode.loc[sun] = starts.loc[sun].cumsum()
    usable = valid & episode.notna()
    if usable.sum() < 20 or episode.loc[usable].nunique() < 2:
        raise ValueError("负载感知太阳阵训练样本或片段不足")
    work = frame.loc[usable].copy().reset_index(drop=True)
    work["phase_norm"] = phase.loc[usable].to_numpy()
    work["episode"] = episode.loc[usable].astype(int).to_numpy()
    work["target"] = target.loc[usable].to_numpy()
    work["P_load"] = load.loc[usable].to_numpy()
    work["task_unit_power_W"] = task_power.loc[usable].to_numpy()
    work["active_unit_count"] = active_count.loc[usable].to_numpy()
    elapsed = np.empty(len(work), dtype=float)
    for _, positions_index in work.groupby("episode").groups.items():
        positions = np.asarray(positions_index, dtype=int)
        segment_start = work.iloc[positions]["t"].min()
        elapsed[positions] = (
            (work.iloc[positions]["t"] - segment_start)
            .dt.total_seconds().to_numpy(dtype=float) / 3600)
    work["elapsed_hours"] = elapsed
    raw_target = work["target"].to_numpy(dtype=float)
    cap = float(np.quantile(raw_target, .999))
    clipped_target = np.clip(raw_target, 0, cap)
    available_features = {
        "elapsed_hours": work["elapsed_hours"].to_numpy(dtype=float),
        "load_W": work["P_load"].to_numpy(dtype=float),
        "task_unit_power_W": work["task_unit_power_W"].to_numpy(dtype=float),
        "active_unit_count": work["active_unit_count"].to_numpy(dtype=float),
    }
    candidate_features = {
        "time_plus_load": ["elapsed_hours", "load_W"],
        "time_load_task_power": [
            "elapsed_hours", "load_W", "task_unit_power_W"],
        "time_load_active_count": [
            "elapsed_hours", "load_W", "active_unit_count"],
        "time_load_task_power_active_count": [
            "elapsed_hours", "load_W",
            "task_unit_power_W", "active_unit_count"],
    }
    fold_count = min(5, int(work["episode"].nunique()))
    folds = work["episode"].to_numpy(dtype=int) % fold_count
    candidate_scores = {}
    for candidate_name, feature_names in candidate_features.items():
        x = np.column_stack([
            np.ones(len(work)),
            *[available_features[name] for name in feature_names],
        ])
        squared_errors = []
        for fold in range(fold_count):
            test = folds == fold
            train = ~test
            if test.sum() < 2 or train.sum() < 5:
                continue
            coefficients, *_ = np.linalg.lstsq(
                x[train], clipped_target[train], rcond=None)
            squared_errors.extend(
                ((x[test] @ coefficients
                  - clipped_target[test]) ** 2).tolist())
        if squared_errors:
            candidate_scores[candidate_name] = float(
                np.sqrt(np.mean(squared_errors)))
    if not candidate_scores:
        raise ValueError("负载感知模型分组交叉验证没有可用折")
    if feature_policy is None:
        selected_feature_name = min(
            candidate_scores, key=lambda name: (candidate_scores[name], name))
    else:
        if feature_policy not in candidate_features:
            raise ValueError(f"未知负载感知特征策略: {feature_policy}")
        selected_feature_name = feature_policy
    feature_names = candidate_features[selected_feature_name]
    feature_rmse = candidate_scores[selected_feature_name]
    time_rmse = float(baseline["cross_validation"]["selected_rmse_W"])
    model = baseline
    model["cross_validation"]["feature_candidate_rmse_W"] = {
        name: round(score, 6)
        for name, score in sorted(candidate_scores.items())}
    if feature_rmse < time_rmse - 1e-12:
        x = np.column_stack([
            np.ones(len(work)),
            *[available_features[name] for name in feature_names],
        ])
        final_coefficients, *_ = np.linalg.lstsq(
            x, clipped_target, rcond=None)
        model["model_type"] = "load_aware_linear"
        model["feature_model"] = {
            "features": feature_names,
            "intercept": round(float(final_coefficients[0]), 9),
            "coefficients": [
                round(float(value), 9)
                for value in final_coefficients[1:]],
            "time_term_degree": 1,
            "target_clip_W": round(cap, 6),
            "selection": selected_feature_name,
            "selection_policy": (
                "fixed policy selected by integrated holdout forecast"
                if feature_policy is not None else
                "minimum grouped-CV RMSE"),
        }
        model["cross_validation"]["selected_model"] = "load_aware_linear"
        model["cross_validation"]["selected_rmse_W"] = round(
            feature_rmse, 6)
        model["warnings"].append(
            "太阳阵模型使用预测负载/任务状态特征子集；"
            "目标时刻后的BCR遥测不作为输入")
    else:
        model["cross_validation"]["selected_model"] = model["model_type"]
    return model


def estimate_bus_transfer_efficiency(
        frame: pd.DataFrame,
        *,
        min_samples: int = 20,
        max_gap_minutes: float = 5.0,
) -> dict:
    """Fit an empirical raw-proxy to bus-side solar transfer factor."""
    required = {"t", "phase", "P_solar_target", "P_solar_diag"}
    if not isinstance(min_samples, int) or min_samples < 2:
        raise ValueError("min_samples 必须为不小于2的整数")
    if max_gap_minutes <= 0:
        raise ValueError("max_gap_minutes 必须为正数")
    if required - set(frame.columns):
        raise ValueError(
            f"云尖3D太阳阵目标缺少列: {sorted(required-set(frame.columns))}")
    work = frame.copy()
    work["t"] = pd.to_datetime(work["t"], errors="coerce")
    raw = pd.to_numeric(work["P_solar_target"], errors="coerce")
    bus_side = pd.to_numeric(work["P_solar_diag"], errors="coerce")
    sun = work["phase"].astype(str).str.strip().isin(
        {"日照", "阳照", "阳照区", "sun"})
    valid = sun & work["t"].notna() & np.isfinite(raw) & np.isfinite(bus_side)
    valid &= raw.gt(0) & bus_side.ge(0)
    if int(valid.sum()) < min_samples:
        raise ValueError(
            f"有效阳照效率样本 {int(valid.sum())} < {min_samples}")
    samples = pd.DataFrame({
        "t": work.loc[valid, "t"],
        "raw_W": raw.loc[valid],
        "bus_side_W": bus_side.loc[valid],
    }).sort_values("t").reset_index(drop=True)
    denominator = float(np.dot(samples["raw_W"], samples["raw_W"]))
    if not np.isfinite(denominator) or denominator <= 0:
        raise ValueError("云尖3D太阳阵效率分母无效")
    unclipped = float(
        np.dot(samples["raw_W"], samples["bus_side_W"]) / denominator)
    if not np.isfinite(unclipped) or unclipped <= 0:
        raise ValueError("云尖3D太阳阵母线传输效率无效")
    efficiency = min(1.0, unclipped)
    gaps = samples["t"].diff().dt.total_seconds().fillna(np.inf) / 60
    episode = gaps.gt(max_gap_minutes).cumsum()
    episode_ratios = []
    for _, group in samples.groupby(episode):
        if len(group) < 2:
            continue
        grouping_denominator = float(
            np.dot(group["raw_W"], group["raw_W"]))
        if grouping_denominator > 0:
            episode_ratios.append(float(
                np.dot(group["raw_W"], group["bus_side_W"])
                / grouping_denominator))
    episode_p05 = (
        float(np.quantile(episode_ratios, .05))
        if episode_ratios else None)
    episode_p95 = (
        float(np.quantile(episode_ratios, .95))
        if episode_ratios else None)
    return {
        "efficiency": round(efficiency, 9),
        "unclipped_efficiency": round(unclipped, 9),
        "clipped_at_one": bool(unclipped > 1.0),
        "method": "sunlight through-origin least squares",
        "diagnostic_formula": "P_bus - P_battery_signed",
        "raw_formula": "TMEA003 × TMEA007",
        "n_samples": int(len(samples)),
        "energy_ratio": round(float(
            samples["bus_side_W"].sum() / samples["raw_W"].sum()), 9),
        "episode_ratio_p05": (
            round(episode_p05, 9) if episode_p05 is not None else None),
        "episode_ratio_p95": (
            round(episode_p95, 9) if episode_p95 is not None else None),
        "time_start": samples["t"].min().isoformat(),
        "time_end": samples["t"].max().isoformat(),
        "status": "empirical; includes BCR/shunt/measurement-boundary effects",
    }


def build_forecast_components(
        inventory: dict,
        heater_config: dict,
        *,
        bus_voltage_max_V: float | None = None,
) -> dict:
    """Create separate PL A/B load channels plus existing heater branches."""
    components = []
    for unit in inventory.get("units", []):
        channel = int(unit["pl_channel"])
        for bank in ("A", "B"):
            measurement = unit.get("banks", {}).get(bank, {})
            if "voltage" not in measurement or "current" not in measurement:
                continue
            max_voltage = _range_upper(
                measurement["voltage"].get("workbook_range"), "V")
            max_current = _range_upper(
                measurement["current"].get("workbook_range"), "A")
            component = {
                "id": f"pl{channel}_{bank.lower()}",
                "name": f"{unit['name']} · PL_{channel}{bank}",
                "role": "load",
                "plugin": "vi",
                "voltage_code": measurement["voltage"]["code"],
                "current_code": measurement["current"]["code"],
                "enabled": True,
            }
            if max_voltage is not None:
                component["max_voltage_V"] = max_voltage
            if max_current is not None:
                component["max_current_A"] = max_current
            if max_voltage is not None and max_current is not None:
                component["max_power_W"] = round(
                    max_voltage * max_current, 12)
            components.append(component)
    for raw in heater_config.get("components", []):
        if raw.get("enabled") and raw.get("role") == "load":
            component = dict(raw)
            max_current = component.get("max_current_A")
            if (
                (component.get("plugin") or component.get("model"))
                == "bus_current"
                and isinstance(bus_voltage_max_V, (int, float))
                and not isinstance(bus_voltage_max_V, bool)
                and math.isfinite(float(bus_voltage_max_V))
                and float(bus_voltage_max_V) > 0
                and isinstance(max_current, (int, float))
                and not isinstance(max_current, bool)
                and math.isfinite(float(max_current))
                and float(max_current) > 0
            ):
                component["max_voltage_V"] = float(bus_voltage_max_V)
                component["max_power_W"] = round(
                    float(bus_voltage_max_V) * float(max_current), 12)
            components.append(component)
    seen = set()
    for component in components:
        if component["id"] in seen:
            raise ValueError(f"重复云尖3D单机通道: {component['id']}")
        seen.add(component["id"])
    return {
        "satellite_id": "YJ-3D",
        "balance_boundary_verified": False,
        "plugin_modules": [],
        "_note": (
            "PL A/B按供电通道分别建模，不合并；无电流的PL_25/29"
            "不进入功率模型。max_power_W由工作簿电压/电流量程生成，"
            "供公共功率过滤器剔除超限遥测。"
        ),
        "components": components,
    }


def unit_rows_to_component_minute(rows: list[dict], component_config: dict) -> pd.DataFrame:
    if not rows:
        raise ValueError("云尖3D单机分钟行不能为空")
    frame = pd.DataFrame(rows)
    if "minute_utc" not in frame or "bus_voltage" not in frame:
        raise ValueError("分钟行必须包含 minute_utc/bus_voltage")
    stamps = pd.to_datetime(frame["minute_utc"], errors="coerce", utc=True)
    if stamps.isna().any():
        raise ValueError("云尖3D单机分钟行含无效UTC时间")
    if stamps.duplicated().any():
        raise ValueError("云尖3D单机分钟行含重复时间")
    result = pd.DataFrame({
        "t": stamps.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None),
    })

    def numeric(code):
        if code not in frame:
            return pd.Series(np.nan, index=frame.index)
        return pd.to_numeric(frame[code], errors="coerce")

    def bounded_numeric(series: pd.Series, upper: object) -> pd.Series:
        bounded = series.where(series >= 0)
        if (
            isinstance(upper, (int, float))
            and not isinstance(upper, bool)
            and math.isfinite(float(upper))
            and float(upper) > 0
        ):
            bounded = bounded.where(bounded <= float(upper))
        return bounded

    bus_voltage = pd.to_numeric(frame["bus_voltage"], errors="coerce")
    for spec in component_config.get("components", []):
        if spec.get("role") != "load" or not spec.get("enabled", True):
            continue
        plugin = spec.get("plugin", spec.get("model"))
        if plugin == "vi":
            voltage = bounded_numeric(
                numeric(spec["voltage_code"]), spec.get("max_voltage_V"))
            current = bounded_numeric(
                numeric(spec["current_code"]), spec.get("max_current_A"))
            power = voltage * current
        elif plugin == "bus_current":
            voltage = bounded_numeric(
                bus_voltage, spec.get("max_voltage_V"))
            current = bounded_numeric(
                numeric(spec["current_code"]), spec.get("max_current_A"))
            power = voltage * current
        else:
            raise ValueError(f"云尖3D不支持单机插件: {plugin}")
        max_power = spec.get("max_power_W")
        valid = np.isfinite(power) & (power >= 0)
        if (
            isinstance(max_power, (int, float))
            and not isinstance(max_power, bool)
            and math.isfinite(float(max_power))
            and float(max_power) > 0
        ):
            valid &= power <= float(max_power)
        result[f"P_{spec['id']}_W"] = power.where(valid)
    return result


class YJ3DHistoricalSnapshotProvider:
    """Exact Beijing-minute snapshot from YJ-3D historical CSV/DataFrame."""

    def __init__(self, power_frame: pd.DataFrame,
                 unit_frame: pd.DataFrame | None = None,
                 unit_models: dict | None = None):
        power = power_frame.copy()
        power["t"] = pd.to_datetime(power["t"], errors="coerce")
        if power["t"].isna().any() or power["t"].duplicated().any():
            raise ValueError("云尖3D功率分钟表含无效或重复时间")
        self.power = power.set_index("t").sort_index()
        self.unit = None
        if unit_frame is not None:
            units = unit_frame.copy()
            units["t"] = pd.to_datetime(units["t"], errors="coerce")
            if units["t"].isna().any() or units["t"].duplicated().any():
                raise ValueError("云尖3D单机分钟表含无效或重复时间")
            if unit_models is not None:
                units = units.sort_values("t").reset_index(drop=True)
                for unit_id, model in unit_models.get("models", {}).items():
                    column = f"P_{unit_id}_W"
                    if column not in units:
                        continue
                    filtered, _ = filter_power_values(
                        units[column],
                        upper_limit_W=(model.get("outlier_filter") or {}).get(
                            "upper_limit_W"),
                    )
                    units[column] = filtered.interpolate(
                        method="linear", limit=3,
                        limit_direction="both")
            self.unit = units.set_index("t").sort_index()

    def snapshot(self, timestamp, unit_ids) -> CurrentPowerSnapshot:
        stamp = pd.Timestamp(timestamp).tz_convert(
            "Asia/Shanghai").tz_localize(None)
        if stamp not in self.power.index:
            raise ValueError(f"云尖3D功率表没有精确分钟: {stamp}")
        row = self.power.loc[stamp]
        unit_power = {}
        missing = {}
        if unit_ids:
            if self.unit is None or stamp not in self.unit.index:
                raise ValueError(f"云尖3D单机表没有精确分钟: {stamp}")
            unit_row = self.unit.loc[stamp]
            for unit_id in unit_ids:
                column = f"P_{unit_id}_W"
                if column not in unit_row or pd.isna(unit_row[column]):
                    missing[unit_id] = "missing_current_power"
                else:
                    unit_power[unit_id] = float(unit_row[column])
            if missing:
                raise ValueError(
                    "云尖3D任务单机当前功率缺测: "
                    + ", ".join(sorted(missing)))
        return CurrentPowerSnapshot(
            time=pd.Timestamp(timestamp),
            battery_voltage_V=float(row["battery_voltage_mean"]),
            bus_voltage_V=float(row["bus_voltage_mean"]),
            total_load_W=float(row["bus_measure_W_mean"]),
            phase=str(row["phase"]),
            unit_power_W=unit_power,
            missing_unit_power_W=missing,
            provenance={
                "provider": "YJ3DHistoricalSnapshotProvider",
                "selected_time_beijing": stamp.isoformat(),
            },
        )


class YJ3DClickHouseSnapshotProvider:
    """Fetch one exact Beijing minute from chdb_3D.yc_history."""

    def __init__(self, profile_dir: str | Path, *,
                 query_rows=None):
        self.profile_dir = Path(profile_dir)
        self.query_rows = query_rows or native_query_iter
        self.components = json.loads(
            (self.profile_dir.parent / "power_model" /
             "forecast_components.json").read_text(encoding="utf-8"))
        self.topology = json.loads(
            (self.profile_dir.parent / "power_model" /
             "ab_topology.json").read_text(encoding="utf-8"))
        self.branch_models = json.loads(
            (self.profile_dir.parent / "power_model" /
             "unit_power_models.json").read_text(encoding="utf-8"))

    def snapshot(self, timestamp, unit_ids) -> CurrentPowerSnapshot:
        stamp_bj = pd.Timestamp(timestamp).tz_convert("Asia/Shanghai")
        start = stamp_bj.floor("min").tz_convert("UTC")
        end = start + pd.Timedelta(minutes=1)
        cfg = json.loads(
            (self.profile_dir / "telemetry_params.json").read_text(
                encoding="utf-8"))
        measurements = cfg["measurements"]
        requested_ids = set(unit_ids)
        component_by_id = {
            spec["id"]: spec for spec in self.components["components"]}
        physical_by_id = {
            pair["physical_unit"]: pair
            for pair in self.topology.get("pairs", [])
        }
        expanded_branch_ids = set()
        for unit_id in requested_ids:
            if unit_id in component_by_id:
                expanded_branch_ids.add(unit_id)
            elif unit_id in physical_by_id:
                expanded_branch_ids.update(
                    physical_by_id[unit_id]["members"])
            else:
                raise ValueError(f"云尖3D任务含未知通道: {unit_id}")
        specs = [
            component_by_id[unit_id]
            for unit_id in sorted(expanded_branch_ids)
        ]
        known = set(component_by_id) | set(physical_by_id)
        if not requested_ids <= known:
            raise ValueError(
                f"云尖3D任务含未知通道: {sorted(requested_ids-known)}")
        codes = []
        for code in (
                measurements["bus_voltage"]["code"],
                measurements["bus_current"]["code"],
                measurements["battery_voltage"]["code"],
                measurements["battery_current"]["code"]):
            codes.append(code)
        for spec in specs:
            if "voltage_code" in spec:
                codes.append(spec["voltage_code"])
            if "current_code" in spec:
                codes.append(spec["current_code"])
        codes = list(dict.fromkeys(codes))
        select = ["toStartOfMinute(timestamp) AS minute_utc"]
        aliases = {
            measurements["bus_voltage"]["code"]: "bus_voltage",
            measurements["bus_current"]["code"]: "bus_current",
            measurements["battery_voltage"]["code"]: "battery_voltage",
            measurements["battery_current"]["code"]: "battery_current",
        }
        for code in codes:
            output = aliases.get(code, code)
            select.append(
                f"avgIf(toFloat64OrNull({code}), "
                f"isNotNull(toFloat64OrNull({code}))) AS {output}")
        select.append(
            f"anyIf(toString({cfg['phase_state']['code']}), 1) AS phase")
        sql = (
            "SELECT " + ", ".join(select) + " FROM "
            f"{cfg['source_database']}.{cfg['source_table']} "
            "WHERE timestamp >= toDateTime64({start:String},3,'UTC') "
            "AND timestamp < toDateTime64({end:String},3,'UTC') "
            "GROUP BY minute_utc ORDER BY minute_utc LIMIT 1"
        )
        rows = list(self.query_rows(
            sql,
            params={
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=30, max_execution_time=30))
        if not rows:
            raise ValueError(f"云尖3D精确UTC分钟没有遥测: {start}")
        row = rows[0]
        bus_voltage = float(row["bus_voltage"])
        bus_current = float(row["bus_current"])
        unit_frame = unit_rows_to_component_minute(
            [row], {"components": specs}) if specs else pd.DataFrame()
        selected_pairs = [
            pair for unit_id, pair in physical_by_id.items()
            if unit_id in requested_ids
        ]
        if selected_pairs:
            unit_frame, _ = aggregate_branch_power_frame(
                unit_frame,
                {"pairs": selected_pairs, "default_aggregation_mode": "sum"},
                self.branch_models,
            )
        unit_power = {}
        missing = {}
        for unit_id in requested_ids:
            column = f"P_{unit_id}_W"
            if column not in unit_frame or pd.isna(unit_frame.loc[0, column]):
                missing[unit_id] = "missing_current_power"
            else:
                unit_power[unit_id] = float(unit_frame.loc[0, column])
        if missing:
            raise ValueError(
                "云尖3D任务单机当前功率缺测: " + ", ".join(sorted(missing)))
        return CurrentPowerSnapshot(
            time=stamp_bj,
            battery_voltage_V=float(row["battery_voltage"]),
            bus_voltage_V=bus_voltage,
            total_load_W=bus_voltage * bus_current,
            phase=str(row["phase"]),
            unit_power_W=unit_power,
            missing_unit_power_W=missing,
            provenance={
                "provider": "YJ3DClickHouseSnapshotProvider",
                "query_start_utc": start.isoformat(),
                "query_end_utc": end.isoformat(),
                "source": f"{cfg['source_database']}.{cfg['source_table']}",
            },
        )


def voltage_soc_curve_document(calibration: dict) -> dict:
    """Convert the YJ 7.84 A discharge table to generic inverse-curve JSON."""
    points = calibration.get("points")
    if points:
        values = [
            [float(item["voltage_V"]), float(item["soc_pct"])]
            for item in points
        ]
    else:
        voltage = np.asarray(calibration["voltage_V"], dtype=float)
        soc = np.asarray(calibration["soc_pct"], dtype=float)
        if len(voltage) != len(soc):
            raise ValueError("云尖3D电压和SOC点长度不一致")
        values = [[float(v), float(s)] for v, s in zip(voltage, soc)]
    values = sorted(values, key=lambda item: item[0])
    values = [
        [voltage_V, soc / 100 if soc > 1 else soc]
        for voltage_V, soc in values
    ]
    if len(values) < 2 or not all(
            np.isfinite(v) for point in values for v in point):
        raise ValueError("云尖3D V-SOC点无效")
    return {
        "schema_version": "1.0",
        "satellite_id": "YJ-3D",
        "status": "conditional_discharge_mapping_only",
        "source": {
            key: calibration.get(key)
            for key in ("source", "source_sha256", "config_sha256")
            if key in calibration
        },
        "points": values,
        "design": {
            "rated_capacity_Ah": float(calibration["capacity_Ah"]),
            "calibration_current_A": float(calibration["current_A"]),
            "pack_config": "YJ-3D calibrated discharge table",
        },
        "caveats": [
            "仅适用于7.84A放电曲线附近条件SOC映射",
            "充电滞回、温度、倍率和老化未建模",
            "不能从曲线推断在轨容量或SOH",
        ],
    }


def _query_codes(components: dict, bus_voltage_code: str) -> list[str]:
    codes = [bus_voltage_code]
    for spec in components["components"]:
        if spec.get("enabled"):
            if "voltage_code" in spec:
                codes.append(spec["voltage_code"])
            if "current_code" in spec:
                codes.append(spec["current_code"])
    codes = list(dict.fromkeys(codes))
    if any(re.fullmatch(r"[A-Z]{3,4}\d{3,4}", code) is None for code in codes):
        raise ValueError("云尖3D单机遥测代号格式无效")
    return codes


def fetch_unit_power_minutes(
        profile_dir: str | Path,
        output: str | Path,
        *,
        query_rows=None,
        chunk_days: int = 3,
) -> pd.DataFrame:
    profile_dir, output = Path(profile_dir), Path(output)
    cfg = json.loads(
        (profile_dir / "telemetry_params.json").read_text(encoding="utf-8"))
    assessment = json.loads(
        (profile_dir / "assessment_params.json").read_text(encoding="utf-8"))
    inventory = json.loads(
        (profile_dir.parent / "unit_inventory.json").read_text(encoding="utf-8"))
    heater = json.loads(
        (profile_dir / "components.json").read_text(encoding="utf-8"))
    components = build_forecast_components(
        inventory,
        heater,
        bus_voltage_max_V=float(
            cfg["measurements"]["bus_voltage"]["range"][1]),
    )
    codes = _query_codes(
        components, cfg["measurements"]["bus_voltage"]["code"])
    source = f"{cfg['source_database']}.{cfg['source_table']}"
    if not (re.fullmatch(r"[A-Za-z_]\w*", cfg["source_database"])
            and re.fullmatch(r"[A-Za-z_]\w*", cfg["source_table"])):
        raise ValueError("云尖3D源表名称无效")
    select = [
        "toStartOfMinute(timestamp) AS minute_utc",
        "avgIf(toFloat64OrNull(TMEA003), "
        "isNotNull(toFloat64OrNull(TMEA003))) AS bus_voltage",
    ]
    for code in codes:
        if code == "TMEA003":
            continue
        select.append(
            f"avgIf(toFloat64OrNull({code}), "
            f"isNotNull(toFloat64OrNull({code}))) AS {code}")
    sql = (
        "SELECT " + ", ".join(select) + f" FROM {source} "
        "WHERE timestamp >= toDateTime64({start:String},3,'UTC') "
        "AND timestamp < toDateTime64({end:String},3,'UTC') "
        "GROUP BY minute_utc ORDER BY minute_utc"
    )
    start = pd.Timestamp(assessment["assessment_start_beijing"]).tz_localize(
        "Asia/Shanghai").tz_convert("UTC")
    end = pd.Timestamp(
        assessment["assessment_end_exclusive_beijing"]).tz_localize(
            "Asia/Shanghai").tz_convert("UTC")
    query = query_rows or native_query_iter
    rows = []
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=chunk_days), end)
        rows.extend(query(
            sql,
            params={
                "start": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                "end": stop.strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=180, max_execution_time=180))
        cursor = stop
    frame = unit_rows_to_component_minute(rows, components)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(output)
    config_path = output.parent / "forecast_components.json"
    config_temp = config_path.with_name(config_path.name + ".tmp")
    config_temp.write_text(
        json.dumps(components, ensure_ascii=False, indent=2),
        encoding="utf-8")
    config_temp.replace(config_path)
    return frame


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2,
                   allow_nan=False, default=str),
        encoding="utf-8")
    temp.replace(path)


def render_yj3d_solar_report(model: dict) -> str:
    training = model["training"]
    cv = model["cross_validation"]
    stats = model["target_stats_W"]
    lines = [
        "# 云尖3D星太阳阵功率历史拟合",
        "",
        "## 目标口径",
        "",
        "```text",
        "P_solar_to_bus = Σ(V_BCR_out,j × I_BCR_out,j)",
        "P_solar_to_bus <= total_load + charge_margin  # optional only",
        "P_solar_diag = P_bus - P_battery_signed",
        "```",
        "",
        "训练目标采用8路BCR输出侧电压×电流之和。BCR输出是MPPT/DC-DC后的"
        "母线侧功率，与母线负载、电池充放电处于同一功能节点；"
        "`P_solar_diag`仅作交叉核验。",
        "",
        "## 模型",
        "",
        f"- 类型：`{model['model_type']}`",
        f"- 阶数：`{model['selected_degree']}`",
        (f"- 特征：`{model.get('feature_model', {}).get('features')}`；"
         f"截距/系数 `{model.get('feature_model', {}).get('intercept')}` / "
         f"`{model.get('feature_model', {}).get('coefficients')}`"
         if model["model_type"] == "load_aware_linear" else
         f"- 系数：`{model['coefficients_W_per_hour_power']}`"),
        (f"- 特征选择：`{model.get('feature_model', {}).get('selection')}`；"
         f"`{model.get('feature_model', {}).get('selection_policy')}`。"
         if model["model_type"] == "load_aware_linear" else
         "- 模型仅使用阳照片段经历小时。"),
        f"- 时间支撑：`{model['support_hours']} h`，超出后截断。",
        "- 阴影片段：固定0 W。",
        "- 母线传输效率：不额外施加；BCR输出目标已经位于母线侧。",
        "- 负荷跟随充电余量："
        f"`{model.get('load_following_charge_margin_W')}` W；"
        "MPPT模式默认不启用，仅可作为保守规划上界。",
        "",
        "## 验证",
        "",
        f"- 有效阳照分钟：`{training['n_valid_sun_samples']:,}`",
        f"- 阳照片段：`{training['sun_segments']:,}`",
        f"- 阴影分钟：`{training['shadow_samples_in_source']:,}`",
        f"- 目标p05/p50/p95：`{stats['p05']}` / `{stats['p50']}` / "
        f"`{stats['p95']}` W",
        f"- 恒定模型RMSE：`{cv['constant_rmse_W']}` W",
        f"- 各阶RMSE：`{json.dumps(cv['candidate_rmse_W'], ensure_ascii=False)}`",
        f"- 选中RMSE：`{cv['selected_rmse_W']}` W",
        (f"- 负载/任务特征候选RMSE："
         f"`{json.dumps(cv.get('feature_candidate_rmse_W', {}), ensure_ascii=False)}`"
         if cv.get("feature_candidate_rmse_W") else
         "- 未启用负载/任务特征候选。"),
        "",
        "## 证据边界",
        "",
        "- 太阳阵控制逻辑为MPPT；`P_BCR_out`是MPPT/DC-DC后的输出侧功率，"
        "不是`V_PV×I_PV`输入功率。",
        "- 8路PV输入电压存在，但8路PV输入电流未在现有遥测中提供。",
        "- BCR输出与母线/电池的功能拓扑已由硬件说明和同源遥测验证；"
        "精确针脚网络仍需单线图核准。",
        "- 负荷跟随封顶是任务规划约束，不是电池实际充电接受能力测量；"
        "余量为0表示不预测未确认的太阳充电余量。",
        "- `TMEA004`负载覆盖和`TMEA006`方向仍需电路资料最终核准。",
        "- 多项式未显式包含温度、太阳入射角、SAA/姿态、分流工作点和阵列老化。",
        "- 超出历史光照时长范围的预测按训练边界截断。",
        "",
    ]
    return "\n".join(lines)


def build_yj3d_power_artifacts(
        profile_dir: str | Path,
        power_minute_csv: str | Path,
        bcr_minute_csv: str | Path,
        unit_minute_csv: str | Path,
        unit_model_json: str | Path,
        output_dir: str | Path,
) -> tuple[dict, dict]:
    (profile_dir, power_minute_csv, bcr_minute_csv,
     unit_minute_csv, unit_model_json, output_dir) = (
        Path(profile_dir), Path(power_minute_csv),
        Path(bcr_minute_csv), Path(unit_minute_csv),
        Path(unit_model_json), Path(output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(power_minute_csv, parse_dates=["t"])
    bcr = pd.read_csv(bcr_minute_csv)
    bcr["t"] = pd.to_datetime(
        bcr["minute_beijing"], errors="coerce")
    if bcr["t"].isna().any() or bcr["t"].duplicated().any():
        raise ValueError("云尖3D BCR分钟数据含无效或重复北京时间")
    source = source.merge(
        bcr[["t", "bcr_out_W"]], on="t", how="inner",
        validate="one_to_one")
    if source.empty:
        raise ValueError("云尖3D BCR分钟数据与功率分钟数据无法对齐")
    unit_frame = pd.read_csv(unit_minute_csv, parse_dates=["t"])
    unit_models = json.loads(unit_model_json.read_text(encoding="utf-8"))
    task_features = historical_task_state_features(
        unit_frame, unit_models)
    source = source.merge(task_features, on="t", how="left",
                          validate="one_to_one")
    target = prepare_solar_target_frame(source)
    target = target.merge(task_features, on="t", how="left",
                          validate="one_to_one")
    training_path = output_dir / "solar_training_data.csv"
    target.to_csv(training_path, index=False, encoding="utf-8-sig")
    target_definition = {
        "formula": "Σ(V_BCR_out,j × I_BCR_out,j)",
        "p_solar_basis": (
            "sum of eight same-source BCR output voltage × current "
            "channels, positive output only"),
        "crosscheck": "P_bus - P_battery_signed",
        "control_logic": "maximum power point tracking (MPPT)",
        "status": (
            "YJ-3D MPPT/DC-DC BCR output-side power; not PV input power"),
    }
    model = calibrate_load_aware_solar_model(
        training_path,
        target_definition=target_definition,
        feature_policy="time_plus_load",
    )
    model["bus_transfer_efficiency"] = 1.0
    model["bus_transfer_efficiency_model"] = {
        "efficiency": 1.0,
        "status": "not applicable; target is already BCR output-side power",
    }
    model["load_following_charge_margin_W"] = None
    model["load_following_charge_margin_model"] = {
        "constraint": "P_solar_to_bus <= total_load + margin",
        "margin_W": None,
        "enabled": False,
        "status": (
            "optional conservative planning constraint only; disabled because "
            "YJ-3D solar output is controlled by MPPT"),
    }
    model["warnings"].append(
        "太阳阵目标为8路BCR输出功率之和，不额外乘母线传输效率")
    model["warnings"].append(
        "BCR输出是MPPT/DC-DC后的母线侧功率，不是PV输入功率")
    model["warnings"] = [
        ("BCR输出目标来自8路同源输出电压×电流配对，端口边界仍需单线图最终核准"
         if "母线电压代理" in warning else warning)
        for warning in model["warnings"]
    ]
    solar_path = output_dir / "solar_power_model.json"
    _write_json(solar_path, model)
    report_path = output_dir / "solar_power_model_report.md"
    report_temp = report_path.with_name(report_path.name + ".tmp")
    report_temp.write_text(
        render_yj3d_solar_report(model), encoding="utf-8")
    report_temp.replace(report_path)

    from yj3d_soc import load_calibration
    calibration = load_calibration(profile_dir.parent)
    curve = voltage_soc_curve_document(calibration)
    curve["source"]["source_csv"] = str(calibration["source"])
    _write_json(output_dir / "v_soc_curve.json", curve)
    return model, curve


def main() -> int:
    parser = argparse.ArgumentParser(description="云尖3D星功率预测数据构建")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-units")
    fetch.add_argument("--profile-dir", type=Path, required=True)
    fetch.add_argument("--out-dir", type=Path, required=True)
    fetch.add_argument("--chunk-days", type=int, default=3)
    fetch_bcr = sub.add_parser("fetch-bcr")
    fetch_bcr.add_argument("--profile-dir", type=Path, required=True)
    fetch_bcr.add_argument("--out-dir", type=Path, required=True)
    fetch_bcr.add_argument("--chunk-days", type=int, default=3)
    solar = sub.add_parser("build-solar")
    solar.add_argument("--profile-dir", type=Path, required=True)
    solar.add_argument("--power-minute", type=Path, required=True)
    solar.add_argument("--bcr-minute", type=Path, required=True)
    solar.add_argument("--unit-minute", type=Path, required=True)
    solar.add_argument("--unit-model", type=Path, required=True)
    solar.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "fetch-units":
        output = args.out_dir / "unit_power_minute.csv"
        frame = fetch_unit_power_minutes(
            args.profile_dir, output, chunk_days=args.chunk_days)
        print(f"已保存 {len(frame)} 行 {len(frame.columns)-1} 个通道: {output}")
    elif args.command == "fetch-bcr":
        output = args.out_dir / "bcr_output_minute.csv"
        frame = fetch_bcr_output_minutes(
            args.profile_dir, output, chunk_days=args.chunk_days)
        print(f"已保存 {len(frame)} 行 BCR输出分钟数据: {output}")
    else:
        model, curve = build_yj3d_power_artifacts(
            args.profile_dir, args.power_minute,
            args.bcr_minute, args.unit_minute,
            args.unit_model, args.out_dir)
        print(
            f"太阳阵模型 degree={model['selected_degree']}，"
            f"CV RMSE={model['cross_validation']['selected_rmse_W']} W；"
            f"V-SOC点={len(curve['points'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
