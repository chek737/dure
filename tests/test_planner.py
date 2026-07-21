import unittest

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

    def test_new_gpu_expands_existing_pipeline_from_n_to_n_plus_one(self):
        three = [
            profile(f"gpu-{index}", address=f"192.168.1.{index + 10}")
            for index in range(3)
        ]
        before = build_plan(three, model_id="qwen2.5-72b-awq")
        after = build_plan(
            [*three, profile("gpu-3", address="192.168.1.13")],
            model_id="qwen2.5-72b-awq",
        )

        assert before is not None and after is not None
        self.assertEqual(before.pipeline_parallel_size, 3)
        self.assertEqual(after.pipeline_parallel_size, 4)
        self.assertEqual(len(after.assignments), 4)
        self.assertEqual(
            [(item.layer_start, item.layer_end) for item in after.assignments],
            [(0, 19), (20, 39), (40, 59), (60, 79)],
        )

    def test_two_eligible_gpus_share_smaller_auto_model(self):
        plan = build_plan([profile("gpu-b"), profile("gpu-a")])

        assert plan is not None
        self.assertEqual(plan.model.model_id, "qwen2.5-32b-awq")
        self.assertEqual(plan.pipeline_parallel_size, 2)
        self.assertEqual([item.node_id for item in plan.assignments], ["gpu-a", "gpu-b"])


if __name__ == "__main__":
    unittest.main()
