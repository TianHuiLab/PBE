#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit season, phase, and temperature coverage of extended telemetry."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


SCHEMA_VERSION = "1.0"


def _season(month):
    if month in (12, 1, 2):
        return "DJF"
    if month in (3, 4, 5):
        return "MAM"
    if month in (6, 7, 8):
        return "JJA"
    return "SON"


def _json_number(value):
    return None if pd.isna(value) else float(value)


def build_coverage(result_csv, minute_csv, *, temperature_codes):
    result = pd.read_csv(result_csv, usecols=["t", "phase"])
    result["t"] = pd.to_datetime(result["t"], errors="coerce")
    if result.empty or result["t"].isna().any():
        raise ValueError("结果分钟为空或含无效时间")
    if result["t"].duplicated().any():
        raise ValueError("结果分钟含重复时间")
    result = result.sort_values("t").reset_index(drop=True)

    minute = pd.read_csv(
        minute_csv,
        usecols=["t", "param_code", "param_name", "avg"],
    )
    minute["t"] = pd.to_datetime(minute["t"], errors="coerce")
    minute["avg"] = pd.to_numeric(minute["avg"], errors="coerce")
    if minute["t"].isna().any():
        raise ValueError("分钟遥测含无效时间")
    if minute.duplicated(["t", "param_code"]).any():
        raise ValueError("分钟遥测含重复 t/param_code")
    selected = minute.loc[
        minute["param_code"].isin(temperature_codes)].copy()
    names = (
        selected.drop_duplicates("param_code")
        .set_index("param_code")["param_name"]
        .to_dict()
    )
    wide = selected.pivot(
        index="t", columns="param_code", values="avg")
    merged = result.merge(
        wide.reset_index(), on="t", how="left", validate="one_to_one")
    merged["month"] = merged["t"].dt.strftime("%Y-%m")
    merged["season"] = merged["t"].dt.month.map(_season)

    phase_counts = {
        str(key): int(value)
        for key, value in merged["phase"].value_counts().to_dict().items()
    }
    season_counts = {
        season: int((merged["season"] == season).sum())
        for season in ("DJF", "MAM", "JJA", "SON")
    }
    monthly = []
    for month, group in merged.groupby("month", sort=True):
        monthly.append({
            "month": month,
            "observed_minutes": int(len(group)),
            "sun_minutes": int((group["phase"] == "日照").sum()),
            "eclipse_minutes": int((group["phase"] == "地影").sum()),
            "unknown_minutes": int((group["phase"] == "未判定").sum()),
        })
    temperatures = {}
    for code in temperature_codes:
        values = (
            pd.to_numeric(merged[code], errors="coerce").dropna()
            if code in merged else pd.Series(dtype=float))
        temperatures[code] = {
            "name": names.get(code, code),
            "valid_minutes": int(len(values)),
            "raw_min": _json_number(values.min()) if len(values) else None,
            "raw_median": _json_number(values.median()) if len(values) else None,
            "raw_max": _json_number(values.max()) if len(values) else None,
        }
    transitions = int(
        merged["phase"].ne(merged["phase"].shift()).iloc[1:].sum())
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(
            ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "time_start": merged["t"].min().isoformat(),
        "time_end_inclusive": merged["t"].max().isoformat(),
        "observed_minutes": int(len(merged)),
        "phase_counts": phase_counts,
        "season_counts": season_counts,
        "phase_transition_count": transitions,
        "monthly": monthly,
        "temperature_codes": temperatures,
        "temperature_semantics": (
            "原始遥测数值；单位/比例变换和物理温度换算需依遥测大表确认，"
            "当前仅用于覆盖和变化审计，不标注为摄氏度"),
        "source": {
            "result_csv": str(result_csv),
            "minute_csv": str(minute_csv),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-csv", type=Path, required=True)
    parser.add_argument("--minute-csv", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    profile = json.loads(
        (args.profile_dir / "telemetry_params.json").read_text(
            encoding="utf-8"))
    codes = [
        code for code in profile.get("fetch_extra_codes", [])
        if code in {"TMD079", "TMD080", "TMD081", "TMD082", "TMD083"}
    ]
    report = build_coverage(
        args.result_csv, args.minute_csv, temperature_codes=codes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_name(args.out.name + ".tmp")
    temp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(args.out)
    print(json.dumps({
        "observed_minutes": report["observed_minutes"],
        "season_counts": report["season_counts"],
        "phase_counts": report["phase_counts"],
        "temperature_codes": codes,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
