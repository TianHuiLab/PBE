"""Tests for stratified historical forecast validation windows."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_validate_forecast import (HistoricalResultSnapshotProvider,
                                     build_window_validation_inputs,
                                     run_batch_validation,
                                     select_phase_validation_windows,
                                     select_validation_windows)


def make_segment(start, minutes, load, phase="日照"):
    times = pd.date_range(start, periods=minutes + 1, freq="min")
    return pd.DataFrame({
        "t": times,
        "phase": phase,
        "P_load": load,
        "SOC": 80.0,
        "V_bat": 28.0,
    })


class ValidationSelectionTests(unittest.TestCase):
    def test_selects_eclipse_windows_only_from_allowed_dates(self):
        frame = pd.concat([
            make_segment("2026-09-30 22:00", 12, 180.0, phase="地影"),
            make_segment("2026-10-01 22:00", 20, 250.0, phase="地影"),
            make_segment("2026-10-02 22:00", 25, 80.0, phase="地影"),
            make_segment("2026-10-03 10:00", 30, 100.0, phase="日照"),
        ], ignore_index=True)
        windows = select_phase_validation_windows(
            frame,
            phase="eclipse",
            allowed_dates={"2026-10-01", "2026-10-02"},
            min_minutes=5,
        )

        self.assertEqual(len(windows), 2)
        self.assertTrue(windows["phase"].eq("eclipse").all())
        self.assertEqual(
            set(windows["validation_date"].astype(str)),
            {"2026-10-01", "2026-10-02"},
        )
        self.assertTrue(windows["duration_minutes"].ge(5).all())

    def test_selects_available_season_duration_load_cells(self):
        frame = pd.concat([
            make_segment("2026-01-10 10:00", 30, 250.0),
            make_segment("2026-04-10 10:00", 12, 50.0),
            make_segment("2026-06-10 10:00", 6, 150.0),
        ], ignore_index=True)
        windows, coverage = select_validation_windows(
            frame, max_per_stratum=1)

        self.assertEqual(len(windows), 3)
        self.assertEqual(
            set(zip(windows["season"], windows["duration_band"],
                    windows["load_band"])),
            {
                ("DJF", "long", "high"),
                ("MAM", "medium", "low"),
                ("JJA", "short", "medium"),
            })
        self.assertEqual(coverage["seasons_available"], ["DJF", "MAM", "JJA"])
        self.assertIn("SON", coverage["seasons_missing"])
        self.assertEqual(
            windows.loc[windows["season"] == "DJF", "duration_minutes"].iloc[0],
            30)

    def test_gaps_and_non_sun_rows_break_candidate_windows(self):
        first = make_segment("2026-01-10 10:00", 8, 50.0)
        second = make_segment("2026-01-10 10:10", 8, 50.0)
        eclipse = make_segment("2026-01-10 10:20", 5, 180.0, phase="地影")
        windows, _ = select_validation_windows(
            pd.concat([first, second, eclipse], ignore_index=True),
            min_minutes=5, max_per_stratum=2)
        self.assertEqual(len(windows), 2)
        self.assertTrue(windows["duration_minutes"].eq(8).all())

    def test_validation_dates_are_reported_for_training_exclusion(self):
        frame = pd.concat([
            make_segment("2026-01-10 10:00", 30, 250.0),
            make_segment("2026-01-11 10:00", 12, 50.0),
        ], ignore_index=True)
        windows, _ = select_validation_windows(frame)
        self.assertEqual(windows["validation_date"].tolist(),
                         [pd.Timestamp("2026-01-10").date(),
                          pd.Timestamp("2026-01-11").date()])

    def test_validation_windows_honor_frozen_holdout_dates(self):
        frame = pd.concat([
            make_segment("2026-01-10 10:00", 30, 250.0),
            make_segment("2026-01-11 10:00", 12, 50.0),
            make_segment("2026-04-10 10:00", 12, 50.0),
        ], ignore_index=True)

        windows, coverage = select_validation_windows(
            frame, allowed_dates={"2026-01-10", "2026-01-11"})

        self.assertEqual(
            set(windows["validation_date"].astype(str)),
            {"2026-01-10", "2026-01-11"},
        )
        self.assertEqual(coverage["seasons_missing"], ["MAM", "JJA", "SON"])

    def test_historical_snapshot_uses_exact_minute_values(self):
        frame = pd.DataFrame({
            "t": pd.to_datetime(["2026-01-10 10:00", "2026-01-10 10:01"]),
            "phase": ["日照", "日照"],
            "V_bat": [28.0, 28.1],
            "P_load": [100.0, 120.0],
            "SOC": [80.0, 81.0],
        })
        provider = HistoricalResultSnapshotProvider(frame)
        snapshot = provider.snapshot(
            pd.Timestamp("2026-01-10T10:00:00+08:00"), ())
        self.assertEqual(snapshot.total_load_W, 100.0)
        self.assertEqual(snapshot.battery_voltage_V, 28.0)
        self.assertEqual(snapshot.phase, "日照")

    def test_batch_runner_reports_soc_and_power_errors(self):
        result = pd.concat([
            make_segment("2026-01-10 10:00", 30, 250.0),
            make_segment("2026-06-10 10:00", 6, 50.0),
        ], ignore_index=True)
        result["P_charge_est"] = 0.0
        result["P_disch_est"] = -50.0
        components = pd.DataFrame({
            "t": result["t"],
            "P_computer_W": [20.0] * len(result),
        })
        unit_model = {
            "schema_version": "1.0",
            "satellite_id": "SCS-01-01",
            "timezone": "Asia/Shanghai",
            "models": {
                "computer": {
                    "usable_for_prediction": True,
                    "source": {"power_column": "P_computer_W"},
                    "active_power_W": 20.0,
                    "low_power_W": None,
                    "active_threshold_W": None,
                    "power_bands": [
                        {"id": "active", "power_p50_W": 20.0},
                    ],
                    "modes": {},
                },
            },
        }
        solar_model = {
            "schema_version": "1.0",
            "satellite_id": "SCS-01-01",
            "model_type": "constant",
            "selected_degree": 1,
            "coefficients_W_per_hour_power": [120.0],
            "constant_power_W": 120.0,
            "support_hours": [0.0, 1.0],
            "extrapolation_policy": "clamp_to_support",
        }
        windows, summary = run_batch_validation(
            result, unit_model, solar_model,
            base_curve_points=np.asarray([[24.0, .2], [28.0, .6], [30.0, 1.0]]),
            design={"battery_capacity_Ah": 100.0, "bus_nominal_V": 28.0,
                    "eta_charge": 1.0, "eta_discharge": 1.0,
                    "soc_floor": 0.05},
            components=components)
        self.assertEqual(len(windows), 2)
        self.assertIn("soc_error_pp", windows.columns)
        self.assertTrue(windows["soc_error_pp"].notna().all())
        self.assertTrue(windows["n_task_units"].eq(1).all())
        self.assertEqual(summary["task_usage"]["windows_with_tasks"], 2)
        self.assertEqual(summary["task_usage"]["total_tasks"], 2)
        self.assertEqual(summary["coverage"]["selected_windows"], 2)

    def test_builds_tasks_and_background_from_historical_component_power(self):
        times = pd.date_range("2026-01-10 10:00", periods=7, freq="min")
        result = pd.DataFrame({
            "t": times,
            "phase": ["日照"] * 7,
            "V_bat": [28.0] * 7,
            "SOC": [80.0] * 7,
            "P_load": [100.0] * 7,
        })
        components = pd.DataFrame({
            "t": times,
            "P_computer_W": [2.0, 2.0, 2.0, 40.0, 40.0, 40.0, 40.0],
        })
        unit_model = {
            "schema_version": "1.0",
            "satellite_id": "SCS-01-01",
            "models": {
                "computer": {
                    "usable_for_prediction": True,
                    "source": {"power_column": "P_computer_W"},
                    "active_power_W": 40.0,
                    "low_power_W": 2.0,
                    "active_threshold_W": 20.0,
                    "power_bands": [
                        {"id": "low", "power_p50_W": 2.0},
                        {"id": "active", "power_p50_W": 40.0},
                    ],
                    "modes": {},
                },
            },
        }
        inputs = build_window_validation_inputs(
            result, components,
            pd.Timestamp("2026-01-10 10:00"),
            pd.Timestamp("2026-01-10 10:06"),
            unit_model)
        self.assertEqual(inputs["selected_units"], ["computer"])
        self.assertEqual(
            [(task["unit_id"], task["mode"]) for task in inputs["tasks"]],
            [("computer", "idle"), ("computer", "active")])
        self.assertEqual(
            [segment["power_W"]
             for segment in inputs["background_load_segments"]],
            [98.0, 60.0])

    def test_extracted_tasks_keep_high_power_level(self):
        times = pd.date_range("2026-01-10 10:00", periods=7, freq="min")
        result = pd.DataFrame({
            "t": times,
            "phase": ["日照"] * 7,
            "V_bat": [28.0] * 7,
            "SOC": [80.0] * 7,
            "P_load": [150.0] * 7,
        })
        components = pd.DataFrame({
            "t": times,
            "P_computer_W": [2.0, 2.0, 2.0, 40.0, 40.0, 100.0, 100.0],
        })
        unit_model = {
            "schema_version": "1.0",
            "satellite_id": "SCS-01-01",
            "timezone": "Asia/Shanghai",
            "models": {
                "computer": {
                    "usable_for_prediction": True,
                    "source": {"power_column": "P_computer_W"},
                    "active_power_W": 40.0,
                    "low_power_W": 2.0,
                    "active_threshold_W": 20.0,
                    "high_power_W": 100.0,
                    "high_threshold_W": 80.0,
                    "power_bands": [
                        {"id": "low", "power_p50_W": 2.0},
                        {"id": "active", "power_p50_W": 40.0},
                        {"id": "high", "power_p50_W": 100.0},
                    ],
                    "modes": {},
                },
            },
        }

        inputs = build_window_validation_inputs(
            result, components,
            pd.Timestamp("2026-01-10 10:00"),
            pd.Timestamp("2026-01-10 10:06"),
            unit_model)

        self.assertEqual(
            [(task["mode"], task.get("power_level"))
             for task in inputs["tasks"]],
            [("idle", None), ("active", None), ("active", "high")])

    def test_batch_runner_uses_explicit_solar_target_column(self):
        result = make_segment("2026-01-10 10:00", 30, 250.0)
        result["P_charge_est"] = 0.0
        result["P_disch_est"] = -50.0
        result["P_solar_target"] = 150.0
        unit_model = {
            "schema_version": "1.0",
            "satellite_id": "YJ-3D",
            "timezone": "Asia/Shanghai",
            "models": {},
        }
        solar_model = {
            "schema_version": "1.0",
            "satellite_id": "YJ-3D",
            "model_type": "constant",
            "selected_degree": 1,
            "coefficients_W_per_hour_power": [150.0],
            "constant_power_W": 150.0,
            "support_hours": [0.0, 1.0],
            "extrapolation_policy": "clamp_to_support",
        }
        windows, _ = run_batch_validation(
            result, unit_model, solar_model,
            base_curve_points=np.asarray(
                [[24.0, .2], [28.0, .6], [30.0, 1.0]]),
            design={"battery_capacity_Ah": 100.0, "bus_nominal_V": 28.0,
                    "eta_charge": 1.0, "eta_discharge": 1.0,
                    "soc_floor": 0.05},
            solar_target_column="P_solar_target")
        self.assertAlmostEqual(
            windows.loc[0, "solar_obs_proxy_energy_kWh"], 0.075, places=6)
        self.assertAlmostEqual(
            windows.loc[0, "solar_energy_error_kWh"], 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
