import json
import unittest
from unittest.mock import MagicMock, patch

from dure.readiness import ReadinessVerifier
from dure.readiness import validate_ray_snapshot
from dure.planner import build_plan
from tests.helpers import profile


def _response(status=200, body=b""):
    value = MagicMock()
    value.status = status
    value.read.return_value = body
    value.__enter__.return_value = value
    value.__exit__.return_value = False
    return value


class ReadinessTests(unittest.TestCase):
    def test_api_requires_health_and_a_served_model(self):
        model_body = json.dumps({"data": [{"id": "qwen-test"}]}).encode()
        with patch(
            "dure.readiness.urllib.request.urlopen",
            side_effect=[_response(), _response(body=model_body)],
        ):
            result = ReadinessVerifier().api("http://127.0.0.1:8000")

        self.assertTrue(result.ok, result.detail)
        self.assertIn("qwen-test", result.detail)

    def test_ray_snapshot_requires_exact_gpu_count_and_membership(self):
        plan = build_plan(
            [
                profile("a", address="192.168.0.1"),
                profile("b", address="192.168.0.2"),
                profile("c", address="192.168.0.3"),
            ],
            model_id="qwen2.5-72b-awq",
        )
        assert plan is not None
        exact = {
            "resources": {"GPU": 3},
            "nodes": [
                {"address": f"192.168.0.{index}", "alive": True, "gpu": 1}
                for index in range(1, 4)
            ],
        }
        self.assertTrue(validate_ray_snapshot(plan, exact).ok)

        extra = dict(exact, resources={"GPU": 4})
        self.assertFalse(validate_ray_snapshot(plan, extra).ok)

        wrong_member = dict(exact)
        wrong_member["nodes"] = exact["nodes"][:-1] + [
            {"address": "192.168.0.9", "alive": True, "gpu": 1}
        ]
        self.assertFalse(validate_ray_snapshot(plan, wrong_member).ok)

        exact["nodes"] = [
            dict(
                item,
                resources={
                    f"dure_node_uuid:{node_id}": 1,
                    "GPU": 1,
                },
            )
            for item, node_id in zip(exact["nodes"], ("a", "b", "c"))
        ]
        self.assertTrue(validate_ray_snapshot(plan, exact).ok)
        exact["nodes"][2]["resources"] = {"dure_node_uuid:unexpected": 1, "GPU": 1}
        self.assertFalse(validate_ray_snapshot(plan, exact).ok)


if __name__ == "__main__":
    unittest.main()
