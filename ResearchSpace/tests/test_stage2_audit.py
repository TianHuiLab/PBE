"""Second-stage audit metrics and data-quality checks."""
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import tempfile
import unittest

from stage2_audit import (
    ab_topology_audit,
    ab_coactivity,
    bcr_outlier_audit,
    bootstrap_mean_ci,
    build_manifest,
    build_stage2_audit,
    hierarchical_background_comparison,
    partition_soc_saturation,
    rolling_background_comparison,
    solar_structure_audit,
    summarize_metric,
)

BASE = Path(__file__).resolve().parents[1]


class Stage2AuditTests(unittest.TestCase):
    def test_bootstrap_ci_is_deterministic_and_contains_sample_mean(self):
        values = [0.2, 0.8, 1.3, 2.0, 2.4, 3.1]
        first = bootstrap_mean_ci(values, samples=2000, seed=20261104)
        second = bootstrap_mean_ci(values, samples=2000, seed=20261104)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first["mean"], 1.6333333333333333)
        self.assertLessEqual(first["lower"], first["mean"])
        self.assertGreaterEqual(first["upper"], first["mean"])

    def test_summary_reports_effective_dates_and_duration_weighting(self):
        result = summarize_metric(
            [1.0, 3.0, 2.0, 4.0],
            dates=["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"],
            durations=[1.0, 3.0, 1.0, 1.0],
            energy_weights=[1.0, 1.0, 2.0, 2.0],
            samples=1000,
            seed=7,
        )
        self.assertEqual(result["n_windows"], 4)
        self.assertEqual(result["effective_dates"], 2)
        self.assertAlmostEqual(result["window_equal"]["mean"], 2.5)
        self.assertAlmostEqual(result["duration_weighted_mean"], 16 / 6)
        self.assertAlmostEqual(result["energy_weighted_mean"], 16 / 6)
        self.assertEqual(len(result["window_equal"]["bootstrap_95_ci"]), 2)

    def test_saturation_partition_uses_observed_soc_as_reference(self):
        rows = [
            {"observed": 1.0, "predicted": 1.0, "error": 0.0},
            {"observed": 1.0, "predicted": 0.99, "error": 1.0},
            {"observed": 0.80, "predicted": 0.80, "error": 0.0},
            {"observed": 0.90, "predicted": 0.95, "error": 5.0},
        ]
        result = partition_soc_saturation(
            rows, observed_key="observed", predicted_key="predicted",
            error_key="error")
        self.assertEqual(result["observed_saturated"]["n_windows"], 2)
        self.assertEqual(result["observed_non_saturated"]["n_windows"], 2)
        self.assertAlmostEqual(
            result["observed_non_saturated"]["mae_pp"], 2.5)
        self.assertEqual(result["all"]["zero_error_count"], 2)
        self.assertEqual(result["all"]["predicted_ceiling_count"], 1)

    def test_bcr_outlier_audit_quantifies_count_and_energy_excess(self):
        result = bcr_outlier_audit([90.0, 100.0, 120.0, 400.0], cap_W=100.0)
        self.assertEqual(result["n_valid"], 4)
        self.assertEqual(result["above_cap_count"], 2)
        self.assertAlmostEqual(result["above_cap_fraction"], 0.5)
        self.assertAlmostEqual(
            result["excess_energy_fraction"], 320 / 710)
        self.assertEqual(result["max_W"], 400.0)

    def test_solar_structure_marks_missing_physical_inputs(self):
        model = {
            "model_type": "load_aware_linear",
            "feature_model": {
                "features": ["elapsed_hours", "load_W"],
                "coefficients": [-0.02, 0.94],
            },
        }
        result = solar_structure_audit(model, {
            "status": "blocked_until_available_generation_inputs",
            "required_features": [
                "orbit_phase", "sun_incidence_deg", "charge_acceptance_W"],
                "available_features": ["elapsed_hours", "load_W"],
        })
        self.assertEqual(
            result["status"],
            "blocked_until_available_generation_inputs",
        )
        self.assertEqual(
            result["missing_features"],
            ["orbit_phase", "sun_incidence_deg", "charge_acceptance_W"],
        )
        self.assertAlmostEqual(result["load_coefficient"], 0.94)
        self.assertTrue(result["load_following_sensitivity"])

    def test_ab_coactivity_counts_both_side_and_single_side_minutes(self):
        models = {
            "pl1_a": {"active_threshold_W": 10.0,
                      "active_power_W": 20.0,
                      "models": {}},
            "pl1_b": {"active_threshold_W": 5.0,
                      "active_power_W": 10.0,
                      "models": {}},
        }
        rows = [
            {"t": "2026-01-01 00:00:00",
             "P_pl1_a_W": 20.0, "P_pl1_b_W": 10.0},
            {"t": "2026-01-01 00:01:00",
             "P_pl1_a_W": 0.0, "P_pl1_b_W": 10.0},
            {"t": "2026-01-01 00:02:00",
             "P_pl1_a_W": 20.0, "P_pl1_b_W": 0.0},
            {"t": "2026-01-01 00:03:00",
             "P_pl1_a_W": None, "P_pl1_b_W": 0.0},
        ]
        result = ab_coactivity(rows, models)
        self.assertEqual(len(result["pairs"]), 1)
        pair = result["pairs"][0]
        self.assertEqual(pair["pl_channel"], 1)
        self.assertEqual(pair["valid_minutes"], 3)
        self.assertEqual(pair["both_active_minutes"], 1)
        self.assertEqual(pair["a_only_minutes"], 1)
        self.assertEqual(pair["b_only_minutes"], 1)
        self.assertEqual(pair["neither_active_minutes"], 0)

    def test_ab_coactivity_can_scan_multiple_pairs_in_one_pass(self):
        models = {
            "pl1_a": {"active_threshold_W": 10.0,
                      "active_power_W": 20.0},
            "pl1_b": {"active_threshold_W": 5.0,
                      "active_power_W": 10.0},
            "pl2_a": {"active_threshold_W": 2.0,
                      "active_power_W": 4.0},
            "pl2_b": {"active_threshold_W": 3.0,
                      "active_power_W": 6.0},
        }

        def rows():
            yield {
                "t": "2026-01-01 00:00:00",
                "P_pl1_a_W": 20.0, "P_pl1_b_W": 10.0,
                "P_pl2_a_W": 4.0, "P_pl2_b_W": 6.0,
            }

        result = ab_coactivity(rows(), models)
        self.assertEqual(
            [(item["pl_channel"], item["both_active_minutes"])
             for item in result["pairs"]],
            [(1, 1), (2, 1)])

    def test_ab_topology_does_not_assume_sum_or_or(self):
        coactivity = {"pairs": [{
            "pl_channel": 1,
            "a_unit": "pl1_a",
            "b_unit": "pl1_b",
        }]}
        unresolved = ab_topology_audit(coactivity, {
            "status": "unresolved",
            "default_aggregation_mode": "unresolved",
            "pairs": [],
        })
        self.assertEqual(unresolved["n_unresolved"], 1)
        self.assertTrue(unresolved["blocks_task_aggregation"])
        self.assertEqual(
            unresolved["pairs"][0]["aggregation_mode"], "unresolved")

        resolved = ab_topology_audit(coactivity, {
            "status": "approved",
            "default_aggregation_mode": "unresolved",
            "pairs": [{
                "pl_channel": 1,
                "aggregation_mode": "select",
                "source": "PDU-SLD-001",
            }],
        })
        self.assertEqual(resolved["n_resolved"], 1)
        self.assertFalse(resolved["blocks_task_aggregation"])
        self.assertEqual(resolved["pairs"][0]["aggregation_mode"], "select")

    def test_rolling_background_uses_only_pre_window_history(self):
        start = datetime(2026, 1, 1, 0, 10)
        observations = []
        for minute in range(0, 13):
            value = 100.0 if minute < 10 else (110.0 if minute == 10 else 500.0)
            observations.append({
                "t": (start - timedelta(minutes=10) +
                      timedelta(minutes=minute)).isoformat(sep=" "),
                "phase": "日照",
                "background_W": value,
            })
        windows = [{
            "start": "2026-01-01 00:10:00",
            "end": "2026-01-01 00:12:00",
            "phase": "日照",
        }]
        result = rolling_background_comparison(
            observations, windows, lookbacks=(5,), min_history_minutes=3)
        models = result["windows"][0]["models"]
        self.assertEqual(models["fixed"]["predicted_background_W"], 110.0)
        self.assertEqual(models["rolling_5m"]["predicted_background_W"], 100.0)
        self.assertEqual(
            models["rolling_5m"]["history_minutes_available"], 5)

    def test_hierarchical_background_falls_back_to_previous_day(self):
        start = datetime(2026, 1, 2, 10, 0)
        observations = []
        for minute in range(-15, 16):
            stamp = start - timedelta(days=1) + timedelta(minutes=minute)
            observations.append({
                "t": stamp.isoformat(sep=" "),
                "phase": "日照",
                "background_W": 40.0,
            })
        observations.append({
            "t": (start - timedelta(minutes=1)).isoformat(sep=" "),
            "phase": "日照",
            "background_W": 70.0,
        })
        for minute in range(0, 11):
            observations.append({
                "t": (start + timedelta(minutes=minute)).isoformat(sep=" "),
                "phase": "日照",
                "background_W": 42.0,
            })
        windows = [{
            "start": "2026-01-02 10:00:00",
            "end": "2026-01-02 10:10:00",
            "phase": "日照",
        }]
        result = hierarchical_background_comparison(
            observations,
            windows,
            rolling_lookback_minutes=60,
            min_history_minutes=5,
            previous_day_tolerance_minutes=15,
            min_previous_day_minutes=5,
        )
        item = result["windows"][0]
        self.assertEqual(item["selected_source"], "previous_day_same_phase")
        self.assertIsNotNone(item["predicted_background_W"])
        self.assertEqual(result["summary"]["n_windows"], 1)

    def test_manifest_hashes_declared_inputs_and_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            input_path = root / "input.csv"
            code_path = root / "code.py"
            input_path.write_text("a,b\n1,2\n", encoding="utf-8")
            code_path.write_text("VALUE = 1\n", encoding="utf-8")
            audit = {
                "satellite_id": "TEST-1",
                "validation": {"wind_validation_dates": ["2026-01-01"]},
                "metrics": {"soc": {"window_equal": {"mean": 1.0}}},
            }
            manifest = build_manifest(
                root, audit, input_paths=[input_path], code_paths=[code_path])
            self.assertEqual(manifest["satellite_id"], "TEST-1")
            self.assertEqual(manifest["inputs"][0]["sha256"],
                             hashlib.sha256(input_path.read_bytes()).hexdigest())
            self.assertEqual(manifest["code"][0]["sha256"],
                             hashlib.sha256(code_path.read_bytes()).hexdigest())
            self.assertEqual(manifest["validation"]["wind_validation_dates"],
                             ["2026-01-01"])


