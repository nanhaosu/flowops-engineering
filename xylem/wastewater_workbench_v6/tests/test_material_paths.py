import unittest
from xylem.wastewater_workbench_v6 import build_task_pack

class MaterialPathsTests(unittest.TestCase):
    def test_material_source_accepts_workbench_and_workspace_relative_paths(self):
        expected=build_task_pack.HERE/'artifacts/comparison/v6_r2'
        self.assertEqual(build_task_pack.source_path('artifacts/comparison/v6_r2'),expected)
        self.assertEqual(build_task_pack.source_path('xylem/wastewater_workbench_v6/artifacts/comparison/v6_r2'),expected)
