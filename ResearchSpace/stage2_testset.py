#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export isolated second-stage model inputs, labels, and baseline predictions."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from batch_validate_forecast import select_phase_validation_windows
from power_outlier_filter import filter_power_values
from solar_power_provider import PolynomialSolarPowerProvider
from stage2_audit import sha256_file
from stage2_cases import (
    _SCSSnapshotProvider,
    _extract_historical_tasks,
)
from stage2_framework import (
    load_profile_contract,
    load_validation_split,
    primary_profile_path,
    write_evaluation_summary,
)
from stage2_quality import (
    capped_energy,
    integrate_checked_energy,
    integrate_checked_soc,
)
from unit_power_model import forecast_from_plan
from yj3d_power_forecast import YJ3DHistoricalSnapshotProvider


SCHEMA_VERSION = "1.0"


def _read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _read_csv(path):
    return pd.read_csv(path, parse_dates=["t"]).sort_values("t")


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    temp.replace(path)


def _write_csv(path, frame):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig")
    temp.replace(path)


def _atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _input_document(satellite_id, start, end, tasks, phase):
    return {
        "time": start,
        "prediction": {
            "end": end,
            "step_seconds": 60,
            "phase_segments": [{
                "start": start,
                "end": end,
                "phase": phase,
            }],
            "tasks": tasks,
        },
    }


def _task_models_available(frame, models):
    available = {}
    unavailable = []
    if frame.empty:
        return available, unavailable
    for unit_id, model in models.items():
        threshold = model.get("active_threshold_W")
        column = f"P_{unit_id}_W"
        if threshold is None:
            continue
        if column not in frame or pd.isna(frame[column].iloc[0]):
            unavailable.append(unit_id)
        else:
            available[unit_id] = model
    return available, unavailable


def _predicted_soc_frame(summary, timeline):
    values = [float(summary["initial_state"]["soc"])]
    values.extend(float(value) for value in timeline["soc_end"].tolist())
    times = [summary["window"]["start"]]
    times.extend(pd.Timestamp(value).isoformat(sep=" ")
                 for value in timeline["t_end"].tolist())
    return pd.DataFrame({
        "t": times,
        "soc_pct": [value * 100 for value in values],
    })


def _label_quality_scs(phase):
    solar_status = (
        "not_applicable" if phase == "eclipse" else "pass")
    return {
        "soc_reference": "v_soc_minute_mapping",
        "usable_for_absolute_soc": False,
        "usable_for_trend": True,
        "qc_status": "pass",
        "soc_qc_status": "pass",
        "solar_qc_status": solar_status,
        "load_qc_status": "pass",
        "quality_reasons": {},
        "solar_energy_applicability": (
            "not_applicable_eclipse" if phase == "eclipse"
            else "sunlight_proxy"),
        "solar_energy_reference": "scheme_b_proxy",
        "load_energy_reference": "tmd018_minute_power",
        "notes": (
            "SOC为TMD019逐分钟电压映射；方案B太阳能量与TMD018负载"
            "均为条件参考，不是独立硬件真值"),
    }


def _label_quality_yj(
        *,
        end_voltage_valid,
        saturated,
        soc_qc_status,
        solar_qc_status,
        load_qc_status,
        quality_reasons,
        bcr_above_cap_count,
        bcr_cap_W,
):
    qc_status = (
        "pass"
        if all(value == "pass" for value in (
            soc_qc_status, solar_qc_status, load_qc_status))
        else "fail"
    )
    return {
        "soc_reference": (
            "current_integrated_with_v_soc_endpoint_crosscheck"),
        "usable_for_absolute_soc": False,
        "usable_for_trend": True,
        "qc_status": qc_status,
        "soc_qc_status": soc_qc_status,
        "solar_qc_status": solar_qc_status,
        "load_qc_status": load_qc_status,
        "quality_reasons": quality_reasons,
        "bcr_above_cap_count": int(bcr_above_cap_count),
        "bcr_cap_W": float(bcr_cap_W),
        "has_v_soc_endpoint": bool(end_voltage_valid),
        "observed_endpoint_saturated": bool(saturated),
        "solar_energy_reference": "bcr_output",
        "solar_energy_primary_policy": "training_99p9_winsor_cap",
        "load_energy_reference": "bus_measure",
        "notes": (
            "SOC按初始V-SOC锚点加TMEA006积分，仅当全部区间通过量程、"
            "时间连续性、相位和冻结检查时形成主标签；原始值和有效区间"
            "条件值均保留。BCR主能量使用训练同口径缩尾，原始尖峰保留为"
            "诊断指标"),
    }


