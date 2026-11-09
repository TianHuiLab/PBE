"""Behavior tests for the simplified future-task forecast entry point."""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unit_power_model import (CurrentPowerSnapshot, UnitPowerModelError,
                              derive_current_snapshot, forecast_from_plan)
from solar_power_provider import (EclipseOnlySolarProvider,
                                  PolynomialSolarPowerProvider)


class StaticSnapshotProvider:
    def __init__(self, snapshot):
        self.snapshot_value = snapshot
        self.requested_time = None
        self.requested_units = None

    def snapshot(self, timestamp, unit_ids):
        self.requested_time = timestamp
        self.requested_units = tuple(unit_ids)
        return self.snapshot_value


class FixedSolarProvider:
    source = "test_fixed_solar"

    def __init__(self, power_W):
        self.power_W = power_W

    def profile(self, *, start, end, step_seconds, snapshot,
                phase_segments=None):
        return [{"start": start, "end": end, "power_W": self.power_W}]


def model_document():
    return {
        "schema_version": "1.0",
        "satellite_id": "TEST-SAT",
        "timezone": "Asia/Shanghai",
        "models": {
            "computer": {
                "name": "计算机",
                "model_type": "constant",
                "usable_for_prediction": True,
                "active_power_W": 40.0,
                "low_power_W": 2.0,
                "power_bands": [
                    {"id": "low", "label": "低", "power_p50_W": 2.0},
                    {"id": "active", "label": "工作", "power_p50_W": 40.0},
                ],
                "modes": {},
                "polynomial": {"selected": False},
                "stats": {"p05_W": 2.0, "p99_W": 40.0},
                "warnings": [],
            }
        },
    }


def request():
    return {
        "time": "2026-01-01T10:00:00+08:00",
        "prediction": {
            "end": "2026-01-01T10:30:00+08:00",
            "step_seconds": 60,
        "phase_segments": [{
            "start": "10:00",
            "end": "10:30",
            "phase": "eclipse",
        }],
            "tasks": [{
                "start": "10:10",
                "end": "10:15",
                "units": ["computer"],
                "after_mode": "off",
            }],
        },
    }


def snapshot():
    return CurrentPowerSnapshot(
        time=pd.Timestamp("2026-01-01T10:00:00+08:00"),
        battery_voltage_V=28.0,
        bus_voltage_V=28.0,
        total_load_W=100.0,
        phase="阴影区",
        unit_power_W={"computer": 10.0},
        missing_unit_power_W={},
        provenance={"source": "test"},
    )


