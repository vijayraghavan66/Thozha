from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjarvis.tasks.remote_dev_worker import (
    DEFAULT_WORKSPACE,
    GuestWorkspace,
    RemoteMcpWorkerClient,
    RemoteWorkerConfig,
    RemoteWorkerError,
    RemoteWorkspace,
    worker_handle,
)


def test_guest_workspace_reads_writes_and_reports_manifest(tmp_path):
    workspace = GuestWorkspace(tmp_path)
    content = "print('hello')\n"
    receipt = workspace.write_text(
        "src/main.py",
        content,
        hashlib.sha256(content.encode()).hexdigest(),
        len(content.encode()),
    )

    assert receipt["path"] == "src/main.py"
    assert workspace.read_text("src/main.py") == "print('hello')\n"
    assert workspace.list_dir("src") == ["main.py"]
    manifest = workspace.manifest(["src/main.py"])
    assert manifest["files"] == [
        {
            "path": "src/main.py",
            "bytes": len(content.encode()),
            "sha256": hashlib.sha256(b"print('hello')\n").hexdigest(),
        }
    ]


@pytest.mark.parametrize(
    "path", ["../outside", "/etc/passwd", "src/../../outside", "a//b", "a\\b"]
)
def test_guest_workspace_rejects_path_escape_and_malformed_paths(tmp_path, path):
    workspace = GuestWorkspace(tmp_path)
    with pytest.raises((ValueError, FileNotFoundError)):
        workspace.read_bytes(path)
    with pytest.raises(ValueError):
        workspace.write_text(path, "x", hashlib.sha256(b"x").hexdigest(), 1)


def test_guest_workspace_rejects_symlink_ancestors_and_targets(tmp_path):
    workspace = GuestWorkspace(tmp_path)
    elsewhere = tmp_path.parent / "outside"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("synthetic only")
    (tmp_path / "escape").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError):
        workspace.read_text("escape/secret.txt")
    with pytest.raises(ValueError):
        workspace.write_text("escape/new.txt", "x", hashlib.sha256(b"x").hexdigest(), 1)


def test_guest_workspace_rejects_write_digest_size_and_nul(tmp_path):
    workspace = GuestWorkspace(tmp_path)
    with pytest.raises(ValueError):
        workspace.write_text("x.txt", "abc", hashlib.sha256(b"other").hexdigest(), 3)
    with pytest.raises(ValueError):
        workspace.write_text("x.txt", "abc", hashlib.sha256(b"abc").hexdigest(), 2)
    with pytest.raises(ValueError):
        workspace.write_text("x.txt", "\x00", hashlib.sha256(b"\x00").hexdigest(), 1)


def test_worker_rpc_allows_only_fixed_tools_and_returns_guest_context(tmp_path):
    workspace = GuestWorkspace(tmp_path)
    marker = tmp_path / "preflight-sentinel.txt"
    marker.write_text("VM-ONLY-SYNTHETIC-MARKER\n")

    result = worker_handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "list_project_files",
                "arguments": {},
                "task_id": "a" * 32,
            },
        },
        workspace,
        expected_task_id="a" * 32,
    )
    assert result["id"] == 8
    assert result["result"]["isError"] is False

    denied = worker_handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "shell_exec",
                "arguments": {"command": "id"},
                "task_id": "a" * 32,
            },
        },
        workspace,
        expected_task_id="a" * 32,
    )
    assert denied["id"] == 9
    assert denied["result"]["isError"] is True

    wrong_task = worker_handle(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "list_project_files",
                "arguments": {},
                "task_id": "b" * 32,
            },
        },
        workspace,
        expected_task_id="a" * 32,
    )
    assert wrong_task["result"]["isError"] is True


