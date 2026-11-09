"""Tests for historical solar-array power fitting and phase-aware profiles."""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from solar_power_provider import PolynomialSolarPowerProvider
from solar_power_calibration import calibrate_solar_power_model
from unit_power_model import CurrentPowerSnapshot


class SolarCalibrationTests(unittest.TestCase):
    def test_excluded_dates_never_enter_solar_training(self):
        rows = []
        for episode in range(6):
            base = pd.Timestamp("2026-06-01 08:00:00") + pd.Timedelta(
                days=episode)
            for minute in range(12):
                rows.append({
                    "t": base + pd.Timedelta(minutes=minute),
                    "phase": "日照",
                    "P_load": 100.0,
                    "P_charge_est": 20.0 + minute,
                })
            rows.append({
                "t": base + pd.Timedelta(minutes=12),
                "phase": "地影",
                "P_load": 100.0,
                "P_charge_est": 0.0,
            })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "energy_v2_result.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            model = calibrate_solar_power_model(
                path,
                satellite_id="SCS-01-01",
                degree_candidates=(1,),
                exclude_dates=["2026-06-02", "2026-06-04"],
            )

        self.assertEqual(
            model["training_split"]["excluded_dates"],
            ["2026-06-02", "2026-06-04"],
        )
        self.assertEqual(model["training_split"]["excluded_rows"], 26)
        self.assertEqual(model["training"]["sun_segments"], 4)
        self.assertEqual(
            model["training"]["time_start"], "2026-06-01T08:00:00")
        self.assertEqual(
            model["training"]["time_end"], "2026-06-06T08:11:00")

    def test_uses_load_plus_charge_and_selects_cross_validated_polynomial(self):
        rows = []
        for episode in range(5):
            base = pd.Timestamp("2026-06-01 08:00:00") + pd.Timedelta(
                days=episode)
            for minute in range(12):
                rows.append({
                    "t": base + pd.Timedelta(minutes=minute),
                    "phase": "日照",
                    "P_load": 100.0,
                    "P_solar": 9999.0,
                    "P_charge_est": 20.0 + minute,
                })
            for minute in range(12, 16):
                rows.append({
                    "t": base + pd.Timedelta(minutes=minute),
                    "phase": "地影",
                    "P_load": 110.0,
                    "P_solar": 0.0,
                    "P_charge_est": 0.0,
                })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "energy_v2_result.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            model = calibrate_solar_power_model(
                path, satellite_id="SCS-01-01", degree_candidates=(1, 2))

        self.assertEqual(model["target_definition"]["formula"],
                         "P_load + P_charge_est")
        self.assertEqual(model["model_type"], "polynomial")
        self.assertEqual(model["selected_degree"], 1)
        self.assertEqual(model["training"]["sun_segments"], 5)
        self.assertAlmostEqual(model["coefficients_W_per_hour_power"][0],
                               120.0, places=6)
        self.assertAlmostEqual(model["coefficients_W_per_hour_power"][1],
                               60.0, places=6)
        self.assertEqual(model["training"]["shadow_samples_in_source"], 20)

    def test_explicit_target_column_overrides_load_plus_charge(self):
        rows = []
        for episode in range(5):
            base = pd.Timestamp("2026-06-01 08:00:00") + pd.Timedelta(
                days=episode)
            for minute in range(12):
                rows.append({
                    "t": base + pd.Timedelta(minutes=minute),
                    "phase": "日照",
                    "P_load": 100.0,
                    "P_solar": 9999.0,
                    "P_solar_target": 50.0,
                    "P_charge_est": 20.0,
                })
            for minute in range(12, 16):
                rows.append({
                    "t": base + pd.Timedelta(minutes=minute),
                    "phase": "地影",
                    "P_load": 110.0,
                    "P_solar": 0.0,
                    "P_solar_target": 0.0,
                    "P_charge_est": 0.0,
                })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "energy_v2_result.csv"
            pd.DataFrame(rows).to_csv(path, index=False)
            model = calibrate_solar_power_model(
                path, satellite_id="YJ-3D", degree_candidates=(1,),
                target_column="P_solar_target")

        self.assertEqual(model["model_type"], "constant")
        self.assertAlmostEqual(model["constant_power_W"], 50.0, places=6)


