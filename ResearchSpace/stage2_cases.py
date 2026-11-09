#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Load one representative second-stage forecast case per satellite."""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import csv
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from power_outlier_filter import filter_power_values
from solar_power_provider import PolynomialSolarPowerProvider
from stage2_framework import load_profile_contract, primary_profile_path
from stage2_quality import capped_energy, integrate_checked_soc
from unit_power_model import (
    CurrentPowerSnapshot,
    forecast_from_plan,
)
from yj3d_power_forecast import YJ3DHistoricalSnapshotProvider


def _read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _linear_interp(xs, ys, value):
    if not xs or value < xs[0] or value > xs[-1]:
        return None
    for index in range(1, len(xs)):
        if value <= xs[index]:
            x0, x1 = xs[index - 1], xs[index]
            y0, y1 = ys[index - 1], ys[index]
            if x1 == x0:
                return y1
            fraction = (value - x0) / (x1 - x0)
            return y0 + fraction * (y1 - y0)
    return ys[-1]


def _stable_states(series, thresholds, min_run=3):
    smooth = pd.to_numeric(series, errors="coerce").interpolate(
        limit_direction="both").rolling(
            3, center=True, min_periods=1).median()
    if isinstance(thresholds, (int, float)):
        thresholds = [thresholds]
    boundaries = sorted(float(value) for value in thresholds)
    states = np.digitize(
        smooth.to_numpy(dtype=float), boundaries, right=False).astype(int)
    for _ in range(4):
        runs = []
        index = 0
        while index < len(states):
            end = index + 1
            while end < len(states) and states[end] == states[index]:
                end += 1
            runs.append((index, end))
            index = end
        changed = False
        for start, end in runs:
            if end - start < min_run and 0 < start and end < len(states):
                states[start:end] = [states[start - 1]] * (end - start)
                changed = True
        if not changed:
            break
    return states.tolist()


def _extract_historical_tasks(unit_frame, models, end):
    tasks = []
    plan = {}
    times = list(unit_frame.index)
    for unit_id, model in models.items():
        threshold = model.get("active_threshold_W")
        column = f"P_{unit_id}_W"
        if (threshold is None or column not in unit_frame
                or unit_frame[column].isna().all()):
            continue
        thresholds = [float(threshold)]
        high_threshold = model.get("high_threshold_W")
        if high_threshold is not None and float(high_threshold) > float(threshold):
            thresholds.append(float(high_threshold))
        observed, _ = filter_power_values(
            unit_frame[column],
            upper_limit_W=(model.get("outlier_filter") or {}).get(
                "upper_limit_W"),
        )
        states = _stable_states(observed, thresholds, min_run=3)
        if not any(left != right for left, right in zip(states, states[1:])):
            continue
        index = 0
        unit_runs = []
        while index < len(states):
            next_index = index + 1
            while (next_index < len(states)
                   and states[next_index] == states[index]):
                next_index += 1
            start = times[index]
            stop = (times[next_index]
                    if next_index < len(times) else end)
            if stop > start:
                if states[index] == 0:
                    mode = "idle"
                    power_level = None
                elif states[index] >= 2:
                    mode = "active_high"
                    power_level = "high"
                else:
                    mode = "active"
                    power_level = None
                run_mode = "active" if power_level else mode
                task = {
                    "start": start.isoformat(),
                    "end": stop.isoformat(),
                    "unit": unit_id,
                    "run_mode": run_mode,
                    "after_mode": (
                        "active" if mode in {"active", "active_high"}
                        else "idle"),
                }
                if power_level:
                    task["power_level"] = power_level
                tasks.append(task)
                unit_run = {
                    "start": task["start"],
                    "end": task["end"],
                    "mode": mode,
                }
                if power_level:
                    unit_run["power_level"] = power_level
                unit_runs.append(unit_run)
            index = next_index
        plan[unit_id] = unit_runs
    return tasks, plan


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    temp.replace(path)


