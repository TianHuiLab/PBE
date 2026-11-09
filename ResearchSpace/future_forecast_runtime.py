#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime dependencies for the simplified SCS-01-01 forecast entry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import ch_http as C
from components import CONFIG as COMPONENT_CONFIG
from components import load_config as load_components
from s1_fetch import (SQL_WIDE, _settings, native_query_iter, source_table,
                      wide_pairs)
from unit_power_model import (CurrentPowerSnapshot, UnitPowerModelError,
                              _parse_time, _require, derive_current_snapshot,
                              sha256_file)


def minute_rows_to_wide(rows: list[dict]) -> pd.DataFrame:
    """Convert grouped minute rows into the wide/minute-state representation."""
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    required = {"t", "param_code", "avg"}
    missing = required - set(frame.columns)
    if missing:
        raise UnitPowerModelError(f"遥测分钟行缺少列: {sorted(missing)}")
    frame["t"] = pd.to_datetime(frame["t"], errors="coerce")
    if frame["t"].isna().any():
        raise UnitPowerModelError("遥测分钟行含无效时间")
    if frame.duplicated(["t", "param_code"]).any():
        raise UnitPowerModelError("遥测分钟行含重复的 t/param_code")
    wide = (frame.pivot(index="t", columns="param_code", values="avg")
            .sort_index())
    if "state_text" in frame:
        states = frame.loc[frame["state_text"].fillna("").ne("")]
        if not states.empty:
            state_wide = states.pivot(
                index="t", columns="param_code", values="state_text")
            state_wide = state_wide.add_suffix("__state")
            wide = wide.join(state_wide)
    return wide


class ClickHouseSnapshotProvider:
    """Fetch one exact minute from the configured satellite wide table."""

    def __init__(self, profile_dir: str | Path, *,
                 query_rows: Callable | None = None,
                 transport: str = "native",
                 source_timezone: str = "Asia/Shanghai"):
        self.profile_dir = Path(profile_dir)
        self.transport = transport
        self.source_timezone = source_timezone
        self.query_rows = query_rows
        if transport not in ("native", "http"):
            raise UnitPowerModelError("transport 必须是 native 或 http")
        if source_timezone != "Asia/Shanghai":
            raise UnitPowerModelError("onboard_time_bj 固定使用 Asia/Shanghai")

    def snapshot(self, timestamp: Any,
                 unit_ids: tuple[str, ...] | list[str]) -> CurrentPowerSnapshot:
        requested = _parse_time(timestamp, self.source_timezone)
        _require(requested.second == 0 and requested.microsecond == 0,
                 "time 必须是整分钟")
        start = requested.floor("min")
        end = start + pd.Timedelta(minutes=1)
        satellite_id, codes, _ = _settings(self.profile_dir)
        table = source_table(self.profile_dir)
        _require(table.startswith("master_satellite_"),
                 "精确分钟自动快照当前仅支持 master_satellite_01/02")
        sql = (SQL_WIDE.replace("{source_table}", table)
               .replace("{pairs}", wide_pairs(codes)))
        params = {
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "tz": self.source_timezone,
            "spacecraft_id": satellite_id,
        }
        query = self.query_rows or (
            C.query_iter if self.transport == "http" else native_query_iter)
        rows = list(query(
            sql, params=params, timeout=30, max_execution_time=30))
        _require(bool(rows),
                 f"time 精确分钟没有遥测记录: {start.isoformat()}")
        wide = minute_rows_to_wide(rows)
        cfg = json.loads(
            (self.profile_dir / "telemetry_params.json").read_text(
                encoding="utf-8"))
        specs = load_components(
            self.profile_dir / COMPONENT_CONFIG.name, satellite_id)
        phase = cfg.get("phase_state") or {}
        provenance = {
            "provider": "ClickHouseSnapshotProvider",
            "source": f"{C.DB}.{table}",
            "query_start_inclusive": start.isoformat(),
            "query_end_exclusive": end.isoformat(),
            "row_count": len(rows),
            "param_codes_requested": len(codes),
            "transport": self.transport,
            "runtime_code_sha256": sha256_file(Path(__file__).resolve()),
            "telemetry_config_sha256": sha256_file(
                self.profile_dir / "telemetry_params.json"),
            "components_config_sha256": sha256_file(
                self.profile_dir / COMPONENT_CONFIG.name),
        }
        return derive_current_snapshot(
            wide, requested, specs, task_unit_ids=unit_ids,
            bus_voltage_code=cfg["columns"]["bus_V"]["code"],
            load_current_code=cfg["columns"]["load_I"]["code"],
            battery_voltage_code=cfg["columns"]["bat_V"]["code"],
            phase_code=phase.get("code"),
            provenance=provenance,
            timezone_name=self.source_timezone)
