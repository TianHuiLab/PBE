"""Shared stage-two profile, split, and evaluation contracts."""
from pathlib import Path
import json
import tempfile
import unittest

from stage2_framework import (
    cluster_bootstrap_mean_ci,
    load_profile_contract,
    load_validation_split,
    summarize_cases,
    write_evaluation_summary,
    write_validation_split,
)


BASE = Path(__file__).resolve().parents[1]


def _case(case_id, date, phase, soc_error, observed_soc=80.0):
    return {
        "case_id": case_id,
        "input": {},
        "label": {
            "label_quality": {
                "usable_for_absolute_soc": False,
                "observed_endpoint_saturated": observed_soc >= 99.9999,
            },
            "observed": {
                "target_soc_pct": observed_soc,
                "remaining_Ah": observed_soc * 0.65,
                "solar_energy_kWh": 0.1,
                "load_energy_kWh": 0.08,
            },
            "window": {
                "start": f"{date}T10:00:00+08:00",
                "end": f"{date}T10:10:00+08:00",
            },
        },
        "prediction": {
            "evaluation": {
                "target_soc_error_pp": soc_error,
                "remaining_Ah_error": soc_error * 0.65,
                "solar_energy_relative_error_pct": 5.0,
                "load_energy_relative_error_pct": -3.0,
            },
        },
        "metadata": {
            "phase": phase,
            "season": "DJF",
            "duration_band": "short",
            "load_band": "medium",
            "task_count": 1,
        },
        "task_plan": {"tasks": []},
        "predicted_soc": None,
        "observed_soc": None,
    }