def _jsonable(value):
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class _SCSSnapshotProvider:
    def __init__(self, result, components, models):
        self.result = result.set_index("t").sort_index()
        self.components = components.set_index("t").sort_index()
        self.models = models

    def snapshot(self, timestamp, unit_ids):
        stamp = pd.Timestamp(timestamp).tz_convert(
            "Asia/Shanghai").tz_localize(None)
        power = self.result.loc[stamp]
        units = self.components.loc[stamp]
        unit_power = {}
        missing = {}
        for unit_id in unit_ids:
            column = self.models[unit_id]["source"]["power_column"]
            value = units.get(column)
            if value is None or pd.isna(value):
                missing[unit_id] = "missing_current_power"
            else:
                unit_power[unit_id] = float(value)
        return CurrentPowerSnapshot(
            time=pd.Timestamp(timestamp),
            battery_voltage_V=float(power["V_bat"]),
            bus_voltage_V=float(power["V_bus"]),
            total_load_W=float(power["P_load"]),
            phase=str(power["phase"]),
            unit_power_W=unit_power,
            missing_unit_power_W=missing,
            provenance={
                "provider": "SCS historical exact-minute snapshot",
                "selected_time": stamp.isoformat(),
            },
        )


def _window_times(rows, start, end, time_key="t"):
    start_time = datetime.fromisoformat(start).replace(tzinfo=None)
    end_time = datetime.fromisoformat(end).replace(tzinfo=None)
    selected = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row[time_key])).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError):
            continue
        if start_time <= stamp <= end_time:
            selected.append(row)
    return selected


