from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import portable_manager as manager


class PortableManagerTests(unittest.TestCase):
    def test_start_parser_exposes_explicit_local_only_mode(self):
        args = manager._build_parser().parse_args(["start", "--local-only"])
        self.assertTrue(args.local_only)

    def test_default_start_does_not_enable_local_only(self):
        args = manager._build_parser().parse_args(["start"])
        self.assertFalse(args.local_only)

    def test_private_json_is_atomic_shape_and_mode(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config" / "web-auth.json"
            manager._atomic_private_json(path, {"owner_id": "test", "password_hash": "hash"})
            self.assertEqual(json.loads(path.read_text()), {"owner_id": "test", "password_hash": "hash"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_server_identity_persists_across_starts(self):
        with tempfile.TemporaryDirectory() as root:
            identity_path = Path(root) / "config" / "server-identity.json"
            with patch.object(manager, "SERVER_IDENTITY", identity_path):
                first = manager._server_identity()
                second = manager._server_identity()
            self.assertEqual(first, second)
            self.assertTrue(first["hostname"].endswith(".local"))
            self.assertEqual(identity_path.stat().st_mode & 0o777, 0o600)

    def test_codex_auth_is_forced_into_portable_home_without_overwriting_settings(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "codex-home"
            home.mkdir()
            config = home / "config.toml"
            config.write_text('model = "gpt-6-luna"\ncli_auth_credentials_store = "file"\n')
            manager._prepare_codex_file_auth(home)
            self.assertIn('model = "gpt-6-luna"', config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_codex_auth_refuses_keyring_override(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
            with self.assertRaisesRegex(manager.PortableError, "non-file credential storage"):
                manager._prepare_codex_file_auth(home)

    def test_gateway_policy_updates_dhcp_ip_but_keeps_network_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            policy_path = Path(root) / "gateway-policy.json"
            original = {
                "interface": "en0",
                "allowed_ssid": "approved-network",
                "bind_ipv4": "192.168.1.20",
                "hostname": "thozha-identity.local",
                "certificate_sha256": "sha256:" + "a" * 64,
                "gateway_port": 8444,
                "loopback_port": 8002,
            }
            policy_path.write_text(json.dumps(original))
            with (
                patch.object(manager, "GATEWAY_POLICY", policy_path),
                patch.object(manager, "_wifi_interfaces", return_value=[("en0", "approved-network", "192.168.1.21")]),
            ):
                updated = manager._prepare_gateway({}, local_only=False)
            self.assertEqual(updated["bind_ipv4"], "192.168.1.21")
            self.assertEqual(updated["hostname"], original["hostname"])
            self.assertEqual(updated["certificate_sha256"], original["certificate_sha256"])
            self.assertEqual(updated["gateway_port"], 8444)
            self.assertEqual(updated["loopback_port"], 8002)

    def test_gateway_fails_closed_when_ssid_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as root:
            policy_path = Path(root) / "gateway-policy.json"
            policy_path.write_text(json.dumps({"interface": "en0", "allowed_ssid": "approved-network"}))
            with (
                patch.object(manager, "GATEWAY_POLICY", policy_path),
                patch.object(manager, "_wifi_interfaces", return_value=[]),
            ):
                with self.assertRaisesRegex(manager.PortableError, "cannot be verified"):
                    manager._prepare_gateway({}, local_only=False)

    def test_local_only_never_reads_or_creates_gateway_policy(self):
        with tempfile.TemporaryDirectory() as root:
            policy_path = Path(root) / "gateway-policy.json"
            with patch.object(manager, "GATEWAY_POLICY", policy_path):
                self.assertIsNone(manager._prepare_gateway({}, local_only=True))
            self.assertFalse(policy_path.exists())

    def test_portable_gateway_uses_isolated_supported_port_pair(self):
        self.assertEqual(manager.PORTABLE_LOOPBACK_GATEWAY_PORT, 8002)
        self.assertEqual(manager.PORTABLE_TLS_PORT, 8444)

    def test_base_environment_does_not_inherit_provider_secrets(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-pass", "OTHER_TOKEN": "also-private"}, clear=False):
            env = manager._base_environment()
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OTHER_TOKEN", env)
        self.assertEqual(env["JARVIS_MCP_OPEN_APP_ENABLED"], "0")


if __name__ == "__main__":
    unittest.main()
