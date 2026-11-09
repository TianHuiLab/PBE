#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Direction-aware provisional V-SOC calibration helpers.

The calibration is conditional on the current capacity, efficiency and inferred
charge/discharge power definitions.  It does not establish an independent SOC
ground truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _monotonic_increasing(values: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators isotonic regression for increasing values."""
    blocks = []
    for value in np.asarray(values, dtype=float):
        blocks.append([float(value), 1])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            right = blocks.pop()
            left = blocks.pop()
            total = left[1] + right[1]
            blocks.append([
                (left[0] * left[1] + right[0] * right[1]) / total,
                total,
            ])
    result = []
    for value, count in blocks:
        result.extend([value] * count)
    return np.asarray(result, dtype=float)


def collect_directional_soc_samples(
        result: pd.DataFrame,
        base_curve_points: np.ndarray,
        *,
        capacity_Ah: float,
        nominal_voltage_V: float,
        eta_charge: float,
        eta_discharge: float,
        min_segment_minutes: int = 10,
        max_gap_seconds: float = 90.0,
) -> pd.DataFrame:
    required = {"t", "phase", "V_bat", "P_charge_est", "P_disch_est"}
    if not isinstance(result, pd.DataFrame) or required - set(result.columns):
        raise ValueError(f"历史结果缺少列: {sorted(required-set(result.columns))}")
    if min_segment_minutes < 2:
        raise ValueError("min_segment_minutes 至少为2")
    curve = np.asarray(base_curve_points, dtype=float)
    if curve.ndim != 2 or curve.shape[1] != 2 or len(curve) < 2:
        raise ValueError("base_curve_points 必须为 [voltage,SOC] 点列")
    frame = result.copy()
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    frame = frame.sort_values("t").reset_index(drop=True)
    if frame["t"].isna().any() or frame["t"].duplicated().any():
        raise ValueError("历史结果含无效或重复时间")
    frame["phase"] = frame["phase"].astype(str)
    for column in ("V_bat", "P_charge_est", "P_disch_est"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    gaps = frame["t"].diff().dt.total_seconds().fillna(np.inf)
    segment_id = (
        frame["phase"].ne(frame["phase"].shift()) |
        gaps.gt(max_gap_seconds)
    ).cumsum()
    rows = []
    capacity_Wh = float(capacity_Ah) * float(nominal_voltage_V)
    if capacity_Wh <= 0:
        raise ValueError("容量和标称电压必须为正数")
    for segment, group in frame.groupby(segment_id, sort=True):
        group = group.reset_index(drop=True)
        if len(group) < min_segment_minutes:
            continue
        if group["V_bat"].isna().any():
            continue
        charge_power = group["P_charge_est"].fillna(0).clip(lower=0)
        discharge_power = group["P_disch_est"].fillna(0).abs()
        phase = group["phase"].iloc[0]
        if phase in {"日照", "sun", "阳照"} and charge_power.sum() > 0:
            direction = "charge"
        elif phase in {"地影", "eclipse", "阴影"} and discharge_power.sum() > 0:
            direction = "discharge"
        else:
            continue
        soc = float(np.interp(
            float(group["V_bat"].iloc[0]), curve[:, 0], curve[:, 1]))
        soc = float(np.clip(soc, 0, 1))
        for index, row in group.iterrows():
            if index > 0:
                charge = float(charge_power.iloc[index - 1])
                discharge = float(discharge_power.iloc[index - 1])
                delta = (
                    charge * float(eta_charge)
                    - discharge / float(eta_discharge)
                ) / capacity_Wh / 60
                soc = float(np.clip(soc + delta, 0, 1))
            rows.append({
                "t": row["t"],
                "segment_id": int(segment),
                "direction": direction,
                "soc": soc,
                "voltage": float(row["V_bat"]),
                "phase": phase,
            })
    if not rows:
        raise ValueError("没有满足条件的充电/放电连续片段")
    return pd.DataFrame(rows)


def fit_directional_voltage_curves(
        samples: pd.DataFrame,
        *,
        min_samples: int = 10,
        bin_width: float = .05,
) -> dict:
    if not isinstance(samples, pd.DataFrame):
        raise ValueError("samples 必须为 DataFrame")
    required = {"direction", "soc", "voltage"}
    if required - set(samples.columns):
        raise ValueError(f"samples 缺少列: {sorted(required-set(samples.columns))}")
    if not 0 < bin_width <= .25:
        raise ValueError("bin_width 必须在 (0,0.25] 内")
    frame = samples[["direction", "soc", "voltage"]].copy()
    frame["direction"] = frame["direction"].astype(str)
    frame["soc"] = pd.to_numeric(frame["soc"], errors="coerce")
    frame["voltage"] = pd.to_numeric(frame["voltage"], errors="coerce")
    frame = frame[
        frame["direction"].isin(["charge", "discharge"])
        & np.isfinite(frame["soc"]) & frame["soc"].between(0, 1)
        & np.isfinite(frame["voltage"]) & frame["voltage"].gt(0)
    ]
    curves = {}
    stats = {}
    for direction in ("charge", "discharge"):
        part = frame[frame["direction"] == direction].copy()
        if part.empty:
            raise ValueError(f"缺少 {direction} 方向样本")
        part["soc_bin"] = (part["soc"] / bin_width).round() * bin_width
        grouped = (part.groupby("soc_bin", sort=True)["voltage"]
                   .agg(["median", "count"]).reset_index())
        grouped = grouped[grouped["count"] >= min_samples]
        if len(grouped) < 2:
            raise ValueError(
                f"{direction} 方向有效SOC分箱不足（需要至少两个）")
        soc = grouped["soc_bin"].clip(0, 1).to_numpy(dtype=float)
        voltage = _monotonic_increasing(
            grouped["median"].to_numpy(dtype=float))
        # Keep inverse lookup deterministic even when isotonic fitting produces
        # a short voltage plateau.
        voltage = np.maximum.accumulate(
            voltage + np.arange(len(voltage)) * 1e-9)
        curves[direction] = np.column_stack([voltage, soc])
        stats[direction] = {
            "samples": int(len(part)),
            "bins": int(len(grouped)),
            "soc_min": float(soc.min()),
            "soc_max": float(soc.max()),
            "voltage_min": float(voltage.min()),
            "voltage_max": float(voltage.max()),
        }
    return {
        "schema_version": "1.0",
        "calibration_status": "provisional_conditional_directional",
        "bin_width": float(bin_width),
        "curves": curves,
        "stats": stats,
    }


def calibrate_directional_v_soc(
        result: pd.DataFrame,
        base_curve_points: np.ndarray,
        *,
        capacity_Ah: float,
        nominal_voltage_V: float,
        eta_charge: float,
        eta_discharge: float,
        min_segment_minutes: int = 10,
        bin_width: float = .05,
        min_samples: int = 10,
        folds: int = 5,
        charge_power_source: str = "P_charge_est",
        discharge_power_source: str = "abs(P_disch_est)",
) -> dict:
    base = np.asarray(base_curve_points, dtype=float)
    samples = collect_directional_soc_samples(
        result, base, capacity_Ah=capacity_Ah,
        nominal_voltage_V=nominal_voltage_V,
        eta_charge=eta_charge, eta_discharge=eta_discharge,
        min_segment_minutes=min_segment_minutes)
    unique_segments = np.sort(samples["segment_id"].unique())
    if len(unique_segments) < 2:
        raise ValueError("方向校准至少需要两个连续片段")
    folds = max(2, min(int(folds), len(unique_segments)))
    split = samples["segment_id"].astype(int) % folds
    squared = {"charge": [], "discharge": []}
    squared_base = {"charge": [], "discharge": []}
    soc_abs = {"charge": [], "discharge": []}
    soc_abs_base = {"charge": [], "discharge": []}
    soc_squared = {"charge": [], "discharge": []}
    soc_squared_base = {"charge": [], "discharge": []}
    validation_samples = 0
    for fold in range(folds):
        train = samples[split != fold]
        test = samples[split == fold]
        if test.empty:
            continue
        if set(train["direction"]) != {"charge", "discharge"}:
            continue
        fitted = fit_directional_voltage_curves(
            train, min_samples=min_samples, bin_width=bin_width)
        for direction in ("charge", "discharge"):
            part = test[test["direction"] == direction]
            if part.empty:
                continue
            curve = fitted["curves"][direction]
            predicted = np.interp(
                part["soc"].to_numpy(float),
                curve[:, 1], curve[:, 0])
            baseline = np.interp(
                part["soc"].to_numpy(float),
                base[:, 1], base[:, 0])
            squared[direction].extend(
                ((predicted - part["voltage"].to_numpy(float)) ** 2).tolist())
            squared_base[direction].extend(
                ((baseline - part["voltage"].to_numpy(float)) ** 2).tolist())
            baseline_soc = np.interp(
                part["voltage"].to_numpy(float), base[:, 0], base[:, 1])
            directional_soc = np.interp(
                part["voltage"].to_numpy(float), curve[:, 0], curve[:, 1])
            reference_soc = part["soc"].to_numpy(float)
            base_error = baseline_soc - reference_soc
            directional_error = directional_soc - reference_soc
            soc_abs_base[direction].extend(np.abs(base_error).tolist())
            soc_abs[direction].extend(np.abs(directional_error).tolist())
            soc_squared_base[direction].extend((base_error ** 2).tolist())
            soc_squared[direction].extend((directional_error ** 2).tolist())
            validation_samples += len(part)
    if any(not squared[direction] for direction in squared):
        raise ValueError("方向V-SOC交叉验证没有同时覆盖充电和放电")
    model = fit_directional_voltage_curves(
        samples, min_samples=min_samples, bin_width=bin_width)
    model["curves"] = {
        direction: curve.tolist()
        for direction, curve in model["curves"].items()
    }
    charge = model["curves"]["charge"]
    discharge = model["curves"]["discharge"]
    charge = np.asarray(charge, dtype=float)
    discharge = np.asarray(discharge, dtype=float)
    soc_min = max(float(charge[:, 1].min()), float(discharge[:, 1].min()))
    soc_max = min(float(charge[:, 1].max()), float(discharge[:, 1].max()))
    grid = np.linspace(soc_min, soc_max, 101)
    gap = (np.interp(grid, charge[:, 1], charge[:, 0])
           - np.interp(grid, discharge[:, 1], discharge[:, 0]))
    cv = {
        direction: {
            "n_validation": int(len(squared[direction])),
            "baseline_mae_V": float(
                np.mean(np.sqrt(squared_base[direction]))),
            "directional_mae_V": float(
                np.mean(np.sqrt(squared[direction]))),
            "baseline_rmse_V": float(np.sqrt(np.mean(squared_base[direction]))),
            "directional_rmse_V": float(np.sqrt(np.mean(squared[direction]))),
            "baseline_soc_mae_pp": float(
                np.mean(soc_abs_base[direction]) * 100),
            "directional_soc_mae_pp": float(
                np.mean(soc_abs[direction]) * 100),
            "baseline_soc_rmse_pp": float(
                np.sqrt(np.mean(soc_squared_base[direction])) * 100),
            "directional_soc_rmse_pp": float(
                np.sqrt(np.mean(soc_squared[direction])) * 100),
        }
        for direction in ("charge", "discharge")
    }
    model.update({
        "calibration_status": "provisional_conditional_directional",
        "conditional_on": {
            "capacity_Ah": float(capacity_Ah),
            "nominal_voltage_V": float(nominal_voltage_V),
            "eta_charge": float(eta_charge),
            "eta_discharge": float(eta_discharge),
            "charge_power_source": str(charge_power_source),
            "discharge_power_source": str(discharge_power_source),
        },
        "direction_gap_voltage": {
            "definition": "V_charge - V_discharge at matched propagated SOC",
            "soc_min": soc_min,
            "soc_max": soc_max,
            "median_V": float(np.median(gap)),
            "p05_V": float(np.quantile(gap, .05)),
            "p95_V": float(np.quantile(gap, .95)),
        },
        "cross_validation": cv,
        "n_segments": int(samples["segment_id"].nunique()),
        "n_validation_samples": int(validation_samples),
        "warnings": [
            "方向曲线以能量递推SOC为伪标签，不是独立SOC真值",
            "P_charge_est/P_disch_est与65Ah容量假设会同时影响方向曲线",
            "交叉验证用于A/B比较，不证明充放电滞回已独立辨识",
        ],
    })
    total = sum(cv[direction]["n_validation"]
                for direction in ("charge", "discharge"))
    baseline_overall = sum(
        cv[direction]["baseline_soc_mae_pp"]
        * cv[direction]["n_validation"]
        for direction in ("charge", "discharge")) / total
    directional_overall = sum(
        cv[direction]["directional_soc_mae_pp"]
        * cv[direction]["n_validation"]
        for direction in ("charge", "discharge")) / total
    charge_improvement = (
        cv["charge"]["baseline_soc_mae_pp"]
        - cv["charge"]["directional_soc_mae_pp"])
    discharge_improvement = (
        cv["discharge"]["baseline_soc_mae_pp"]
        - cv["discharge"]["directional_soc_mae_pp"])
    recommended = charge_improvement > 0 and discharge_improvement > 0
    model["adoption_decision"] = {
        "baseline_overall_soc_mae_pp": baseline_overall,
        "directional_overall_soc_mae_pp": directional_overall,
        "overall_improvement_pp": baseline_overall - directional_overall,
        "charge_improvement_pp": charge_improvement,
        "discharge_improvement_pp": discharge_improvement,
        "recommended_for_production": bool(recommended),
        "reason": (
            "充电与放电方向的SOC映射MAE均改善"
            if recommended else
            "至少一个方向的SOC映射MAE未改善；保留现有单曲线"
        ),
    }
    model["recommended_for_production"] = bool(recommended)
    return model


def map_soc_from_voltage(voltage: float, direction: str, model: dict) -> float:
    if direction not in {"charge", "discharge"}:
        raise ValueError("direction 必须为 charge 或 discharge")
    try:
        curve = np.asarray(model["curves"][direction], dtype=float)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"模型缺少 {direction} 方向曲线") from exc
    if curve.ndim != 2 or curve.shape[1] != 2 or len(curve) < 2:
        raise ValueError("方向曲线必须为 [voltage,SOC] 点列")
    value = float(voltage)
    return float(np.clip(
        np.interp(value, curve[:, 0], curve[:, 1]), 0.0, 1.0))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def render_directional_report(model: dict) -> str:
    cv = model["cross_validation"]
    gap = model["direction_gap_voltage"]
    decision = model["adoption_decision"]
    satellite_label = {
        "SCS-01-01": "三体01星",
        "YJ-3D": "云尖3D星",
    }.get(model.get("satellite_id"), model.get("satellite_id", "卫星"))
    lines = [
        f"# {satellite_label}充电/放电方向V-SOC校准",
        "",
        "## 结论",
        "",
        f"- 校准状态：`{model['calibration_status']}`",
        f"- 充电与放电中位电压差：`{gap['median_V']:.4f} V`，"
        f"p05–p95为`{gap['p05_V']:.4f}–{gap['p95_V']:.4f} V`",
        f"- 是否建议替换生产单曲线：**"
        f"{'是' if model['recommended_for_production'] else '否'}**",
        f"- 原因：{decision['reason']}",
        "",
        "## A/B留出误差",
        "",
        "| 方向 | 验证样本 | 基线电压MAE | 方向曲线电压MAE | "
        "基线SOC MAE | 方向曲线SOC MAE |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for direction in ("charge", "discharge"):
        item = cv[direction]
        lines.append(
            f"| {direction} | {item['n_validation']} | "
            f"{item['baseline_mae_V']:.4f} V | "
            f"{item['directional_mae_V']:.4f} V | "
            f"{item['baseline_soc_mae_pp']:.4f} pp | "
            f"{item['directional_soc_mae_pp']:.4f} pp |")
    lines.extend([
        "",
        f"总体SOC MAE由`{decision['baseline_overall_soc_mae_pp']:.4f} pp`变为"
        f"`{decision['directional_overall_soc_mae_pp']:.4f} pp`，"
        f"改善量`{decision['overall_improvement_pp']:.4f} pp`。",
        "",
        "## 证据边界",
        "",
        f"- 方向SOC标签由`{model['conditional_on']['charge_power_source']}` / "
        f"`{model['conditional_on']['discharge_power_source']}`、"
        f"{model['conditional_on']['capacity_Ah']}Ah容量和效率递推得到，"
        "不是独立SOC测量。",
        "- 电压曲线虽然能降低部分方向的电压误差，但充电方向的SOC反演误差可能变差。",
        "- 只有充电和放电两个方向的SOC映射误差都改善，才建议更新生产曲线。",
        "- 本模型不能用于宣称SOH、真实容量或飞行级剩余电量。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="方向感知V-SOC条件校准")
    parser.add_argument("--result-csv", type=Path, required=True)
    parser.add_argument("--satellite-id", default="SCS-01-01")
    parser.add_argument("--v-soc-curve", type=Path, required=True)
    parser.add_argument("--design-params", type=Path, required=True)
    parser.add_argument("--min-segment-minutes", type=int, default=10)
    parser.add_argument("--bin-width", type=float, default=.05)
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--charge-power-source", default="P_charge_est")
    parser.add_argument("--discharge-power-source", default="abs(P_disch_est)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-report", type=Path, required=True)
    args = parser.parse_args()
    result = pd.read_csv(args.result_csv, parse_dates=["t"])
    curve = json.loads(args.v_soc_curve.read_text(encoding="utf-8"))
    design = json.loads(args.design_params.read_text(encoding="utf-8"))
    model = calibrate_directional_v_soc(
        result, np.asarray(curve["points"], dtype=float),
        capacity_Ah=design["battery_capacity_Ah"],
        nominal_voltage_V=design["bus_nominal_V"],
        eta_charge=design["eta_charge"],
        eta_discharge=design["eta_discharge"],
        min_segment_minutes=args.min_segment_minutes,
        bin_width=args.bin_width,
        min_samples=args.min_samples,
        folds=args.folds,
        charge_power_source=args.charge_power_source,
        discharge_power_source=args.discharge_power_source)
    model["satellite_id"] = args.satellite_id
    model["source"] = {
        "result_csv": str(args.result_csv),
        "result_sha256": _sha256(args.result_csv),
        "base_v_soc_curve": str(args.v_soc_curve),
        "base_v_soc_sha256": _sha256(args.v_soc_curve),
    }
    model["model_code"] = {
        "file": Path(__file__).name,
        "sha256": _sha256(Path(__file__).resolve()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_name(args.out.name + ".tmp")
    temp.write_text(
        json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    temp.replace(args.out)
    args.out_report.parent.mkdir(parents=True, exist_ok=True)
    report_temp = args.out_report.with_name(args.out_report.name + ".tmp")
    report_temp.write_text(
        render_directional_report(model), encoding="utf-8")
    report_temp.replace(args.out_report)
    print(
        f"方向V-SOC: gap={model['direction_gap_voltage']['median_V']:.4f} V; "
        f"启用={model['recommended_for_production']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
