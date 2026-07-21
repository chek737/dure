from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dure.agent import join_control_plane, resolve_join_settings, unjoin_control_plane
from dure.state import NodeState, StateStore
from tests.helpers import FakeRunner, profile


class FakeJoinClient:
    requests = []

    def __init__(self, base_url, token=None, *, verify_tls=True):
        self.base_url = base_url
        self.verify_tls = verify_tls

    def request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        return {"node_id": "server-node-id", "credential": "node-secret", "status": "pending"}


class FakeUnjoinClient(FakeJoinClient):
    def request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        return {"ok": True, "node_id": "server-node-id", "status": "unjoined"}


class JoinTests(unittest.TestCase):
    def test_packaged_settings_resolve_without_command_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "client.env"
            config.write_text("DURE_SERVER=http://control:8081\nDURE_INSECURE=true\n", encoding="utf-8")
            self.assertEqual(resolve_join_settings(client_config=config), ("http://control:8081", True))

    @patch("dure.agent.os.geteuid", return_value=0, create=True)
    def test_join_registers_config_and_starts_agent(self, _geteuid):
        runner = FakeRunner(executables={"systemctl"})
        with tempfile.TemporaryDirectory() as temporary:
            client_config = Path(temporary) / "client.env"
            agent_config = Path(temporary) / "agent.json"
            client_config.write_text("DURE_SERVER=https://control.example\n", encoding="utf-8")
            FakeJoinClient.requests = []
            with patch("dure.agent.JSONClient", FakeJoinClient), patch(
                "dure.agent.NodeProbe.collect", return_value=profile("joined-host")
            ):
                result = join_control_plane(
                    config_path=agent_config,
                    client_config=client_config,
                    runner=runner,
                )
            stored = json.loads(agent_config.read_text(encoding="utf-8"))
            self.assertEqual(result, {"node_id": "server-node-id", "status": "pending"})
            self.assertEqual(stored["server"], "https://control.example")
            self.assertEqual(stored["credential"], "node-secret")
            self.assertIn(("systemctl", "enable", "--now", "dure-agent"), runner.calls)
            self.assertEqual(FakeJoinClient.requests[0][1], "/v1/nodes/join")

    def test_http_server_requires_explicit_insecure_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "client.env"
            config.write_text("DURE_SERVER=http://control:8081\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                resolve_join_settings(client_config=config)

    @patch("dure.agent.os.geteuid", return_value=0, create=True)
    def test_join_is_idempotent_and_restarts_agent(self, _geteuid):
        runner = FakeRunner(executables={"systemctl"})
        with tempfile.TemporaryDirectory() as temporary:
            client_config = Path(temporary) / "client.env"
            agent_config = Path(temporary) / "agent.json"
            client_config.write_text("DURE_SERVER=https://control.example\n", encoding="utf-8")
            agent_config.write_text(
                json.dumps({"server": "https://control.example", "node_id": "existing", "credential": "secret"}),
                encoding="utf-8",
            )
            result = join_control_plane(
                config_path=agent_config,
                client_config=client_config,
                runner=runner,
            )
            self.assertEqual(result, {"node_id": "existing", "status": "already-joined"})
            self.assertIn(("systemctl", "enable", "--now", "dure-agent"), runner.calls)

    @patch("dure.agent.os.geteuid", return_value=1000, create=True)
    def test_join_requires_root(self, _geteuid):
        with self.assertRaisesRegex(PermissionError, "must run as root"):
            join_control_plane(start_service=False)

    @patch("dure.agent.os.geteuid", return_value=0, create=True)
    def test_unjoin_releases_deployment_and_scrubs_credential(self, _geteuid):
        runner = FakeRunner(executables={"systemctl", "docker"})
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "agent.json"
            state_path = Path(temporary) / "state.json"
            config_path.write_text(
                json.dumps(
                    {
                        "server": "https://control.example",
                        "node_id": "server-node-id",
                        "credential": "node-secret",
                        "install_id": "install-12345678",
                        "verify_tls": True,
                        "state_file": str(state_path),
                    }
                ),
                encoding="utf-8",
            )
            StateStore(state_path).save(
                NodeState(node_id="server-node-id", deployment_id="deploy-1", phase="READY")
            )
            FakeUnjoinClient.requests = []
            with patch("dure.agent.JSONClient", FakeUnjoinClient):
                result = unjoin_control_plane(config_path=config_path, runner=runner)

            self.assertEqual(result["status"], "unjoined")
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                {"install_id": "install-12345678"},
            )
            self.assertEqual(StateStore(state_path).load().phase, "UNJOINED")
            self.assertIn(
                ("docker", "ps", "-q", "--filter", "label=dure.deployment=deploy-1"),
                runner.calls,
            )
            self.assertIn(("systemctl", "disable", "--now", "dure-agent"), runner.calls)
            self.assertEqual(FakeUnjoinClient.requests[-1][1], "/v1/agent/unjoin")

    @patch("dure.agent.os.geteuid", return_value=1000, create=True)
    def test_unjoin_requires_root(self, _geteuid):
        with self.assertRaisesRegex(PermissionError, "must run as root"):
            unjoin_control_plane()
