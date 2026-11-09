"""Telemetry interval quality and BCR target-policy tests."""
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from stage2_quality import (
    analyze_intervals,
    capped_energy,
    integrate_checked_soc,
)


class Stage2QualityTests(unittest.TestCase):
    def test_range_violation_is_kept_in_raw_but_not_primary_soc(self):
        frame = pd.DataFrame({
            "t": pd.date_range("2026-01-01 00:00:00", periods=6, freq="min"),
            "phase": ["日照"] * 6,
            "battery_current_mean": [0.0, 0.0, 12.0, 0.0, 0.0, 0.0],
            "battery_voltage_mean": [27.0] * 6,
        })
        result = integrate_checked_soc(
            frame,
            initial_soc=0.5,
            capacity_Ah=10.0,
            ranges={
                "battery_current_mean": (-10.0, 10.0),
                "battery_voltage_mean": (21.0, 29.2),
            },
            max_gap_seconds=90,
        )

        self.assertEqual(result["qc_status"], "fail")
        self.assertIsNone(result["target_soc_pct"])
        self.assertIsNotNone(result["target_soc_pct_raw"])
        self.assertEqual(
            result["quality"]["reason_counts"]["range_violation"], 2)
        self.assertEqual(
            result["quality"]["range_violation_fields"],
            {"battery_current_mean": 1},
        )
        self.assertEqual(result["quality"]["valid_intervals"], 3)

    def test_gap_phase_transition_duplicate_and_frozen_are_explicit(self):
        frame = pd.DataFrame({
            "t": [
                "2026-01-01 00:00:00",
                "2026-01-01 00:01:00",
                "2026-01-01 00:01:00",
                "2026-01-01 00:07:00",
                "2026-01-01 00:08:00",
                "2026-01-01 00:09:00",
                "2026-01-01 00:10:00",
            ],
            "phase": ["日照"] * 2 + ["地影"] * 5,
            "battery_current_mean": [2.0] * 7,
            "battery_voltage_mean": [27.0] * 7,
        })
        intervals, summary = analyze_intervals(
            frame,
            ranges={
                "battery_current_mean": (-10.0, 10.0),
                "battery_voltage_mean": (21.0, 29.2),
            },
            max_gap_seconds=90,
            frozen_min_samples=4,
        )

        self.assertIn("duplicate", summary["reason_counts"])
        self.assertIn("gap", summary["reason_counts"])
        self.assertIn("phase_transition", summary["reason_counts"])
        self.assertIn("frozen_sensor", summary["reason_counts"])
        self.assertTrue(intervals["quality_reasons"].str.contains(
            "frozen_sensor").any())

    def test_bcr_primary_uses_training_cap_and_raw_is_diagnostic(self):
        frame = pd.DataFrame({
            "t": pd.date_range(
                "2026-01-01 00:00:00", periods=4, freq="min"),
            "bcr_out_W": [100.0, 100.0, 400.0, 100.0],
        })
        result = capped_energy(
            frame, value_column="bcr_out_W", cap_W=100.0)

        self.assertAlmostEqual(result["primary_energy_kWh"], 0.005)
        self.assertAlmostEqual(result["raw_energy_kWh"], 0.010)
        self.assertEqual(result["above_cap_count"], 1)
        self.assertAlmostEqual(result["excess_energy_kWh"], 0.005)
        self.assertEqual(result["qc_status"], "pass")

    def test_missing_and_empty_inputs_do_not_become_zero(self):
        frame = pd.DataFrame({
            "t": pd.date_range("2026-01-01", periods=2, freq="min"),
            "phase": ["日照", "日照"],
            "battery_current_mean": [2.0, None],
            "battery_voltage_mean": [27.0, 27.0],
        })
        result = integrate_checked_soc(
            frame,
            initial_soc=0.5,
            capacity_Ah=10.0,
            ranges={
                "battery_current_mean": (-10.0, 10.0),
                "battery_voltage_mean": (21.0, 29.2),
            },
        )
        self.assertEqual(result["qc_status"], "fail")
        self.assertIn("missing", result["quality"]["reason_counts"])
        self.assertIsNone(result["target_soc_pct"])

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.csv"
            path.write_text("t,value\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                capped_energy(
                    pd.read_csv(path),
                    value_column="value",
                    cap_W=100.0,
                )


if __name__ == "__main__":
    unittest.main()