def _case_hash_map(case_dir):
    names = (
        "input.json", "label.json", "prediction.json",
        "predicted_soc.csv", "observed_soc.csv",
        "task_plan.json", "metadata.json")
    return {
        name: sha256_file(case_dir / name)
        for name in names
    }


def _scs_cases(profile_root):
    model_dir = profile_root / "power_model"
    result = _read_csv(profile_root / "out" / "energy_v2_result.csv").set_index("t")
    components = _read_csv(
        profile_root / "out" / "energy_v2_components.csv").set_index("t")
    contract = load_profile_contract(profile_root)
    unit_model_path = primary_profile_path(profile_root, "unit_model_path")
    solar_model_path = primary_profile_path(profile_root, "solar_model_path")
    units = _read_json(unit_model_path)
    solar_model = _read_json(solar_model_path)
    curve = _read_json(profile_root / "config" / "v_soc_curve.json")
    design = _read_json(profile_root / "config" / "design_params.json")
    windows = pd.read_csv(model_dir / "batch_validation_windows.csv")
    windows["start_ts"] = pd.to_datetime(windows["start"])
    windows = windows.sort_values("start_ts").reset_index(drop=True)
    windows["phase"] = "sun"
    split = load_validation_split(profile_root)
    eclipse_windows = select_phase_validation_windows(
        result.reset_index(),
        phase="eclipse",
        allowed_dates=split["dates"],
        min_minutes=5,
        max_per_stratum=1,
    )
    if not eclipse_windows.empty:
        windows = pd.concat(
            [windows, eclipse_windows], ignore_index=True, sort=False)
        windows["start_ts"] = pd.to_datetime(windows["start"])
        windows = windows.sort_values(
            ["start_ts", "phase"]).reset_index(drop=True)
    provider = _SCSSnapshotProvider(
        result.reset_index(), components.reset_index(), units["models"])
    solar_provider = PolynomialSolarPowerProvider(
        solar_model, model_path=solar_model_path)
    cases = []
    for index, row in windows.iterrows():
        start_naive = pd.Timestamp(row["start"])
        end_naive = pd.Timestamp(row["end"])
        start = start_naive.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        end = end_naive.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        phase = str(row.get("phase", "sun"))
        unit_window = components.loc[start_naive:end_naive]
        task_models, unavailable_units = _task_models_available(
            unit_window, units["models"])
        tasks, plan = _extract_historical_tasks(
            unit_window, task_models, end_naive)
        request = _input_document(
            "SCS-01-01", start, end, tasks, phase)
        timeline, summary = forecast_from_plan(
            request, units, provider,
            v_soc_curve=curve, design_params=design,
            solar_provider=solar_provider)
        observed = result.loc[start_naive:end_naive].copy()
        observed_soc = observed["SOC"].to_numpy(float) / 100
        observed_solar = (
            observed["P_load"].to_numpy(float)
            + observed["P_charge_est"].to_numpy(float))
        observed_load = observed["P_load"].to_numpy(float)
        observed_solar_kwh = float(
            ((observed_solar[:-1] + observed_solar[1:]) / 2).sum()
            / 60 / 1000)
        observed_load_kwh = float(
            ((observed_load[:-1] + observed_load[1:]) / 2).sum()
            / 60 / 1000)
        capacity = float(design["battery_capacity_Ah"])
        predicted_soc = float(summary["target_end_state"]["soc"]) * 100
        observed_soc_pct = float(observed_soc[-1]) * 100
        predicted_solar = float(summary["energy"]["solar_to_bus_kWh"])
        predicted_load = float(summary["energy"]["load_kWh"])
        case_id = f"W{index + 1:02d}"
        label = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "satellite_id": "SCS-01-01",
            "window": {"start": start, "end": end},
            "observed": {
                "target_soc_pct": observed_soc_pct,
                "remaining_Ah": observed_soc_pct / 100 * capacity,
                "remaining_energy_nominal_kWh": (
                    observed_soc_pct / 100 * capacity
                    * float(design["bus_nominal_V"]) / 1000),
                "solar_energy_kWh": observed_solar_kwh,
                "load_energy_kWh": observed_load_kwh,
            },
            "label_quality": _label_quality_scs(phase),
        }
        prediction = dict(summary)
        prediction["evaluation"] = {
            "target_soc_error_pp": predicted_soc - observed_soc_pct,
            "remaining_Ah_error": (
                predicted_soc - observed_soc_pct) / 100 * capacity,
            "solar_energy_relative_error_pct": (
                (predicted_solar - observed_solar_kwh)
                / observed_solar_kwh * 100),
            "load_energy_relative_error_pct": (
                (predicted_load - observed_load_kwh)
                / observed_load_kwh * 100),
        }
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "satellite_id": "SCS-01-01",
            "split": "validation",
            "season": str(row["season"]),
            "duration_band": str(row["duration_band"]),
            "load_band": str(row["load_band"]),
            "duration_minutes": int(row["duration_minutes"]),
            "task_count": len(tasks),
            "task_state": (
                "stable_transitions_extracted" if tasks
                else "no_stable_state_change"),
            "task_units_unavailable_at_start": unavailable_units,
            "phase": phase,
            "input_contract": "simplified_phase_and_tasks_only",
            "solar_power": "computed_by_holdout_degree3_model",
            "evaluation_mode": contract["primary"]["evaluation_mode"],
            "background_policy": contract["primary"]["background_policy"],
            "unit_model_split": contract["primary"]["unit_model_split"],
            "label_artifact": "observed_soc.csv",
        }
        observed_frame = pd.DataFrame({
            "t": observed.index.strftime("%Y-%m-%dT%H:%M:%S"),
            "soc_pct": observed_soc * 100,
            "solar_proxy_W": observed_solar,
            "load_W": observed_load,
        })
        cases.append({
            "case_id": case_id,
            "input": request,
            "label": label,
            "prediction": prediction,
            "task_plan": {"unit_plan": plan, "tasks": tasks},
            "metadata": metadata,
            "predicted_soc": _predicted_soc_frame(summary, timeline),
            "observed_soc": observed_frame,
        })
    support = {
        "satellite_id": "SCS-01-01",
        "task_extraction_rule": (
            "3-point centered median smoothing; merge state runs shorter "
            "than 3 minutes into the adjacent state"),
        "supported_task_units": [
            {"unit_id": unit_id,
             "active_threshold_W": model["active_threshold_W"],
             "high_threshold_W": model.get("high_threshold_W"),
             "active_power_W": model["active_power_W"],
             "high_power_W": model.get("high_power_W"),
             "low_power_W": model["low_power_W"]}
            for unit_id, model in units["models"].items()
            if model.get("active_threshold_W") is not None],
        "constant_only_units": [
            {"unit_id": unit_id, "active_power_W": model.get("active_power_W")}
            for unit_id, model in units["models"].items()
            if model.get("active_threshold_W") is None],
    }
    inputs = [
        (profile_root / "out" / "energy_v2_result.csv"),
        (profile_root / "out" / "energy_v2_components.csv"),
        unit_model_path,
        solar_model_path,
        model_dir / "batch_validation_windows.csv",
        profile_root / "config" / "v_soc_curve.json",
        profile_root / "config" / "design_params.json",
        profile_root / "config" / "stage2_profile.json",
        profile_root / "power_model" / "validation_split.json",
    ]
    return cases, support, inputs


