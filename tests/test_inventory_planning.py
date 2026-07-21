from __future__ import annotations

import unittest

from dure.cli import _profiles_from_inventory

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


if __name__ == "__main__":
    unittest.main()
