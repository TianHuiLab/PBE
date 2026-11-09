"""Unit tests for calibrated power bands and battery task prediction."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unit_power_model import (UnitPowerModelError, calibrate_unit_models,
                              load_excluded_dates, model_summary_frame,
                              _detect_power_bands, _model_power,
                              predict_battery_state)


def config_for(units):
    return {
        "satellite_id": "TEST-SAT",
        "components": [
            {
                "id": unit_id,
                "name": unit_id,
                "role": "load",
                "plugin": "bus_current",
                "current_code": f"I_{unit_id}",
                "enabled": True,
                **({"mode": {"code": f"MODE_{unit_id}", "values": {}}}
                   if with_mode else {}),
            }
            for unit_id, with_mode in units
        ],
    }


class CalibrationTests(unittest.TestCase):
    def test_exclude_date_file_accepts_validation_split_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "validation_split.json"
            path.write_text(json.dumps({
                "dates": ["2026-01-02", "2026-01-01", "2026-01-01"],
            }), encoding="utf-8")
            self.assertEqual(
                load_excluded_dates(path),
                ["2026-01-01", "2026-01-02"],
            )

    def test_calibration_can_exclude_grouped_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            times = []
            for day in range(1, 5):
                times.extend(pd.date_range(
                    f"2026-01-{day:02d}", periods=30, freq="min"))
            frame = pd.DataFrame({
                "t": times,
                "P_constant_unit_W": np.full(120, 12.0),
            })
            csv_path = root / "components.csv"
            config_path = root / "components.json"
            frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
            config_path.write_text(
                json.dumps(config_for([("constant_unit", False)]),
                           ensure_ascii=False), encoding="utf-8")

            models = calibrate_unit_models(
                csv_path, config_path, satellite_id="TEST-SAT",
                min_samples=10,
                exclude_dates=["2026-01-01"])
            model = models["models"]["constant_unit"]

            self.assertEqual(model["observations"]["valid_samples"], 90)
            self.assertEqual(
                models["training_split"]["excluded_dates"],
                ["2026-01-01"],
            )
            self.assertEqual(
                models["training_split"]["excluded_rows"], 30)
            self.assertEqual(
                models["created_from"]["time_start"],
                "2026-01-02T00:00:00",
            )

    def test_detects_constant_and_mode_power_without_filling_missing_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            times = pd.date_range("2026-01-01", periods=80, freq="min")
            frame = pd.DataFrame({
                "t": times,
                "P_constant_unit_W": np.full(80, 12.0),
                "P_mode_unit_W": np.where(np.arange(80) < 40, 5.0, 15.0),
                "mode_mode_unit": np.where(np.arange(80) < 40, "待机", "工作"),
            })
            frame.loc[3:7, "P_constant_unit_W"] = np.nan
            csv_path = root / "components.csv"
            config_path = root / "components.json"
            frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
            config_path.write_text(
                json.dumps(config_for([
                    ("constant_unit", False),
                    ("mode_unit", True),
                ]), ensure_ascii=False), encoding="utf-8")

            models = calibrate_unit_models(
                csv_path, config_path, satellite_id="TEST-SAT",
                min_samples=10)

            constant = models["models"]["constant_unit"]
            self.assertEqual(constant["model_type"], "constant")
            self.assertAlmostEqual(constant["active_power_W"], 12.0)
            self.assertEqual(constant["observations"]["valid_samples"], 75)

            mode_unit = models["models"]["mode_unit"]
            self.assertEqual(mode_unit["model_type"], "mode_constant")
            self.assertAlmostEqual(
                mode_unit["modes"]["待机"]["power_p50_W"], 5.0)
            self.assertAlmostEqual(
                mode_unit["modes"]["工作"]["power_p50_W"], 15.0)
            summary = model_summary_frame(models)
            self.assertEqual(list(summary["unit_id"]), [
                "constant_unit", "mode_unit"])
            self.assertEqual(int(summary["explicit_modes"].sum()), 2)

    def test_calibration_filters_impossible_power_spikes_and_keeps_rare_active_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            times = pd.date_range("2026-01-01", periods=120, freq="min")
            powers = np.full(120, 1.0)
            powers[40:44] = 12.0
            powers[[20, 80]] = 500_000.0
            frame = pd.DataFrame({
                "t": times,
                "P_spike_unit_W": powers,
            })
            csv_path = root / "components.csv"
            config_path = root / "components.json"
            frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
            config = config_for([("spike_unit", False)])
            config["components"][0]["max_power_W"] = 20.0
            config_path.write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8")

            models = calibrate_unit_models(
                csv_path, config_path, satellite_id="TEST-SAT",
                min_samples=20)
            model = models["models"]["spike_unit"]

            self.assertEqual(model["observations"]["valid_samples"], 118)
            self.assertEqual(model["observations"]["outlier_samples"], 2)
            self.assertAlmostEqual(model["stats"]["max_W"], 12.0)
            self.assertEqual(
                model["outlier_filter"]["raw_max_W"], 500_000.0)
            self.assertEqual(
                model["outlier_filter"]["upper_limit_W"], 20.0)
            self.assertEqual(
                model["outlier_filter"]["method"], "configured_max_power")
            self.assertAlmostEqual(model["active_power_W"], 12.0)

    def test_calibration_applies_robust_extreme_filter_without_config_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            powers = np.full(240, 3.0)
            powers[100] = 1_000_000.0
            frame = pd.DataFrame({
                "t": pd.date_range("2026-02-01", periods=240, freq="min"),
                "P_unbounded_unit_W": powers,
            })
            csv_path = root / "components.csv"
            config_path = root / "components.json"
            frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
            config_path.write_text(
                json.dumps(config_for([("unbounded_unit", False)]),
                           ensure_ascii=False), encoding="utf-8")

            models = calibrate_unit_models(
                csv_path, config_path, satellite_id="TEST-SAT",
                min_samples=20)
            model = models["models"]["unbounded_unit"]

            self.assertEqual(model["observations"]["valid_samples"], 239)
            self.assertEqual(model["observations"]["outlier_samples"], 1)
            self.assertEqual(
                model["outlier_filter"]["method"],
                "robust_extreme_tail",
            )
            self.assertAlmostEqual(model["active_power_W"], 3.0)

    def test_polynomial_can_win_only_on_grouped_run_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stamps = []
            powers = []
            for episode in range(8):
                start = pd.Timestamp("2026-02-01") + pd.Timedelta(days=episode)
                for minute in range(12):
                    stamps.append(start + pd.Timedelta(minutes=minute))
                    powers.append(20.0 + 1.5 * minute + 0.05 * episode)
            frame = pd.DataFrame({
                "t": stamps,
                "P_variable_unit_W": powers,
            })
            csv_path = root / "components.csv"
            config_path = root / "components.json"
            frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
            config_path.write_text(
                json.dumps(config_for([("variable_unit", False)]),
                           ensure_ascii=False), encoding="utf-8")

            models = calibrate_unit_models(
                csv_path, config_path, satellite_id="TEST-SAT",
                min_samples=20, polynomial_degree=2)
            model = models["models"]["variable_unit"]

            self.assertEqual(model["model_type"], "polynomial")
            polynomial = model["polynomial"]
            self.assertTrue(polynomial["selected"])
            self.assertGreater(polynomial["support_hours"][1], 0.0)
            self.assertGreater(
                polynomial["cross_validation"]["relative_improvement"], 0.10)
            self.assertTrue(polynomial["monotonic_within_support"])
            self.assertEqual(
                len(polynomial["runtime_clip_W"]), 2)
            self.assertIn(
                "raw_polynomial_rmse_W",
                polynomial["cross_validation"])
            self.assertIn(
                "clipped_polynomial_rmse_W",
                polynomial["cross_validation"])

    def test_separated_high_power_state_is_not_folded_into_polynomial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            minutes = np.arange(1648)
            powers = np.full(1648, 1.0)
            powers[1600:1640] = (
                10.0 + (minutes[1600:1640] % 7) * 0.1)
            powers[1640:] = (
                300.0 + (minutes[1640:] % 5) * 0.2)
            frame = pd.DataFrame({
                "t": pd.date_range("2026-03-01", periods=1648, freq="min"),
                "P_transient_unit_W": powers,
            })
            csv_path = root / "components.csv"
            config_path = root / "components.json"
            frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
            config_path.write_text(
                json.dumps(config_for([("transient_unit", False)]),
                           ensure_ascii=False), encoding="utf-8")

            models = calibrate_unit_models(
                csv_path, config_path, satellite_id="TEST-SAT",
                min_samples=20, polynomial_degree=2)
            model = models["models"]["transient_unit"]

            self.assertEqual(
                [band["id"] for band in model["power_bands"]],
                ["low", "active", "high"])
            self.assertGreater(model["high_threshold_W"],
                               model["active_threshold_W"])
            self.assertGreater(model["high_power_W"],
                               model["active_power_W"] * 2)
            self.assertFalse(model["polynomial"]["selected"])
            self.assertIn("高功率", model["polynomial"]["reason"])

    def test_rejected_global_high_tail_does_not_drop_samples_from_bands(self):
        values = np.concatenate([
            np.full(1000, 1.0),
            np.full(90, 20.0),
            np.full(10, 100.0),
        ])

        band = _detect_power_bands(values)

        self.assertEqual(sum(item["n"] for item in band["bands"]), len(values))
        self.assertEqual(
            [item["id"] for item in band["bands"]],
            ["low", "active"],
        )
        self.assertAlmostEqual(band["low_power_W"], 1.0)
        self.assertAlmostEqual(band["active_power_W"], 20.0)
        self.assertGreater(
            next(item for item in band["bands"]
                 if item["id"] == "active")["power_p95_W"],
            90.0,
        )

    def test_missing_high_band_can_use_sibling_feed_prior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            minutes = np.arange(1648)
            feed_a = np.full(1648, 1.0)
            feed_a[1600:1640] = 10.0
            feed_b = np.full(1648, 1.0)
            feed_b[1600:1640] = 10.0
            feed_b[1640:] = 300.0 + (minutes[1640:] % 5) * 0.2
            frame = pd.DataFrame({
                "t": pd.date_range(
                    "2026-03-01", periods=1648, freq="min"),
                "P_feed_a_W": feed_a,
                "P_feed_b_W": feed_b,
            })
            csv_path = root / "components.csv"
            config_path = root / "components.json"
            frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
            config = config_for([("feed_a", False), ("feed_b", False)])
            config["shared_power_band_groups"] = [{
                "id": "paired_power",
                "members": ["feed_a", "feed_b"],
                "reason": "intentional test group",
            }]
            config_path.write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8")

            models = calibrate_unit_models(
                csv_path, config_path, satellite_id="TEST-SAT",
                min_samples=20)
            model_a = models["models"]["feed_a"]
            model_b = models["models"]["feed_b"]

            self.assertIsNotNone(model_b["high_power_W"])
            self.assertAlmostEqual(
                model_a["high_power_W"], model_b["high_power_W"])
            self.assertAlmostEqual(
                model_a["high_threshold_W"], model_b["high_threshold_W"])
            self.assertEqual(
                model_a["shared_power_band_source"]["source_units"],
                ["feed_b"],
            )
            self.assertEqual(
                next(
                    band for band in model_a["power_bands"]
                    if band["id"] == "high"
                )["support"],
                "shared_power_band_group",
            )

    def test_runtime_clipping_uses_polynomial_declared_bounds(self):
        model = {
            "model_type": "polynomial",
            "active_power_W": 3.0,
            "low_power_W": 1.0,
            "power_bands": [],
            "modes": {},
            "polynomial": {
                "selected": True,
                "coefficients_W_per_hour_power": [3.0, 1000.0],
                "support_hours": [0.0, 1.0],
                "runtime_clip_W": [2.0, 5.0],
            },
            "stats": {"p05_W": 1.0, "p99_W": 100.0},
        }
        warnings = []
        power = _model_power(
            model, {"mode": "active"}, 0.5, warnings, "test_unit")
        self.assertEqual(power, 5.0)
        self.assertTrue(any("截断" in item for item in warnings))


class PredictionTests(unittest.TestCase):
    def setUp(self):
        self.models = {
            "schema_version": "1.0",
            "satellite_id": "TEST-SAT",
            "timezone": "Asia/Shanghai",
            "models": {
                "computer": {
                    "name": "计算机",
                    "model_type": "constant",
                    "usable_for_prediction": True,
                    "active_power_W": 280.0,
                    "low_power_W": 2.0,
                    "power_bands": [
                        {"id": "low", "label": "低", "power_p50_W": 2.0},
                        {"id": "active", "label": "工作", "power_p50_W": 280.0},
                    ],
                    "modes": {},
                    "polynomial": {"selected": False},
                    "stats": {"p05_W": 2.0, "p99_W": 280.0},
                }
            },
        }
        self.request = {
            "schema_version": "1.0",
            "satellite_id": "TEST-SAT",
            "current_state": {
                "time": "2026-01-01T10:00:00",
                "battery_soc": 0.5,
                "background_load_W": 0.0,
            },
            "prediction": {
                "start": "2026-01-01T10:00:00",
                "end": "2026-01-01T12:00:00",
                "step_seconds": 1800,
                "solar_segments": [{
                    "start": "2026-01-01T10:00:00",
                    "end": "2026-01-01T12:00:00",
                    "power_W": 0.0,
                }],
            },
            "tasks": [{
                "unit_id": "computer",
                "start": "2026-01-01T10:00:00",
                "end": "2026-01-01T11:00:00",
                "mode": "active",
            }],
            "assumptions": {
                "battery_capacity_Ah": 100.0,
                "nominal_voltage_V": 28.0,
                "eta_charge": 1.0,
                "eta_discharge": 1.0,
                "soc_floor": 0.05,
            },
        }

    def test_constant_task_reduces_soc_by_expected_energy(self):
        timeline, summary = predict_battery_state(self.models, self.request)

        self.assertAlmostEqual(summary["initial_state"]["soc"], 0.5)
        self.assertAlmostEqual(summary["target_end_state"]["soc"], 0.4)
        self.assertAlmostEqual(
            summary["target_end_state"]["remaining_Ah"], 40.0)
        self.assertAlmostEqual(
            summary["energy"]["unit_energy_kWh"]["computer"], 0.28)
        self.assertEqual(len(timeline), 4)
        self.assertAlmostEqual(timeline["total_load_W"].max(), 280.0)

    def test_idle_and_off_modes_use_distinct_values(self):
        self.request["tasks"][0]["mode"] = "idle"
        _, idle = predict_battery_state(self.models, self.request)
        self.assertAlmostEqual(idle["energy"]["load_kWh"], 0.002)

        self.request["tasks"][0]["mode"] = "off"
        _, off = predict_battery_state(self.models, self.request)
        self.assertAlmostEqual(off["energy"]["load_kWh"], 0.0)

    def test_voltage_state_requires_curve(self):
        request = json.loads(json.dumps(self.request))
        request["current_state"].pop("battery_soc")
        request["current_state"]["battery_voltage_V"] = 28.0
        with self.assertRaisesRegex(UnitPowerModelError, "V-SOC"):
            predict_battery_state(self.models, request)

        curve = {"points": [[24.0, 0.2], [28.0, 0.6], [30.0, 1.0]]}
        _, summary = predict_battery_state(
            self.models, request, v_soc_curve=curve)
        self.assertAlmostEqual(summary["initial_state"]["soc"], 0.6)

    def test_overlap_unknown_unit_and_uncovered_solar_are_rejected(self):
        overlap = json.loads(json.dumps(self.request))
        overlap["tasks"].append(dict(overlap["tasks"][0]))
        with self.assertRaisesRegex(UnitPowerModelError, "不能重叠"):
            predict_battery_state(self.models, overlap)

        unknown = json.loads(json.dumps(self.request))
        unknown["tasks"][0]["unit_id"] = "missing"
        with self.assertRaisesRegex(UnitPowerModelError, "未知单机"):
            predict_battery_state(self.models, unknown)

        uncovered = json.loads(json.dumps(self.request))
        uncovered["prediction"]["solar_segments"][0]["end"] = "2026-01-01T11:00:00"
        with self.assertRaisesRegex(UnitPowerModelError, "未覆盖"):
            predict_battery_state(self.models, uncovered)

    def test_background_load_can_be_derived_from_current_total(self):
        self.request["current_state"].pop("background_load_W")
        self.request["current_state"]["load_power_W"] = 300.0
        self.request["current_state"]["current_unit_power_W"] = {
            "computer": 20.0
        }
        _, summary = predict_battery_state(self.models, self.request)
        # Background 280 W for 2 h plus the scheduled 280 W computer for 1 h.
        self.assertAlmostEqual(summary["energy"]["load_kWh"], 0.84)

    def test_floor_shortfall_takes_priority_over_other_warnings(self):
        self.request["prediction"]["end"] = "2026-01-01T20:00:00"
        self.request["prediction"]["solar_segments"][0]["end"] = (
            "2026-01-01T20:00:00")
        self.request["tasks"][0]["end"] = "2026-01-01T20:00:00"
        _, summary = predict_battery_state(self.models, self.request)
        self.assertEqual(summary["status"], "completed_with_shortfall")
        self.assertGreater(
            summary["energy"]["unserved_energy_at_floor_kWh"], 0)
        self.assertEqual(summary["target_end_state"]["soc"], 0.05)


if __name__ == "__main__":
    unittest.main()
