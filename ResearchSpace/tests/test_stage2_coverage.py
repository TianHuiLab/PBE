"""Coverage audit for extended telemetry windows."""
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from stage2_coverage import build_coverage


class Stage2CoverageTests(unittest.TestCase):
    def test_reports_seasons_phases_and_temperature_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "result.csv"
            minute_path = root / "minute.csv"
            pd.DataFrame({
                "t": [
                    "2026-09-30 23:59:00",
                    "2026-10-01 00:00:00",
                    "2026-10-01 00:01:00",
                ],
                "phase": ["日照", "日照", "地影"],
            }).to_csv(result_path, index=False)
            pd.DataFrame({
                "t": [
                    "2026-09-30 23:59:00",
                    "2026-10-01 00:00:00",
                    "2026-10-01 00:01:00",
                ],
                "param_code": ["TMD080"] * 3,
                "param_name": ["电池管理模块温度"] * 3,
                "avg": [20.0, 22.0, 24.0],
            }).to_csv(minute_path, index=False)

            report = build_coverage(
                result_path,
                minute_path,
                temperature_codes=["TMD080"],
            )

        self.assertEqual(report["observed_minutes"], 3)
        self.assertEqual(report["phase_counts"], {"日照": 2, "地影": 1})
        self.assertEqual(report["season_counts"]["SON"], 3)
        self.assertEqual(report["phase_transition_count"], 1)
        temperature = report["temperature_codes"]["TMD080"]
        self.assertEqual(temperature["valid_minutes"], 3)
        self.assertAlmostEqual(temperature["raw_median"], 22.0)
        self.assertAlmostEqual(temperature["raw_min"], 20.0)
        self.assertAlmostEqual(temperature["raw_max"], 24.0)


if __name__ == "__main__":
    unittest.main()
