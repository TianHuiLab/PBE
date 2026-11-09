#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fit the SCS-01-01 provisional solar-array power proxy from history.

User-confirmed 01-star formula for this calibration stage:
``P_solar_array_proxy = P_load + P_charge_est``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _phase(value) -> str:
    text = str(value).strip()
    if text in {"日照", "阳照", "阳照区", "sun"}:
        return "sun"
    if text in {"地影", "阴影", "阴影区", "eclipse"}:
        return "eclipse"
    return "unknown"


def _episodes(times: pd.Series, sun: pd.Series,
              max_gap_minutes: float) -> pd.Series:
    gaps = times.diff().dt.total_seconds() / 60
    starts = sun & ((~sun.shift(fill_value=False))
                    | gaps.gt(max_gap_minutes).fillna(True))
    return starts.cumsum().where(sun)


def calibrate_solar_power_model(
        result_csv: str | Path,
        *,
        satellite_id: str = "SCS-01-01",
        degree_candidates: tuple[int, ...] = (1, 2, 3),
        max_gap_minutes: float = 5.0,
        min_samples: int = 20,
        winsor_quantile: float = 0.999,
        target_definition: dict | None = None,
        target_column: str | None = None,
        exclude_dates: list[str] | tuple[str, ...] | None = None,
) -> dict:
    path = Path(result_csv)
    _require(path.is_file(), f"历史结果 CSV 不存在: {path}")
    _require(degree_candidates and all(
        isinstance(degree, int) and 1 <= degree <= 3
        for degree in degree_candidates),
        "degree_candidates 仅支持 1..3 的正整数")
    _require(max_gap_minutes > 0, "max_gap_minutes 必须为正数")
    _require(0.9 <= winsor_quantile <= 1.0, "winsor_quantile 必须在 [0.9,1]")

    frame = pd.read_csv(path)
    required = {"t", "phase", "P_load", "P_charge_est"}
    if target_column:
        _require(isinstance(target_column, str) and target_column.strip(),
                 "target_column 必须是非空列名")
        required.add(target_column)
    missing = required - set(frame.columns)
    _require(not missing, f"历史结果缺少列: {sorted(missing)}")
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    _require(not frame["t"].isna().any(), "历史结果含无效时间")
    _require(not frame["t"].duplicated().any(), "历史结果含重复时间")
    frame = frame.sort_values("t").reset_index(drop=True)
    source_rows = int(len(frame))
    source_time_start = frame["t"].iloc[0].isoformat()
    source_time_end = frame["t"].iloc[-1].isoformat()
    excluded_dates = sorted({
        str(value).strip()
        for value in (exclude_dates or [])
        if str(value).strip()
    })
    if excluded_dates:
        date_text = frame["t"].dt.strftime("%Y-%m-%d")
        excluded_mask = date_text.isin(excluded_dates)
        frame = frame.loc[~excluded_mask].reset_index(drop=True)
    else:
        excluded_mask = pd.Series(False, index=range(source_rows))
    _require(not frame.empty, "排除验证日期后历史结果为空")
    phase = frame["phase"].map(_phase)
    p_load = pd.to_numeric(frame["P_load"], errors="coerce")
    p_charge = pd.to_numeric(frame["P_charge_est"], errors="coerce")
    target = (pd.to_numeric(frame[target_column], errors="coerce")
              if target_column else p_load + p_charge)
    valid = (np.isfinite(target) & target.ge(0)
             & np.isfinite(p_load) & p_load.ge(0)
             & np.isfinite(p_charge) & p_charge.ge(0))
    sun = phase.eq("sun") & valid
    shadow = phase.eq("eclipse")
    _require(int(sun.sum()) >= min_samples,
             f"有效阳照样本 {int(sun.sum())} < {min_samples}")
    episode = _episodes(frame["t"], sun, max_gap_minutes)
    usable = sun & episode.notna()
    episode = episode[usable].astype(int)
    times = frame.loc[usable, "t"].reset_index(drop=True)
    target = target.loc[usable].reset_index(drop=True)
    episode = episode.reset_index(drop=True)
    _require(episode.nunique() >= 2, "至少需要两个阳照运行片段进行分组验证")

    elapsed = np.empty(len(times), dtype=float)
    for _, positions_index in episode.groupby(episode).groups.items():
        positions = np.asarray(positions_index, dtype=int)
        segment_start = times.iloc[positions].min()
        elapsed[positions] = (
            (times.iloc[positions] - segment_start).dt.total_seconds()
            .to_numpy(dtype=float) / 3600)
    x = elapsed
    y_raw = target.to_numpy(dtype=float)
    cap = float(np.quantile(y_raw, winsor_quantile))
    y = np.clip(y_raw, 0, cap)
    episode_values = episode.to_numpy(dtype=int)

    fold_count = min(5, int(episode.nunique()))
    folds = (episode_values % fold_count).astype(int)
    constant_squared = []
    candidate_errors = {degree: [] for degree in degree_candidates}
    for fold in range(fold_count):
        test = folds == fold
        train = ~test
        if test.sum() < 2 or train.sum() < 3:
            continue
        baseline = float(np.median(y[train]))
        constant_squared.extend(((baseline - y[test]) ** 2).tolist())
        for degree in degree_candidates:
            fit = np.polynomial.Polynomial.fit(
                x[train], y[train], degree).convert()
            candidate_errors[degree].extend(
                ((fit(x[test]) - y[test]) ** 2).tolist())
    _require(bool(constant_squared), "分组交叉验证没有可用折")
    constant_rmse = float(np.sqrt(np.mean(constant_squared)))
    scores = {
        degree: float(np.sqrt(np.mean(errors)))
        for degree, errors in candidate_errors.items() if errors
    }
    _require(bool(scores), "多项式分组交叉验证没有可用结果")
    selected_degree = min(scores, key=lambda degree: (
        scores[degree], degree))
    selected_rmse = scores[selected_degree]
    model_type = ("polynomial"
                  if selected_rmse < constant_rmse - 1e-12
                  else "constant")
    final_fit = np.polynomial.Polynomial.fit(x, y, selected_degree).convert()
    support_hours = [float(x.min()), float(x.max())]
    q = np.quantile(y_raw, [0, .05, .5, .95, .99, 1])
    model = {
        "schema_version": "1.0",
        "satellite_id": satellite_id,
        "model_type": model_type,
        "selected_degree": int(selected_degree),
        "coefficients_W_per_hour_power": [
            round(float(value), 9) for value in final_fit.coef],
        "constant_power_W": round(float(np.median(y)), 6),
        "target_definition": target_definition or {
            "formula": "P_load + P_charge_est",
            "p_load_basis": "TMD016 × TMD018 minute means",
            "p_charge_basis": (
                "P_charge_est = max(TMD016×TMD017 - TMD016×TMD018, 0); "
                "inferred, not independent battery-charge measurement"),
            "status": "user-confirmed provisional 01-star formula",
            "excluded_alternative": "TMD016×TMD017 + battery charge",
        },
        "time_feature": {
            "name": "elapsed_hours_since_sunlight_segment_start",
            "reset_at": "every continuous sunlight segment",
        },
        "support_hours": [round(value, 6) for value in support_hours],
        "extrapolation_policy": "clamp_to_support",
        "shadow_power_W": 0.0,
        "training": {
            "n_valid_sun_samples": int(len(y)),
            "sun_segments": int(episode.nunique()),
            "shadow_samples_in_source": int(shadow.sum()),
            "time_start": times.min().isoformat(),
            "time_end": times.max().isoformat(),
            "winsor_quantile": float(winsor_quantile),
            "winsor_cap_W": round(cap, 6),
        },
        "training_split": {
            "method": "exclude_whole_dates",
            "excluded_dates": excluded_dates,
            "source_rows": source_rows,
            "retained_rows": int(len(frame)),
            "excluded_rows": int(excluded_mask.sum()),
            "source_time_start": source_time_start,
            "source_time_end": source_time_end,
        },
        "target_stats_W": {
            "min": round(float(q[0]), 6),
            "p05": round(float(q[1]), 6),
            "p50": round(float(q[2]), 6),
            "p95": round(float(q[3]), 6),
            "p99": round(float(q[4]), 6),
            "max": round(float(q[5]), 6),
        },
        "cross_validation": {
            "method": "sunlight-episode-grouped-deterministic-folds",
            "folds": fold_count,
            "constant_rmse_W": round(constant_rmse, 6),
            "candidate_rmse_W": {
                str(degree): round(rmse, 6)
                for degree, rmse in sorted(scores.items())},
            "selected_rmse_W": round(selected_rmse, 6),
        },
        "warnings": [
            "太阳阵功率为目标代理量，不是独立阵列功率测量",
            ("太阳阵目标来自显式目标列；母线电压代理和测点边界仍需核验"
             if target_column else
             "P_charge_est由母线测点差额推断，充电支路边界仍未验证"),
            "温度、太阳入射角、S3R工作点和阵列老化未显式建模",
        ],
        "source": {
            "file": str(path),
            "sha256": _sha256(path),
        },
        "model_code": {
            "file": Path(__file__).name,
            "sha256": _sha256(Path(__file__).resolve()),
        },
    }
    return model