class SimplifiedForecastTests(unittest.TestCase):
    def setUp(self):
        self.curve = {"points": [[24.0, 0.2], [28.0, 0.6], [30.0, 1.0]]}
        self.design = {
            "battery_capacity_Ah": 100.0,
            "bus_nominal_V": 28.0,
            "eta_charge": 1.0,
            "eta_discharge": 1.0,
            "soc_floor": 0.05,
        }

    def test_task_plan_alone_supports_eclipse_forecast(self):
        provider = StaticSnapshotProvider(snapshot())
        timeline, summary = forecast_from_plan(
            request(), model_document(), provider,
            v_soc_curve=self.curve, design_params=self.design)

        self.assertEqual(provider.requested_units, ("computer",))
        self.assertEqual(summary["schema_version"], "1.1")
        self.assertIn("prediction.phase_segments",
                      summary["input_contract"]["user_inputs"])
        self.assertAlmostEqual(summary["initial_state"]["soc"], 0.6)
        self.assertAlmostEqual(summary["derived_state"]["background_load_W"], 90.0)
        self.assertAlmostEqual(
            summary["energy"]["load_kWh"], 0.050, places=6)
        self.assertAlmostEqual(
            summary["target_end_state"]["soc"], 0.5821428571, places=6)
        self.assertAlmostEqual(
            timeline.iloc[0]["total_load_W"], 100.0)
        self.assertAlmostEqual(
            timeline.loc[timeline["t_start"] == "2026-01-01T10:10:00+08:00",
                         "total_load_W"].iloc[0], 130.0)
        self.assertAlmostEqual(
            timeline.iloc[-1]["total_load_W"], 90.0)
        self.assertFalse(any("显式 power_W" in warning
                             for warning in summary["warnings"]))
        self.assertTrue(any("eclipse-only" in warning
                            for warning in summary["warnings"]))
        self.assertTrue(any("背景负载" in warning
                            for warning in summary["warnings"]))
        self.assertIn("精确分钟", summary["evidence"]["background_load_status"])
        self.assertIn("eclipse-only", summary["evidence"]["solar_input_status"])

    def test_default_after_mode_restores_current_power(self):
        plan = request()
        plan["prediction"]["tasks"][0].pop("after_mode")
        _, summary = forecast_from_plan(
            plan, model_document(), StaticSnapshotProvider(snapshot()),
            v_soc_curve=self.curve, design_params=self.design)

        # 100 W for 30 min plus 30 W extra for 5 min.
        self.assertAlmostEqual(summary["energy"]["load_kWh"], 0.0525)
        self.assertAlmostEqual(
            summary["target_end_state"]["soc"], 0.58125)

    def test_after_mode_applies_until_the_next_task(self):
        plan = request()
        plan["prediction"]["tasks"] = [
            {
                "start": "10:10",
                "end": "10:15",
                "unit": "computer",
                "after_mode": "off",
            },
            {
                "start": "10:20",
                "end": "10:25",
                "unit": "computer",
                "run_mode": "idle",
            },
        ]
        _, summary = forecast_from_plan(
            plan, model_document(), StaticSnapshotProvider(snapshot()),
            v_soc_curve=self.curve, design_params=self.design)
        expected_wh = (100 * 10 + 130 * 5 + 90 * 5 + 92 * 5 + 100 * 5) / 60
        self.assertAlmostEqual(
            summary["energy"]["load_kWh"], expected_wh / 1000, places=6)

    def test_singular_unit_and_common_run_mode_are_supported(self):
        plan = request()
        task = plan["prediction"]["tasks"][0]
        task.pop("units")
        task["unit"] = "computer"
        task["run_mode"] = "idle"
        task["after_mode"] = "off"
        provider = StaticSnapshotProvider(snapshot())
        _, summary = forecast_from_plan(
            plan, model_document(), provider,
            v_soc_curve=self.curve, design_params=self.design)
        self.assertEqual(provider.requested_units, ("computer",))
        # Current 10 W baseline, 2 W idle for 5 min, off for 15 min.
        load_kwh = (100 * 10 + (90 + 2) * 5 + 90 * 15) / 60 / 1000
        self.assertAlmostEqual(
            summary["energy"]["load_kWh"], load_kwh, places=6)

    def test_ambiguous_legacy_mode_key_is_rejected(self):
        plan = request()
        plan["prediction"]["tasks"][0]["mode"] = "active"
        with self.assertRaisesRegex(UnitPowerModelError, "run_mode"):
            forecast_from_plan(
                plan, model_document(), StaticSnapshotProvider(snapshot()),
                v_soc_curve=self.curve, design_params=self.design)

        solar_plan = request()
        solar_plan["prediction"]["solar_segments"] = []
        with self.assertRaisesRegex(UnitPowerModelError, "自动计算"):
            forecast_from_plan(
                solar_plan, model_document(), StaticSnapshotProvider(snapshot()),
                v_soc_curve=self.curve, design_params=self.design)

    def test_time_must_be_an_exact_minute(self):
        plan = request()
        plan["time"] = "2026-01-01T10:00:30+08:00"
        with self.assertRaisesRegex(UnitPowerModelError, "整分钟"):
            forecast_from_plan(
                plan, model_document(), StaticSnapshotProvider(snapshot()),
                v_soc_curve=self.curve, design_params=self.design)

    def test_unknown_run_or_after_mode_is_rejected_instead_of_becoming_active(self):
        bad_run = request()
        bad_run["prediction"]["tasks"][0]["run_mode"] = "typo"
        with self.assertRaisesRegex(UnitPowerModelError, "未支持模式"):
            forecast_from_plan(
                bad_run, model_document(), StaticSnapshotProvider(snapshot()),
                v_soc_curve=self.curve, design_params=self.design)

        bad_after = request()
        bad_after["prediction"]["tasks"][0]["after_mode"] = "typo"
        with self.assertRaisesRegex(UnitPowerModelError, "未支持模式"):
            forecast_from_plan(
                bad_after, model_document(), StaticSnapshotProvider(snapshot()),
                v_soc_curve=self.curve, design_params=self.design)

    def test_tasks_must_cover_known_unit_and_current_power(self):
        original = snapshot()
        bad_snapshot = CurrentPowerSnapshot(
            time=original.time,
            battery_voltage_V=original.battery_voltage_V,
            bus_voltage_V=original.bus_voltage_V,
            total_load_W=original.total_load_W,
            phase=original.phase,
            unit_power_W={},
            missing_unit_power_W={"computer": "missing"},
            provenance=original.provenance,
        )
        with self.assertRaisesRegex(UnitPowerModelError, "当前功率"):
            forecast_from_plan(
                request(), model_document(), StaticSnapshotProvider(bad_snapshot),
                v_soc_curve=self.curve, design_params=self.design)

        plan = request()
        plan["prediction"]["tasks"][0]["units"] = ["unknown"]
        with self.assertRaisesRegex(UnitPowerModelError, "未知单机"):
            forecast_from_plan(
                plan, model_document(), StaticSnapshotProvider(snapshot()),
                v_soc_curve=self.curve, design_params=self.design)

    def test_overlapping_tasks_for_the_same_unit_are_rejected(self):
        plan = request()
        plan["prediction"]["tasks"].append({
            "start": "10:14",
            "end": "10:16",
            "unit": "computer",
        })
        with self.assertRaisesRegex(UnitPowerModelError, "不能重叠"):
            forecast_from_plan(
                plan, model_document(), StaticSnapshotProvider(snapshot()),
                v_soc_curve=self.curve, design_params=self.design)

    def test_sun_phase_requires_future_solar_power_provider(self):
        sun_snapshot = snapshot()
        sun_snapshot = CurrentPowerSnapshot(
            time=sun_snapshot.time,
            battery_voltage_V=sun_snapshot.battery_voltage_V,
            bus_voltage_V=sun_snapshot.bus_voltage_V,
            total_load_W=sun_snapshot.total_load_W,
            phase="阳照区",
            unit_power_W=sun_snapshot.unit_power_W,
            missing_unit_power_W=sun_snapshot.missing_unit_power_W,
            provenance=sun_snapshot.provenance,
        )
        plan = request()
        plan["prediction"]["phase_segments"] = [{
            "start": "10:00", "end": "10:30", "phase": "sun"}]
        with self.assertRaisesRegex(UnitPowerModelError, "太阳阵功率"):
            forecast_from_plan(
                plan, model_document(), StaticSnapshotProvider(sun_snapshot),
                v_soc_curve=self.curve, design_params=self.design)

    def test_explicit_eclipse_provider_records_its_evidence_source(self):
        _, summary = forecast_from_plan(
            request(), model_document(), StaticSnapshotProvider(snapshot()),
            v_soc_curve=self.curve, design_params=self.design,
            solar_provider=EclipseOnlySolarProvider())
        self.assertEqual(summary["derived_state"]["solar_source"],
                         "eclipse_only_zero")

    def test_positive_solar_profile_charges_battery(self):
        plan = request()
        plan["prediction"]["tasks"] = []
        plan["prediction"]["phase_segments"] = [{
            "start": "10:00", "end": "10:30", "phase": "sun"}]
        sun_snapshot = snapshot()
        sun_snapshot = CurrentPowerSnapshot(
            time=sun_snapshot.time,
            battery_voltage_V=sun_snapshot.battery_voltage_V,
            bus_voltage_V=sun_snapshot.bus_voltage_V,
            total_load_W=sun_snapshot.total_load_W,
            phase="阳照区",
            unit_power_W={},
            missing_unit_power_W={},
            provenance=sun_snapshot.provenance,
        )
        _, summary = forecast_from_plan(
            plan, model_document(), StaticSnapshotProvider(sun_snapshot),
            v_soc_curve=self.curve, design_params=self.design,
            solar_provider=FixedSolarProvider(112.0))
        self.assertAlmostEqual(summary["energy"]["solar_to_bus_kWh"], 0.056)
        self.assertAlmostEqual(
            summary["target_end_state"]["soc"], 0.6021428571, places=6)

    def test_solar_provider_can_cap_bus_input_at_forecast_load(self):
        plan = request()
        plan["prediction"]["tasks"] = []
        plan["prediction"]["phase_segments"] = [{
            "start": "10:00", "end": "10:30", "phase": "sun"}]
        sun_snapshot = CurrentPowerSnapshot(
            time=snapshot().time,
            battery_voltage_V=snapshot().battery_voltage_V,
            bus_voltage_V=snapshot().bus_voltage_V,
            total_load_W=snapshot().total_load_W,
            phase="阳照区",
            unit_power_W={},
            missing_unit_power_W={},
            provenance=snapshot().provenance,
        )
        provider = FixedSolarProvider(112.0)
        provider.load_following_charge_margin_W = 0.0
        _, summary = forecast_from_plan(
            plan, model_document(), StaticSnapshotProvider(sun_snapshot),
            v_soc_curve=self.curve, design_params=self.design,
            solar_provider=provider)
        self.assertAlmostEqual(summary["energy"]["solar_to_bus_kWh"], 0.050)
        self.assertAlmostEqual(summary["target_end_state"]["soc"], 0.6)

    def test_task_boundaries_split_a_large_forecast_step(self):
        plan = request()
        plan["prediction"]["step_seconds"] = 300
        plan["prediction"]["tasks"][0]["start"] = "10:03"
        plan["prediction"]["tasks"][0]["end"] = "10:06"
        timeline, _ = forecast_from_plan(
            plan, model_document(), StaticSnapshotProvider(snapshot()),
            v_soc_curve=self.curve, design_params=self.design)
        self.assertEqual(
            timeline["t_start"].tolist()[:4],
            [
                "2026-01-01T10:00:00+08:00",
                "2026-01-01T10:03:00+08:00",
                "2026-01-01T10:06:00+08:00",
                "2026-01-01T10:11:00+08:00",
            ])

    def test_phase_segments_must_cover_the_window_without_gaps(self):
        plan = request()
        plan["prediction"]["phase_segments"] = [{
            "start": "10:00", "end": "10:20", "phase": "eclipse"}]
        with self.assertRaisesRegex(UnitPowerModelError, "phase_segments"):
            forecast_from_plan(
                plan, model_document(), StaticSnapshotProvider(snapshot()),
                v_soc_curve=self.curve, design_params=self.design)

    def test_sun_then_eclipse_uses_historical_polynomial_profile(self):
        plan = request()
        plan["prediction"]["tasks"] = []
        plan["prediction"]["step_seconds"] = 900
        plan["prediction"]["phase_segments"] = [
            {"start": "10:00", "end": "10:20", "phase": "sun"},
            {"start": "10:20", "end": "10:30", "phase": "eclipse"},
        ]
        sun_snapshot = snapshot()
        sun_snapshot = CurrentPowerSnapshot(
            time=sun_snapshot.time,
            battery_voltage_V=sun_snapshot.battery_voltage_V,
            bus_voltage_V=sun_snapshot.bus_voltage_V,
            total_load_W=sun_snapshot.total_load_W,
            phase="阳照区",
            unit_power_W={},
            missing_unit_power_W={},
            provenance=sun_snapshot.provenance,
        )
        solar_model = {
            "schema_version": "1.0",
            "satellite_id": "TEST-SAT",
            "model_type": "polynomial",
            "selected_degree": 1,
            "coefficients_W_per_hour_power": [120.0, 60.0],
            "constant_power_W": 120.0,
            "support_hours": [0.0, 0.5],
        }
        _, summary = forecast_from_plan(
            plan, model_document(), StaticSnapshotProvider(sun_snapshot),
            v_soc_curve=self.curve, design_params=self.design,
            solar_provider=PolynomialSolarPowerProvider(solar_model))
        self.assertAlmostEqual(summary["energy"]["solar_to_bus_kWh"],
                               0.0433333333, places=6)
        self.assertEqual(summary["derived_state"]["phase_segments"], [
            {
                "start": "2026-01-01T10:00:00+08:00",
                "end": "2026-01-01T10:20:00+08:00",
                "phase": "sun",
            },
            {
                "start": "2026-01-01T10:20:00+08:00",
                "end": "2026-01-01T10:30:00+08:00",
                "phase": "eclipse",
            },
        ])


