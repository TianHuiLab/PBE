"""Tests for the shared power/telemetry outlier filter."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from power_outlier_filter import filter_power_values


class PowerOutlierFilterTests(unittest.TestCase):
    def test_configured_upper_limit_preserves_index_and_real_rare_state(self):
        values = pd.Series(
            [1.0, 1.0, 12.0, 500_000.0, np.nan, -1.0],
            index=["a", "b", "c", "d", "e", "f"],
            dtype=float,
        )

        cleaned, metadata = filter_power_values(
            values, upper_limit_W=20.0)

        self.assertEqual(list(cleaned.index), list(values.index))
        self.assertEqual(cleaned.loc["c"], 12.0)
        self.assertTrue(np.isnan(cleaned.loc["d"]))
        self.assertTrue(np.isnan(cleaned.loc["e"]))
        self.assertTrue(np.isnan(cleaned.loc["f"]))
        self.assertEqual(metadata["method"], "configured_max_power")
        self.assertEqual(metadata["outlier_samples"], 1)
        self.assertEqual(metadata["invalid_samples"], 2)
        self.assertEqual(metadata["raw_max_W"], 500_000.0)

    def test_robust_tail_filters_extreme_spike_without_config_limit(self):
        values = pd.Series(np.full(500, 3.0))
        values.iloc[100] = 1_000_000.0

        cleaned, metadata = filter_power_values(values)

        self.assertEqual(int(cleaned.notna().sum()), 499)
        self.assertTrue(np.isnan(cleaned.iloc[100]))
        self.assertEqual(metadata["method"], "robust_extreme_tail")
        self.assertEqual(metadata["outlier_samples"], 1)

    def test_unbounded_all_zero_series_does_not_invent_a_filter(self):
        cleaned, metadata = filter_power_values(pd.Series(np.zeros(10)))

        self.assertTrue(cleaned.notna().all())
        self.assertEqual(metadata["method"], "unbounded_no_positive_tail")
        self.assertEqual(metadata["outlier_samples"], 0)


if __name__ == "__main__":
    unittest.main()
