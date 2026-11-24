"""LaunchAgent wrapper validates the Web profile without persisting credentials."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openjarvis.server.thozha_web_auth import verify_password
from openjarvis.server.thozha_web_launch import prepare_web_environment, server_command

PASSWORD = "isolated launch wrapper password"
OWNER = "launch-owner-test"
DEVICE_ID = "a" * 32


def _config(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    password_file = state / "owner-password"
    password_file.write_text(PASSWORD, encoding="utf-8")
    os.chmod(password_file, 0o600)
    core_source = Path(__file__).parents[2] / "src"
    return {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "THOZHA_CORE_SOURCE": str(core_source),
        "THOZHA_WEB_PASSWORD_FILE": str(password_file),
        "THOZHA_WEB_STATE_ROOT": str(state),
        "THOZHA_WEB_OWNER_ID": OWNER,
        "THOZHA_WEB_ORIGIN": "http://127.0.0.1:5174",
        "THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV": "1",
        "THOZHA_WEB_ALLOWED_DEVICE_IDS": DEVICE_ID,
        "THOZHA_WEB_SESSION_DB": str(state / "sessions.db"),
        "THOZHA_WEB_MEMORY_DB": str(state / "memory.db"),
        "THOZHA_WEB_CONVERSATION_DB": str(state / "conversations.db"),
        "JARVIS_DEV_WORKER_CONFIG": str(state / "worker-config.json"),
        "THOZHA_WEB_AUTOMATION_ENABLED": "1",
        "JARVIS_MCP_OPEN_APP_ENABLED": "1",
    }


def test_wrapper_hashes_owner_password_and_forces_local_safe_profile(tmp_path):
    environ = _config(tmp_path)
    worker_config = Path(environ["JARVIS_DEV_WORKER_CONFIG"])
    worker_config.write_text("{}", encoding="utf-8")
    os.chmod(worker_config, 0o600)

    result = prepare_web_environment(environ)

    password_hash = result["THOZHA_WEB_PASSWORD_HASH"]
    assert verify_password(PASSWORD, password_hash)
    assert PASSWORD not in result.values()
    assert result["JARVIS_MCP_OPEN_APP_ENABLED"] == "0"
    assert result["THOZHA_WEB_AUTOMATION_ENABLED"] == "0"
    assert result["THOZHA_WEB_OWNER_ID"] == OWNER
    assert result["THOZHA_WEB_ALLOWED_DEVICE_IDS"] == DEVICE_ID
    assert result["THOZHA_WEB_CONVERSATION_DB"].endswith("conversations.db")
    assert result["PYTHONPATH"].startswith(environ["THOZHA_CORE_SOURCE"])
    assert "THOZHA_WEB_PASSWORD_FILE" not in result


@pytest.mark.parametrize(
    "field,value",
    [
        ("THOZHA_WEB_ORIGIN", "http://0.0.0.0:5174"),
        ("THOZHA_WEB_ALLOWED_DEVICE_IDS", "not-a-device-id"),
        ("THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV", "0"),
    ],
)
def test_wrapper_fails_closed_for_public_origin_or_invalid_mapping(tmp_path, field, value):
    environ = _config(tmp_path)
    environ[field] = value
    with pytest.raises((RuntimeError, ValueError)):
        prepare_web_environment(environ)


def test_wrapper_requires_owner_only_password_file(tmp_path):
    environ = _config(tmp_path)
    Path(environ["THOZHA_WEB_PASSWORD_FILE"]).chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        prepare_web_environment(environ)


def test_service_command_is_loopback_only_and_uses_existing_cli(tmp_path):
    cli = tmp_path / ".openjarvis/src/.venv/bin/jarvis"
    cli.parent.mkdir(mode=0o700, parents=True)
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o700)
    assert server_command(tmp_path) == [
        str(cli),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