def _case_scs(profile_root):
    model_dir = profile_root / "power_model"
    start_local = "2026-03-01 10:56:00"
    end_local = "2026-03-01 11:06:00"
    start = "2026-03-01T10:56:00+08:00"
    end = "2026-03-01T11:06:00+08:00"
    result = pd.read_csv(
        profile_root / "out" / "energy_v2_result.csv",
        parse_dates=["t"]).sort_values("t")
    components = pd.read_csv(
        profile_root / "out" / "energy_v2_components.csv",
        parse_dates=["t"]).sort_values("t")
    unit_models_path = primary_profile_path(
        profile_root, "unit_model_path")
    solar_model_path = primary_profile_path(
        profile_root, "solar_model_path")
    unit_models = _read_json(unit_models_path)
    solar_model = _read_json(solar_model_path)
    curve = _read_json(profile_root / "config" / "v_soc_curve.json")
    design = _read_json(profile_root / "config" / "design_params.json")
    start_naive = pd.Timestamp(start_local)
    end_naive = pd.Timestamp(end_local)
    unit_window = components.set_index("t").loc[start_naive:end_naive]
    tasks, task_plan = _extract_historical_tasks(
        unit_window, unit_models["models"], end_naive)
    request = {
        "time": start,
        "prediction": {
            "end": end,
            "step_seconds": 60,
            "phase_segments": [{
                "start": "10:56",
                "end": "11:06",
                "phase": "sun",
            }],
            "tasks": tasks,
        },
    }
    timeline_frame, summary = forecast_from_plan(
        request, unit_models,
        _SCSSnapshotProvider(result, components, unit_models["models"]),
        v_soc_curve=curve, design_params=design,
        solar_provider=PolynomialSolarPowerProvider(
            solar_model, model_path=solar_model_path))
    request = _jsonable(request)
    observed_rows = result.loc[
        (result["t"] >= start_naive) & (result["t"] <= end_naive)
    ].sort_values("t")
    times = observed_rows["t"].dt.strftime("%Y-%m-%dT%H:%M:%S").tolist()
    observed = [
        float(value) / 100 for value in observed_rows["SOC"].tolist()]
    predicted = [summary["initial_state"]["soc"] * 100]
    predicted.extend(
        float(value) * 100 for value in timeline_frame["soc_end"].tolist())
    if len(predicted) != len(times):
        raise ValueError("三体01星预测与实际SOC时间轴长度不一致")
    actual_solar = (
        observed_rows["P_load"].to_numpy(float)
        + observed_rows["P_charge_est"].to_numpy(float))
    actual_load = observed_rows["P_load"].to_numpy(float)
    observed_solar = float(
        ((actual_solar[:-1] + actual_solar[1:]) / 2).sum()
        / 60 / 1000)
    observed_load = float(
        ((actual_load[:-1] + actual_load[1:]) / 2).sum()
        / 60 / 1000)
    predicted_solar = float(
        timeline_frame["solar_power_W"].sum() / 60 / 1000)
    predicted_load = float(
        timeline_frame["total_load_W"].sum() / 60 / 1000)
    capacity = float(design["battery_capacity_Ah"])
    predicted_soc = float(summary["target_end_state"]["soc"])
    observed_soc = observed[-1]

    case_dir = model_dir / "representative_case_scs"
    case_input = case_dir / "input.json"
    case_output = case_dir / "output.json"
    case_timeline = case_dir / "prediction_timeline.csv"
    case_observed = case_dir / "observed_soc.csv"
    case_tasks = case_dir / "task_plan.json"
    _atomic_json(case_input, request)
    _atomic_json(case_output, summary)
    _atomic_json(case_tasks, _jsonable({
        "extraction": (
            "3-point median smoothing and removal of runs shorter than "
            "3 minutes from historical component power"),
        "unit_plan": task_plan,
    }))
    case_dir.mkdir(parents=True, exist_ok=True)
    temp = case_timeline.with_name(case_timeline.name + ".tmp")
    timeline_frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(case_timeline)
    temp = case_observed.with_name(case_observed.name + ".tmp")
    pd.DataFrame({
        "t": times,
        "soc_pct": [value * 100 for value in observed],
    }).to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(case_observed)

    return {
        "schema_version": "1.0",
        "case_id": "scs-task-sun-20260301-1056",
        "satellite_id": "SCS-01-01",
        "title": "2条历史提取任务，太阳阵由代理模型内部计算",
        "window": {
            "start": start,
            "end": end,
            "minutes": len(times) - 1,
            "phase": "sun",
        },
        "reference_mode": "conditional_v_soc_minute_curve",
        "input_request": request,
        "derived_inputs": {
            "initial_soc_pct": summary["initial_state"]["soc"] * 100,
            "battery_voltage_V": summary["initial_state"]["battery_voltage_V"],
            "background_load_W": summary["derived_state"]["background_load_W"],
            "phase": summary["derived_state"]["phase"],
            "task_count": len(request["prediction"]["tasks"]),
        },
        "outputs": {
            "predicted_target_soc_pct": predicted_soc * 100,
            "observed_target_soc_pct": observed_soc * 100,
            "soc_signed_error_pp": (predicted_soc - observed_soc) * 100,
            "predicted_remaining_Ah": predicted_soc * capacity,
            "observed_remaining_Ah": observed_soc * capacity,
            "remaining_Ah_error": (
                predicted_soc - observed_soc) * capacity,
            "predicted_solar_energy_kWh": predicted_solar,
            "observed_solar_proxy_energy_kWh": observed_solar,
            "solar_energy_relative_error_pct": (
                (predicted_solar - observed_solar) / observed_solar * 100),
            "predicted_load_energy_kWh": predicted_load,
            "observed_load_energy_kWh": observed_load,
            "load_energy_relative_error_pct": (
                (predicted_load - observed_load) / observed_load * 100),
        },
        "series": {
            "times": times,
            "predicted_soc_pct": predicted,
            "observed_soc_pct": [value * 100 for value in observed],
        },
        "sources": {
            "input": "power_model/representative_case_scs/input.json",
            "output": "power_model/representative_case_scs/output.json",
            "predicted_timeline": (
                "power_model/representative_case_scs/"
                "prediction_timeline.csv"),
            "observed_timeline": "out/energy_v2_result.csv",
            "task_plan": "power_model/representative_case_scs/task_plan.json",
            "observed_soc": (
                "power_model/representative_case_scs/observed_soc.csv"),
        },
        "boundaries": [
            "实际SOC由TMD019与项目V-SOC曲线逐分钟映射，不是独立硬件SOC真值",
            "太阳阵目标采用方案B条件代理，实际值不是独立阵列测量",
            "2条任务由历史成分功率的3点中位平滑和3分钟去抖提取，是验证构造而非未来任务计划",
            "输入不提供太阳阵功率，阳照功率由历史留出三次多项式按相位段内部生成",
            "该单窗口解释误差形态，不替代19个窗口的总体审计",
        ],
    }


