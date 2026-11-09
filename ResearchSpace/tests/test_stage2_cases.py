"""Representative second-stage input/output and SOC trajectory cases."""
from pathlib import Path
import unittest

import pandas as pd

try:
    from ._data import require_local_data
except ImportError:
    from _data import require_local_data
from stage2_cases import _extract_historical_tasks, load_representative_case


BASE = Path(__file__).resolve().parents[1]


class Stage2RepresentativeCaseTests(unittest.TestCase):
    def test_historical_tasks_preserve_high_power_level(self):
        times = pd.date_range(
            "2026-01-01T00:00:00+08:00", periods=18, freq="min")
        frame = pd.DataFrame({
            "P_transient_unit_W": [
                1, 1, 1,
                10, 10, 10,
                80, 80, 80,
                10, 10, 10,
                1, 1, 1,
                80, 80, 80,
            ],
        }, index=times)
        models = {
            "transient_unit": {
                "active_threshold_W": 5.0,
                "high_threshold_W": 40.0,
                "power_bands": [
                    {"id": "low", "power_p50_W": 1.0},
                    {"id": "active", "power_p50_W": 10.0},
                    {"id": "high", "power_p50_W": 80.0},
                ],
            },
        }
        tasks, plan = _extract_historical_tasks(
            frame, models, times[-1] + pd.Timedelta(minutes=1))
        high_tasks = [
            task for task in tasks
            if task.get("power_level") == "high"
        ]
        self.assertEqual(len(high_tasks), 2)
        self.assertTrue(all(
            item.get("power_level") == "high"
            for item in plan["transient_unit"]
            if item["mode"] == "active_high"
        ))

    @require_local_data(
        BASE / "satellites" / "SCS-01-01" / "out" / "energy_v2_result.csv")
    def test_scs_case_aligns_predicted_and_observed_soc(self):
        case = load_representative_case(BASE / "satellites" / "SCS-01-01")
        self.assertEqual(case["satellite_id"], "SCS-01-01")
        self.assertEqual(case["window"]["start"], "2026-03-01T10:56:00+08:00")
        self.assertEqual(case["window"]["end"], "2026-03-01T11:06:00+08:00")
        self.assertEqual(
            len(case["input_request"]["prediction"]["tasks"]), 2)
        self.assertEqual(case["derived_inputs"]["task_count"], 2)
        self.assertEqual(
            set(case["input_request"]["prediction"]),
            {"end", "step_seconds", "phase_segments", "tasks"})
        self.assertNotIn(
            "solar_segments", case["input_request"]["prediction"])
        self.assertNotIn(
            "background_load_segments", case["input_request"]["prediction"])
        self.assertEqual(len(case["series"]["times"]), 11)
        self.assertAlmostEqual(
            case["series"]["observed_soc_pct"][-1], 89.3651, places=3)
        self.assertAlmostEqual(
            case["series"]["predicted_soc_pct"][-1], 91.1324, places=3)
        self.assertAlmostEqual(
            case["outputs"]["soc_signed_error_pp"], 1.7673206349, places=4)
        self.assertGreater(
            case["outputs"]["predicted_solar_energy_kWh"], 0)
        self.assertEqual(case["reference_mode"],
                         "conditional_v_soc_minute_curve")

    @require_local_data(
        BASE / "satellites" / "YJ-3D" / "data_power_minute.csv")
    def test_yj_case_uses_extracted_tasks_and_current_integrated_soc(self):
        case = load_representative_case(BASE / "satellites" / "YJ-3D")
        self.assertEqual(case["satellite_id"], "YJ-3D")
        self.assertEqual(case["window"]["start"], "2026-07-28T16:22:00+08:00")
        self.assertEqual(case["window"]["end"], "2026-07-28T17:11:00+08:00")
        self.assertEqual(
            len(case["input_request"]["prediction"]["tasks"]), 15)
        self.assertEqual(case["derived_inputs"]["task_count"], 15)
        self.assertEqual(
            set(case["input_request"]["prediction"]),
            {"end", "step_seconds", "phase_segments", "tasks"})
        self.assertEqual(len(case["series"]["times"]), 50)
        self.assertAlmostEqual(
            case["series"]["observed_soc_pct"][-1], 75.6320, places=3)
        self.assertGreater(min(case["series"]["observed_soc_pct"]), 70)
        self.assertAlmostEqual(
            case["series"]["predicted_soc_pct"][-1], 76.3649, places=3)
        self.assertAlmostEqual(
            case["outputs"]["soc_signed_error_pp"], 0.7328951772, places=4)
        self.assertAlmostEqual(
            case["outputs"]["v_soc_endpoint_soc_pct"], 72.7374, places=3)
        self.assertAlmostEqual(
            case["outputs"]["v_soc_minus_current_integrated_pp"],
            -2.89457, places=3)
        self.assertEqual(
            case["reference_mode"], "current_integrated_with_v_soc_crosscheck")


if __name__ == "__main__":
    unittest.main()
