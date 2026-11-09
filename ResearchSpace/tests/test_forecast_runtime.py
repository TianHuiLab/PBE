"""Tests for the ClickHouse-backed current telemetry snapshot adapter."""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from future_forecast_runtime import ClickHouseSnapshotProvider, minute_rows_to_wide
from solar_power_provider import EclipseOnlySolarProvider, ModuleSolarPowerProvider
from unit_power_model import CurrentPowerSnapshot, UnitPowerModelError


PROFILE = Path(__file__).resolve().parents[1] / "satellites" / "SCS-01-01" / "config"


class ForecastRuntimeTests(unittest.TestCase):
    def test_minute_rows_keep_text_phase_state(self):
        rows = [
            {"t": "2025-12-29 05:21:00", "param_code": "TMD016",
             "avg": 28.0, "state_text": ""},
            {"t": "2025-12-29 05:21:00", "param_code": "TMK5315",
             "avg": None, "state_text": "阴影区"},
        ]
        wide = minute_rows_to_wide(rows)
        self.assertEqual(wide.loc[pd.Timestamp("2025-12-29 05:21:00"),
                                  "TMK5315__state"], "阴影区")
        self.assertEqual(
            wide.loc[pd.Timestamp("2025-12-29 05:21:00"), "TMD016"], 28.0)

    def test_clickhouse_provider_derives_only_requested_unit_power(self):
        def fake_query(sql, params=None, **kwargs):
            self.assertIn("master_satellite_01", sql)
            self.assertEqual(params["start"], "2025-12-29 05:21:00")
            self.assertEqual(params["end"], "2025-12-29 05:22:00")
            return [
                {"t": "2025-12-29 05:21:00", "param_code": "TMD016",
                 "avg": 28.0, "mn": 28.0, "mx": 28.0, "state_text": ""},
                {"t": "2025-12-29 05:21:00", "param_code": "TMD018",
                 "avg": 10.0, "mn": 10.0, "mx": 10.0, "state_text": ""},
                {"t": "2025-12-29 05:21:00", "param_code": "TMD019",
                 "avg": 28.0, "mn": 28.0, "mx": 28.0, "state_text": ""},
                {"t": "2025-12-29 05:21:00", "param_code": "TMD025",
                 "avg": 2.0, "mn": 2.0, "mx": 2.0, "state_text": ""},
                {"t": "2025-12-29 05:21:00", "param_code": "TMK5315",
                 "avg": 0.0, "mn": 0.0, "mx": 0.0, "state_text": "阴影区"},
            ]

        provider = ClickHouseSnapshotProvider(
            PROFILE, query_rows=fake_query, transport="http")
        result = provider.snapshot(
            pd.Timestamp("2025-12-29T05:21:00+08:00"), ("compute_nx1_f",))

        self.assertAlmostEqual(result.total_load_W, 280.0)
        self.assertAlmostEqual(result.unit_power_W["compute_nx1_f"], 56.0)
        self.assertEqual(result.phase, "阴影区")
        self.assertEqual(result.provenance["row_count"], 5)

    def test_provider_rejects_absent_exact_minute(self):
        provider = ClickHouseSnapshotProvider(
            PROFILE, query_rows=lambda *args, **kwargs: [],
            transport="http")
        with self.assertRaisesRegex(UnitPowerModelError, "精确分钟"):
            provider.snapshot(
                pd.Timestamp("2025-12-29T05:21:00+08:00"),
                ("compute_nx1_f",))

    def test_eclipse_only_provider_requires_shadow_phase(self):
        provider = EclipseOnlySolarProvider()
        shadow = CurrentPowerSnapshot(
            time=pd.Timestamp("2025-12-29T05:21:00+08:00"),
            battery_voltage_V=28.0, bus_voltage_V=28.0,
            total_load_W=220.0, phase="阴影区",
            unit_power_W={}, missing_unit_power_W={}, provenance={})
        result = provider.profile(
            start=shadow.time,
            end=shadow.time + pd.Timedelta(minutes=24),
            step_seconds=60,
            snapshot=shadow,
            phase_segments=[{
                "start": shadow.time, "end": shadow.time + pd.Timedelta(minutes=24),
                "phase": "eclipse"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["power_W"], 0.0)

        sun = CurrentPowerSnapshot(
            time=shadow.time, battery_voltage_V=28.0, bus_voltage_V=28.0,
            total_load_W=220.0, phase="阳照区",
            unit_power_W={}, missing_unit_power_W={}, provenance={})
        with self.assertRaisesRegex(UnitPowerModelError, "阴影區|阴影区"):
            provider.profile(
                start=sun.time, end=sun.time + pd.Timedelta(minutes=24),
                step_seconds=60, snapshot=sun,
                phase_segments=[{
                    "start": sun.time, "end": sun.time + pd.Timedelta(minutes=24),
                    "phase": "sun"}])

    def test_external_solar_module_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            module_path = Path(directory) / "orbit_power.py"
            module_path.write_text(
                "def build_profile(start, end, step_seconds):\n"
                "    return [{'start': start, 'end': end, 'power_W': 125.0}]\n",
                encoding="utf-8")
            provider = ModuleSolarPowerProvider(module_path)
            snapshot = CurrentPowerSnapshot(
                time=pd.Timestamp("2026-01-02T10:00:00+08:00"),
                battery_voltage_V=28.0, bus_voltage_V=28.0,
                total_load_W=100.0, phase="阳照区",
                unit_power_W={}, missing_unit_power_W={}, provenance={})
            result = provider.profile(
                start=snapshot.time,
                end=snapshot.time + pd.Timedelta(minutes=5),
                step_seconds=60,
                snapshot=snapshot)
        self.assertEqual(result[0]["power_W"], 125.0)


if __name__ == "__main__":
    unittest.main()