def _case_yj(profile_root):
    model_dir = profile_root / "power_model"
    input_path = model_dir / "validation_sun_20260728_1622.json"
    output_path = model_dir / "validation_sun_20260728_1622_holdout_output.json"
    curve_path = model_dir / "v_soc_curve.json"
    contract = load_profile_contract(profile_root)
    unit_models_path = primary_profile_path(
        profile_root, "unit_model_path")
    solar_model_path = primary_profile_path(
        profile_root, "solar_model_path")
    design_path = model_dir / "design_params.json"
    request = _read_json(input_path)
    curve = _read_json(curve_path)
    unit_models = _read_json(unit_models_path)
    solar_model = _read_json(solar_model_path)
    design = _read_json(design_path)
    telemetry = _read_json(
        profile_root / "config" / "telemetry_params.json")
    assessment = _read_json(
        profile_root / "config" / "assessment_params.json")
    quality_config = contract.get("quality", {})
    max_gap_seconds = float(quality_config.get(
        "max_gap_seconds",
        assessment.get("maximum_integration_gap_seconds", 90)))
    frozen_min_samples = int(quality_config.get(
        "frozen_sensor_min_samples", 10))
    start = request["time"]
    end = request["prediction"]["end"]
    source = pd.read_csv(
        profile_root / "data_power_minute.csv", parse_dates=["t"]).sort_values("t")
    unit_data = pd.read_csv(
        primary_profile_path(profile_root, "unit_data_path"),
        parse_dates=["t"],
    ).sort_values("t")
    unit_window = unit_data.set_index("t").loc[
        pd.Timestamp(start).tz_localize(None):
        pd.Timestamp(end).tz_localize(None)]
    tasks, task_plan = _extract_historical_tasks(
        unit_window, unit_models["models"], pd.Timestamp(end).tz_localize(None))
    request["prediction"]["tasks"] = tasks
    provider = YJ3DHistoricalSnapshotProvider(
        source, unit_window.reset_index(), unit_models=unit_models)
    timeline_frame, summary = forecast_from_plan(
        request, unit_models, provider,
        v_soc_curve=curve, design_params=design,
        solar_provider=PolynomialSolarPowerProvider(
            solar_model, model_path=solar_model_path))

    observed_rows = source.loc[
        (source["t"] >= pd.Timestamp(start).tz_localize(None))
        & (source["t"] <= pd.Timestamp(end).tz_localize(None))
    ].sort_values("t")
    times = observed_rows["t"].dt.strftime("%Y-%m-%dT%H:%M:%S").tolist()
    capacity = float(design["battery_capacity_Ah"])
    initial_soc = float(summary["initial_state"]["soc"])
    soc_quality = integrate_checked_soc(
        observed_rows,
        initial_soc=initial_soc,
        capacity_Ah=capacity,
        ranges={
            "battery_current_mean":
                telemetry["measurements"]["battery_current"]["range"],
            "battery_voltage_mean":
                telemetry["measurements"]["battery_voltage"]["range"],
        },
        max_gap_seconds=max_gap_seconds,
        frozen_min_samples=frozen_min_samples,
    )
    observed_soc = (
        soc_quality["series"]["soc_valid_intervals_pct"].to_numpy(
            dtype=float) / 100.0).tolist()
    predicted = [initial_soc * 100]
    predicted.extend(
        float(value) * 100 for value in timeline_frame["soc_end"].tolist())
    if not (len(predicted) == len(times) == len(observed_soc)):
        raise ValueError("云尖3D星预测与实际SOC时间轴长度不一致")

    curve_points = sorted(curve["points"], key=lambda point: point[0])
    voltage_end = _number(observed_rows.iloc[-1]["battery_voltage_mean"])
    v_soc_endpoint = _linear_interp(
        [float(point[0]) for point in curve_points],
        [float(point[1]) for point in curve_points],
        voltage_end)
    v_soc_endpoint_pct = (
        None if v_soc_endpoint is None else v_soc_endpoint * 100)
    current_integrated_pct = soc_quality["target_soc_pct"]
    current_integrated_raw_pct = soc_quality["target_soc_pct_raw"]
    load_series = observed_rows["bus_measure_W_mean"].to_numpy(float)
    load_observed = float(
        ((load_series[:-1] + load_series[1:]) / 2).sum()
        / 60 / 1000)
    bcr = pd.read_csv(profile_root / "bcr_output_minute.csv")
    bcr["t"] = pd.to_datetime(bcr["minute_beijing"], errors="coerce")
    bcr_window = bcr.loc[
        (bcr["t"] >= pd.Timestamp(start).tz_localize(None))
        & (bcr["t"] <= pd.Timestamp(end).tz_localize(None))
    ].sort_values("t")
    bcr_quality = capped_energy(
        bcr_window.reset_index(),
        value_column="bcr_out_W",
        cap_W=float(solar_model["training"]["winsor_cap_W"]),
        max_gap_seconds=max_gap_seconds,
    )
    observed_bcr = bcr_quality["primary_energy_kWh"]
    predicted_solar = float(timeline_frame["solar_power_W"].sum() / 60 / 1000)
    predicted_load = float(timeline_frame["total_load_W"].sum() / 60 / 1000)
    current_remaining = (
        current_integrated_pct / 100 * capacity
        if current_integrated_pct is not None else None)
    predicted_remaining = float(summary["target_end_state"]["soc"]) * capacity

    case_dir = model_dir / "representative_case_yj"
    case_input = case_dir / "input.json"
    case_output = case_dir / "output.json"
    case_timeline = case_dir / "prediction_timeline.csv"
    case_observed = case_dir / "observed_soc_current_integrated.csv"
    case_tasks = case_dir / "task_plan.json"
    _atomic_json(case_input, request)
    _atomic_json(case_output, summary)
    _atomic_json(case_tasks, task_plan)
    case_dir.mkdir(parents=True, exist_ok=True)
    temp = case_timeline.with_name(case_timeline.name + ".tmp")
    timeline_frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(case_timeline)
    temp = case_observed.with_name(case_observed.name + ".tmp")
    observed_frame = soc_quality["series"].copy()
    observed_frame["soc_pct"] = observed_frame["soc_valid_intervals_pct"]
    observed_frame["battery_voltage_V"] = (
        observed_rows["battery_voltage_mean"].tolist())
    observed_frame["battery_current_A"] = (
        observed_rows["battery_current_mean"].tolist())
    observed_frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(case_observed)

    return {
        "schema_version": "1.0",
        "case_id": "yj-sun-20260728-1622",
        "satellite_id": "YJ-3D",
        "title": "从历史单机功率提取任务的负载感知留出窗口",
        "window": {
            "start": start,
            "end": end,
            "minutes": len(times) - 1,
            "phase": "sun",
        },
        "reference_mode": "current_integrated_with_v_soc_crosscheck",
        "qc_status": soc_quality["qc_status"],
        "quality": {
            "soc": soc_quality["quality"],
            "solar": bcr_quality,
        },
        "input_request": request,
        "derived_inputs": {
            "initial_soc_pct": summary["initial_state"]["soc"] * 100,
            "battery_voltage_V": summary["initial_state"]["battery_voltage_V"],
            "background_load_W": summary["derived_state"]["background_load_W"],
            "phase": summary["derived_state"]["phase"],
            "task_count": len(request["prediction"].get("tasks", [])),
        },
        "outputs": {
            "predicted_target_soc_pct": (
                summary["target_end_state"]["soc"] * 100),
            "observed_target_soc_pct": current_integrated_pct,
            "observed_target_soc_pct_raw": current_integrated_raw_pct,
            "soc_signed_error_pp": (
                (summary["target_end_state"]["soc"]
                 - float(current_integrated_pct) / 100.0) * 100
                if current_integrated_pct is not None else None),
            "predicted_remaining_Ah": predicted_remaining,
            "observed_remaining_Ah": (
                current_integrated_pct / 100 * capacity
                if current_integrated_pct is not None else None),
            "remaining_Ah_error": (
                predicted_remaining - current_remaining
                if current_remaining is not None else None),
            "predicted_solar_energy_kWh": predicted_solar,
            "observed_solar_proxy_energy_kWh": observed_bcr,
            "solar_energy_relative_error_pct": (
                (predicted_solar - observed_bcr) / observed_bcr * 100),
            "solar_energy_relative_error_raw_pct": (
                (predicted_solar - bcr_quality["raw_energy_kWh"])
                / bcr_quality["raw_energy_kWh"] * 100
                if bcr_quality["raw_energy_kWh"] else None),
            "predicted_load_energy_kWh": predicted_load,
            "observed_load_energy_kWh": load_observed,
            "load_energy_relative_error_pct": (
                (predicted_load - load_observed) / load_observed * 100),
            "v_soc_endpoint_soc_pct": v_soc_endpoint_pct,
            "v_soc_minus_current_integrated_pp": (
                None if (v_soc_endpoint_pct is None
                         or current_integrated_pct is None)
                else v_soc_endpoint_pct - current_integrated_pct),
        },
        "series": {
            "times": times,
            "predicted_soc_pct": predicted,
            "observed_soc_pct": [value * 100 for value in observed_soc],
        },
        "sources": {
            "input": "power_model/representative_case_yj/input.json",
            "output": "power_model/representative_case_yj/output.json",
            "predicted_timeline": (
                "power_model/representative_case_yj/prediction_timeline.csv"),
            "observed_timeline": "data_power_minute.csv",
            "soc_curve": "power_model/v_soc_curve.json",
            "task_plan": "power_model/representative_case_yj/task_plan.json",
            "observed_soc": (
                "power_model/representative_case_yj/"
                "observed_soc_current_integrated.csv"),
        },
        "boundaries": [
            "实际条件曲线由初始V-SOC锚点加TMEA006梯形电流积分得到，不施加放电V-SOC逐分钟映射",
            "72.737%是本窗口电压端点套用7.84 A放电V-SOC表得到的交叉参考，与电流积分终点存在约2.89 pp差异",
            "15条任务由功率序列3点中位平滑并剔除少于3分钟的状态短跳变后提取",
            "BCR输出目标不是PV输入端功率，单窗口不替代10窗口总体审计",
            "SOC主标签要求全部区间通过量程、时间连续性、相位和冻结检查",
            "BCR主能量按训练同一99.9%缩尾上限计算，原始尖峰另作诊断",
        ],
    }


@lru_cache(maxsize=4)
def _load_representative_case_cached(profile_root):
    root = Path(profile_root).resolve()
    if root.name == "SCS-01-01":
        return _case_scs(root)
    if root.name == "YJ-3D":
        return _case_yj(root)
    raise ValueError(f"没有代表性案例配置: {root}")


def load_representative_case(profile_root):
    return _load_representative_case_cached(str(Path(profile_root).resolve()))