class Stage2FrameworkTests(unittest.TestCase):
    def test_profile_contracts_are_explicit_and_satellite_specific(self):
        scs = load_profile_contract(BASE / "satellites" / "SCS-01-01")
        yj = load_profile_contract(BASE / "satellites" / "YJ-3D")

        self.assertEqual(scs["satellite_id"], "SCS-01-01")
        self.assertEqual(scs["primary"]["evaluation_mode"], "frozen_snapshot")
        self.assertEqual(
            scs["diagnostic"]["evaluation_mode"],
            "future_background_replay",
        )
        self.assertEqual(yj["satellite_id"], "YJ-3D")
        self.assertEqual(yj["primary"]["evaluation_mode"], "frozen_snapshot")

    def test_cluster_bootstrap_uses_dates_as_sampling_units(self):
        values = [1.0, 1.0, 9.0]
        cases = [
            _case("W01", "2026-01-01", "sun", 1.0),
            _case("W02", "2026-01-01", "sun", 1.0),
            _case("W03", "2026-01-02", "sun", 9.0),
        ]
        summary = summarize_cases(cases, satellite_id="TEST-SAT")
        metric = summary["subsets"]["all"]["metrics"][
            "soc_abs_error_pp"
        ]

        self.assertEqual(metric["n_cases"], 3)
        self.assertEqual(metric["effective_dates"], 2)
        self.assertAlmostEqual(metric["window_equal_mean"], 11.0 / 3)
        self.assertAlmostEqual(metric["date_equal_mean"], 5.0)
        self.assertEqual(metric["date_cluster_bootstrap"]["sampling_unit"], "date")
        self.assertEqual(metric["date_cluster_bootstrap"]["n_dates"], 2)

        direct = cluster_bootstrap_mean_ci(
            values,
            dates=["2026-01-01", "2026-01-01", "2026-01-02"],
            samples=1000,
            seed=7,
        )
        self.assertEqual(direct["n_dates"], 2)
        self.assertLessEqual(direct["lower"], direct["mean"])
        self.assertGreaterEqual(direct["upper"], direct["mean"])

    def test_summary_partitions_saturated_and_phase_subsets(self):
        cases = [
            _case("W01", "2026-01-01", "sun", 0.5, observed_soc=80.0),
            _case("W02", "2026-01-02", "eclipse", 1.5, observed_soc=60.0),
            _case("W03", "2026-01-03", "sun", 0.0, observed_soc=100.0),
        ]
        summary = summarize_cases(cases, satellite_id="TEST-SAT")

        self.assertEqual(summary["subsets"]["sun"]["n_cases"], 2)
        self.assertEqual(summary["subsets"]["eclipse"]["n_cases"], 1)
        self.assertEqual(summary["subsets"]["saturated"]["n_cases"], 1)
        self.assertEqual(summary["subsets"]["non_saturated"]["n_cases"], 2)
        self.assertAlmostEqual(
            summary["subsets"]["non_saturated"]["metrics"][
                "soc_abs_error_pp"
            ]["window_equal_mean"],
            1.0,
        )

    def test_metric_specific_qc_keeps_clean_solar_when_soc_fails(self):
        case = _case("W01", "2026-01-01", "sun", 1.0)
        case["label"]["label_quality"].update({
            "soc_qc_status": "fail",
            "solar_qc_status": "pass",
            "load_qc_status": "pass",
        })
        case["prediction"]["evaluation"]["target_soc_error_pp"] = None
        case["prediction"]["evaluation"]["remaining_Ah_error"] = None
        case["prediction"]["evaluation"]["target_soc_error_raw_pp"] = 4.0
        case["prediction"]["evaluation"][
            "solar_energy_relative_error_raw_pct"] = 15.0

        summary = summarize_cases([case], satellite_id="TEST-SAT")
        clean_metrics = summary["subsets"]["all"]["metrics"]
        raw_metrics = summary["subsets"]["raw"]["metrics"]

        self.assertEqual(clean_metrics["soc_abs_error_pp"]["n_cases"], 0)
        self.assertAlmostEqual(
            clean_metrics["solar_energy_abs_relative_error_pct"][
                "window_equal_mean"], 5.0)
        self.assertAlmostEqual(
            raw_metrics["soc_abs_error_pp"]["window_equal_mean"], 4.0)
        self.assertAlmostEqual(
            raw_metrics["solar_energy_abs_relative_error_pct"][
                "window_equal_mean"], 15.0)

    def test_writes_machine_readable_evaluation_and_validation_split(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            satellite_root = root / "TEST-SAT"
            satellite_root.mkdir()
            evaluation_path, cases_path = write_evaluation_summary(
                satellite_root,
                [_case("W01", "2026-01-01", "sun", 1.0)],
                satellite_id="TEST-SAT",
            )
            self.assertTrue(evaluation_path.is_file())
            self.assertTrue(cases_path.is_file())
            summary = json.loads(evaluation_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["satellite_id"], "TEST-SAT")
            self.assertEqual(summary["subsets"]["all"]["n_cases"], 1)

            split_path = write_validation_split(
                satellite_root,
                satellite_id="TEST-SAT",
                dates=["2026-01-02", "2026-01-01", "2026-01-01"],
                source="unit-test",
            )
            split = load_validation_split(satellite_root)
            self.assertEqual(
                split["dates"], ["2026-01-01", "2026-01-02"])
            self.assertEqual(split["satellite_id"], "TEST-SAT")
            self.assertTrue(split_path.is_file())


class Stage2FrameworkIntegrationTests(unittest.TestCase):
    def test_scs_testset_has_primary_evaluation_and_frozen_contract(self):
        root = BASE / "satellites" / "SCS-01-01" / "stage2_testset"
        manifest = json.loads(
            (root / "manifest.json").read_text(encoding="utf-8"))
        summary = json.loads(
            (root / "evaluation_summary.json").read_text(encoding="utf-8"))

        self.assertEqual(
            summary["primary"]["evaluation_mode"], "frozen_snapshot")
        self.assertEqual(
            summary["primary"]["unit_model_split"],
            "date_grouped_holdout",
        )
        self.assertEqual(manifest["evaluation_summary"],
                         "evaluation_summary.json")
        self.assertEqual(
            summary["subsets"]["all"]["metrics"][
                "soc_abs_error_pp"
            ]["n_cases"],
            25,
        )
        self.assertEqual(summary["subsets"]["sun"]["n_cases"], 19)
        self.assertEqual(summary["subsets"]["eclipse"]["n_cases"], 6)
        self.assertEqual(summary["subsets"]["qc_clean"]["n_cases"], 25)

    def test_scs_unit_model_and_validation_split_use_the_same_16_dates(self):
        root = BASE / "satellites" / "SCS-01-01"
        split = load_validation_split(root)
        model = json.loads(
            (root / "power_model" / "unit_power_holdout_models.json").read_text(
                encoding="utf-8"))
        metadata = json.loads(
            (root / "stage2_testset" / "cases" / "W01" /
             "metadata.json").read_text(encoding="utf-8"))

        self.assertEqual(len(split["dates"]), 16)
        self.assertEqual(
            model["training_split"]["excluded_dates"],
            split["dates"],
        )
        self.assertEqual(metadata["evaluation_mode"], "frozen_snapshot")
        self.assertEqual(
            metadata["background_policy"],
            "window_start_snapshot",
        )
        self.assertEqual(
            metadata["unit_model_split"],
            "date_grouped_holdout",
        )

        solar = json.loads(
            (root / "power_model" /
             "batch_validation_holdout_solar_model.json").read_text(
                encoding="utf-8"))
        self.assertEqual(
            solar["training_split"]["excluded_dates"],
            split["dates"],
        )
        self.assertTrue(
            solar["training"]["time_end"].startswith("2026-11-05"))


if __name__ == "__main__":
    unittest.main()
