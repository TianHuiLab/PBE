"""Per-unit training-data export contracts for both satellites."""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest

import pandas as pd

try:
    from ._data import require_local_data
except ImportError:
    from _data import require_local_data
from unit_training_export import write_unit_training


BASE = Path(__file__).resolve().parents[1]


@require_local_data(
    BASE / "satellites" / "SCS-01-01" / "out" /
    "energy_v2_components.csv",
    BASE / "satellites" / "YJ-3D" / "power_model" /
    "physical_unit_power_minute.csv",
)
class UnitTrainingExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        temp_root = Path(cls.temp.name)
        cls.scs = write_unit_training(
            BASE / "satellites" / "SCS-01-01",
            output_dir=temp_root / "SCS-01-01" / "unit_training")
        cls.yj = write_unit_training(
            BASE / "satellites" / "YJ-3D",
            output_dir=temp_root / "YJ-3D" / "unit_training")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_units_are_exported_to_separate_satellite_directories(self):
        self.assertEqual(self.scs["root"].parent.name, "SCS-01-01")
        self.assertEqual(self.yj["root"].parent.name, "YJ-3D")
        self.assertNotEqual(self.scs["root"], self.yj["root"])
        scs_manifest = json.loads(
            self.scs["manifest"].read_text(encoding="utf-8"))
        yj_manifest = json.loads(
            self.yj["manifest"].read_text(encoding="utf-8"))
        self.assertEqual(scs_manifest["n_units"], 25)
        self.assertEqual(yj_manifest["n_units"], 32)

    def test_exported_samples_have_training_features_and_targets(self):
        for paths in (self.scs, self.yj):
            manifest = json.loads(
                paths["manifest"].read_text(encoding="utf-8"))
            unit = manifest["units"][0]
            train = pd.read_csv(
                paths["root"] / unit["path"] / "train.csv",
                encoding="utf-8-sig")
            expected = {
                "timestamp", "observed_power_W", "target_power_W",
                "valid_power", "active_label", "power_band",
                "episode_id", "elapsed_hours", "source_codes",
                "fit_role",
            }
            self.assertTrue(expected.issubset(train.columns))
            self.assertTrue(train["valid_power"].all())
            self.assertTrue(train["target_power_W"].notna().all())

    def test_yj_holdout_dates_are_excluded_from_train_and_present_in_eval(self):
        manifest = json.loads(
            self.yj["manifest"].read_text(encoding="utf-8"))
        unit = manifest["units"][0]
        base = self.yj["root"] / unit["path"]
        train = pd.read_csv(base / "train.csv", encoding="utf-8-sig")
        evaluation = pd.read_csv(base / "eval.csv", encoding="utf-8-sig")
        train_dates = set(pd.to_datetime(train["timestamp"]).dt.date.astype(str))
        eval_dates = set(pd.to_datetime(evaluation["timestamp"]).dt.date.astype(str))
        excluded = set(manifest["excluded_dates"])
        self.assertFalse(train_dates & excluded)
        self.assertTrue(eval_dates)
        self.assertTrue(eval_dates <= excluded)

    def test_scs_validation_dates_are_excluded_from_unit_training(self):
        manifest = json.loads(
            self.scs["manifest"].read_text(encoding="utf-8"))
        self.assertTrue(manifest["has_independent_unit_holdout"])
        self.assertEqual(len(manifest["excluded_dates"]), 16)
        readme = (self.scs["root"] / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("当前没有独立单机留出", readme)
        self.assertIn("整日留出日期未进入", readme)
        unit = manifest["units"][0]
        base = self.scs["root"] / unit["path"]
        train = pd.read_csv(
            base / "train.csv", encoding="utf-8-sig")
        evaluation = pd.read_csv(
            base / "eval.csv", encoding="utf-8-sig")
        train_dates = set(
            pd.to_datetime(train["timestamp"]).dt.date.astype(str))
        eval_dates = set(
            pd.to_datetime(evaluation["timestamp"]).dt.date.astype(str))
        excluded = set(manifest["excluded_dates"])
        self.assertFalse(train_dates & excluded)
        self.assertTrue(eval_dates)
        self.assertTrue(eval_dates <= excluded)
        overlap = pd.read_csv(
            base / "validation_overlap.csv", encoding="utf-8-sig")
        self.assertGreater(len(overlap), 0)
        self.assertFalse(overlap["used_in_model_fit"].any())

    def test_units_without_thresholds_do_not_invent_active_labels(self):
        manifest = json.loads(
            self.yj["manifest"].read_text(encoding="utf-8"))
        unit = next(
            item for item in manifest["units"]
            if not item["has_active_threshold"])
        train = pd.read_csv(
            self.yj["root"] / unit["path"] / "train.csv",
            encoding="utf-8-sig")
        self.assertTrue(train["active_label"].isna().all())
        self.assertTrue(train["episode_id"].isna().all())
        self.assertTrue(
            train["power_band"].eq("unclassified_single_band").all())

    def test_yj_physical_unit_metadata_keeps_branch_membership(self):
        support = json.loads(
            (self.yj["root"] / "model_support.json").read_text(
                encoding="utf-8"))
        unit = next(item for item in support["units"]
                    if item["unit_id"] == "pl16")
        self.assertEqual(unit["physical_branches"], ["pl16_a", "pl16_b"])
        self.assertEqual(unit["aggregation_mode"], "sum")

    def test_scs_export_preserves_separated_high_power_band(self):
        manifest = json.loads(
            self.scs["manifest"].read_text(encoding="utf-8"))
        unit = next(
            item for item in manifest["units"]
            if item["unit_id"] == "electric_propulsion_b")
        self.assertTrue(unit["has_high_power_band"])
        train = pd.read_csv(
            self.scs["root"] / unit["path"] / "train.csv",
            encoding="utf-8-sig")
        high = train[train["power_band"].eq("high")]
        self.assertGreater(len(high), 0)
        self.assertTrue(high["active_label"].all())
        metadata = json.loads(
            (self.scs["root"] / unit["path"] /
             "metadata.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(metadata["model"]["high_threshold_W"])
        self.assertIsNotNone(metadata["model"]["high_power_W"])

    def test_excluded_labels_are_not_backfilled_with_zero(self):
        manifest = json.loads(
            self.scs["manifest"].read_text(encoding="utf-8"))
        unit = next(
            item for item in manifest["units"]
            if item["counts"]["excluded_rows"] > 0)
        excluded = pd.read_csv(
            self.scs["root"] / unit["path"] / "excluded_samples.csv",
            encoding="utf-8-sig")
        self.assertFalse(excluded["valid_power"].any())
        self.assertTrue(excluded["target_power_W"].isna().all())
        self.assertTrue(excluded["excluded_reason"].notna().all())

    def test_manifest_hashes_every_unit_artifact(self):
        for paths in (self.scs, self.yj):
            manifest = json.loads(
                paths["manifest"].read_text(encoding="utf-8"))
            for unit in manifest["units"]:
                for relative, digest in unit["sha256"].items():
                    path = paths["root"] / unit["path"] / relative
                    self.assertTrue(path.is_file(), path)
                    self.assertEqual(
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        digest,
                        path)


if __name__ == "__main__":
    unittest.main()