class SnapshotDerivationTests(unittest.TestCase):
    def test_background_uses_only_task_units(self):
        wide = pd.DataFrame(
            {"BUS_V": [28.0], "LOAD_I": [10.0], "I_COMPUTER": [2.0],
             "I_OTHER": [3.0], "PHASE": ["阴影区"]},
            index=pd.DatetimeIndex(["2026-01-01 10:00:00"]))
        specs = [
            {"id": "computer", "role": "load", "plugin": "bus_current",
             "current_code": "I_COMPUTER", "enabled": True},
            {"id": "other", "role": "load", "plugin": "bus_current",
             "current_code": "I_OTHER", "enabled": True},
        ]
        result = derive_current_snapshot(
            wide, pd.Timestamp("2026-01-01T10:00:00+08:00"),
            specs, task_unit_ids={"computer"}, bus_voltage_code="BUS_V",
            load_current_code="LOAD_I", battery_voltage_code=None,
            battery_voltage_V=28.0, phase_code="PHASE")

        self.assertAlmostEqual(result.total_load_W, 280.0)
        self.assertAlmostEqual(result.unit_power_W["computer"], 56.0)
        self.assertNotIn("other", result.unit_power_W)
        self.assertEqual(result.phase, "阴影区")

    def test_text_phase_state_is_used_from_minute_pivot(self):
        wide = pd.DataFrame(
            {"BUS_V": [28.0], "LOAD_I": [10.0], "I_COMPUTER": [2.0],
             "PHASE": [None], "PHASE__state": ["阴影区"]},
            index=pd.DatetimeIndex(["2026-01-01 10:00:00"]))
        specs = [
            {"id": "computer", "role": "load", "plugin": "bus_current",
             "current_code": "I_COMPUTER", "enabled": True},
        ]
        result = derive_current_snapshot(
            wide, pd.Timestamp("2026-01-01T10:00:00+08:00"),
            specs, task_unit_ids={"computer"}, bus_voltage_code="BUS_V",
            load_current_code="LOAD_I", battery_voltage_code=None,
            battery_voltage_V=28.0, phase_code="PHASE")
        self.assertEqual(result.phase, "阴影区")

    def test_missing_task_unit_current_power_is_not_treated_as_zero(self):
        wide = pd.DataFrame(
            {"BUS_V": [28.0], "LOAD_I": [10.0], "I_COMPUTER": [None],
             "PHASE": ["阴影区"]},
            index=pd.DatetimeIndex(["2026-01-01 10:00:00"]))
        specs = [
            {"id": "computer", "role": "load", "plugin": "bus_current",
             "current_code": "I_COMPUTER", "enabled": True},
        ]
        with self.assertRaisesRegex(UnitPowerModelError, "当前功率缺测"):
            derive_current_snapshot(
                wide, pd.Timestamp("2026-01-01T10:00:00+08:00"),
                specs, task_unit_ids={"computer"}, bus_voltage_code="BUS_V",
                load_current_code="LOAD_I", battery_voltage_code=None,
                battery_voltage_V=28.0, phase_code="PHASE")


if __name__ == "__main__":
    unittest.main()
