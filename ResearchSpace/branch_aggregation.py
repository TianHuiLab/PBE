#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aggregate redundant power branches into one logical physical unit."""

from __future__ import annotations

import copy
import math
from typing import Any

import numpy as np
import pandas as pd

from power_outlier_filter import filter_power_values


class BranchAggregationError(ValueError):
    """Raised when a branch aggregation contract is incomplete or ambiguous."""


def _models(raw: dict | None) -> dict[str, dict]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BranchAggregationError("branch_models 必须为对象")
    models = raw.get("models", raw)
    if not isinstance(models, dict):
        raise BranchAggregationError("branch_models.models 必须为对象")
    return models


def _members(pair: dict) -> list[str]:
    raw = pair.get("members")
    if raw is None:
        raw = [pair.get("a_unit"), pair.get("b_unit")]
    if (
        not isinstance(raw, list)
        or len(raw) < 2
        or len(set(raw)) != len(raw)
        or not all(isinstance(item, str) and item for item in raw)
    ):
        raise BranchAggregationError("A/B 聚合组 members 至少包含两个不同通道")
    return list(raw)


def _aggregation_mode(pair: dict, default: str) -> str:
    mode = str(pair.get("aggregation_mode", default))
    if mode not in {"sum", "max", "select"}:
        raise BranchAggregationError(
            f"{pair.get('physical_unit')} 的 aggregation_mode 不支持: {mode}")
    return mode