def render_solar_model_report(model: dict) -> str:
    training = model["training"]
    cv = model["cross_validation"]
    stats = model["target_stats_W"]
    coefficients = model["coefficients_W_per_hour_power"]
    lines = [
        "# 三体01星太阳阵发电功率历史拟合",
        "",
        "## 1. 目标口径",
        "",
        "采用用户确认的方案B：",
        "",
        "```text",
        "P_solar_array_proxy = P_load + P_charge_est",
        "P_load = TMD016 × TMD018",
        "P_charge_est = max(TMD016×TMD017 - TMD016×TMD018, 0)",
        "```",
        "",
        "这是01星暂定目标代理量。`P_charge_est`由母线测点差额推断，不是独立电池充电测量；"
        "太阳阵电压测点损坏也不参与本模型。",
        "",
        "## 2. 模型",
        "",
        f"- 类型：`{model['model_type']}`",
        f"- 阶数：`{model['selected_degree']}`",
        f"- 系数（按阳照片段开始后小时数的幂次升序）：`{coefficients}`",
        f"- 时间支撑：`{model['support_hours']} h`；超出后按训练边界截断。",
        f"- 阴影片段：固定`{model['shadow_power_W']} W`。",
        "",
        "## 3. 训练与验证",
        "",
        f"- 有效阳照分钟：`{training['n_valid_sun_samples']:,}`",
        f"- 阳照连续片段：`{training['sun_segments']:,}`",
        f"- 历史阴影分钟：`{training['shadow_samples_in_source']:,}`",
        f"- 时间范围：`{training['time_start']}` 至 `{training['time_end']}`",
        f"- 整日排除：`{model.get('training_split', {}).get('excluded_dates', [])}`",
        f"- 排除行数：`{model.get('training_split', {}).get('excluded_rows', 0):,}`",
        f"- 目标功率 p05 / p50 / p95 / p99："
        f"`{stats['p05']}` / `{stats['p50']}` / `{stats['p95']}` / "
        f"`{stats['p99']}` W",
        f"- 分组验证恒定模型RMSE：`{cv['constant_rmse_W']}` W",
        f"- 各阶候选RMSE：`{json.dumps(cv['candidate_rmse_W'], ensure_ascii=False)}`",
        f"- 选中模型RMSE：`{cv['selected_rmse_W']}` W",
        "",
        "验证按完整阳照片段分组，避免同一片段相邻分钟同时进入训练和留出集。",
        "",
        "## 4. 预测输入",
        "",
        "`prediction.phase_segments`必须连续、有序、无重叠，并完整覆盖`[time,end)`。"
        "`phase`可取`sun/eclipse`或“阳照/阴影”等别名。阳照片段按片段内经历小时评价多项式，"
        "阴照片段功率为0 W。",
        "",
        "## 5. 证据边界",
        "",
        "- 目标功率是用户指定方案B的条件代理量，不是独立太阳阵功率测量。",
        "- `P_charge_est`仍受 `TMD017` 测点边界影响，不能支持物理闭合结论。",
        "- 模型未显式包含温度、太阳入射角、S3R工作点、阵列老化或姿态变化。",
        "- 多项式超出历史时长范围会截断，不向未知长光照窗口自由外推。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-csv", type=Path, required=True)
    parser.add_argument("--satellite-id", default="SCS-01-01")
    parser.add_argument("--max-gap-minutes", type=float, default=5.0)
    parser.add_argument("--degree", type=int, action="append",
                        dest="degrees", choices=(1, 2, 3))
    parser.add_argument("--exclude-dates-file", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-report", type=Path)
    args = parser.parse_args()
    exclude_dates = []
    if args.exclude_dates_file:
        from unit_power_model import load_excluded_dates
        exclude_dates = load_excluded_dates(args.exclude_dates_file)
    model = calibrate_solar_power_model(
        args.result_csv, satellite_id=args.satellite_id,
        max_gap_minutes=args.max_gap_minutes,
        degree_candidates=tuple(args.degrees or (1, 2, 3)),
        exclude_dates=exclude_dates)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_name(args.out.name + ".tmp")
    temp.write_text(
        json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    temp.replace(args.out)
    if args.out_report:
        args.out_report.parent.mkdir(parents=True, exist_ok=True)
        report_temp = args.out_report.with_name(args.out_report.name + ".tmp")
        report_temp.write_text(
            render_solar_model_report(model), encoding="utf-8")
        report_temp.replace(args.out_report)
    print(f"太阳阵模型: {model['model_type']} degree="
          f"{model['selected_degree']} segments={model['training']['sun_segments']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
