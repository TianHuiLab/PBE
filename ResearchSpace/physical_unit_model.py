#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build calibrated physical-unit models from configured power branches."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from branch_aggregation import (
    aggregate_branch_power_frame,
    physical_component_config,
)
from unit_power_model import (
    _atomic_write_frame,
    _atomic_write_text,
    calibrate_unit_models,
    load_excluded_dates,
    model_summary_frame,
    render_markdown_report,
)


def _read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_physical_unit_artifacts(
    branch_data: str | Path,
    branch_config: str | Path,
    branch_models: str | Path,
    topology: str | Path,
    out_dir: str | Path,
    *,
    satellite_id: str,
    exclude_dates_file: str | Path | None = None,
    model_name: str = "physical_unit_models.json",
    report_name: str = "physical_unit_model_report.md",
) -> dict:
    """Aggregate branch telemetry, calibrate, and persist physical-unit models."""
    branch_data = Path(branch_data)
    branch_config = Path(branch_config)
    branch_models_path = Path(branch_models)
    topology_path = Path(topology)
    out_dir = Path(out_dir)
    import pandas as pd

    frame = pd.read_csv(branch_data)
    branch_config_doc = _read_json(branch_config)
    branch_model_doc = _read_json(branch_models_path)
    topology_doc = _read_json(topology_path)
    if branch_model_doc.get("satellite_id") != satellite_id:
        raise ValueError("branch model satellite_id 不匹配")
    if topology_doc.get("satellite_id") != satellite_id:
        raise ValueError("A/B topology satellite_id 不匹配")
    if topology_doc.get("status") != "approved":
        raise ValueError("A/B topology 尚未批准，不能生成物理单机模型")

    aggregated, aggregation_audit = aggregate_branch_power_frame(
        frame, topology_doc, branch_model_doc)
    physical_config = physical_component_config(
        branch_config_doc, topology_doc)
    physical_config["satellite_id"] = satellite_id
    out_dir.mkdir(parents=True, exist_ok=True)
    physical_data_path = out_dir / "physical_unit_power_minute.csv"
    physical_config_path = out_dir / "physical_unit_components.json"
    _atomic_write_frame(aggregated, physical_data_path)
    _atomic_write_text(
        physical_config_path,
        json.dumps(physical_config, ensure_ascii=False, indent=2) + "\n",
    )

    exclude_dates = (
        load_excluded_dates(exclude_dates_file)
        if exclude_dates_file else []
    )
    document = calibrate_unit_models(
        physical_data_path,
        physical_config_path,
        satellite_id=satellite_id,
        exclude_dates=exclude_dates,
    )
    config_by_id = {
        item["id"]: item for item in physical_config["components"]}
    for unit_id, model in document["models"].items():
        component = config_by_id[unit_id]
        if "physical_branches" in component:
            model["physical_branches"] = component["physical_branches"]
            model["aggregation_mode"] = component["aggregation_mode"]
            model.setdefault("source", {})["source_codes"] = (
                component.get("source_codes", []))
            model["source"]["physical_branches"] = (
                component["physical_branches"])
    document["physical_aggregation"] = {
        **aggregation_audit,
        "topology_sha256": _sha256(topology_path),
        "branch_model_sha256": _sha256(branch_models_path),
        "branch_data_sha256": _sha256(branch_data),
        "excluded_dates": exclude_dates,
    }
    model_path = out_dir / model_name
    report_path = out_dir / report_name
    summary_path = out_dir / (
        Path(model_name).stem + "_summary.csv")
    _atomic_write_text(
        model_path,
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
    )
    report_text = render_markdown_report(document).replace(
        "SCS-01-01", satellite_id)
    _atomic_write_text(report_path, report_text)
    _atomic_write_frame(model_summary_frame(document), summary_path)
    return {
        "model": model_path,
        "report": report_path,
        "summary": summary_path,
        "physical_data": physical_data_path,
        "physical_config": physical_config_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch-data", type=Path, required=True)
    parser.add_argument("--branch-config", type=Path, required=True)
    parser.add_argument("--branch-models", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--satellite-id", required=True)
    parser.add_argument("--exclude-dates-file", type=Path)
    parser.add_argument("--model-name", default="physical_unit_models.json")
    parser.add_argument(
        "--report-name", default="physical_unit_model_report.md")
    args = parser.parse_args()
    outputs = build_physical_unit_artifacts(
        args.branch_data,
        args.branch_config,
        args.branch_models,
        args.topology,
        args.out_dir,
        satellite_id=args.satellite_id,
        exclude_dates_file=args.exclude_dates_file,
        model_name=args.model_name,
        report_name=args.report_name,
    )
    print(json.dumps(
        {key: str(value) for key, value in outputs.items()},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
