import unittest

from dure.models import InstalledModelProfile
from dure.planner import build_plan, classify_node, recommend_local_model

from .helpers import profile


class PlannerTests(unittest.TestCase):
    def test_cpu_node_is_utility(self):
        node = profile("cpu-1", gpu_memory_mib=None)
        node.cpu_count = 4
        node.memory_mib = 3800

        role, capabilities = classify_node(node)

        self.assertEqual(role, "utility")
        self.assertIn("utility-controller", capabilities)
        self.assertIn("artifact-cache", capabilities)
        self.assertNotIn("gpu-worker", capabilities)
        self.assertIsNone(recommend_local_model(node))

    def test_three_24g_gpus_select_72b_pipeline(self):
        nodes = [
            profile("camp-7", address="192.168.0.228"),
            profile("camp-9", address="192.168.0.83"),
            profile("camp-8", address="192.168.0.84"),
        ]

        plan = build_plan(nodes)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.model.model_id, "qwen2.5-72b-awq")
        self.assertEqual(plan.pipeline_parallel_size, 3)
        self.assertEqual(plan.tensor_parallel_size, 1)
        self.assertEqual(plan.world_size, 3)
        self.assertEqual(plan.ray_head_address, "192.168.0.228:6379")
        self.assertEqual(
            [(item.layer_start, item.layer_end) for item in plan.assignments],
            [(0, 26), (27, 53), (54, 79)],
        )

    def test_non_contiguous_gpu_index_is_supported(self):
        node = profile("gpu-2", gpu_index=2)
        plan = build_plan([node], model_id="qwen2.5-32b-awq")
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.assignments[0].gpu_index, 2)

    def test_72b_requires_three_eligible_gpus(self):
        with self.assertRaisesRegex(ValueError, "requires 3"):
            build_plan([profile("one")], model_id="qwen2.5-72b-awq")

    def test_duplicate_node_profiles_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate node"):
            build_plan([profile("same"), profile("same")])

    def test_235b_requires_seven_free_gpu_nodes(self):
        nodes = [profile(f"gpu-{index}", address=f"192.168.1.{index}") for index in range(7)]

        plan = build_plan(nodes, model_id="qwen3-235b-a22b-awq")

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.pipeline_parallel_size, 7)
        self.assertEqual(
            [(item.layer_start, item.layer_end) for item in plan.assignments],
            [(0, 13), (14, 27), (28, 41), (42, 54), (55, 67), (68, 80), (81, 93)],
        )
        with self.assertRaisesRegex(ValueError, "requires 7"):
            build_plan(nodes[:6], model_id="qwen3-235b-a22b-awq")

    def test_busy_gpu_is_not_selected(self):
        busy = profile("busy", gpu_memory_used_mib=12000)
        self.assertIsNone(build_plan([busy]))

    def test_complete_shared_model_path_is_reused(self):
        nodes = [profile(f"gpu-{index}", address=f"192.168.2.{index}") for index in range(3)]
        for node in nodes:
            node.installed_models.append(
                InstalledModelProfile(
                    source="dure",
                    model_id="Qwen/Qwen2.5-72B-Instruct-AWQ",
                    path="/models/qwen72",
                    quantization="awq",
                    complete=True,
                )
            )

        plan = build_plan(nodes, model_id="qwen2.5-72b-awq")

        assert plan is not None
        self.assertEqual(plan.model_path, "/models/qwen72")


if __name__ == "__main__":
    unittest.main()
