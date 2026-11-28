"""Web conversation tool-denial checks for the Codex SDK adapter."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from openjarvis.core.types import Message, Role
from openjarvis.engine import codex_sdk
from openjarvis.tasks.supervisor import SupervisorStore


def test_configured_codex_python_preserves_venv_launcher_symlink(
    tmp_path, monkeypatch
):
    from pathlib import Path

    launcher = tmp_path / ".venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(sys.executable)
    monkeypatch.setenv("OPENJARVIS_PYTHON", str(launcher))

    assert codex_sdk._resolve_codex_python() == launcher


def test_cancel_active_codex_task_stops_its_app_server_and_dev_mcp_children(monkeypatch):
    task_id = "e" * 32

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    codex_process = FakeProcess(2_000_000)
    mcp_process = FakeProcess(2_000_001)

    class FakeClient:
        _proc = codex_process

        def close(self):
            codex_process.terminate()

    codex = SimpleNamespace(_client=FakeClient())
    monkeypatch.setitem(codex_sdk._ACTIVE_CODEX_CLIENTS, task_id, codex)
    monkeypatch.setitem(codex_sdk._ACTIVE_DEVELOPMENT_MCP_PROCESSES, task_id, {mcp_process})
    monkeypatch.setattr(codex_sdk, "_process_table", lambda: [])

    result = codex_sdk.cancel_active_codex_task(task_id)

    assert result == {"active": True, "confirmed": True, "mcp_processes_stopped": 1}
    assert codex_process.poll() is not None
    assert mcp_process.poll() is not None
    assert task_id not in codex_sdk._ACTIVE_CODEX_CLIENTS
    assert task_id not in codex_sdk._ACTIVE_DEVELOPMENT_MCP_PROCESSES


def test_cancel_active_codex_task_finds_task_process_after_server_registry_restart(monkeypatch):
    task_id = "f" * 32
    task_process = 321
    captured = []
    monkeypatch.setattr(
        codex_sdk,
        "_process_table",
        lambda: [
            (task_process, 1, os.getuid(), f"codex --config mcp_servers.jarvis_dev.env.JARVIS_SUPERVISOR_TASK_ID=\"{task_id}\" app-server"),
            (task_process + 1, task_process, os.getuid(), "python jarvis_dev_mcp.py"),
        ],
    )
    monkeypatch.setattr(
        codex_sdk,
        "_terminate_task_process_tree",
        lambda roots: captured.extend(sorted(roots)) or True,
    )

    result = codex_sdk.cancel_active_codex_task(task_id)

    assert result["active"] is True
    assert result["confirmed"] is True
    assert captured == [task_process]


def _running_link_task(store, owner_actor: str) -> dict[str, str]:
    objective = "Summarize the selected development workspace"
    idempotency_key = "codex-start-barrier-" + hashlib.sha256(owner_actor.encode()).hexdigest()[:16]
    created = store.create_link_task(
        objective,
        capability_scope=["development"],
        idempotency_key=idempotency_key,
        owner_actor=owner_actor,
    )
    digest = hashlib.sha256(objective.encode()).hexdigest()
    store.start_link_task(
        created["task_id"],
        objective_digest=digest,
        objective_text_digest=digest,
        capability_scope=["development"],
        idempotency_key=idempotency_key,
    )
    return {"task_id": created["task_id"], "idempotency_key": idempotency_key}


def test_codex_startup_guard_refuses_cancelled_task(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    owner = "device:" + "a" * 32
    task = _running_link_task(store, owner)
    cancel_event = codex_sdk._codex_task_cancel_event(task["task_id"])
    cancel_event.set()

    with pytest.raises(codex_sdk.EngineConnectionError, match="cancelled"):
        with codex_sdk._codex_task_startup_guard(
            task["task_id"], cancellation_event=cancel_event, supervisor_store=store
        ):
            pytest.fail("a cancelled task must not start Codex")
    assert task["task_id"] not in codex_sdk._ACTIVE_CODEX_CLIENTS
    codex_sdk._clear_codex_task_cancel_event(task["task_id"], cancel_event)
    store.close()


def test_codex_startup_guard_serializes_process_registration_with_cancel(tmp_path):
    database = tmp_path / "supervisor.db"
    store = SupervisorStore(str(database), run_recovery=False)
    owner = "device:" + "b" * 32
    task = _running_link_task(store, owner)
    cancel_store = SupervisorStore(str(database), run_recovery=False)
    cancel_started = threading.Event()
    cancel_finished = threading.Event()
    registration = []
    result = {}

    def cancel_task():
        cancel_started.set()
        result["cancel"] = cancel_store.cancel_link_task(task["task_id"], actor=owner)
        cancel_finished.set()

    with codex_sdk._codex_task_startup_guard(
        task["task_id"], supervisor_store=store
    ):
        # This represents creating/entering the Codex app-server and registering
        # its process while the startup transaction fences supervisor cancel.
        registration.append(task["task_id"])
        thread = threading.Thread(target=cancel_task)
        thread.start()
        assert cancel_started.wait(timeout=5)
        time.sleep(0.05)
        assert not cancel_finished.is_set()

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert registration == [task["task_id"]]
    assert result["cancel"]["cancelled"] is True
    with pytest.raises(codex_sdk.EngineConnectionError, match="no longer active"):
        with codex_sdk._codex_task_startup_guard(
            task["task_id"], supervisor_store=store
        ):
            pytest.fail("Codex must not launch after cancellation commits")
    cancel_store.close()
    store.close()


class _FakeThread:
    def run(self, *_args, **_kwargs):
        return SimpleNamespace(final_response="fixture response", usage=None)


class _FakeCodex:
    last = None

    def __init__(self, *, config):
        self.config = config
        self._client = SimpleNamespace()
        self.thread_start_kwargs = None
        type(self).last = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def thread_start(self, **kwargs):
        self.thread_start_kwargs = kwargs
        return _FakeThread()


def test_empty_tools_disables_every_configured_mcp_and_command_surface(
    tmp_path, monkeypatch
):
    import openai_codex

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.extra]\ncommand="/usr/bin/true"\n'
        '[mcp_servers.jarvis_mac]\ncommand="/usr/bin/true"\n'
        '[mcp_servers.jarvis_dev]\ncommand="/usr/bin/true"\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(codex_sdk, "CODEX_HOME", codex_home)
    monkeypatch.setattr(
        codex_sdk, "_working_directory_for_current_task", lambda: str(workspace)
    )
    monkeypatch.setattr(openai_codex, "Codex", _FakeCodex)

    engine = codex_sdk.CodexSDKEngine()
    result = engine.generate(
        [Message(role=Role.USER, content="What is two plus two?")],
        model=codex_sdk.MODEL,
        tools=[],
    )
    assert result["content"] == "fixture response"
    overrides = set(_FakeCodex.last.config.config_overrides)
    assert {
        "mcp_servers.extra.enabled=false",
        "mcp_servers.jarvis_mac.enabled=false",
        "mcp_servers.jarvis_dev.enabled=false",
        'mcp_servers.jarvis_mac.command="/usr/bin/false"',
        'mcp_servers.jarvis_dev.command="/usr/bin/false"',
        "features.shell_tool=false",
        "features.unified_exec=false",
        "features.unified_exec_tty=false",
    } <= overrides
    assert (
        _FakeCodex.last.thread_start_kwargs["sandbox"] == openai_codex.Sandbox.read_only
    )
    assert "No local tools" in _FakeCodex.last.thread_start_kwargs["base_instructions"]

    handler = _FakeCodex.last._client._approval_handler
    denied = handler(
        "mcpServer/tool/call",
        {
            "server": "jarvis_mac",
            "tool": "get_battery_level",
            "arguments": {},
        },
    )
    assert denied["isError"] is True


def test_normal_codex_turn_retains_existing_mcp_configuration(tmp_path, monkeypatch):
    import openai_codex

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setattr(codex_sdk, "CODEX_HOME", codex_home)
    monkeypatch.setattr(
        codex_sdk, "_working_directory_for_current_task", lambda: str(tmp_path)
    )
    monkeypatch.setattr(openai_codex, "Codex", _FakeCodex)

    result = codex_sdk.CodexSDKEngine().generate(
        [Message(role=Role.USER, content="Read the local status")],
        model=codex_sdk.MODEL,
    )
    assert result["content"] == "fixture response"
    overrides = set(_FakeCodex.last.config.config_overrides)
    assert any(
        value.startswith("mcp_servers.jarvis_mac.command=") for value in overrides
    )
    assert "mcp_servers.jarvis_mac.enabled=false" not in overrides


def test_mac_read_scope_enables_only_fixed_mac_bridge_and_disables_shell(
    tmp_path, monkeypatch
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.extra]\ncommand="/usr/bin/true"\n'
        '[mcp_servers.jarvis_dev]\ncommand="/usr/bin/true"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_sdk, "CODEX_HOME", codex_home)
    overrides = set(codex_sdk._config_overrides("mac_read"))
    assert "mcp_servers.extra.enabled=false" in overrides
    assert "mcp_servers.jarvis_dev.enabled=false" in overrides
    assert 'mcp_servers.jarvis_dev.command="/usr/bin/false"' in overrides
    assert "mcp_servers.jarvis_mac.enabled=true" in overrides
    assert any(value.startswith("mcp_servers.jarvis_mac.command=") for value in overrides)
    assert "features.shell_tool=false" in overrides
    assert "features.unified_exec=false" in overrides
    assert "features.unified_exec_tty=false" in overrides

    from openjarvis.tasks.supervisor import bind_task
    from openjarvis.tasks import supervisor

    test_db = str(tmp_path / "supervisor.db")
    monkeypatch.setattr(
        supervisor,
        "get_supervisor_store",
        lambda: SimpleNamespace(db_path=test_db),
    )

    task_id = "c" * 32
    with bind_task(task_id, source="jarvis_link", scope="mac_read"):
        scoped = set(codex_sdk._config_overrides("mac_read"))
    assert any(f"JARVIS_SUPERVISOR_TASK_ID=\"{task_id}\"" in value for value in scoped)
    assert 'JARVIS_SUPERVISOR_TASK_SCOPE="mac_read"' in " ".join(scoped)
    assert any("JARVIS_SUPERVISOR_DB" in value and test_db in value for value in scoped)


def test_supervised_assistant_mcp_receives_its_task_store_path(tmp_path, monkeypatch):
    from openjarvis.tasks import supervisor
    from openjarvis.tasks.supervisor import bind_task

    task_db = str(tmp_path / "supervisor.db")
    monkeypatch.setattr(
        supervisor,
        "get_supervisor_store",
        lambda: SimpleNamespace(db_path=task_db),
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setattr(codex_sdk, "CODEX_HOME", codex_home)
    monkeypatch.setattr(codex_sdk, "_working_directory_for_current_task", lambda: str(tmp_path))

    task_id = "b" * 32
    with bind_task(
        task_id,
        source="jarvis_link",
        scope="assistant",
    ):
        overrides = set(codex_sdk._config_overrides("control"))
    assert any(f"JARVIS_SUPERVISOR_TASK_ID=\"{task_id}\"" in value for value in overrides)
    assert any("JARVIS_SUPERVISOR_DB" in value and task_db in value for value in overrides)


def test_read_only_link_scope_enables_only_fixed_disk_tool(tmp_path, monkeypatch):
    from openjarvis.tasks import supervisor
    from openjarvis.tasks.supervisor import bind_task

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.extra]\ncommand="/usr/bin/true"\n'
        '[mcp_servers.jarvis_dev]\ncommand="/usr/bin/true"\n',
        encoding="utf-8",
    )
    task_id = "a" * 32
    task_db = str(tmp_path / "supervisor.db")
    monkeypatch.setattr(codex_sdk, "CODEX_HOME", codex_home)
    monkeypatch.setattr(
        supervisor,
        "get_supervisor_store",
        lambda: SimpleNamespace(db_path=task_db),
    )
    with bind_task(task_id, source="jarvis_link", scope="read_only"):
        overrides = set(codex_sdk._config_overrides("basic"))
    assert "mcp_servers.extra.enabled=false" in overrides
    assert "mcp_servers.jarvis_dev.enabled=false" in overrides
    assert 'mcp_servers.jarvis_dev.command="/usr/bin/false"' in overrides
    assert "mcp_servers.jarvis_mac.enabled=true" in overrides
    assert 'mcp_servers.jarvis_mac.env.JARVIS_DESKTOP_MODE="basic"' in overrides
    assert "features.shell_tool=false" in overrides
    assert "features.unified_exec=false" in overrides
    assert "features.unified_exec_tty=false" in overrides
    assert any(f"JARVIS_SUPERVISOR_TASK_ID=\"{task_id}\"" in value for value in overrides)
    assert 'JARVIS_SUPERVISOR_TASK_SCOPE="read_only"' in " ".join(overrides)
    assert any("JARVIS_SUPERVISOR_DB" in value and task_db in value for value in overrides)


def test_mac_mcp_uses_explicit_supervisor_db_path(monkeypatch, tmp_path):
    import importlib.util
    from openjarvis.tasks import supervisor

    module_path = (
        codex_sdk.Path(__file__).resolve().parents[2]
        / "src"
        / "openjarvis"
        / "mac_desktop_mcp.py"
    )
    spec = importlib.util.spec_from_file_location("thozha_test_mac_mcp_db_path", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "control")
    selected_path = str(tmp_path / "supervisor.db")
    monkeypatch.setenv("JARVIS_SUPERVISOR_DB", selected_path)
    spec.loader.exec_module(module)

    captured = {}

    class FakeStore:
        def __init__(self, db_path=None, *, run_recovery=True):
            captured["db_path"] = db_path
            captured["run_recovery"] = run_recovery

    monkeypatch.setattr(supervisor, "SupervisorStore", FakeStore)
    module._STORE = None
    module._supervisor_store()
    assert captured == {"db_path": selected_path, "run_recovery": False}


def test_control_exposes_only_supervised_browser_workspace_when_open_app_is_disabled(
    tmp_path, monkeypatch
):
    import importlib.util

    from openjarvis.tasks import supervisor

    store = supervisor.SupervisorStore(str(tmp_path / "supervisor.db"))
    objective = "Open a separate blank browser workspace after owner approval."
    key = "approved-browser-workspace-test-0001"
    run = store.create_link_task(
        objective,
        capability_scope=["assistant"],
        idempotency_key=key,
        owner_actor="device:" + ("a" * 32),
    )
    store.start_link_task(
        run["task_id"],
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["assistant"],
        idempotency_key=key,
    )
    monkeypatch.setattr(supervisor, "get_supervisor_store", lambda: store)
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "control")
    monkeypatch.setenv("JARVIS_MCP_OPEN_APP_ENABLED", "0")
    module_path = (
        codex_sdk.Path(__file__).resolve().parents[2]
        / "src"
        / "openjarvis"
        / "mac_desktop_mcp.py"
    )
    spec = importlib.util.spec_from_file_location("thozha_test_control_browser_tool", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = {item["name"] for item in module.tool_list()}
    assert "open_browser_workspace" in names
    assert "open_app" not in names

    def execution_must_not_start(*_args, **_kwargs):
        raise AssertionError("browser launch handler must not run before owner approval")

    monkeypatch.setattr(codex_sdk, "_call_local_mcp", execution_must_not_start)
    with supervisor.bind_task(
        run["task_id"],
        source="jarvis_link",
        scope="assistant",
        allowed_capabilities=supervisor.LINK_SCOPE_CAPABILITIES["assistant"],
    ):
        response = codex_sdk._approval_handler("control")(
            "mcpServer/tool/call",
            {
                "server": "jarvis_mac",
                "tool": "open_browser_workspace",
                "arguments": {"url": "about:blank"},
            },
        )
    assert response["isError"] is True
    assert "waiting for approval" in response["content"][0]["text"].lower()
    pending = [item for item in store.pending_approvals(10) if item["task_id"] == run["task_id"]]
    assert len(pending) == 1
    assert pending[0]["operation"] == "mcp:jarvis_mac:open_browser_workspace"
    assert pending[0]["target"]
    assert pending[0]["safe_args"] == {"url": "about:blank"}
    assert pending[0]["status"] == "PENDING"
    assert store._db.execute(
        "SELECT COUNT(*) FROM mcp_approval_dispatches WHERE approval_id=?",
        (pending[0]["id"],),
    ).fetchone()[0] == 0
    store.close()


def test_codex_readiness_does_not_mislabel_auth_file_as_verified(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setattr(codex_sdk, "CODEX_HOME", codex_home)
    monkeypatch.setattr(
        codex_sdk.shutil,
        "which",
        lambda command: "/codex" if command == "codex" else None,
    )
    engine = codex_sdk.CodexSDKEngine()

    assert engine.readiness()["state"] == "unavailable"
    (codex_home / "auth.json").write_text('{"test_only":true}', encoding="utf-8")
    status = engine.readiness()
    assert status["state"] == "preflight_ready"
    assert status["credential_file_present"] is True
    assert status["runtime_auth_verified"] is False
    assert status["authentication_state"] == "credential_present_unverified"
    assert engine.health() is True


def test_mac_read_scope_exposes_only_version_uptime_and_disk(monkeypatch):
    module_path = (
        codex_sdk.Path(__file__).resolve().parents[2]
        / "src"
        / "openjarvis"
        / "mac_desktop_mcp.py"
    )
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "mac_read")
    spec = importlib.util.spec_from_file_location("thozha_test_mac_read_mcp", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        module.platform,
        "mac_ver",
        lambda: ("15.1", ("", "", ""), "arm64"),
    )
    monkeypatch.setattr(module.psutil, "boot_time", lambda: 1_700_000_000)
    monkeypatch.setattr(module.time, "time", lambda: 1_700_003_600)
    monkeypatch.setattr(
        module,
        "_begin_mac_read_audit",
        lambda _operation: (
            "a" * 32,
            SimpleNamespace(record_event=lambda *_args, **_kwargs: None, close=lambda: None),
        ),
    )

    names = {item["name"] for item in module.tool_list()}
    assert names == {"get_disk_usage", "get_system_info"}
    response = module.call_tool("get_system_info", {})
    facts = json.loads(response["content"][0]["text"])
    assert facts == {"macos_version": "15.1", "uptime_seconds": 3600}
    assert module.call_tool("get_active_app", {})["isError"] is True
    module.MODE = "basic"
    assert {item["name"] for item in module.tool_list()} == {"get_disk_usage"}
    assert module.call_tool("get_system_info", {})["isError"] is True

    from openjarvis.tasks.supervisor import bind_task

    with bind_task("a" * 32, source="jarvis_link", scope="mac_read"):
        assert codex_sdk._desktop_mode_for_current_task() == "mac_read"

    monkeypatch.setattr(
        codex_sdk,
        "_call_local_mcp",
        lambda tool, arguments, mode, **_kwargs: {
            "content": [{"type": "text", "text": '{"macos_version":"15.1"}'}]
        },
    )
    handler = codex_sdk._approval_handler("mac_read")
    result = handler(
        "mcpServer/tool/call",
        {"server": "jarvis_mac", "tool": "get_system_info", "arguments": {}},
    )
    assert result["content"][0]["text"] == '{"macos_version":"15.1"}'
    screen = handler(
        "mcpServer/tool/call",
        {"server": "jarvis_mac", "tool": "get_active_app", "arguments": {}},
    )
    assert screen["isError"] is True
    control = handler(
        "mcpServer/tool/call",
        {"server": "jarvis_mac", "tool": "open_app", "arguments": {"name": "Calculator"}},
    )
    assert control["isError"] is True


def test_link_capability_sets_keep_mac_diagnostics_separate_from_read_only():
    from openjarvis.tasks.supervisor import LINK_SCOPE_CAPABILITIES, is_link_scope

    assert is_link_scope(["read_only"])
    assert is_link_scope(["mac_read"])
    assert LINK_SCOPE_CAPABILITIES["read_only"] == frozenset({"file:read"})
    assert LINK_SCOPE_CAPABILITIES["mac_read"] == frozenset(
        {"file:read", "tool:read"}
    )


def test_read_only_mac_tool_records_content_free_supervisor_events(tmp_path, monkeypatch):
    from openjarvis.tasks import supervisor

    store = supervisor.SupervisorStore(str(tmp_path / "supervisor.db"))
    key = "mac-read-audit-test-0000000001"
    task = store.create_link_task(
        "Read system version and uptime",
        capability_scope=["mac_read"],
        idempotency_key=key,
        owner_actor="device:" + ("a" * 32),
    )
    store.start_link_task(
        task["task_id"],
        objective_digest=task["objective_digest"],
        objective_text_digest=task["objective_digest"],
        capability_scope=["mac_read"],
        idempotency_key=key,
    )
    monkeypatch.setattr(supervisor, "get_supervisor_store", lambda: store)
    monkeypatch.setattr(
        codex_sdk,
        "_call_local_mcp",
        lambda *_args, **_kwargs: {
            "content": [{"type": "text", "text": '{"macos_version":"15.1"}'}]
        },
    )
    with supervisor.bind_task(
        task["task_id"],
        source="jarvis_link",
        scope="mac_read",
        allowed_capabilities=supervisor.LINK_SCOPE_CAPABILITIES["mac_read"],
    ):
        response = codex_sdk._approval_handler("mac_read")(
            "mcpServer/tool/call",
            {"server": "jarvis_mac", "tool": "get_system_info", "arguments": {}},
        )
    assert response["content"][0]["text"] == '{"macos_version":"15.1"}'
    events = store.events(task["task_id"])
    audit = [event for event in events if event["event_type"].startswith("mac.read_only_tool.")]
    assert [event["event_type"] for event in audit] == [
        "mac.read_only_tool.started",
        "mac.read_only_tool.completed",
    ]
    assert audit[-1]["payload"] == {"operation": "get_system_info", "success": True}
    assert "content" not in json.dumps(audit)
    store.close()


def test_mac_read_mcp_process_audits_fixed_tool_against_active_scoped_task(
    tmp_path, monkeypatch
):
    from openjarvis.tasks.supervisor import SupervisorStore

    home = tmp_path / "isolated-openjarvis"
    monkeypatch.setenv("OPENJARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "mac_read")
    store = SupervisorStore(str(home / "supervisor.db"), run_recovery=False)
    key = "mac-read-mcp-process-audit-0001"
    run = store.create_link_task(
        "Read fixed system facts",
        capability_scope=["mac_read"],
        idempotency_key=key,
        owner_actor="device:" + ("a" * 32),
    )
    task_id = run["task_id"]
    store.start_link_task(
        task_id,
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["mac_read"],
        idempotency_key=key,
    )
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_ID", task_id)
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SOURCE", "jarvis_link")
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SCOPE", "mac_read")
    module_path = codex_sdk.Path(__file__).resolve().parents[2] / "src" / "openjarvis" / "mac_desktop_mcp.py"
    spec = importlib.util.spec_from_file_location("thozha_test_mac_read_audited", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    response = module.call_tool("get_system_info", {})
    assert response.get("isError") is not True
    facts = json.loads(response["content"][0]["text"])
    assert facts["macos_version"]
    assert facts["uptime_seconds"] > 0
    audit = [
        event
        for event in store.events(task_id)
        if event["event_type"].startswith("mac.read_only_tool.")
    ]
    assert [event["event_type"] for event in audit] == [
        "mac.read_only_tool.started",
        "mac.read_only_tool.completed",
    ]
    assert audit[-1]["payload"] == {"operation": "get_system_info", "success": True}
    assert "uptime_seconds" not in json.dumps(audit)
    store.close()


def test_mac_read_mcp_refuses_missing_supervisor_task_context(monkeypatch):
    module_path = codex_sdk.Path(__file__).resolve().parents[2] / "src" / "openjarvis" / "mac_desktop_mcp.py"
    spec = importlib.util.spec_from_file_location("thozha_test_mac_read_no_context", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "mac_read")
    monkeypatch.delenv("JARVIS_SUPERVISOR_TASK_ID", raising=False)
    monkeypatch.delenv("JARVIS_SUPERVISOR_TASK_SOURCE", raising=False)
    monkeypatch.delenv("JARVIS_SUPERVISOR_TASK_SCOPE", raising=False)
    spec.loader.exec_module(module)
    response = module.call_tool("get_system_info", {})
    assert response["isError"] is True
    assert "PermissionError" in response["content"][0]["text"]

    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "basic")
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SOURCE", "jarvis_link")
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SCOPE", "read_only")
    monkeypatch.delenv("JARVIS_SUPERVISOR_TASK_ID", raising=False)
    monkeypatch.delenv("JARVIS_SUPERVISOR_DB", raising=False)
    basic_spec = importlib.util.spec_from_file_location(
        "thozha_test_basic_read_no_context", module_path
    )
    assert basic_spec is not None and basic_spec.loader is not None
    basic_module = importlib.util.module_from_spec(basic_spec)
    basic_spec.loader.exec_module(basic_module)
    monkeypatch.setattr(
        basic_module,
        "disk_usage",
        lambda: (_ for _ in ()).throw(AssertionError("Mac tool must not execute")),
    )
    denied = basic_module.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_disk_usage", "arguments": {}},
        }
    )
    assert denied["result"]["isError"] is True
    assert "PermissionError" in denied["result"]["content"][0]["text"]


def test_read_only_link_disk_tool_persists_content_free_execution_audit(
    tmp_path, monkeypatch
):
    from openjarvis.tasks.supervisor import SupervisorStore

    home = tmp_path / "isolated-openjarvis"
    monkeypatch.setenv("OPENJARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "basic")
    task_db = home / "supervisor.db"
    store = SupervisorStore(str(task_db), run_recovery=False)
    key = "readonly-link-disk-audit-test-001"
    run = store.create_link_task(
        "Check Mac disk space",
        capability_scope=["read_only"],
        idempotency_key=key,
        owner_actor="device:" + ("a" * 32),
    )
    task_id = run["task_id"]
    store.start_link_task(
        task_id,
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["read_only"],
        idempotency_key=key,
    )
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_ID", task_id)
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SOURCE", "jarvis_link")
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SCOPE", "read_only")
    monkeypatch.setenv("JARVIS_SUPERVISOR_DB", str(task_db))
    module_path = codex_sdk.Path(__file__).resolve().parents[2] / "src" / "openjarvis" / "mac_desktop_mcp.py"
    spec = importlib.util.spec_from_file_location("thozha_test_basic_disk_audit", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1_000_000_000, free=500_000_000),
    )

    names = {item["name"] for item in module.tool_list()}
    assert names == {"get_disk_usage"}
    response = module.call_tool("get_disk_usage", {})
    assert response.get("isError") is not True
    facts = json.loads(response["content"][0]["text"])
    assert facts["mount"] == "/"
    assert facts["free_gb"] == 0.5
    audit = [
        event
        for event in store.events(task_id)
        if event["event_type"].startswith("mac.read_only_tool.")
    ]
    assert [event["event_type"] for event in audit] == [
        "mac.read_only_tool.started",
        "mac.read_only_tool.completed",
    ]
    assert audit[-1]["payload"] == {"operation": "get_disk_usage", "success": True}
    assert "free_gb" not in json.dumps([event["payload"] for event in audit])
    store.close()