def test_remote_client_uses_pinned_ssh_and_json_stdin_without_interpolating_arguments(
    tmp_path,
):
    identity = tmp_path / "id"
    identity.write_text("test-only private key placeholder")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("[127.0.0.1]:2222 ssh-ed25519 synthetic-test-pin\n")
    config = RemoteWorkerConfig(identity, known_hosts, Path(DEFAULT_WORKSPACE))
    observed = {}

    def fake_runner(argv, **kwargs):
        observed["argv"] = argv
        observed["input"] = kwargs["input"]
        return SimpleNamespace(
            returncode=0,
            stdout='{"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"ok":true},"isError":false}}\n',
        )

    client = RemoteMcpWorkerClient(config, runner=fake_runner, task_id="a" * 32)
    assert client.call_tool("list_project_files", {"dir": "$(touch nope)"}) == {
        "ok": True
    }
    command = observed["argv"]
    assert command[0] == "/usr/bin/ssh"
    assert "StrictHostKeyChecking=yes" in command
    assert str(known_hosts) in " ".join(command)
    assert "$(touch nope)" not in " ".join(command)
    sent = [json.loads(line) for line in observed["input"].splitlines()]
    assert sent[-1]["params"]["arguments"] == {"dir": "$(touch nope)"}


def test_remote_client_fails_closed_for_unknown_tools_without_transport(tmp_path):
    identity = tmp_path / "id"
    identity.write_text("test-only")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("pin\n")
    config = RemoteWorkerConfig(identity, known_hosts, Path(DEFAULT_WORKSPACE))
    client = RemoteMcpWorkerClient(
        config,
        runner=lambda *a, **k: pytest.fail("transport must not run"),
        task_id="b" * 32,
    )
    with pytest.raises(RemoteWorkerError):
        client.call_tool("shell_exec", {"command": "anything"})


def test_remote_client_cancellation_stops_only_its_random_guest_unit(tmp_path):
    identity = tmp_path / "id"
    identity.write_text("test-only")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("pin\n")
    config = RemoteWorkerConfig(identity, known_hosts, Path(DEFAULT_WORKSPACE))
    task_id = "e" * 32
    cancel = threading.Event()
    cancel.set()
    units = []

    class FakeProcess:
        returncode = None

        def __init__(self, argv, **_kwargs):
            self.argv = argv
            self.first_communicate = True

        def communicate(self, _input=None, timeout=None):
            if self.first_communicate:
                self.first_communicate = False
                raise subprocess.TimeoutExpired("ssh", timeout)
            return b"", b""

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    client = RemoteMcpWorkerClient(config, process_factory=FakeProcess, task_id=task_id)
    client._stop_guest_unit = lambda unit: units.append(unit)
    with pytest.raises(RemoteWorkerError, match="was cancelled"):
        client.call_tool("run_project_tests", {"command": "unittest-tests"}, cancel_event=cancel)
    assert len(units) == 1
    assert units[0].startswith("jarvis-dev-")
    assert len(units[0]) == len("jarvis-dev-") + 16


def test_worker_config_requires_fixed_root_and_private_identity(tmp_path):
    identity = tmp_path / "id"
    identity.write_text("test-only")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("pin\n")
    config_path = tmp_path / "worker.json"
    config_path.write_text(
        json.dumps(
            {
                "identity_file": str(identity),
                "known_hosts_file": str(known_hosts),
                "workspace_root": "/tmp/not-taskflow",
            }
        )
    )
    with pytest.raises(RemoteWorkerError):
        RemoteWorkerConfig.load(config_path)

    identity.chmod(0o644)
    config_path.write_text(
        json.dumps(
            {
                "identity_file": str(identity),
                "known_hosts_file": str(known_hosts),
                "workspace_root": DEFAULT_WORKSPACE,
            }
        )
    )
    with pytest.raises(RemoteWorkerError):
        RemoteWorkerConfig.load(config_path)


def test_jarvis_development_workspace_refuses_local_fallback(monkeypatch):
    from openjarvis.tasks.jarvis_dev_mcp import _workspace

    monkeypatch.setenv("JARVIS_DEV_EXECUTION_MODE", "vm")
    monkeypatch.delenv("JARVIS_DEV_WORKER_CONFIG", raising=False)
    with pytest.raises(PermissionError, match="configuration is unavailable"):
        _workspace()


def test_remote_workspace_rejects_non_guest_execution_context(tmp_path):
    class WrongGuest:
        task_id = "c" * 32

        def call_tool(self, *args, **kwargs):
            return {
                "os": "Darwin",
                "architecture": "arm64",
                "cwd": str(tmp_path),
                "task_id": self.task_id,
            }

    workspace = RemoteWorkspace(tmp_path, WrongGuest())
    with pytest.raises(RemoteWorkerError, match="not the approved"):
        workspace.execution_context()
