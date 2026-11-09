"""云尖3D星支路摘要覆盖保护。"""
import unittest

from yj3d_components import require_nonempty_component_rows


class YJ3DComponentSummaryTests(unittest.TestCase):
    def test_empty_query_is_rejected(self):
        components = {
            'heater_battery_main': {'valid_source_rows': 0},
            'heater_star_a_main': {'valid_source_rows': 0},
        }
        with self.assertRaisesRegex(ValueError, '拒绝用全零摘要覆盖'):
            require_nonempty_component_rows(components)

    def test_one_nonempty_channel_is_accepted(self):
        components = {
            'heater_battery_main': {'valid_source_rows': 1},
            'heater_star_a_main': {'valid_source_rows': 0},
        }
        require_nonempty_component_rows(components)


if __name__ == '__main__':
    unittest.main()