def _yj_cases(profile_root):
    model_dir = profile_root / "power_model"
    power = _read_csv(profile_root / "data_power_minute.csv").set_index("t")
    contract = load_profile_contract(profile_root)
    unit_model_path = primary_profile_path(profile_root, "unit_model_path")
    unit_data_path = primary_profile_path(profile_root, "unit_data_path")
    branch_unit_model_path = primary_profile_path(
        profile_root, "branch_unit_model_path")
    physical_config_path = primary_profile_path(
        profile_root, "physical_config_path")
    units_data = _read_csv(unit_data_path).set_index("t")
    solar_model_path = primary_profile_path(profile_root, "solar_model_path")
    units = _read_json(unit_model_path)
    solar_model = _read_json(solar_model_path)
    curve_doc = _read_json(model_dir / "v_soc_curve.json")
    curve = sorted(curve_doc["points"], key=lambda point: point[0])
    curve_v = [float(point[0]) for point in curve]
    curve_s = [float(point[1]) for point in curve]
    design = _read_json(model_dir / "design_params.json")
    telemetry = _read_json(profile_root / "config" / "telemetry_params.json")
    assessment = _read_json(
        profile_root / "config" / "assessment_params.json")
    quality_config = contract.get("quality", {})
    max_gap_seconds = float(quality_config.get(
        "max_gap_seconds",
        assessment.get("maximum_integration_gap_seconds", 90)))
    frozen_min_samples = int(quality_config.get(
        "frozen_sensor_min_samples", 10))
    soc_ranges = {
        "battery_current_mean":
            telemetry["measurements"]["battery_current"]["range"],
        "battery_voltage_mean":
            telemetry["measurements"]["battery_voltage"]["range"],
    }
    load_ranges = {
        "bus_voltage_mean":
            telemetry["measurements"]["bus_voltage"]["range"],
        "bus_current_mean":
            telemetry["measurements"]["bus_current"]["range"],
    }
    bcr_cap_W = float(solar_model["training"]["winsor_cap_W"])
    windows = pd.read_csv(model_dir / "load_aware_comparison_windows.csv")
    bcr = pd.read_csv(profile_root / "bcr_output_minute.csv")
    bcr["t"] = pd.to_datetime(bcr["minute_beijing"], errors="coerce")
    bcr = bcr.set_index("t").sort_index()
    solar_provider = PolynomialSolarPowerProvider(
        solar_model, model_path=solar_model_path)
    cases = []
    for index, row in windows.iterrows():
        start_naive = pd.Timestamp(row["start"])
        end_naive = pd.Timestamp(row["end"])
        start = start_naive.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        end = end_naive.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        unit_window = units_data.loc[start_naive:end_naive]
        unit_window = unit_window.copy()
        for unit_id, model in units["models"].items():
            column = f"P_{unit_id}_W"
            if column not in unit_window:
                continue
            unit_window[column], _ = filter_power_values(
                unit_window[column],
                upper_limit_W=(model.get("outlier_filter") or {}).get(
                    "upper_limit_W"),
            )
            unit_window[column] = unit_window[column].interpolate(
                method="linear", limit=3, limit_direction="both")
        task_models, unavailable_units = _task_models_available(
            unit_window, units["models"])
        tasks, plan = _extract_historical_tasks(
            unit_window, task_models, end_naive)
        request = _input_document(
            "YJ-3D", start, end, tasks, "sun")
        provider = YJ3DHistoricalSnapshotProvider(
            power.reset_index(), unit_window.reset_index(),
            unit_models=units)
        timeline, summary = forecast_from_plan(
            request, units, provider,
            v_soc_curve=curve_doc, design_params=design,
            solar_provider=solar_provider)
        observed = power.loc[start_naive:end_naive].copy()
        initial_soc = float(summary["initial_state"]["soc"])
        capacity = float(design["battery_capacity_Ah"])
        soc_quality = integrate_checked_soc(
            observed.reset_index(),
            initial_soc=initial_soc,
            capacity_Ah=capacity,
            ranges=soc_ranges,
            max_gap_seconds=max_gap_seconds,
            frozen_min_samples=frozen_min_samples,
        )
        observed_soc_pct = soc_quality["target_soc_pct"]
        observed_soc_raw_pct = soc_quality["target_soc_pct_raw"]
        observed_soc_valid_only_pct = soc_quality[
            "target_soc_pct_valid_intervals_only"]
        end_voltage = float(observed["battery_voltage_mean"].iloc[-1])
        end_voltage_valid = curve_v[0] <= end_voltage <= curve_v[-1]
        v_soc_endpoint = None
        if end_voltage_valid:
            v_soc_endpoint = float(__import__("numpy").interp(
                end_voltage, curve_v, curve_s)) * 100
        bcr_window = bcr.loc[start_naive:end_naive]
        bcr_quality = capped_energy(
            bcr_window.reset_index(),
            value_column="bcr_out_W",
            cap_W=bcr_cap_W,
            max_gap_seconds=max_gap_seconds,
        )
        observed_bcr = bcr_quality["primary_energy_kWh"]
        observed_bcr_raw = bcr_quality["raw_energy_kWh"]
        load_quality = integrate_checked_energy(
            observed.reset_index(),
            value_column="bus_measure_W_mean",
            ranges=load_ranges,
            max_gap_seconds=max_gap_seconds,
            frozen_min_samples=frozen_min_samples,
        )
        observed_load = load_quality["energy_kWh"]
        observed_load_raw = load_quality["energy_kWh_raw"]
        predicted_soc = float(summary["target_end_state"]["soc"]) * 100
        predicted_solar = float(summary["energy"]["solar_to_bus_kWh"])
        predicted_load = float(summary["energy"]["load_kWh"])
        case_id = f"W{index + 1:02d}"
        label = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "satellite_id": "YJ-3D",
            "window": {"start": start, "end": end},
            "observed": {
                "target_soc_pct": observed_soc_pct,
                "target_soc_pct_raw": observed_soc_raw_pct,
                "target_soc_pct_valid_intervals_only":
                    observed_soc_valid_only_pct,
                "remaining_Ah": (
                    observed_soc_pct / 100 * capacity
                    if observed_soc_pct is not None else None),
                "remaining_energy_nominal_kWh": (
                    observed_soc_pct / 100 * capacity
                    * float(design["bus_nominal_V"]) / 1000
                    if observed_soc_pct is not None else None),
                "solar_energy_kWh": observed_bcr,
                "solar_energy_raw_kWh": observed_bcr_raw,
                "load_energy_kWh": observed_load,
                "load_energy_raw_kWh": observed_load_raw,
                "v_soc_endpoint_pct": v_soc_endpoint,
            },
            "label_quality": _label_quality_yj(
                end_voltage_valid=end_voltage_valid,
                saturated=(
                    observed_soc_pct is not None
                    and observed_soc_pct >= 99.9999),
                soc_qc_status=soc_quality["qc_status"],
                solar_qc_status=bcr_quality["qc_status"],
                load_qc_status=load_quality["qc_status"],
                quality_reasons={
                    "soc": soc_quality["quality"]["reason_counts"],
                    "load": load_quality["quality"]["reason_counts"],
                    "solar": {
                        "above_cap": bcr_quality["above_cap_count"],
                        "missing": bcr_quality["missing_samples"],
                        "gap": bcr_quality["invalid_intervals"],
                    },
                },
                bcr_above_cap_count=bcr_quality["above_cap_count"],
                bcr_cap_W=bcr_cap_W,
            ),
        }
        prediction = dict(summary)
        prediction["evaluation"] = {
            "target_soc_error_pp": (
                predicted_soc - observed_soc_pct
                if observed_soc_pct is not None else None),
            "remaining_Ah_error": (
                (predicted_soc - observed_soc_pct) / 100 * capacity
                if observed_soc_pct is not None else None),
            "target_soc_error_raw_pp": (
                predicted_soc - observed_soc_raw_pct),
            "remaining_Ah_error_raw": (
                (predicted_soc - observed_soc_raw_pct)
                / 100 * capacity),
            "target_soc_error_valid_intervals_only_pp": (
                predicted_soc - observed_soc_valid_only_pct),
            "solar_energy_relative_error_pct": (
                (predicted_solar - observed_bcr) / observed_bcr * 100),
            "solar_energy_relative_error_raw_pct": (
                (predicted_solar - observed_bcr_raw) / observed_bcr_raw * 100
                if observed_bcr_raw else None),
            "load_energy_relative_error_pct": (
                (predicted_load - observed_load) / observed_load * 100
                if observed_load not in (None, 0) else None),
            "load_energy_relative_error_raw_pct": (
                (predicted_load - observed_load_raw) / observed_load_raw * 100
                if observed_load_raw else None),
        }
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "satellite_id": "YJ-3D",
            "split": "validation",
            "season": str(row["season"]),
            "duration_band": str(row["duration_band"]),
            "load_band": str(row["load_band"]),
            "duration_minutes": int(row["minutes"]),
            "task_count": len(tasks),
            "task_state": (
                "stable_transitions_extracted" if tasks
                else "no_stable_state_change"),
            "task_units_unavailable_at_start": unavailable_units,
            "phase": "sun",
            "input_contract": "simplified_phase_and_tasks_only",
            "solar_power": "computed_by_load_aware_holdout_model",
            "evaluation_mode": contract["primary"]["evaluation_mode"],
            "background_policy": contract["primary"]["background_policy"],
            "unit_model_split": contract["primary"]["unit_model_split"],
            "qc_status": (
                "pass"
                if all(value == "pass" for value in (
                    soc_quality["qc_status"],
                    bcr_quality["qc_status"],
                    load_quality["qc_status"],
                ))
                else "fail"
            ),
            "soc_qc_status": soc_quality["qc_status"],
            "solar_qc_status": bcr_quality["qc_status"],
            "load_qc_status": load_quality["qc_status"],
            "label_artifact": "observed_soc.csv",
        }
        observed_frame = soc_quality["series"].copy()
        observed_frame["t"] = pd.to_datetime(
            observed_frame["t"]).dt.strftime("%Y-%m-%dT%H:%M:%S")
        observed_frame["soc_pct"] = observed_frame[
            "soc_valid_intervals_pct"]
        observed_frame["battery_voltage_V"] = (
            observed["battery_voltage_mean"].values)
        observed_frame["battery_current_A"] = (
            observed["battery_current_mean"].values)
        observed_frame["v_soc_endpoint_pct"] = (
            [None] * (len(observed) - 1) + [v_soc_endpoint])
        cases.append({
            "case_id": case_id,
            "input": request,
            "label": label,
            "prediction": prediction,
            "task_plan": {"unit_plan": plan, "tasks": tasks},
            "metadata": metadata,
            "predicted_soc": _predicted_soc_frame(summary, timeline),
            "observed_soc": observed_frame,
        })
    support = {
        "satellite_id": "YJ-3D",
        "task_extraction_rule": (
            "3-point centered median smoothing; merge state runs shorter "
            "than 3 minutes into the adjacent state"),
        "supported_task_units": [
            {"unit_id": unit_id,
             "active_threshold_W": model["active_threshold_W"],
             "high_threshold_W": model.get("high_threshold_W"),
             "active_power_W": model["active_power_W"],
             "high_power_W": model.get("high_power_W"),
             "low_power_W": model["low_power_W"]}
            for unit_id, model in units["models"].items()
            if model.get("active_threshold_W") is not None],
        "constant_only_units": [
            {"unit_id": unit_id, "active_power_W": model.get("active_power_W")}
            for unit_id, model in units["models"].items()
            if model.get("active_threshold_W") is None],
    }
    inputs = [
        profile_root / "data_power_minute.csv",
        unit_data_path,
        unit_model_path,
        branch_unit_model_path,
        physical_config_path,
        solar_model_path,
        model_dir / "load_aware_comparison_windows.csv",
        model_dir / "v_soc_curve.json",
        model_dir / "design_params.json",
        profile_root / "bcr_output_minute.csv",
        profile_root / "config" / "stage2_profile.json",
        profile_root / "config" / "telemetry_params.json",
        profile_root / "config" / "assessment_params.json",
        profile_root / "power_model" / "validation_split.json",
        profile_root / "power_model" / "ab_topology.json",
    ]
    return cases, support, inputs


