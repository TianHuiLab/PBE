"""Regression checks for the rendered YJ-3D phase-two report section."""
import json
import unittest
from pathlib import Path

from gen_charts import phase2_yj_section


BASE = Path(__file__).resolve().parents[1]
PROFILE = BASE / "satellites" / "YJ-3D"


class YJ3DHTMLTests(unittest.TestCase):
    def test_latest_physical_unit_results_are_rendered(self):
        html = phase2_yj_section(PROFILE)
        audit = json.loads(
            (PROFILE / "power_model" / "stage2_audit.json").read_text(
                encoding="utf-8"))
        topology = json.loads(
            (PROFILE / "power_model" / "ab_topology.json").read_text(
                encoding="utf-8"))
        physical = json.loads(
            (PROFILE / "power_model" / "physical_unit_models.json").read_text(
                encoding="utf-8"))

        self.assertIn("物理单机模型", html)
        self.assertIn("A/B双支路与物理单机建模", html)
        self.assertIn(str(len(physical["models"])), html)
        self.assertEqual(topology["status"], "approved")
        self.assertIn(
            f'{audit["metrics"]["load_energy_abs_relative_error_pct"]["window_equal"]["mean"]:.2f}%',
            html,
        )
        self.assertNotIn("<tbody>['", html)
        self.assertNotIn("A/B 通道物理拓扑与全部负载覆盖仍需单线图核准", html)


if __name__ == "__main__":
    unittest.main()