class Stage2AuditIntegrationTests(unittest.TestCase):
    def test_scs_audit_reports_effective_dates_and_weighted_metrics(self):
        audit = build_stage2_audit(BASE / "satellites" / "SCS-01-01")
        self.assertEqual(audit["satellite_id"], "SCS-01-01")
        soc = audit["metrics"]["soc_abs_error_pp"]
        self.assertEqual(soc["n_windows"], 25)
        self.assertEqual(soc["effective_dates"], 16)
        self.assertAlmostEqual(soc["window_equal"]["mean"], 1.951625270)
        self.assertEqual(
            audit["metrics_context"]["evaluation_mode"],
            "frozen_snapshot",
        )
        self.assertEqual(
            audit["diagnostic_metrics_context"]["evaluation_mode"],
            "future_background_replay",
        )
        self.assertAlmostEqual(
            audit["diagnostic_metrics"]["soc_abs_error_pp"][
                "window_equal"]["mean"],
            1.460143191,
        )
        self.assertGreaterEqual(
            audit["data_coverage"]["season_counts"]["SON"], 1)
        self.assertGreaterEqual(
            audit["data_coverage"]["phase_counts"]["地影"], 1)
        self.assertEqual(
            len(audit["data_coverage"]["temperature_codes"]), 5)
        self.assertEqual(
            len(soc["window_equal"]["bootstrap_95_ci"]), 2)
        self.assertEqual(
            audit["representative_case"]["satellite_id"], "SCS-01-01")
        self.assertEqual(
            len(audit["representative_case"]["series"]["times"]), 11)
        self.assertEqual(
            audit["representative_case"]["derived_inputs"]["task_count"], 2)

    def test_yj_audit_contains_saturation_background_ab_and_bcr_checks(self):
        audit = build_stage2_audit(BASE / "satellites" / "YJ-3D")
        saturation = audit["soc_saturation"]
        self.assertEqual(
            saturation["observed_saturated"]["n_windows"], 4)
        self.assertEqual(
            saturation["observed_non_saturated"]["n_windows"], 3)
        self.assertAlmostEqual(
            saturation["observed_non_saturated"]["mae_pp"], 0.3478508048)
        self.assertEqual(audit["metrics"]["soc_abs_error_pp"]["n_windows"], 7)
        self.assertEqual(
            audit["metrics"]["soc_abs_error_pp"]["effective_dates"], 7)
        self.assertEqual(
            set(audit["background_load"]["summary"]),
            {"fixed", "rolling_30m", "rolling_60m", "hierarchical"})
        self.assertGreaterEqual(audit["ab_coactivity"]["n_pairs"], 1)
        self.assertFalse(audit["ab_topology"]["blocks_task_aggregation"])
        self.assertEqual(audit["ab_topology"]["n_unresolved"], 0)
        self.assertEqual(audit["ab_topology"]["n_pairs"], 22)
        self.assertEqual(
            audit["solar_structure"]["status"],
            "blocked_until_available_generation_inputs",
        )
        self.assertEqual(
            audit["bcr_outliers"]["above_cap_count"], 129)
        self.assertEqual(
            audit["representative_case"]["reference_mode"],
            "current_integrated_with_v_soc_crosscheck")
        self.assertEqual(
            audit["representative_case"]["derived_inputs"]["task_count"], 15)

    def test_manifest_hashes_match_declared_stage2_inputs(self):
        for folder in ("SCS-01-01", "YJ-3D"):
            root = BASE / "satellites" / folder
            manifest = json.loads(
                (root / "power_model" / "stage2_manifest.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(manifest["satellite_id"], folder)
            self.assertGreater(len(manifest["inputs"]), 0)
            self.assertGreater(len(manifest["code"]), 0)
            if folder == "YJ-3D":
                self.assertEqual(
                    manifest["specialized_audits"]["soc_saturation"]
                    ["observed_non_saturated"]["n_windows"],
                    3,
                )
                self.assertEqual(
                    manifest["specialized_audits"]["bcr_outliers"]
                    ["above_cap_count"],
                    129,
                )
            self.assertEqual(
                manifest["representative_case"]["satellite_id"], folder)
            for item in manifest["inputs"] + manifest["code"]:
                path = Path(item["path"])
                if not path.is_absolute():
                    path = root / path
                self.assertTrue(path.is_file(), path)
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    item["sha256"], path)


if __name__ == "__main__":
    unittest.main()