class PolynomialSolarProviderTests(unittest.TestCase):
    def setUp(self):
        self.model = {
            "schema_version": "1.0",
            "satellite_id": "SCS-01-01",
            "model_type": "polynomial",
            "selected_degree": 1,
            "coefficients_W_per_hour_power": [120.0, 60.0],
            "constant_power_W": 120.0,
            "support_hours": [0.0, 0.5],
            "extrapolation_policy": "clamp_to_support",
        }
        self.snapshot = CurrentPowerSnapshot(
            time=pd.Timestamp("2026-06-01T08:00:00+08:00"),
            battery_voltage_V=28.0, bus_voltage_V=28.0,
            total_load_W=100.0, phase="阳照区",
            unit_power_W={}, missing_unit_power_W={}, provenance={})

    def test_profile_is_zero_in_eclipse_and_polynomial_in_sun(self):
        provider = PolynomialSolarPowerProvider(self.model)
        result = provider.profile(
            start=pd.Timestamp("2026-06-01T08:00:00+08:00"),
            end=pd.Timestamp("2026-06-01T08:30:00+08:00"),
            step_seconds=900,
            snapshot=self.snapshot,
            phase_segments=[
                {"start": "08:00", "end": "08:20", "phase": "sun"},
                {"start": "08:20", "end": "08:30", "phase": "eclipse"},
            ])
        self.assertEqual(len(result), 3)
        # Sun intervals use segment-relative midpoint 10 min and 20 min.
        self.assertAlmostEqual(result[0]["power_W"], 127.5)
        self.assertAlmostEqual(result[1]["power_W"], 137.5)
        self.assertEqual(result[2]["power_W"], 0.0)
        self.assertEqual(result[2]["phase"], "eclipse")

    def test_profile_clamps_polynomial_beyond_training_support(self):
        model = dict(self.model)
        model["support_hours"] = [0.0, 0.1]
        provider = PolynomialSolarPowerProvider(model)
        result = provider.profile(
            start=pd.Timestamp("2026-06-01T08:00:00+08:00"),
            end=pd.Timestamp("2026-06-01T08:20:00+08:00"),
            step_seconds=600,
            snapshot=self.snapshot,
            phase_segments=[
                {"start": "08:00", "end": "08:20", "phase": "sun"},
            ])
        self.assertAlmostEqual(result[-1]["power_W"], 126.0)
        self.assertTrue(any("training support" in warning
                            for warning in provider.warnings))

    def test_profile_applies_empirical_bus_transfer_efficiency(self):
        model = dict(self.model)
        model["bus_transfer_efficiency"] = 0.8
        provider = PolynomialSolarPowerProvider(model)
        result = provider.profile(
            start=pd.Timestamp("2026-06-01T08:00:00+08:00"),
            end=pd.Timestamp("2026-06-01T08:20:00+08:00"),
            step_seconds=600,
            snapshot=self.snapshot,
            phase_segments=[
                {"start": "08:00", "end": "08:20", "phase": "sun"},
            ])
        baseline = PolynomialSolarPowerProvider(self.model).profile(
            start=pd.Timestamp("2026-06-01T08:00:00+08:00"),
            end=pd.Timestamp("2026-06-01T08:20:00+08:00"),
            step_seconds=600,
            snapshot=self.snapshot,
            phase_segments=[
                {"start": "08:00", "end": "08:20", "phase": "sun"},
            ])
        self.assertAlmostEqual(
            result[0]["power_W"], baseline[0]["power_W"] * 0.8)
        self.assertAlmostEqual(
            result[1]["power_W"], baseline[1]["power_W"] * 0.8)
        self.assertTrue(any("bus transfer efficiency" in warning
                            for warning in provider.warnings))

    def test_load_aware_model_uses_predicted_load_and_task_state(self):
        model = {
            "schema_version": "1.0",
            "satellite_id": "YJ-3D",
            "model_type": "load_aware_linear",
            "support_hours": [0.0, 1.0],
            "feature_model": {
                "features": [
                    "elapsed_hours", "load_W",
                    "task_unit_power_W", "active_unit_count"],
                "coefficients": [10.0, 0.5, 2.0, 3.0],
                "intercept": 5.0,
            },
        }
        load_segments = [
            {
                "start": "2026-06-01T08:00:00+08:00",
                "end": "2026-06-01T08:10:00+08:00",
                "power_W": 100.0,
                "task_unit_power_W": 20.0,
                "active_unit_count": 2,
            },
            {
                "start": "2026-06-01T08:10:00+08:00",
                "end": "2026-06-01T08:20:00+08:00",
                "power_W": 120.0,
                "task_unit_power_W": 30.0,
                "active_unit_count": 3,
            },
        ]
        result = PolynomialSolarPowerProvider(model).profile(
            start=pd.Timestamp("2026-06-01T08:00:00+08:00"),
            end=pd.Timestamp("2026-06-01T08:20:00+08:00"),
            step_seconds=600,
            snapshot=self.snapshot,
            phase_segments=[
                {"start": "08:00", "end": "08:20", "phase": "sun"},
            ],
            load_segments=load_segments)
        self.assertAlmostEqual(result[0]["power_W"], 5 + 10 * (5 / 60) + 50 + 40 + 6)
        self.assertAlmostEqual(result[1]["power_W"], 5 + 10 * (15 / 60) + 60 + 60 + 9)


if __name__ == "__main__":
    unittest.main()
