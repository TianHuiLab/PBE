#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stratified historical validation for the solar/battery forecast."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from power_outlier_filter import filter_power_values
from solar_power_calibration import calibrate_solar_power_model
from solar_power_provider import PolynomialSolarPowerProvider
from stage2_framework import write_validation_split
from unit_power_model import CurrentPowerSnapshot
from unit_power_model import predict_battery_state


SEASON_ORDER = ["DJF", "MAM", "JJA", "SON"]


class HistoricalResultSnapshotProvider:
    """Exact-minute snapshot backed by a historical result CSV/DataFrame."""

    def __init__(self, result: pd.DataFrame):
        frame = result.copy()
        frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
        frame = frame.sort_values("t")
        if frame["t"].isna().any() or frame["t"].duplicated().any():
            raise ValueError("历史快照时间含无效或重复值")
        self.frame = frame.set_index("t")

    def snapshot(self, timestamp, unit_ids) -> CurrentPowerSnapshot:
        if tuple(unit_ids):
            raise ValueError("历史批量验证当前只支持空任务计划")
        stamp = pd.Timestamp(timestamp).tz_localize(None)
        if stamp not in self.frame.index:
            raise ValueError(f"历史结果没有精确分钟: {stamp}")
        row = self.frame.loc[stamp]
        return CurrentPowerSnapshot(
            time=pd.Timestamp(timestamp),
            battery_voltage_V=float(row["V_bat"]),
            bus_voltage_V=float(row["V_bus"]) if "V_bus" in row else np.nan,
            total_load_W=float(row["P_load"]),
            phase=str(row["phase"]),
            unit_power_W={},
            missing_unit_power_W={},
            provenance={
                "provider": "HistoricalResultSnapshotProvider",
                "selected_time": stamp.isoformat(),
            },
        )


def _season(timestamp: pd.Timestamp) -> str:
    month = timestamp.month
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _duration_band(minutes: float) -> str:
    if minutes <= 10:
        return "short"
    if minutes <= 20:
        return "medium"
    return "long"


def _load_band(load_W: float) -> str:
    if load_W < 100:
        return "low"
    if load_W <= 200:
        return "medium"
    return "high"


def _phase_key(value):
    text = str(value or "").strip().lower()
    if text in {"日照", "阳光", "阳照", "阳照区", "sun", "sunlight"}:
        return "sun"
    if text in {"地影", "阴影", "阴影区", "eclipse"}:
        return "eclipse"
    return "unknown"


