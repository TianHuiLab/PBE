"""Tests for direction-aware provisional V-SOC calibration."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from battery_soc_calibration import (collect_directional_soc_samples,
                                     calibrate_directional_v_soc,
                                     fit_directional_voltage_curves,
                                     map_soc_from_voltage)


class DirectionalVoltageCurveTests(unittest.TestCase):
    def setUp(self):
        soc = np.arange(.1, 1.0, .1)
        discharge = np.column_stack([20.0 + 10.0 * soc, soc])
        charge = np.column_stack([20.0 + 10.0 * soc + .5, soc])
        self.samples = pd.concat([
            pd.DataFrame({
                "direction": "charge", "soc": discharge[:, 1],
                "voltage": charge[:, 0],
            }),
            pd.DataFrame({
                "direction": "discharge", "soc": charge[:, 1],
                "voltage": discharge[:, 0],
            }),
        ], ignore_index=True)

    def test_charge_curve_is_above_discharge_and_monotonic(self):
        model = fit_directional_voltage_curves(
            self.samples, min_samples=1, bin_width=.1)
        charge = model["curves"]["charge"]
        discharge = model["curves"]["discharge"]
        self.assertTrue(np.all(np.diff(charge[:, 0]) > 0))
        self.assertTrue(np.all(np.diff(discharge[:, 0]) > 0))
        v_charge = np.interp(.5, charge[:, 1], charge[:, 0])
        v_discharge = np.interp(.5, discharge[:, 1], discharge[:, 0])
        self.assertAlmostEqual(v_charge - v_discharge, .5, places=6)

    def test_inverse_mapping_uses_direction(self):
        model = fit_directional_voltage_curves(
            self.samples, min_samples=1, bin_width=.1)
        charge_soc = map_soc_from_voltage(25.5, "charge", model)
        discharge_soc = map_soc_from_voltage(25.5, "discharge", model)
        self.assertAlmostEqual(charge_soc, .5, places=6)
        self.assertAlmostEqual(discharge_soc, .55, places=6)
        self.assertGreater(discharge_soc - charge_soc, .049)

    def test_unknown_direction_is_rejected(self):
        model = fit_directional_voltage_curves(
            self.samples, min_samples=1, bin_width=.1)
        with self.assertRaises(ValueError):
            map_soc_from_voltage(25.5, "rest", model)


class DirectionalSampleCollectionTests(unittest.TestCase):
    def test_charge_soc_is_propagated_from_energy_with_fixed_capacity(self):
        times = pd.date_range("2026-01-01 10:00", periods=3, freq="min")
        frame = pd.DataFrame({
            "t": times,
            "phase": ["日照", "日照", "日照"],
            "V_bat": [28.0, 28.1, 28.2],
            "P_charge_est": [1680.0, 1680.0, 0.0],
            "P_disch_est": [0.0, 0.0, 0.0],
        })
        base = np.asarray([[24.0, .2], [28.0, .6], [30.0, 1.0]])
        samples = collect_directional_soc_samples(
            frame, base,
            capacity_Ah=100.0, nominal_voltage_V=28.0,
            eta_charge=1.0, eta_discharge=1.0,
            min_segment_minutes=3)
        self.assertEqual(samples["direction"].tolist(),
                         ["charge", "charge", "charge"])
        # 1680 W for one minute out of 2800 Wh adds 0.01 SOC.
        self.assertAlmostEqual(samples.loc[1, "soc"], .61, places=9)
        self.assertAlmostEqual(samples.loc[2, "soc"], .62, places=9)

    def test_history_calibration_reports_direction_gap_and_holdout_gain(self):
        rows = []
        for segment in range(8):
            for direction, phase, power in (
                    ("charge", "日照", 1680.0),
                    ("discharge", "地影", -1680.0)):
                start = pd.Timestamp("2026-01-01") + pd.Timedelta(
                    days=segment * 2 + (direction == "discharge"))
                for minute in range(12):
                    base_soc = .6 + (
                        minute * .01 if direction == "charge"
                        else -minute * .01)
                    voltage = 20 + 10 * base_soc
                    voltage += .3 if direction == "charge" else -.2
                    rows.append({
                        "t": start + pd.Timedelta(minutes=minute),
                        "phase": phase,
                        "V_bat": voltage,
                        "P_charge_est": power if direction == "charge" else 0.0,
                        "P_disch_est": power if direction == "discharge" else 0.0,
                    })
        frame = pd.DataFrame(rows)
        base = np.asarray([[20.0, 0.0], [25.0, .5], [30.0, 1.0]])
        model = calibrate_directional_v_soc(
            frame, base, capacity_Ah=100.0, nominal_voltage_V=28.0,
            eta_charge=1.0, eta_discharge=1.0,
            min_segment_minutes=5, bin_width=.05, min_samples=2)

        self.assertEqual(model["calibration_status"],
                         "provisional_conditional_directional")
        self.assertGreater(model["direction_gap_voltage"]["median_V"], .45)
        cv = model["cross_validation"]
        self.assertGreater(cv["charge"]["n_validation"], 0)
        self.assertGreater(cv["discharge"]["n_validation"], 0)
        self.assertGreaterEqual(cv["charge"]["directional_mae_V"], 0)
        self.assertGreaterEqual(cv["discharge"]["directional_mae_V"], 0)
        self.assertIn("baseline_soc_mae_pp", cv["charge"])
        self.assertIn("directional_soc_mae_pp", cv["discharge"])
        self.assertIn("recommended_for_production", model)


if __name__ == "__main__":
    unittest.main()
