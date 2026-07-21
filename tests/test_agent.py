from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dure.agent import Agent, TaskExecutor
from dure.command import CommandResult
from dure.planner import build_plan
from dure.state import StateStore
from tests.helpers import profile


class AgentRunner:
    def __init__(self):
        self.calls = []

    def exists(self, executable):
        return executable in {"docker", "nvidia-smi"}

    def run(self, argv, *, timeout=15, env=None):
        command = tuple(argv)
        self.calls.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            return CommandResult(command, 0, "available")
        if command[:2] == ("docker", "inspect"):
            return CommandResult(command, 1, stderr="not found")
        if command[:3] == ("docker", "ps", "-q"):
            return CommandResult(command, 0, "owned-container")
        if command[:4] == ("docker", "stop", "--time", "30"):
            return CommandResult(command, 0, "owned-container")
        if command[:2] == ("docker", "exec") and "ray.cluster_resources" in command[-1]:
            return CommandResult(command, 0, json.dumps({"GPU": 1}))
        return CommandResult(command, 0, "ok")


class AgentTaskExecutorTests(unittest.TestCase):
    def test_allowed_task_lifecycle_uses_internal_operations(self):
        node_id = "4ec02dee-c5f5-4466-96c5-adc754ef52b8"
        node_profile = profile(node_id)
        runner = AgentRunner()
        with tempfile.TemporaryDirectory() as temporary:
            model_path = Path(temporary) / "model"
            model_path.mkdir()
            (model_path / "config.json").write_text("{}", encoding="utf-8")
            state_path = Path(temporary) / "state.json"
            plan = build_plan([node_profile], image="registry/vllm@sha256:" + "a" * 64)
            plan.model_path = str(model_path)
            payload = {"plan": plan.to_dict(), "generation": plan.generation, "serve": False}
            executor = TaskExecutor(node_id, runner=runner, state_path=state_path)
            with patch("dure.probe.NodeProbe.collect", return_value=node_profile):
                probed = executor.execute({"type": "PROBE", "payload": {}})
                self.assertEqual(probed["profile"]["node_id"], node_id)
                verified = executor.execute({"type": "VERIFY", "payload": payload})
                self.assertTrue(verified["ok"])
                applied = executor.execute({"type": "APPLY_DEPLOYMENT", "payload": payload})
                self.assertTrue(applied["checks"])
                stopped = executor.execute({"type": "STOP_DEPLOYMENT", "payload": payload})
                self.assertEqual(stopped["checks"][0]["name"], "deployment-stop")
                restarted = executor.execute({"type": "RESTART_DEPLOYMENT", "payload": payload})
                self.assertTrue(restarted["checks"])
                unjoined = executor.execute({"type": "UNJOIN_NODE", "payload": {}})
                self.assertTrue(unjoined["unjoined"])
                self.assertEqual(StateStore(state_path).load().phase, "UNJOINING")
        stop_calls = [call for call in runner.calls if call[:2] == ("docker", "stop")]
        self.assertTrue(stop_calls)
        self.assertNotIn("sh", {part for call in runner.calls for part in call})

    def test_arbitrary_task_type_is_rejected(self):
        with self.assertRaises(ValueError):
            TaskExecutor("node").execute({"type": "SHELL", "payload": {"command": "id"}})

    def test_remote_unjoin_disables_agent_and_scrubs_local_credential(self):
        runner = AgentRunner()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "agent.json"
            state_path = Path(temporary) / "state.json"
            history_path = Path(temporary) / "history.json"
            config = {
                "server": "https://control.example",
                "node_id": "node-1",
                "credential": "secret",
                "install_id": "install-12345678",
                "state_file": str(state_path),
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            agent = Agent(
                config,
                config_path=config_path,
                history_path=history_path,
                runner=runner,
            )

            agent._finish_unjoin()

            self.assertFalse(agent.running)
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                {"install_id": "install-12345678"},
            )
            self.assertIn(("systemctl", "disable", "dure-agent"), runner.calls)
