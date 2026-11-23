"""Web conversation tool-denial checks for the Codex SDK adapter."""

from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

from openjarvis.core.types import Message, Role
from openjarvis.engine import codex_sdk


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