def aggregate_branch_power_frame(
    frame: pd.DataFrame,
    topology: dict,
    branch_models: dict | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Add one power column per physical unit while retaining branch columns.

    ``sum`` is strict: every member branch must have a valid value. Missing
    telemetry is therefore never converted to 0 W. ``max`` requires at least
    one valid member, and ``select`` requires its configured selected member.
    """
    if not isinstance(frame, pd.DataFrame):
        raise BranchAggregationError("frame 必须为 DataFrame")
    if "t" not in frame:
        raise BranchAggregationError("frame 缺少 t 列")
    pairs = topology.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise BranchAggregationError("topology 缺少非空 pairs 列表")
    default_mode = str(topology.get("default_aggregation_mode", "sum"))
    model_by_id = _models(branch_models)
    result = frame.copy()
    seen_physical: set[str] = set()
    audit_pairs = []
    for pair in pairs:
        if not isinstance(pair, dict):
            raise BranchAggregationError("topology pair 必须为对象")
        physical_unit = pair.get("physical_unit")
        if not isinstance(physical_unit, str) or not physical_unit:
            raise BranchAggregationError("topology pair 缺少 physical_unit")
        if physical_unit in seen_physical:
            raise BranchAggregationError(f"重复 physical_unit: {physical_unit}")
        seen_physical.add(physical_unit)
        members = _members(pair)
        mode = _aggregation_mode(pair, default_mode)
        selected = pair.get("select_unit")
        if mode == "select" and selected not in members:
            raise BranchAggregationError(
                f"{physical_unit} 的 select_unit 必须属于 members")

        columns = []
        valid_columns = []
        for member in members:
            source_column = f"P_{member}_W"
            if source_column not in result:
                raise BranchAggregationError(
                    f"{physical_unit} 缺少支路列 {source_column}")
            upper_limit = (
                (model_by_id.get(member, {}).get("outlier_filter") or {})
                .get("upper_limit_W")
            )
            cleaned, _ = filter_power_values(
                result[source_column], upper_limit_W=upper_limit)
            columns.append(cleaned)
            valid_columns.append(cleaned.notna())

        values = pd.concat(columns, axis=1)
        valid = pd.concat(valid_columns, axis=1)
        if mode == "sum":
            aggregate = values.sum(axis=1, min_count=len(members))
        elif mode == "max":
            aggregate = values.max(axis=1, skipna=True)
        else:
            aggregate = values.iloc[:, members.index(selected)]
        result[f"P_{physical_unit}_W"] = aggregate
        result[f"Q_{physical_unit}_valid_branches"] = valid.sum(axis=1)
        result[f"Q_{physical_unit}_complete_branches"] = (
            valid.all(axis=1) if mode == "sum"
            else valid.any(axis=1)
        )
        audit_pairs.append({
            "pl_channel": pair.get("pl_channel"),
            "physical_unit": physical_unit,
            "members": members,
            "aggregation_mode": mode,
            "select_unit": selected,
            "valid_samples": int(aggregate.notna().sum()),
            "complete_samples": int(
                result[f"Q_{physical_unit}_complete_branches"].sum()),
        })
    audit = {
        "schema_version": "1.0",
        "status": "aggregated",
        "source_document": topology.get("source_document"),
        "default_aggregation_mode": default_mode,
        "pairs": audit_pairs,
        "policy": (
            "physical unit count is deduplicated; member branch power is "
            "summed for parallel feeds; missing branch telemetry remains missing"
        ),
    }
    return result, audit


def physical_component_config(
    branch_config: dict,
    topology: dict,
) -> dict:
    """Create a deduplicated component config for physical-unit calibration."""
    components = branch_config.get("components")
    if not isinstance(components, list):
        raise BranchAggregationError("branch_config 缺少 components 列表")
    by_id = {
        item.get("id"): item for item in components
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if len(by_id) != len(components):
        raise BranchAggregationError("branch_config 含重复或无效组件 id")
    pairs = topology.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise BranchAggregationError("topology 缺少非空 pairs 列表")
    default_mode = str(topology.get("default_aggregation_mode", "sum"))
    member_ids: set[str] = set()
    physical_components = []
    for pair in pairs:
        physical_unit = pair.get("physical_unit")
        members = _members(pair)
        mode = _aggregation_mode(pair, default_mode)
        missing = sorted(set(members) - set(by_id))
        if missing:
            raise BranchAggregationError(
                f"{physical_unit} 引用了不存在的支路: {missing}")
        if set(members) & member_ids:
            raise BranchAggregationError(
                f"{physical_unit} 与其他物理组重复引用支路")
        member_ids.update(members)
        branch_specs = [by_id[item] for item in members]
        max_values = [
            float(item["max_power_W"])
            for item in branch_specs
            if isinstance(item.get("max_power_W"), (int, float))
            and math.isfinite(float(item["max_power_W"]))
            and float(item["max_power_W"]) > 0
        ]
        source_codes = []
        for item in branch_specs:
            for key, value in item.items():
                if key.endswith("_code") and isinstance(value, str):
                    source_codes.append(value)
        base = branch_specs[0]
        name = pair.get("name") or str(base.get("name", physical_unit)).split(
            " · ")[0]
        component = {
            "id": physical_unit,
            "name": name,
            "role": "load",
            "plugin": "aggregated_branch_power",
            "enabled": True,
            "physical_branches": members,
            "aggregation_mode": mode,
            "source_codes": list(dict.fromkeys(source_codes)),
            "source_document": topology.get("source_document"),
        }
        if mode == "select":
            component["select_unit"] = pair["select_unit"]
        if max_values:
            component["max_power_W"] = (
                sum(max_values) if mode == "sum"
                else max(max_values)
            )
        physical_components.append(component)
    unpaired = [
        copy.deepcopy(item) for item in components
        if item.get("enabled") and item.get("role") == "load"
        and item.get("id") not in member_ids
    ]
    result = copy.deepcopy(branch_config)
    result["components"] = physical_components + unpaired
    result["_note"] = (
        "物理单机层：同编号A/B支路按拓扑约定合并，设备只计一次；"
        "支路明细仍保留在原始通道数据中供审计。"
    )
    result["physical_unit_topology"] = {
        "source_document": topology.get("source_document"),
        "default_aggregation_mode": default_mode,
        "n_physical_units": len(physical_components),
    }
    return result