def select_phase_validation_windows(
        result: pd.DataFrame,
        *,
        phase,
        allowed_dates=None,
        min_minutes: int = 5,
        max_per_stratum: int = 1,
) -> pd.DataFrame:
    """Select one or more continuous windows for a requested phase."""
    phase = _phase_key(phase)
    if phase == "unknown":
        raise ValueError("phase 必须为 sun 或 eclipse")
    if min_minutes < 1 or max_per_stratum < 1:
        raise ValueError("min_minutes/max_per_stratum 必须为正整数")
    required = {"t", "phase", "P_load", "SOC", "V_bat"}
    if not isinstance(result, pd.DataFrame) or required - set(result.columns):
        raise ValueError(f"历史结果缺少列: {sorted(required-set(result.columns))}")
    frame = result.copy()
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    frame = frame.sort_values("t").reset_index(drop=True)
    if frame["t"].isna().any() or frame["t"].duplicated().any():
        raise ValueError("历史结果含无效或重复时间")
    allowed = (
        {str(pd.Timestamp(value).date()) for value in allowed_dates}
        if allowed_dates is not None else None)
    if allowed is not None:
        frame = frame.loc[
            frame["t"].dt.strftime("%Y-%m-%d").isin(allowed)
        ].reset_index(drop=True)
    for column in ("P_load", "SOC", "V_bat"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["_phase"] = frame["phase"].map(_phase_key)
    gap = frame["t"].diff().dt.total_seconds().fillna(np.inf)
    segment_id = (
        frame["_phase"].ne(frame["_phase"].shift())
        | gap.gt(90)
    ).cumsum()
    candidates = []
    for _, group in frame.groupby(segment_id, sort=True):
        group = group.reset_index(drop=True)
        if group.empty or group["_phase"].iloc[0] != phase:
            continue
        if (group["t"].diff().dt.total_seconds().dropna() > 90).any():
            continue
        duration = (
            group["t"].iloc[-1] - group["t"].iloc[0]
        ).total_seconds() / 60
        if duration < min_minutes:
            continue
        if group[["P_load", "SOC", "V_bat"]].iloc[
                [0, -1]].isna().any().any():
            continue
        timestamp = group["t"].iloc[0]
        load = float(group["P_load"].iloc[:-1].mean())
        candidates.append({
            "phase": phase,
            "start": timestamp,
            "end": group["t"].iloc[-1],
            "duration_minutes": int(round(duration)),
            "load_mean_W": load,
            "validation_date": timestamp.date(),
            "season": _season(timestamp),
            "duration_band": _duration_band(duration),
            "load_band": _load_band(load),
            "source_rows": len(group),
        })
    if not candidates:
        return pd.DataFrame(columns=[
            "phase", "start", "end", "duration_minutes", "load_mean_W",
            "validation_date", "season", "duration_band", "load_band",
            "source_rows",
        ])
    windows = pd.DataFrame(candidates)
    windows = (
        windows.sort_values(
            ["season", "duration_band", "load_band", "duration_minutes"],
            ascending=[True, True, True, False])
        .groupby(["season", "duration_band", "load_band"], sort=False)
        .head(max_per_stratum)
        .sort_values("start")
        .reset_index(drop=True)
    )
    return windows


def select_validation_windows(
        result: pd.DataFrame,
        *,
        allowed_dates=None,
        min_minutes: int = 5,
        max_per_stratum: int = 1,
) -> tuple[pd.DataFrame, dict]:
    required = {"t", "phase", "P_load", "SOC", "V_bat"}
    if not isinstance(result, pd.DataFrame) or required - set(result.columns):
        raise ValueError(f"历史结果缺少列: {sorted(required-set(result.columns))}")
    if min_minutes < 1 or max_per_stratum < 1:
        raise ValueError("min_minutes/max_per_stratum 必须为正整数")
    frame = result.copy()
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    frame = frame.sort_values("t").reset_index(drop=True)
    if frame["t"].isna().any() or frame["t"].duplicated().any():
        raise ValueError("历史结果含无效或重复时间")
    if allowed_dates is not None:
        allowed = {str(pd.Timestamp(value).date()) for value in allowed_dates}
        frame = frame.loc[
            frame["t"].dt.strftime("%Y-%m-%d").isin(allowed)
        ].reset_index(drop=True)
        if frame.empty:
            raise ValueError("指定验证日期在历史结果中没有样本")
    for column in ("P_load", "SOC", "V_bat"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    gap = frame["t"].diff().dt.total_seconds().fillna(np.inf)
    segment_id = (
        frame["phase"].ne(frame["phase"].shift())
        | gap.gt(90)
    ).cumsum()
    candidates = []
    for _, group in frame.groupby(segment_id, sort=True):
        group = group.reset_index(drop=True)
        if group.empty or group["phase"].iloc[0] not in {"日照", "sun", "阳照"}:
            continue
        if (group["t"].diff().dt.total_seconds().dropna() > 90).any():
            continue
        duration = (group["t"].iloc[-1] - group["t"].iloc[0]).total_seconds() / 60
        if duration < min_minutes:
            continue
        if group[["P_load", "SOC", "V_bat"]].iloc[[0, -1]].isna().any().any():
            continue
        load = float(group["P_load"].iloc[:-1].mean())
        timestamp = group["t"].iloc[0]
        candidates.append({
            "start": timestamp,
            "end": group["t"].iloc[-1],
            "duration_minutes": int(round(duration)),
            "load_mean_W": load,
            "load_cv": (float(group["P_load"].iloc[:-1].std()
                              / load) if load > 0 else np.nan),
            "validation_date": timestamp.date(),
            "season": _season(timestamp),
            "duration_band": _duration_band(duration),
            "load_band": _load_band(load),
            "source_rows": len(group),
        })
    if not candidates:
        raise ValueError("没有满足条件的阳照验证片段")
    candidates = pd.DataFrame(candidates)
    candidates = (
        candidates.sort_values(
            ["season", "duration_band", "load_band", "duration_minutes"],
            ascending=[True, True, True, False])
        .groupby(["season", "duration_band", "load_band"], sort=False)
        .head(max_per_stratum)
        .reset_index(drop=True)
    )
    available = sorted(candidates["season"].unique().tolist(), key=SEASON_ORDER.index)
    coverage = {
        "seasons_available": available,
        "seasons_missing": [season for season in SEASON_ORDER
                            if season not in available],
        "duration_bands": ["short", "medium", "long"],
        "load_bands_W": {
            "low": "<100",
            "medium": "100..200",
            "high": ">200",
        },
        "selected_windows": int(len(candidates)),
        "validation_dates": sorted({
            str(value) for value in candidates["validation_date"]}),
        "missing_cells": [
            {
                "season": season,
                "duration_band": duration,
                "load_band": load,
            }
            for season in SEASON_ORDER
            for duration in ("short", "medium", "long")
            for load in ("low", "medium", "high")
            if not (
                (candidates["season"] == season)
                & (candidates["duration_band"] == duration)
                & (candidates["load_band"] == load)
            ).any()
        ],
    }
    return candidates, coverage


def build_window_validation_inputs(
        result: pd.DataFrame,
        components: pd.DataFrame,
        start,
        end,
        unit_model: dict,
        *,
        max_task_units: int = 5,
) -> dict:
    """Extract future task modes and residual background from history."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    result = result.copy()
    components = components.copy()
    result["t"] = pd.to_datetime(result["t"], errors="coerce")
    components["t"] = pd.to_datetime(components["t"], errors="coerce")
    window = result.loc[
        (result["t"] >= start) & (result["t"] <= end),
        ["t", "P_load"]
    ].sort_values("t").reset_index(drop=True)
    comp_window = components.loc[
        (components["t"] >= start) & (components["t"] <= end)
    ].sort_values("t").reset_index(drop=True)
    if len(window) < 2 or not window["t"].equals(comp_window["t"]):
        raise ValueError("部件表与结果表窗口时间轴不一致")
    total_load = pd.to_numeric(window["P_load"], errors="coerce")
    if total_load.isna().any():
        raise ValueError("验证窗口总负载含缺测")

    candidates = []
    for unit_id, model in unit_model.get("models", {}).items():
        if not model.get("usable_for_prediction"):
            continue
        power_column = model.get("source", {}).get("power_column")
        if not power_column or power_column not in comp_window:
            continue
        power, _ = filter_power_values(
            comp_window[power_column],
            upper_limit_W=(model.get("outlier_filter") or {}).get(
                "upper_limit_W"),
        )
        power = power.interpolate(
            method="linear", limit=3, limit_direction="both")
        if power.isna().any() or power.lt(0).any() or not power.gt(0).any():
            continue
        candidates.append({
            "unit_id": unit_id,
            "model": model,
            "power_column": power_column,
            "values": power,
            "score": float(power.std(ddof=0) + power.max() - power.min()),
        })
    candidates.sort(key=lambda item: (-item["score"], item["unit_id"]))
    selected = []
    selected_sum = pd.Series(0.0, index=window.index)
    for candidate in candidates:
        trial = selected_sum + candidate["values"]
        if trial.gt(total_load + 1e-6).any():
            continue
        selected.append(candidate)
        selected_sum = trial
        if len(selected) >= max_task_units:
            break

    interval_times = window["t"].iloc[:-1]
    tasks = []
    for candidate in selected:
        unit_id, model = candidate["unit_id"], candidate["model"]
        power = candidate["values"].iloc[:-1]
        mode_column = f"mode_{unit_id}"
        explicit = (
            comp_window[mode_column].astype("string")
            if mode_column in comp_window else
            pd.Series(pd.NA, index=comp_window.index, dtype="string")
        ).iloc[:-1]
        threshold = model.get("active_threshold_W")
        low_power = model.get("low_power_W")
        task_states = []
        for index, value in enumerate(power):
            mode = explicit.iloc[index]
            if pd.isna(mode) or str(mode) in {"缺测", "未映射", "分钟内切换"}:
                if low_power is None:
                    mode = "active"
                elif threshold is not None and float(value) < float(threshold):
                    mode = "idle"
                else:
                    mode = "active"
            elif (str(mode) not in model.get("modes", {})
                  and str(mode) not in {
                      "active", "on", "工作", "开机", "运行",
                      "idle", "low", "待机", "空闲", "低功率",
                      "off", "关机", "下电"}):
                if low_power is None:
                    mode = "active"
                elif threshold is not None and float(value) < float(threshold):
                    mode = "idle"
                else:
                    mode = "active"
            power_level = None
            high_threshold = model.get("high_threshold_W")
            if (
                str(mode) == "active"
                and high_threshold is not None
                and float(value) >= float(high_threshold)
            ):
                power_level = "high"
            task_states.append((str(mode), power_level))
        run_start = 0
        for index in range(1, len(task_states) + 1):
            if (index == len(task_states)
                    or task_states[index] != task_states[run_start]):
                mode, power_level = task_states[run_start]
                task = {
                    "unit_id": unit_id,
                    "start": interval_times.iloc[run_start],
                    "end": (interval_times.iloc[index]
                            if index < len(interval_times)
                            else end),
                    "mode": mode,
                    "_source": "extracted_history",
                }
                if power_level:
                    task["power_level"] = power_level
                tasks.append(task)
                run_start = index

    residual = total_load - selected_sum
    if residual.lt(-1e-6).any():
        raise ValueError("提取任务单机功率和超过总负载")
    residual = residual.clip(lower=0)
    background_segments = []
    interval_residual = residual.iloc[:-1]
    run_start = 0
    for index in range(1, len(interval_residual) + 1):
        changed = (
            index == len(interval_residual)
            or not np.isclose(
                interval_residual.iloc[index],
                interval_residual.iloc[run_start])
        )
        if changed:
            background_segments.append({
                "start": interval_times.iloc[run_start],
                "end": (interval_times.iloc[index]
                        if index < len(interval_times)
                        else end),
                "power_W": float(interval_residual.iloc[run_start]),
            })
            run_start = index
    return {
        "tasks": tasks,
        "background_load_segments": background_segments,
        "selected_units": [item["unit_id"] for item in selected],
        "current_unit_power_W": {
            item["unit_id"]: float(item["values"].iloc[0])
            for item in selected
        },
        "total_load_W": float(total_load.iloc[0]),
        "background_load_start_W": float(residual.iloc[0]),
    }


def _metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"n": 0}
    absolute = frame["soc_error_pp"].abs()
    return {
        "n": int(len(frame)),
        "soc_signed_error_median_pp": float(frame["soc_error_pp"].median()),
        "soc_abs_error_median_pp": float(absolute.median()),
        "soc_abs_error_mae_pp": float(absolute.mean()),
        "soc_abs_error_p95_pp": float(absolute.quantile(.95)),
        "solar_rmse_median_W": float(frame["solar_rmse_W"].median()),
        "solar_energy_relative_error_median_pct": float(
            frame["solar_energy_relative_error_pct"].median()),
        "load_energy_relative_error_median_pct": float(
            frame["load_energy_relative_error_pct"].median()),
    }


def run_batch_validation(
        result: pd.DataFrame,
        unit_model: dict,
        solar_model: dict,
        *,
        base_curve_points: np.ndarray,
        design: dict,
        min_minutes: int = 5,
        max_per_stratum: int = 1,
        solar_target_column: str | None = None,
        components: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict]:
    holdout_dates = (
        unit_model.get("training_split", {}).get("excluded_dates")
        if "training_split" in unit_model
        else None
    )
    windows, coverage = select_validation_windows(
        result,
        allowed_dates=holdout_dates,
        min_minutes=min_minutes,
        max_per_stratum=max_per_stratum,
    )
    frame = result.copy()
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    frame = frame.sort_values("t").set_index("t")
    component_frame = (
        components.copy() if components is not None
        else pd.DataFrame({"t": frame.index.to_numpy()}))
    solar_provider = PolynomialSolarPowerProvider(solar_model)
    evaluations = []
    for index, row in windows.iterrows():
        start = pd.Timestamp(row["start"]).tz_localize("Asia/Shanghai")
        end = pd.Timestamp(row["end"]).tz_localize("Asia/Shanghai")
        extracted = build_window_validation_inputs(
            frame.reset_index(), component_frame, start.tz_localize(None),
            end.tz_localize(None), unit_model)
        snapshot = CurrentPowerSnapshot(
            time=start,
            battery_voltage_V=float(frame.loc[start.tz_localize(None), "V_bat"]),
            bus_voltage_V=(float(frame.loc[start.tz_localize(None), "V_bus"])
                           if "V_bus" in frame else np.nan),
            total_load_W=extracted["total_load_W"],
            phase="日照",
            unit_power_W=extracted["current_unit_power_W"],
            missing_unit_power_W={},
            provenance={"provider": "extracted_history"},
        )
        phase_segments = [{
            "start": start, "end": end, "phase": "sun"}]
        solar_records = solar_provider.profile(
            start=start, end=end, step_seconds=60,
            snapshot=snapshot, phase_segments=phase_segments)
        solar_segments = [{
            "start": pd.Timestamp(item["start"]).isoformat(),
            "end": pd.Timestamp(item["end"]).isoformat(),
            "power_W": float(item["power_W"]),
        } for item in solar_records]
        internal_request = {
            "schema_version": "1.0",
            "satellite_id": unit_model["satellite_id"],
            "current_state": {
                "time": start.isoformat(),
                "battery_voltage_V": snapshot.battery_voltage_V,
                "background_load_W": 0.0,
            },
            "prediction": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "step_seconds": 60,
                "solar_segments": solar_segments,
                "background_load_segments": extracted[
                    "background_load_segments"],
            },
            "tasks": extracted["tasks"],
            "assumptions": {},
        }
        timeline, summary = predict_battery_state(
            unit_model, internal_request,
            v_soc_curve={"points": np.asarray(
                base_curve_points, dtype=float).tolist()},
            design_params=design)
        observed_rows = frame.loc[
            (frame.index >= start.tz_localize(None))
            & (frame.index <= end.tz_localize(None))]
        if len(observed_rows) != len(timeline) + 1:
            raise ValueError(
                f"验证窗口观测行与预测区间数不一致: {start} {end}")
        if solar_target_column:
            if solar_target_column not in observed_rows:
                raise ValueError(
                    f"历史结果缺少太阳阵目标列: {solar_target_column}")
            actual_solar = pd.to_numeric(
                observed_rows[solar_target_column], errors="coerce")
        else:
            actual_solar = pd.to_numeric(
                observed_rows["P_load"] + observed_rows["P_charge_est"],
                errors="coerce")
        actual_load = pd.to_numeric(observed_rows["P_load"], errors="coerce")
        solar_interval = (
            actual_solar.iloc[:-1].to_numpy(float)
            + actual_solar.iloc[1:].to_numpy(float)) / 2
        load_interval = (
            actual_load.iloc[:-1].to_numpy(float)
            + actual_load.iloc[1:].to_numpy(float)) / 2
        predicted_solar = timeline["solar_power_W"].to_numpy(float)
        predicted_load = timeline["total_load_W"].to_numpy(float)
        observed_soc = float(observed_rows["SOC"].iloc[-1] / 100)
        predicted_soc = float(summary["target_end_state"]["soc"])
        solar_obs_energy = float(solar_interval.sum() / 60 / 1000)
        solar_pred_energy = float(predicted_solar.sum() / 60 / 1000)
        load_obs_energy = float(load_interval.sum() / 60 / 1000)
        load_pred_energy = float(predicted_load.sum() / 60 / 1000)
        evaluations.append({
            **row.to_dict(),
            "n_task_units": len(extracted["selected_units"]),
            "n_tasks": len(extracted["tasks"]),
            "selected_units": "|".join(extracted["selected_units"]),
            "predicted_target_soc": predicted_soc,
            "observed_target_soc": observed_soc,
            "soc_error_pp": (predicted_soc - observed_soc) * 100,
            "solar_pred_energy_kWh": solar_pred_energy,
            "solar_obs_proxy_energy_kWh": solar_obs_energy,
            "solar_energy_error_kWh": solar_pred_energy - solar_obs_energy,
            "solar_energy_relative_error_pct": (
                (solar_pred_energy - solar_obs_energy)
                / solar_obs_energy * 100 if solar_obs_energy else np.nan),
            "solar_bias_W": float(np.mean(predicted_solar - solar_interval)),
            "solar_MAE_W": float(np.mean(np.abs(
                predicted_solar - solar_interval))),
            "solar_rmse_W": float(np.sqrt(np.mean(
                (predicted_solar - solar_interval) ** 2))),
            "load_pred_energy_kWh": load_pred_energy,
            "load_obs_energy_kWh": load_obs_energy,
            "load_energy_error_kWh": load_pred_energy - load_obs_energy,
            "load_energy_relative_error_pct": (
                (load_pred_energy - load_obs_energy)
                / load_obs_energy * 100 if load_obs_energy else np.nan),
            "forecast_status": summary["status"],
        })
    evaluations = pd.DataFrame(evaluations)
    selected_frequency = {}
    for value in evaluations["selected_units"].fillna(""):
        for unit_id in str(value).split("|"):
            if unit_id:
                selected_frequency[unit_id] = (
                    selected_frequency.get(unit_id, 0) + 1)
    solar_target_formula = (
        solar_model.get("target_definition", {}).get("formula")
        or (solar_target_column if solar_target_column
            else "P_load + P_charge_est"))
    summary = {
        "schema_version": "1.0",
        "validation": "stratified_historical_forward_soc",
        "satellite_id": solar_model.get("satellite_id"),
        "solar_target_formula": solar_target_formula,
        "coverage": coverage,
        "overall": _metrics(evaluations),
        "task_usage": {
            "windows_with_tasks": int(
                evaluations["n_task_units"].gt(0).sum()),
            "total_task_units": int(evaluations["n_task_units"].sum()),
            "total_tasks": int(evaluations["n_tasks"].sum()),
            "median_tasks_per_window": float(evaluations["n_tasks"].median()),
            "selected_unit_frequency": dict(sorted(
                selected_frequency.items(),
                key=lambda item: (-item[1], item[0]))),
        },
        "by_season": {
            season: _metrics(group)
            for season, group in evaluations.groupby("season", sort=True)
        },
        "by_duration": {
            band: _metrics(group)
            for band, group in evaluations.groupby("duration_band", sort=True)
        },
        "by_load": {
            band: _metrics(group)
            for band, group in evaluations.groupby("load_band", sort=True)
        },
        "evidence_boundaries": [
            "单机任务由历史部件功率层或已配置模式压缩得到",
            "背景负载由历史总负载减被任务覆盖单机的实测功率提取",
            "无确认状态遥测的单机，其idle/active标签由校准功率层推断",
            "验证窗口限制为单机模型 training_split.excluded_dates；新增数据不得自动扩入已冻结留出集",
            f"太阳阵观测/训练目标为{solar_target_formula}",
            "目标SOC观测值由TMD019与固定V-SOC曲线映射",
            "验证窗口日期应从太阳阵训练集中排除",
        ],
    }
    return evaluations, summary


def render_batch_report(windows: pd.DataFrame, summary: dict) -> str:
    overall = summary["overall"]
    satellite_label = {
        "SCS-01-01": "三体01星",
        "SCS-01-02": "三体02星",
        "YJ-3D": "云尖3D星",
    }.get(
        summary.get("satellite_id"),
        summary.get("satellite_id", windows.loc[0, "satellite_id"]
                     if "satellite_id" in windows else "卫星"))
    lines = [
        f"# {satellite_label}太阳阵与SOC批量留出验证",
        "",
        "## 结论",
        "",
        f"- 验证窗口：`{overall['n']}`",
        f"- SOC绝对误差中位：`{overall['soc_abs_error_median_pp']:.4f} pp`",
        f"- SOC绝对误差MAE：`{overall['soc_abs_error_mae_pp']:.4f} pp`",
        f"- SOC绝对误差p95：`{overall['soc_abs_error_p95_pp']:.4f} pp`",
        f"- 太阳阵功率RMSE中位：`{overall['solar_rmse_median_W']:.2f} W`",
        f"- 太阳阵能量相对误差中位："
        f"`{overall['solar_energy_relative_error_median_pct']:.2f}%`",
        f"- 负载能量相对误差中位："
        f"`{overall['load_energy_relative_error_median_pct']:.2f}%`",
        f"- 使用历史单机任务的窗口："
        f"`{summary['task_usage']['windows_with_tasks']}`",
        f"- 历史任务区间总数：`{summary['task_usage']['total_tasks']}`",
        "",
        "## 分层结果",
        "",
        "| 季节 | 时长档 | 负载档 | 目标SOC偏差pp | 太阳能量误差% | 负载能量误差% |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in windows.sort_values(
            ["season", "duration_band", "load_band"]).itertuples():
        lines.append(
            f"| {row.season} | {row.duration_band} | {row.load_band} | "
            f"{row.soc_error_pp:.4f} | "
            f"{row.solar_energy_relative_error_pct:.2f} | "
            f"{row.load_energy_relative_error_pct:.2f} |")
    lines.extend(["", "## 分层汇总", ""])
    for title, key in (
            ("季节", "by_season"),
            ("光照时长", "by_duration"),
            ("负载水平", "by_load")):
        lines.extend([
            f"### {title}",
            "",
            "| 分层 | 窗口数 | SOC偏差中位pp | SOC绝对误差中位pp | "
            "SOC MAE pp | SOC p95 pp |",
            "|---|---:|---:|---:|---:|---:|",
        ])
        for name, item in summary[key].items():
            lines.append(
                f"| {name} | {item['n']} | "
                f"{item['soc_signed_error_median_pp']:.4f} | "
                f"{item['soc_abs_error_median_pp']:.4f} | "
                f"{item['soc_abs_error_mae_pp']:.4f} | "
                f"{item['soc_abs_error_p95_pp']:.4f} |")
        lines.append("")
    lines.extend([
        "",
        "## 覆盖与限制",
        "",
        f"- 可用季节：`{summary['coverage']['seasons_available']}`",
        f"- 缺失季节：`{summary['coverage']['seasons_missing']}`",
        f"- 未覆盖的完整分层组合："
        f"`{len(summary['coverage']['missing_cells'])}`，明细见JSON",
        "- 目标SOC观测值来自`TMD019`与项目V-SOC曲线。",
        f"- 太阳阵观测/训练目标：`{summary.get('solar_target_formula', 'P_load + P_charge_est')}`。",
        "- 单机任务由历史部件功率或模式遥测压缩，背景负载由总负载减去"
        "被任务覆盖单机的实测功率得到。",
        "- 没有确认状态遥测的单机，其`idle/active`标签由校准功率层推断，"
        "用于验证构造但不等于真实任务状态。",
        "- 验证窗口受单机模型的整日留出清单约束；新增数据不会自动扩入冻结测试集。",
        "- 缺失分层不代表零误差，只表示当前历史数据没有足够连续片段。",
        "",
    ])
    return "\n".join(lines)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2,
                   allow_nan=False, default=str),
        encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="批量留出验证太阳阵与SOC预测")
    parser.add_argument("--result-csv", type=Path, required=True)
    parser.add_argument("--components-csv", type=Path, required=True)
    parser.add_argument("--unit-model", type=Path, required=True)
    parser.add_argument("--v-soc-curve", type=Path, required=True)
    parser.add_argument("--design-params", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--min-minutes", type=int, default=5)
    parser.add_argument("--max-per-stratum", type=int, default=1)
    args = parser.parse_args()

    result = pd.read_csv(args.result_csv, parse_dates=["t"]).sort_values("t")
    components = pd.read_csv(
        args.components_csv, parse_dates=["t"]).sort_values("t")
    unit_model = json.loads(args.unit_model.read_text(encoding="utf-8"))
    holdout_dates = (
        unit_model.get("training_split", {}).get("excluded_dates")
        if "training_split" in unit_model
        else None
    )
    windows, coverage = select_validation_windows(
        result, allowed_dates=holdout_dates, min_minutes=args.min_minutes,
        max_per_stratum=args.max_per_stratum)
    validation_dates = {
        pd.Timestamp(value).date() for value in coverage["validation_dates"]}
    training = result.loc[
        ~result["t"].dt.date.isin(validation_dates),
        ["t", "phase", "P_load", "P_solar", "P_charge_est"]
    ].copy()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    training_path = args.out_dir / "batch_validation_training_excluded.csv"
    training.to_csv(training_path, index=False, encoding="utf-8-sig")
    solar_model = calibrate_solar_power_model(
        training_path, satellite_id="SCS-01-01")
    solar_model["training_split"] = {
        "method": "exclude_whole_dates",
        "excluded_dates": sorted(str(value) for value in validation_dates),
        "source_rows": int(len(result)),
        "retained_rows": int(len(training)),
        "excluded_rows": int(len(result) - len(training)),
        "source_time_start": pd.Timestamp(
            result["t"].min()).isoformat(),
        "source_time_end": pd.Timestamp(
            result["t"].max()).isoformat(),
    }
    solar_model_path = args.out_dir / "batch_validation_holdout_solar_model.json"
    _write_json(solar_model_path, solar_model)

    curve = json.loads(args.v_soc_curve.read_text(encoding="utf-8"))
    design = json.loads(args.design_params.read_text(encoding="utf-8"))
    evaluations, summary = run_batch_validation(
        result, unit_model, solar_model,
        base_curve_points=np.asarray(curve["points"], dtype=float),
        design=design, min_minutes=args.min_minutes,
        max_per_stratum=args.max_per_stratum, components=components)
    summary["holdout_training"] = {
        "excluded_dates": sorted(str(value) for value in validation_dates),
        "n_train_rows": int(len(training)),
        "selected_degree": int(solar_model["selected_degree"]),
        "cv_rmse_W": solar_model["cross_validation"]["selected_rmse_W"],
    }
    evaluations.to_csv(
        args.out_dir / "batch_validation_windows.csv",
        index=False, encoding="utf-8-sig")
    _write_json(args.out_dir / "batch_validation_summary.json", summary)
    write_validation_split(
        args.out_dir.parent,
        satellite_id=solar_model["satellite_id"],
        dates=summary["coverage"]["validation_dates"],
        source="power_model/batch_validation_summary.json",
    )
    report_path = args.out_dir / "batch_validation_report.md"
    report_temp = report_path.with_name(report_path.name + ".tmp")
    report_temp.write_text(
        render_batch_report(evaluations, summary), encoding="utf-8")
    report_temp.replace(report_path)
    print(
        f"验证窗口={len(evaluations)}；SOC绝对误差中位="
        f"{summary['overall']['soc_abs_error_median_pp']:.4f} pp；"
        f"MAE={summary['overall']['soc_abs_error_mae_pp']:.4f} pp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
