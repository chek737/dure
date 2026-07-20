import unittest

from dure.capacity import build_capacity_plan
from dure.cli import _profiles_from_inventory
from dure.models import InstalledModelProfile, WorkloadProfile

from .helpers import profile


def inventory(profiles):
    return {
        "generated_at": "now",
        "nodes": [
            {
                "id": item.node_id,
                "hostname": item.hostname,
                "approved": True,
                "connectivity": "online",
                "profile": item.to_dict(),
            }
            for item in profiles
        ],
    }


class CapacityTests(unittest.TestCase):
    def test_balanced_layout_reacts_to_gpu_count(self):
        seven = [profile(f"gpu-{index}", address=f"192.168.0.{index}") for index in range(7)]

        expected = {
            7: [3, 3, 1],
            6: [3, 3],
            3: [3],
            2: [1, 1],
            1: [1],
        }
        revisions = set()
        for count, layout in expected.items():
            plan = build_capacity_plan(inventory(seven[:count]), objective="balanced")
            self.assertEqual(plan["summary"]["available_gpu_nodes"], count)
            self.assertEqual(
                [item["pipeline_parallel_size"] for item in plan["recommended_deployments"]],
                layout,
            )
            revisions.add(plan["capacity_revision"])
        self.assertEqual(len(revisions), len(expected))

    def test_quality_uses_235b_only_at_seven_gpus(self):
        nodes = [profile(f"gpu-{index}", address=f"192.168.1.{index}") for index in range(7)]

        plan = build_capacity_plan(inventory(nodes), objective="quality")

        self.assertEqual(plan["recommended_deployments"][0]["model_id"], "qwen3-235b-a22b-awq")
        self.assertEqual(plan["recommended_deployments"][0]["pipeline_parallel_size"], 7)

    def test_occupied_unmanaged_gpu_is_preserved(self):
        node = profile("busy", gpu_memory_used_mib=21000)
        node.workloads.append(
            WorkloadProfile(
                name="ray::worker",
                runtime="ray",
                image="",
                status="running",
                source="nvidia-smi",
                gpu_memory_mib=21000,
            )
        )

        plan = build_capacity_plan(inventory([node]))

        self.assertEqual(plan["summary"]["occupied_gpu_nodes"], 1)
        self.assertEqual(plan["recommended_deployments"], [])
        self.assertTrue(any("preserved" in item for item in plan["warnings"]))

    def test_reuse_first_prefers_complete_72b_cache(self):
        nodes = [profile(f"gpu-{index}", address=f"192.168.2.{index}") for index in range(3)]
        nodes[1].installed_models.append(
            InstalledModelProfile(
                source="huggingface-cache",
                model_id="Qwen/Qwen2.5-72B-Instruct-AWQ",
                path="/cache/qwen72",
                complete=True,
            )
        )

        plan = build_capacity_plan(inventory(nodes), objective="reuse-first")

        deployment = plan["recommended_deployments"][0]
        self.assertEqual(deployment["model_id"], "qwen2.5-72b-awq")
        self.assertIn("gpu-1", deployment["cached_node_ids"])

    def test_reserve_gpu_keeps_spare_out_of_layout(self):
        nodes = [profile(f"gpu-{index}", address=f"192.168.3.{index}") for index in range(4)]

        plan = build_capacity_plan(inventory(nodes), reserve_gpus=1)

        self.assertEqual(plan["summary"]["reserved_gpu_nodes"], 1)
        self.assertEqual(len(plan["recommended_deployments"]), 1)
        self.assertEqual(plan["recommended_deployments"][0]["pipeline_parallel_size"], 3)

    def test_central_deployment_profiles_require_dynamic_gpu_usage(self):
        node = profile("gpu-new")
        snapshot = inventory([node])
        self.assertEqual(_profiles_from_inventory(snapshot, ["gpu-new"])[0].node_id, "gpu-new")

        old = node.to_dict()
        old.pop("profile_schema_version")
        old["gpus"][0].pop("memory_used_mib")
        snapshot["nodes"][0]["profile"] = old
        with self.assertRaisesRegex(ValueError, "lacks dynamic GPU usage"):
            _profiles_from_inventory(snapshot, ["gpu-new"])

    def test_stale_profile_is_excluded_and_cannot_create_deployment(self):
        node = profile("gpu-stale")
        snapshot = inventory([node])
        snapshot["nodes"][0]["profile_updated_at"] = "2020-01-01T00:00:00+00:00"

        plan = build_capacity_plan(snapshot)

        self.assertEqual(plan["summary"]["stale_gpu_nodes"], 1)
        self.assertEqual(plan["recommended_deployments"], [])
        with self.assertRaisesRegex(ValueError, "profile is stale"):
            _profiles_from_inventory(snapshot, ["gpu-stale"])


if __name__ == "__main__":
    unittest.main()
