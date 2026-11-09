#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the YJ-3D historical-file forecast adapter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from solar_power_provider import PolynomialSolarPowerProvider
from unit_power_model import forecast_from_plan
from yj3d_power_forecast import (YJ3DClickHouseSnapshotProvider,
                                 YJ3DHistoricalSnapshotProvider)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--unit-model", type=Path, required=True)
    parser.add_argument("--solar-model", type=Path, required=True)
    parser.add_argument("--v-soc-curve", type=Path, required=True)
    parser.add_argument("--design-params", type=Path, required=True)
    parser.add_argument("--source-mode", choices=("historical", "clickhouse"),
                        default="historical")
    parser.add_argument("--power-minute", type=Path)
    parser.add_argument("--unit-minute", type=Path)
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    args = parser.parse_args()

    request = json.loads(args.request.read_text(encoding="utf-8"))
    unit_model = json.loads(args.unit_model.read_text(encoding="utf-8"))
    solar_model = json.loads(args.solar_model.read_text(encoding="utf-8"))
    curve = json.loads(args.v_soc_curve.read_text(encoding="utf-8"))
    design = json.loads(args.design_params.read_text(encoding="utf-8"))
    if args.source_mode == "historical":
        if not args.power_minute or not args.unit_minute:
            parser.error("historical 模式需要 --power-minute 和 --unit-minute")
        provider = YJ3DHistoricalSnapshotProvider(
            pd.read_csv(args.power_minute),
            pd.read_csv(args.unit_minute),
            unit_models=unit_model,
        )
    else:
        if not args.profile_dir:
            parser.error("clickhouse 模式需要 --profile-dir")
        provider = YJ3DClickHouseSnapshotProvider(args.profile_dir)
    timeline, summary = forecast_from_plan(
        request, unit_model, provider,
        v_soc_curve=curve, design_params=design,
        solar_provider=PolynomialSolarPowerProvider(
            solar_model, model_path=args.solar_model))
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out_json.with_name(args.out_json.name + ".tmp")
    temp.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    temp.replace(args.out_json)
    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    csv_temp = args.out_csv.with_name(args.out_csv.name + ".tmp")
    timeline.to_csv(csv_temp, index=False, encoding="utf-8-sig")
    csv_temp.replace(args.out_csv)
    print(
        f"云尖3D目标SOC={summary['target_end_state']['soc']:.4f}; "
        f"剩余={summary['target_end_state']['remaining_Ah']:.3f} Ah")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
