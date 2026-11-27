from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import portable_manager as manager
from openjarvis.server.thozha_web_auth import hash_password, verify_password


class PortableManagerTests(unittest.TestCase):
    def test_start_parser_exposes_explicit_local_only_mode(self):
        args = manager._build_parser().parse_args(["start", "--local-only"])
        self.assertTrue(args.local_only)

    def test_default_start_does_not_enable_local_only(self):
        args = manager._build_parser().parse_args(["start"])
        self.assertFalse(args.local_only)

    def test_password_recovery_command_is_explicit(self):
        args = manager._build_parser().parse_args(["reset-web-password"])
        self.assertEqual(args.command, "reset-web-password")

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

    def test_portable_tls_frontend_match_is_scoped_to_bundle_identity_and_ports(self):
        with tempfile.TemporaryDirectory() as root:
            bundle = Path(root) / "bundle"
            data = Path(root) / "data"
            with patch.object(manager, "ROOT", bundle), patch.object(manager, "DATA", data):
                python = bundle / "runtime/tls-python/bin/python3.14"
                script = bundle / "runtime/server-venv/lib/python3.13/site-packages/openjarvis/server/companion_wifi_tls_proxy.py"
                cert = data / "openjarvis/companion-wifi/tls-cert.pem"
                key = data / "openjarvis/companion-wifi/tls-key.pem"
                command = [
                    str(python), str(script), "--host", "192.168.1.7", "--port", "8444",
                    "--certificate", str(cert), "--private-key", str(key), "--upstream-port", "8002",
                ]
                self.assertTrue(manager._portable_tls_frontend_command_matches(command))
                production_command = command.copy()
                production_command[production_command.index("--port") + 1] = "8443"
                production_command[production_command.index("--upstream-port") + 1] = "8001"
                self.assertFalse(manager._portable_tls_frontend_command_matches(production_command))
                wrong_key_command = command.copy()
                wrong_key_command[wrong_key_command.index("--private-key") + 1] = "/Users/test/.openjarvis/tls-key.pem"
                self.assertFalse(manager._portable_tls_frontend_command_matches(wrong_key_command))

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

    def test_reset_web_password_updates_only_portable_web_auth_and_sessions(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            auth = data / "config" / "web-auth.json"
            auth.parent.mkdir(mode=0o700)
            auth.write_text(json.dumps({"owner_id": "owner", "password_hash": hash_password("old-password-long")}), encoding="utf-8")
            auth.chmod(0o600)
            sessions = data / "openjarvis" / "thozha_web_sessions.db"
            sessions.parent.mkdir(mode=0o700)
            with sqlite3.connect(sessions) as connection:
                connection.execute(
                    "CREATE TABLE web_sessions(session_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf_hash BLOB NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER)"
                )
                connection.execute("INSERT INTO web_sessions VALUES('session1','owner',X'00',1,9999999999,NULL)")
                connection.execute("INSERT INTO web_sessions VALUES('session2','owner',X'00',1,9999999999,NULL)")
            sessions.chmod(0o600)
            supervisor = data / "openjarvis" / "supervisor.db"
            with sqlite3.connect(supervisor) as connection:
                connection.execute("CREATE TABLE pairing_marker(value TEXT)")
                connection.execute("INSERT INTO pairing_marker VALUES('preserved')")
            supervisor.chmod(0o600)

            manager._rotate_web_password(
                "new-portable-password-2026",
                "new-portable-password-2026",
                web_auth_path=auth,
                session_db_path=sessions,
            )

            updated = json.loads(auth.read_text(encoding="utf-8"))
            self.assertEqual(updated["owner_id"], "owner")
            self.assertTrue(verify_password("new-portable-password-2026", updated["password_hash"]))
            self.assertFalse(verify_password("old-password-long", updated["password_hash"]))
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(sessions) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM web_sessions").fetchone()[0], 0)
            with sqlite3.connect(supervisor) as connection:
                self.assertEqual(connection.execute("SELECT value FROM pairing_marker").fetchone()[0], "preserved")

    def test_password_mismatch_does_not_change_auth_or_sessions(self):
        with tempfile.TemporaryDirectory() as root:
            auth = Path(root) / "web-auth.json"
            original = {"owner_id": "owner", "password_hash": hash_password("old-password-long")}
            auth.write_text(json.dumps(original), encoding="utf-8")
            auth.chmod(0o600)
            with self.assertRaisesRegex(manager.PortableError, "did not match"):
                manager._rotate_web_password(
                    "new-password-long", "different-password-long",
                    web_auth_path=auth,
                    session_db_path=Path(root) / "missing-sessions.db",
                )
            self.assertEqual(json.loads(auth.read_text(encoding="utf-8")), original)

    def test_password_reset_command_refuses_running_server_before_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "server.json"
            lock = Path(root) / "server.lock"
            state.write_text("{}", encoding="utf-8")
            with patch.object(manager, "STATE", state), patch.object(manager, "LOCK", lock), \
                 patch.object(manager.getpass, "getpass") as prompt:
                with self.assertRaisesRegex(manager.PortableError, "Stop this portable server first"):
                    manager.reset_web_password(None)
            prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