def _input_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Stage-2 simplified model input",
        "type": "object",
        "required": ["time", "prediction"],
        "properties": {
            "time": {"type": "string", "format": "date-time"},
            "prediction": {
                "type": "object",
                "required": [
                    "end", "step_seconds", "phase_segments", "tasks"],
                "additionalProperties": False,
                "properties": {
                    "end": {"type": "string", "format": "date-time"},
                    "step_seconds": {"type": "integer", "minimum": 1},
                    "phase_segments": {"type": "array"},
                    "tasks": {"type": "array"},
                },
            },
        },
        "additionalProperties": False,
    }


def _label_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Stage-2 observation-derived label",
        "type": "object",
        "required": [
            "schema_version", "case_id", "satellite_id",
            "window", "observed", "label_quality"],
        "properties": {
            "schema_version": {"type": "string"},
            "case_id": {"type": "string"},
            "satellite_id": {"type": "string"},
            "window": {"type": "object"},
            "observed": {"type": "object"},
            "label_quality": {"type": "object"},
        },
    }


def _case_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Stage-2 case metadata",
        "type": "object",
        "required": [
            "case_id", "satellite_id", "split", "season",
            "duration_band", "load_band", "task_count", "label_artifact"],
        "properties": {
            "case_id": {"type": "string"},
            "satellite_id": {"type": "string"},
            "split": {"const": "validation"},
            "season": {"type": "string"},
            "duration_band": {"type": "string"},
            "load_band": {"type": "string"},
            "task_count": {"type": "integer", "minimum": 0},
            "label_artifact": {"type": "string"},
        },
    }


