"""Isolated stage-two test-set exports for each satellite."""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest

try:
    from ._data import require_local_data
except ImportError:
    from _data import require_local_data
from stage2_testset import write_testset


BASE = Path(__file__).resolve().parents[1]


@require_local_data(
    BASE / "satellites" / "SCS-01-01" / "out" / "energy_v2_result.csv",
    BASE / "satellites" / "YJ-3D" / "data_power_minute.csv",
)
class Stage2TestsetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.temp_root = Path(cls.temp.name)
        cls.scs_paths = write_testset(
            BASE / "satellites" / "SCS-01-01",
            output_dir=cls.temp_root / "SCS-01-01" / "stage2_testset")
        cls.yj_paths = write_testset(
            BASE / "satellites" / "YJ-3D",
            output_dir=cls.temp_root / "YJ-3D" / "stage2_testset")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_satellites_export_to_separate_directories_and_case_counts(self):
        self.assertNotEqual(self.scs_paths["root"], self.yj_paths["root"])
        self.assertEqual(
            self.scs_paths["root"].parent.name, "SCS-01-01")
        self.assertEqual(self.yj_paths["root"].parent.name, "YJ-3D")
        scs_manifest = json.loads(
            self.scs_paths["manifest"].read_text(encoding="utf-8"))
        yj_manifest = json.loads(
            self.yj_paths["manifest"].read_text(encoding="utf-8"))
        self.assertEqual(len(scs_manifest["cases"]), 25)
        self.assertEqual(len(yj_manifest["cases"]), 10)
        self.assertEqual(scs_manifest["readme"], "README.md")
        self.assertEqual(yj_manifest["readme"], "README.md")
        self.assertTrue((self.scs_paths["root"] / "README.md").is_file())
        self.assertTrue((self.yj_paths["root"] / "README.md").is_file())

    def test_inputs_have_only_simplified_prediction_fields(self):
        for paths in (self.scs_paths, self.yj_paths):
            manifest = json.loads(
                paths["manifest"].read_text(encoding="utf-8"))
            for item in manifest["cases"]:
                case_dir = paths["root"] / item["path"]
                request = json.loads(
                    (case_dir / "input.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    set(request["prediction"]),
                    {"end", "step_seconds", "phase_segments", "tasks"})
                self.assertNotIn(
                    "solar_segments", request["prediction"])
                self.assertNotIn(
                    "background_load_segments", request["prediction"])

    def test_labels_are_observed_only_and_separate_from_predictions(self):
        for paths in (self.scs_paths, self.yj_paths):
            rows = [
                json.loads(line)
                for line in paths["labels"].read_text(
                    encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(rows)
            for row in rows:
                self.assertIn("observed", row)
                self.assertNotIn("predicted_target_soc_pct", row["observed"])
                self.assertIn("soc_reference", row["label_quality"])
                self.assertIn("usable_for_absolute_soc", row["label_quality"])

    def test_scs_and_yj_labels_preserve_reference_semantics(self):
        scs_manifest = json.loads(
            self.scs_paths["manifest"].read_text(encoding="utf-8"))
        yj_manifest = json.loads(
            self.yj_paths["manifest"].read_text(encoding="utf-8"))
        scs_case = self.scs_paths["root"] / scs_manifest["cases"][0]["path"]
        yj_case = self.yj_paths["root"] / yj_manifest["cases"][0]["path"]
        scs_label = json.loads(
            (scs_case / "label.json").read_text(encoding="utf-8"))
        yj_label = json.loads(
            (yj_case / "label.json").read_text(encoding="utf-8"))
        self.assertEqual(
            scs_label["label_quality"]["soc_reference"],
            "v_soc_minute_mapping")
        self.assertEqual(
            yj_label["label_quality"]["soc_reference"],
            "current_integrated_with_v_soc_endpoint_crosscheck")
        self.assertIn("v_soc_endpoint_pct", yj_label["observed"])

    def test_yj_out_of_range_windows_are_not_primary_labels(self):
        cases = self.yj_paths["root"] / "cases"
        for case_id in ("W07", "W08", "W09"):
            label = json.loads(
                (cases / case_id / "label.json").read_text(encoding="utf-8"))
            prediction = json.loads(
                (cases / case_id / "prediction.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(
                label["label_quality"]["soc_qc_status"], "fail")
            self.assertIsNone(label["observed"]["target_soc_pct"])
            self.assertIsNotNone(
                label["observed"]["target_soc_pct_raw"])
            self.assertIsNone(
                prediction["evaluation"]["target_soc_error_pp"])
            self.assertIsNotNone(
                prediction["evaluation"]["target_soc_error_raw_pp"])

        label = json.loads(
            (cases / "W08" / "label.json").read_text(encoding="utf-8"))
        self.assertGreater(
            label["label_quality"]["bcr_above_cap_count"], 0)
        self.assertGreater(
            label["observed"]["solar_energy_raw_kWh"],
            label["observed"]["solar_energy_kWh"])

    def test_manifest_and_case_files_include_sha256(self):
        for paths in (self.scs_paths, self.yj_paths):
            manifest = json.loads(
                paths["manifest"].read_text(encoding="utf-8"))
            self.assertIn("model_support", manifest)
            for item in manifest["cases"]:
                self.assertIn("sha256", item)
                for relative, expected in item["sha256"].items():
                    path = paths["root"] / item["path"] / relative
                    self.assertTrue(path.is_file(), path)
                    self.assertEqual(
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        expected,
                        path)


if __name__ == "__main__":
    unittest.main()
