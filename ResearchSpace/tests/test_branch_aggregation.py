"""Tests for physical-unit branch aggregation."""
import unittest
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from branch_aggregation import (
    BranchAggregationError,
    aggregate_branch_power_frame,
    physical_component_config,
)
from physical_unit_model import build_physical_unit_artifacts


class BranchAggregationTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame({
            "t": pd.date_range("2026-01-01", periods=4, freq="min"),
            "P_pl1_a_W": [10.0, np.nan, 20.0, 500.0],
            "P_pl1_b_W": [5.0, 7.0, np.nan, 4.0],
            "P_heater_W": [1.0, 2.0, 3.0, 4.0],
        })
        self.models = {"models": {
            "pl1_a": {"outlier_filter": {"upper_limit_W": 100.0}},
            "pl1_b": {"outlier_filter": {"upper_limit_W": 100.0}},
        }}
        self.topology = {
            "source_document": "PDU-SLD",
            "default_aggregation_mode": "sum",
            "pairs": [{
                "pl_channel": 1,
                "physical_unit": "pl1",
                "name": "光纤陀螺",
                "members": ["pl1_a", "pl1_b"],
                "aggregation_mode": "sum",
            }],
        }
        self.config = {
            "satellite_id": "TEST",
            "components": [
                {"id": "pl1_a", "name": "光纤陀螺 · A", "role": "load",
                 "plugin": "vi", "enabled": True, "max_power_W": 100.0},
                {"id": "pl1_b", "name": "光纤陀螺 · B", "role": "load",
                 "plugin": "vi", "enabled": True, "max_power_W": 100.0},
                {"id": "heater", "name": "加热", "role": "load",
                 "plugin": "bus_current", "enabled": True},
            ],
        }

    def test_sum_requires_both_branches_and_never_fills_missing_with_zero(self):
        result, audit = aggregate_branch_power_frame(
            self.frame, self.topology, self.models)

        self.assertEqual(result.loc[0, "P_pl1_W"], 15.0)
        self.assertTrue(np.isnan(result.loc[1, "P_pl1_W"]))
        self.assertTrue(np.isnan(result.loc[2, "P_pl1_W"]))
        self.assertTrue(np.isnan(result.loc[3, "P_pl1_W"]))
        self.assertEqual(result.loc[0, "Q_pl1_valid_branches"], 2)
        self.assertEqual(audit["pairs"][0]["aggregation_mode"], "sum")

    def test_max_mode_can_use_one_valid_branch(self):
        self.topology["pairs"][0]["aggregation_mode"] = "max"
        result, _ = aggregate_branch_power_frame(
            self.frame, self.topology, self.models)

        self.assertEqual(result.loc[0, "P_pl1_W"], 10.0)
        self.assertEqual(result.loc[1, "P_pl1_W"], 7.0)
        self.assertEqual(result.loc[2, "P_pl1_W"], 20.0)

    def test_physical_config_deduplicates_branches_and_keeps_heater(self):
        result = physical_component_config(self.config, self.topology)

        self.assertEqual(
            [item["id"] for item in result["components"]],
            ["pl1", "heater"],
        )
        self.assertEqual(
            result["components"][0]["physical_branches"], ["pl1_a", "pl1_b"])
        self.assertEqual(result["components"][0]["max_power_W"], 200.0)

    def test_select_mode_requires_an_explicit_member(self):
        self.topology["pairs"][0]["aggregation_mode"] = "select"
        with self.assertRaisesRegex(BranchAggregationError, "select_unit"):
            aggregate_branch_power_frame(
                self.frame, self.topology, self.models)

    def test_builder_outputs_one_model_per_physical_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            times = pd.date_range("2026-01-01", periods=60, freq="min")
            frame = pd.DataFrame({
                "t": times,
                "P_pl1_a_W": np.full(60, 10.0),
                "P_pl1_b_W": np.full(60, 5.0),
                "P_heater_W": np.full(60, 2.0),
            })
            branch_data = root / "branches.csv"
            branch_config_path = root / "branches.json"
            branch_models_path = root / "branch_models.json"
            topology_path = root / "topology.json"
            frame.to_csv(branch_data, index=False, encoding="utf-8-sig")
            branch_config = dict(self.config)
            branch_config["satellite_id"] = "TEST"
            branch_config_path.write_text(
                json.dumps(branch_config, ensure_ascii=False),
                encoding="utf-8",
            )
            branch_models_path.write_text(json.dumps({
                "satellite_id": "TEST",
                "models": self.models["models"],
            }, ensure_ascii=False), encoding="utf-8")
            topology_path.write_text(json.dumps({
                "satellite_id": "TEST",
                "status": "approved",
                **self.topology,
            }, ensure_ascii=False), encoding="utf-8")

            outputs = build_physical_unit_artifacts(
                branch_data,
                branch_config_path,
                branch_models_path,
                topology_path,
                root / "out",
                satellite_id="TEST",
            )

            model = json.loads(
                outputs["model"].read_text(encoding="utf-8"))
            self.assertEqual(set(model["models"]), {"pl1", "heater"})
            physical = pd.read_csv(
                outputs["physical_data"], encoding="utf-8-sig")
            self.assertEqual(set(physical.loc[0, [
                "P_pl1_W", "P_heater_W"]]), {15.0, 2.0})


if __name__ == "__main__":
    unittest.main()
