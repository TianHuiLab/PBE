"""Tests for the YJ-3D forecast adaptation."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yj3d_power_forecast import (build_forecast_components,
                                 YJ3DHistoricalSnapshotProvider,
                                 YJ3DClickHouseSnapshotProvider,
                                 estimate_bus_transfer_efficiency,
                                 historical_task_state_features,
                                 prepare_solar_target_frame,
                                 unit_rows_to_component_minute,
                                 voltage_soc_curve_document)


class YJ3DSolarTargetTests(unittest.TestCase):
    def test_solar_target_is_summed_bcr_output_power(self):
        frame = pd.DataFrame({
            "t": pd.to_datetime(["2026-07-24 01:00", "2026-07-24 01:01"]),
            "phase": ["日照", "地影"],
            "bus_measure_W_mean": [100.0, 120.0],
            "battery_signed_trial_W_mean": [-20.0, 30.0],
            "bcr_out_W": [125.0, 0.0],
        })
        result = prepare_solar_target_frame(frame)
        self.assertEqual(result["P_load"].tolist(), [100.0, 120.0])
        self.assertEqual(result["P_solar_target"].tolist(), [125.0, 0.0])
        self.assertEqual(result["P_solar_diag"].tolist(), [120.0, 90.0])
        self.assertEqual(result["P_charge_est"].tolist(), [0.0, 0.0])
        self.assertEqual(result["P_solar"].tolist(), [125.0, 0.0])
        self.assertEqual(result["phase"].tolist(), ["日照", "地影"])

    def test_estimates_empirical_bus_transfer_efficiency_from_sunlight(self):
        frame = pd.DataFrame({
            "t": pd.to_datetime([
                "2026-07-24 01:00", "2026-07-24 01:01",
                "2026-07-24 01:02"]),
            "phase": ["日照", "日照", "地影"],
            "P_solar_target": [100.0, 100.0, 9999.0],
            "P_solar_diag": [90.0, 80.0, 9999.0],
        })
        estimate = estimate_bus_transfer_efficiency(
            frame, min_samples=2)
        self.assertAlmostEqual(estimate["efficiency"], 0.85)
        self.assertEqual(estimate["n_samples"], 2)
        self.assertEqual(estimate["diagnostic_formula"],
                         "P_bus - P_battery_signed")


class YJ3DComponentTests(unittest.TestCase):
    def test_historical_task_state_features_use_active_idle_levels(self):
        unit_frame = pd.DataFrame({
            "t": pd.to_datetime([
                "2026-07-24 01:00", "2026-07-24 01:01",
                "2026-07-24 01:02",
            ]),
            "P_a_W": [12.0, 1.0, 500_000.0],
            "P_b_W": [2.0, 2.0, 2.0],
            "P_constant_W": [5.0, 6.0, 5.5],
        })
        unit_models = {"models": {
            "a": {
                "active_threshold_W": 5.0,
                "active_power_W": 10.0,
                "low_power_W": 0.5,
                "outlier_filter": {"upper_limit_W": 20.0},
            },
            "b": {
                "active_threshold_W": 5.0,
                "active_power_W": 8.0,
                "low_power_W": 1.5,
            },
            "constant": {
                "active_threshold_W": None,
                "active_power_W": 5.0,
                "low_power_W": None,
            },
        }}
        result = historical_task_state_features(unit_frame, unit_models)
        self.assertEqual(
            result["active_unit_count"].tolist(), [1.0, 0.0, 0.0])
        self.assertEqual(
            result["task_unit_power_W"].tolist(), [11.5, 2.0, 1.5])

    def test_a_and_b_branches_remain_separate_source_measurements(self):
        inventory = {
            "units": [{
                "pl_channel": 16,
                "name": "红外相机",
                "banks": {
                    "A": {
                        "voltage": {
                            "code": "TMPA024",
                            "workbook_range": "11V~13V或0V~0.5V",
                        },
                        "current": {
                            "code": "TMPA023",
                            "workbook_range": "0A~1A",
                        },
                    },
                    "B": {
                        "voltage": {
                            "code": "TMPB024",
                            "workbook_range": "11V~13V或0V~0.5V",
                        },
                        "current": {
                            "code": "TMPB023",
                            "workbook_range": "0A~1A",
                        },
                    },
                },
            }]
        }
        heater_config = {
            "components": [{
                "id": "heater_main",
                "name": "加热主路",
                "role": "load",
                "plugin": "bus_current",
                "current_code": "TMEA130",
                "max_current_A": 1.0,
                "enabled": True,
            }]
        }
        config = build_forecast_components(
            inventory, heater_config, bus_voltage_max_V=29.2)
        ids = [item["id"] for item in config["components"]]
        self.assertEqual(ids, ["pl16_a", "pl16_b", "heater_main"])
        self.assertEqual(config["components"][0]["voltage_code"], "TMPA024")
        self.assertEqual(config["components"][0]["max_voltage_V"], 13.0)
        self.assertEqual(config["components"][0]["max_current_A"], 1.0)
        self.assertEqual(config["components"][0]["max_power_W"], 13.0)
        self.assertEqual(config["components"][1]["voltage_code"], "TMPB024")
        self.assertEqual(config["components"][2]["plugin"], "bus_current")
        self.assertEqual(config["components"][2]["max_power_W"], 29.2)

    def test_minute_rows_become_separate_component_power_columns(self):
        components = {
            "components": [
                {"id": "pl16_a", "role": "load", "plugin": "vi",
                 "voltage_code": "TMPA024", "current_code": "TMPA023",
                 "max_voltage_V": 13.0, "max_current_A": 1.0,
                 "max_power_W": 13.0},
                {"id": "pl16_b", "role": "load", "plugin": "vi",
                 "voltage_code": "TMPB024", "current_code": "TMPB023"},
                {"id": "heater_main", "role": "load", "plugin": "bus_current",
                 "current_code": "TMEA130"},
            ]
        }
        rows = [{
            "minute_utc": "2026-07-24 01:00:00",
            "bus_voltage": 20.0,
            "TMPA024": 10.0, "TMPA023": 1.0,
            "TMPB024": 10.0, "TMPB023": 2.0,
            "TMEA130": .5,
        }, {
            "minute_utc": "2026-07-24 01:01:00",
            "bus_voltage": 20.0,
            "TMPA024": 100_000.0, "TMPA023": 1.0,
            "TMPB024": 10.0, "TMPB023": 2.0,
            "TMEA130": .5,
        }]
        frame = unit_rows_to_component_minute(rows, components)
        self.assertEqual(frame.loc[0, "P_pl16_a_W"], 10.0)
        self.assertEqual(frame.loc[0, "P_pl16_b_W"], 20.0)
        self.assertEqual(frame.loc[0, "P_heater_main_W"], 10.0)
        self.assertTrue(np.isnan(frame.loc[1, "P_pl16_a_W"]))
        self.assertEqual(
            frame.loc[0, "t"], pd.Timestamp("2026-07-24 09:00:00"))


class YJ3DVSocTests(unittest.TestCase):
    def test_curve_converts_percent_to_fraction(self):
        calibration = {
            "voltage_V": np.asarray([21.0, 25.0, 28.0]),
            "soc_pct": np.asarray([10.0, 50.0, 100.0]),
            "current_A": 7.84,
            "capacity_Ah": 37.69,
            "points": [
                {"voltage_V": 21.0, "soc_pct": 10.0},
                {"voltage_V": 25.0, "soc_pct": 50.0},
                {"voltage_V": 28.0, "soc_pct": 100.0},
            ],
        }
        document = voltage_soc_curve_document(calibration)
        self.assertEqual(document["points"][0], [21.0, .1])
        self.assertEqual(document["points"][-1], [28.0, 1.0])
        self.assertEqual(document["design"]["rated_capacity_Ah"], 37.69)


class YJ3DSnapshotTests(unittest.TestCase):
    def test_snapshot_uses_exact_bj_minute_and_task_unit_power(self):
        power = pd.DataFrame({
            "t": pd.to_datetime(["2026-07-24 21:03"]),
            "phase": ["地影"],
            "bus_voltage_mean": [27.95],
            "bus_current_mean": [7.5],
            "bus_measure_W_mean": [209.625],
            "battery_voltage_mean": [27.95],
        })
        unit = pd.DataFrame({
            "t": pd.to_datetime(["2026-07-24 21:03"]),
            "P_pl16_a_W": [12.0],
        })
        provider = YJ3DHistoricalSnapshotProvider(power, unit)
        snapshot = provider.snapshot(
            pd.Timestamp("2026-07-24T21:03:00+08:00"), ("pl16_a",))
        self.assertEqual(snapshot.total_load_W, 209.625)
        self.assertEqual(snapshot.battery_voltage_V, 27.95)
        self.assertEqual(snapshot.unit_power_W["pl16_a"], 12.0)
        self.assertEqual(snapshot.phase, "地影")

    def test_historical_snapshot_despikes_task_unit_power_with_model_limit(self):
        power = pd.DataFrame({
            "t": pd.to_datetime(["2026-07-24 21:03"]),
            "phase": ["地影"],
            "bus_voltage_mean": [27.95],
            "bus_current_mean": [7.5],
            "bus_measure_W_mean": [209.625],
            "battery_voltage_mean": [27.95],
        })
        unit = pd.DataFrame({
            "t": pd.date_range("2026-07-24 21:00", periods=5, freq="min"),
            "P_pl16_a_W": [12.0, 12.0, 500_000.0, 12.0, 12.0],
        })
        models = {"models": {
            "pl16_a": {"outlier_filter": {"upper_limit_W": 20.0}},
        }}
        provider = YJ3DHistoricalSnapshotProvider(
            power, unit, unit_models=models)

        snapshot = provider.snapshot(
            pd.Timestamp("2026-07-24T21:03:00+08:00"), ("pl16_a",))

        self.assertEqual(snapshot.unit_power_W["pl16_a"], 12.0)

    def test_clickhouse_snapshot_converts_bj_time_and_uses_task_current(self):
        profile = Path(__file__).resolve().parents[1] / "satellites" / "YJ-3D" / "config"
        calls = []

        def fake_query(sql, params=None, **kwargs):
            calls.append((sql, params))
            return [{
                "minute_utc": "2026-07-24 13:03:00",
                "bus_voltage": 27.95,
                "bus_current": 7.5,
                "battery_voltage": 27.95,
                "phase": "阴影区",
                "TMPA024": 10.0,
                "TMPA023": 0.9,
            }]

        provider = YJ3DClickHouseSnapshotProvider(profile, query_rows=fake_query)
        snapshot = provider.snapshot(
            pd.Timestamp("2026-07-24T21:03:00+08:00"), ("pl16_a",))
        self.assertEqual(calls[0][1]["start"], "2026-07-24 13:03:00")
        self.assertEqual(calls[0][1]["end"], "2026-07-24 13:04:00")
        self.assertAlmostEqual(snapshot.total_load_W, 27.95 * 7.5)
        self.assertAlmostEqual(snapshot.unit_power_W["pl16_a"], 9.0)
        self.assertEqual(snapshot.phase, "阴影区")

    def test_clickhouse_snapshot_sums_physical_unit_branches(self):
        profile = Path(__file__).resolve().parents[1] / "satellites" / "YJ-3D" / "config"

        def fake_query(sql, params=None, **kwargs):
            return [{
                "minute_utc": "2026-07-24 13:03:00",
                "bus_voltage": 27.95,
                "bus_current": 7.5,
                "battery_voltage": 27.95,
                "phase": "阴影区",
                "TMPA024": 10.0,
                "TMPA023": 0.9,
                "TMPB024": 10.0,
                "TMPB023": 0.8,
            }]

        provider = YJ3DClickHouseSnapshotProvider(
            profile, query_rows=fake_query)
        snapshot = provider.snapshot(
            pd.Timestamp("2026-07-24T21:03:00+08:00"), ("pl16",))

        self.assertAlmostEqual(snapshot.unit_power_W["pl16"], 17.0)
        self.assertEqual(snapshot.missing_unit_power_W, {})


if __name__ == "__main__":
    unittest.main()
