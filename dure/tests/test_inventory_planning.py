from __future__ import annotations

import unittest

from dure.cli import _gpu_node_ids_from_inventory, _profiles_from_inventory

from .helpers import profile


def node(node_id: str, *, approved: bool = True, connectivity: str = "online") -> dict:
    return {
        "id": node_id,
        "hostname": node_id,
        "approved": approved,
        "connectivity": connectivity,
        "profile": profile(node_id).to_dict(),
    }


class InventoryPlanningTests(unittest.TestCase):
    def test_all_online_profiles_include_newly_approved_gpu(self):
        inventory = {"nodes": [node("gpu-b"), node("gpu-a"), node("pending", approved=False)]}

        profiles = _profiles_from_inventory(inventory)

        self.assertEqual([item.node_id for item in profiles], ["gpu-a", "gpu-b"])

    def test_explicit_pending_or_offline_node_is_rejected(self):
        inventory = {"nodes": [node("pending", approved=False), node("offline", connectivity="offline")]}

        with self.assertRaisesRegex(ValueError, "pending, offline"):
            _profiles_from_inventory(inventory, ["pending", "offline"])

    def test_unjoin_all_selects_only_approved_gpu_nodes(self):
        cpu = node("cpu")
        cpu["profile"] = profile("cpu", gpu_memory_mib=None).to_dict()
        inventory = {
            "nodes": [node("gpu-b"), cpu, node("pending", approved=False), node("gpu-a")]
        }
        self.assertEqual(_gpu_node_ids_from_inventory(inventory), ["gpu-a", "gpu-b"])

    def test_unjoin_one_rejects_cpu_or_pending_node(self):
        cpu = node("cpu")
        cpu["profile"] = profile("cpu", gpu_memory_mib=None).to_dict()
        inventory = {"nodes": [cpu, node("pending", approved=False)]}
        for node_id in ("cpu", "pending", "missing"):
            with self.subTest(node_id=node_id), self.assertRaisesRegex(ValueError, "non-GPU"):
                _gpu_node_ids_from_inventory(inventory, node_id)


if __name__ == "__main__":
    unittest.main()