def write_testset(profile_root, output_dir=None):
    root = Path(profile_root).resolve()
    satellite_id = root.name
    contract = load_profile_contract(root)
    if satellite_id == "SCS-01-01":
        cases, support, inputs = _scs_cases(root)
    elif satellite_id == "YJ-3D":
        cases, support, inputs = _yj_cases(root)
    else:
        raise ValueError(f"不支持的卫星: {satellite_id}")
    output = (Path(output_dir) if output_dir is not None
              else root / "stage2_testset").resolve()
    cases_dir = output / "cases"
    schema_dir = output / "schema"
    cases_dir.mkdir(parents=True, exist_ok=True)
    schema_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(schema_dir / "input.schema.json", _input_schema())
    _atomic_json(schema_dir / "label.schema.json", _label_schema())
    _atomic_json(schema_dir / "case.schema.json", _case_schema())
    _atomic_json(output / "model_support.json", support)
    input_lines = []
    label_lines = []
    manifest_cases = []
    for case in cases:
        relative = Path("cases") / case["case_id"]
        case_dir = output / relative
        _atomic_json(case_dir / "input.json", case["input"])
        _atomic_json(case_dir / "label.json", case["label"])
        _atomic_json(case_dir / "prediction.json", case["prediction"])
        _atomic_json(case_dir / "task_plan.json", case["task_plan"])
        _atomic_json(case_dir / "metadata.json", case["metadata"])
        _write_csv(case_dir / "predicted_soc.csv", case["predicted_soc"])
        _write_csv(case_dir / "observed_soc.csv", case["observed_soc"])
        input_lines.append({
            "case_id": case["case_id"],
            **case["input"],
        })
        label_lines.append(case["label"])
        manifest_cases.append({
            "case_id": case["case_id"],
            "path": relative.as_posix(),
            "task_count": case["metadata"]["task_count"],
            "season": case["metadata"]["season"],
            "duration_band": case["metadata"]["duration_band"],
            "load_band": case["metadata"]["load_band"],
            "label_quality": case["label"]["label_quality"],
            "sha256": _case_hash_map(case_dir),
        })
    temp = output / "inputs.jsonl.tmp"
    temp.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n"
                for row in input_lines),
        encoding="utf-8")
    temp.replace(output / "inputs.jsonl")
    temp = output / "labels.jsonl.tmp"
    temp.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n"
                for row in label_lines),
        encoding="utf-8")
    temp.replace(output / "labels.jsonl")
    primary = dict(contract["primary"])
    try:
        split = load_validation_split(root)
    except FileNotFoundError:
        split = None
    if split is not None:
        expected_dates = contract.get("split", {}).get("expected_dates")
        if (
            expected_dates is not None
            and len(split["dates"]) != int(expected_dates)
        ):
            raise ValueError(
                f"{satellite_id} validation split 应为 "
                f"{expected_dates} 个日期，实际 {len(split['dates'])}")
        primary["validation_split"] = {
            "split_unit": split["split_unit"],
            "n_dates": len(split["dates"]),
            "dates": split["dates"],
            "source": split["source"],
        }
    evaluation_path, evaluation_cases_path = write_evaluation_summary(
        output,
        cases,
        satellite_id=satellite_id,
        primary=primary,
    )
    code_root = Path(__file__).resolve().parent
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "satellite_id": satellite_id,
        "generated_at": datetime.now(
            ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "n_cases": len(cases),
        "case_ids": [case["case_id"] for case in cases],
        "readme": "README.md",
        "model_support": "model_support.json",
        "evaluation_summary": evaluation_path.name,
        "evaluation_cases": evaluation_cases_path.name,
        "inputs": [
            {"path": str(path.relative_to(root)), "sha256": sha256_file(path)}
            for path in inputs],
        "code": [
            {"path": str(path), "sha256": sha256_file(path)}
            for path in (
                code_root / "stage2_testset.py",
                code_root / "stage2_cases.py",
                code_root / "power_outlier_filter.py",
                code_root / "branch_aggregation.py",
                code_root / "physical_unit_model.py",
                code_root / "unit_power_model.py",
                code_root / "solar_power_calibration.py",
                code_root / "yj3d_power_forecast.py",
                code_root / "stage2_framework.py",
                code_root / "stage2_quality.py",
            )],
        "cases": manifest_cases,
    }
    manifest_path = output / "manifest.json"
    _atomic_json(manifest_path, manifest)
    label_boundary = (
        "云尖星主 SOC 为初始 V-SOC 锚点加 TMEA006 梯形电流积分，"
        "仅当全部区间通过量程、时间连续性、相位和冻结检查时形成主标签。"
        "原始值和有效区间条件值分别保留。BCR 主能量使用所用留出模型"
        "训练时的 99.9% 缩尾上限，原始尖峰另列为异常事件诊断。"
        "任务中的单机为去重后的物理PL对象；同编号A/B支路电流和功率相加。"
        if satellite_id == "YJ-3D" else
        "三体01星 SOC 为 TMD019 逐分钟 V-SOC 映射，属于条件参考。"
    )
    _atomic_text(output / "README.md", f"""# {satellite_id} 第二阶段测试集

- 卫星：`{satellite_id}`
- 案例数：`{len(cases)}`
- 输入：`inputs.jsonl`；每行含 `case_id` 和简化预测请求。
- 标签：`labels.jsonl`；只含观测得到的标签，不含模型预测。
- 模型支持：`model_support.json`。
- 主评测：`evaluation_summary.json`；逐案例指标：`evaluation_cases.csv`。
- 推荐用途：批量推理读取 `inputs.jsonl`，逐案例对照 `label.json`。

## 输入边界

每个 `input.json` 只提供时间、`prediction.end`、步长、连续相位分段和任务计划。
太阳阵功率、背景负载和实际标签不在输入中。
`tasks` 中普通工作使用 `run_mode: "active"`；若模型检测到分离高功率层，
对应任务额外提供 `power_level: "high"`，稳态任务不携带该字段。

## 标签边界

{label_boundary}所有标签质量信息均在 `label_quality` 中显式列出。

## 评测口径

主指标使用 `{contract["primary"]["evaluation_mode"]}`，背景策略为
`{contract["primary"]["background_policy"]}`。共享评测按日期聚类bootstrap，
同日多窗口不会被视为完全独立样本。
""")
    return {
        "root": output,
        "manifest": manifest_path,
        "inputs": output / "inputs.jsonl",
        "labels": output / "labels.jsonl",
        "model_support": output / "model_support.json",
        "evaluation_summary": evaluation_path,
        "evaluation_cases": evaluation_cases_path,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    paths = write_testset(args.profile_dir, output_dir=args.output_dir)
    print(json.dumps({
        "root": str(paths["root"]),
        "manifest": str(paths["manifest"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
