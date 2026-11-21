"""Authenticated, one-shot Codex Link task execution tests."""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openjarvis.core.types import ToolCall, ToolResult
from starlette.requests import Request

import openjarvis.tasks.supervisor as supervisor
from openjarvis.agents._stubs import AgentResult
from openjarvis.server.routes import _begin_supervised_request
from openjarvis.server.supervisor_routes import router
from openjarvis.tasks.supervisor import SupervisorStore, bind_task, current_task_context
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec


class FakeCodexEngine:
    engine_id = "codex_sdk"


class FakeAgent:
    def __init__(self):
        self._model = "gpt-6-luna"
        self.contexts = []
        self.calls = []

    def run(self, text, *, context=None):
        self.calls.append(text)
        self.contexts.append(current_task_context())
        return AgentResult(content="Safe summary: " + text)


class FakeMacControlAgent(FakeAgent):
    """Test-only Codex stand-in that asks for one MCP action, then continues."""

    def run(self, text, *, context=None):
        self.calls.append(text)
        self.contexts.append(current_task_context())
        if len(self.calls) == 1:
            from openjarvis.engine.codex_sdk import _approval_handler

            self.approval_result = _approval_handler("control")(
                "mcpServer/tool/call",
                {"server": "jarvis_mac", "tool": "open_app", "arguments": {"name": "TextEdit"}},
            )
            return AgentResult(content="Waiting for the approved Mac action.")
        return AgentResult(content="TextEdit opened after approval; the task is complete.")


class FakeShell(BaseTool):
    def __init__(self):
        self.calls = []

    @property
    def spec(self):
        return ToolSpec(name="shell_exec", description="fake shell", parameters={}, required_capabilities=["code:execute"])

    def execute(self, **params):
        self.calls.append(params)
        return ToolResult(tool_name="shell_exec", content="executed")


@pytest.fixture
def link_client(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "link-test-admin-key")
    monkeypatch.setenv("JARVIS_MCP_OPEN_APP_ENABLED", "1")
    # Exercise the approval-enabled path explicitly; production defaults this
    # MCP capability off until an operator sets the narrow runtime flag.
    monkeypatch.setenv("JARVIS_MCP_OPEN_APP_ENABLED", "1")
    app = FastAPI()
    app.include_router(router)
    app.state.engine = FakeCodexEngine()
    app.state.model = "gpt-6-luna"
    app.state.agent = FakeAgent()
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        yield client, store, app.state.agent
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def _request(headers: dict[str, str], client: tuple[str, int] = ("127.0.0.1", 12345)) -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": client,
        "server": ("127.0.0.1", 8000),
    })


def test_link_runner_executes_codex_agent_once_and_returns_persisted_digest(link_client):
    client, store, agent = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {
        "objective": "Summarize this read-only task",
        "capability_scope": ["read_only"],
        "idempotency_key": "link-run-2026-11-16-0001",
        "requester_device_id": "a" * 32,
    }
    response = client.post("/v1/jarvis/link/tasks", headers=headers, json=body)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result["state"] == "COMPLETED"
    assert result["summary"] == "Safe summary: Summarize this read-only task"
    assert len(result["result_digest"]) == 64
    assert result["idempotent_replay"] is False
    assert store.get_task(result["task_id"])["state"] == "COMPLETED"
    run = store.link_task_run(result["task_id"])
    assert run["status"] == "COMPLETED"
    assert run["scope"] == '["read_only"]'
    assert run["scope_digest"] == hashlib.sha256(run["scope"].encode()).hexdigest()
    context = agent.contexts[0]
    assert context.source == "jarvis_link"
    assert context.scope == "read_only"
    assert context.allowed_capabilities == frozenset({"file:read"})

    replay = client.post("/v1/jarvis/link/tasks", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["result_digest"] == result["result_digest"]
    assert len(agent.calls) == 1

    status = client.get("/v1/jarvis/link/tasks/" + body["idempotency_key"], headers=headers)
    assert status.status_code == 200
    assert status.json() == {
        "task_id": result["task_id"],
        "state": "COMPLETED",
        "summary": result["summary"],
        "result_text": "Safe summary: Summarize this read-only task",
        "result_digest": result["result_digest"],
    }


def test_link_runner_requires_loopback_auth_and_supported_scope(link_client):
    client, _, _ = link_client
    body = {"objective": "read safely", "capability_scope": ["read_only"], "idempotency_key": "link-run-unique-0001", "requester_device_id": "a" * 32}
    assert client.post("/v1/jarvis/link/tasks", json=body).status_code == 401
    assert client.post(
        "/v1/jarvis/link/tasks",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json={**body, "capability_scope": ["desktop_observation"]},
    ).status_code == 403


def test_link_task_submit_requires_and_binds_verified_requester_device(link_client):
    client, store, agent = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {
        "objective": "Read the current Mac status",
        "capability_scope": ["read_only"],
        "idempotency_key": "link-owner-binding-submit-01",
        "requester_device_id": "a" * 32,
    }
    created = client.post("/v1/jarvis/link/tasks", headers=headers, json=body)
    assert created.status_code == 200, created.json()
    task = store.get_task(created.json()["task_id"])
    assert json.loads(task["recovery_json"])["owner_actor"] == "device:" + "a" * 32

    missing = client.post(
        "/v1/jarvis/link/tasks", headers=headers,
        json={key: value for key, value in body.items() if key != "requester_device_id"},
    )
    assert missing.status_code == 422
    conflicting_replay = client.post(
        "/v1/jarvis/link/tasks", headers=headers,
        json={**body, "requester_device_id": "b" * 32},
    )
    assert conflicting_replay.status_code == 403
    assert agent.calls == [body["objective"]]

    remote = TestClient(client.app, base_url="http://127.0.0.1:8000", client=("192.168.1.7", 12345))
    assert remote.post(
        "/v1/jarvis/link/tasks",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json=body,
    ).status_code == 403


def test_link_runner_rejects_conflicting_idempotency_reuse(link_client):
    client, _, agent = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {"objective": "first objective", "capability_scope": ["read_only"], "idempotency_key": "link-run-conflict-0001", "requester_device_id": "a" * 32}
    assert client.post("/v1/jarvis/link/tasks", headers=headers, json=body).status_code == 200
    changed = {**body, "objective": "different objective"}
    assert client.post("/v1/jarvis/link/tasks", headers=headers, json=changed).status_code == 409
    assert len(agent.calls) == 1


def test_link_request_and_codex_prompt_keep_newlines_and_literal_markers(link_client):
    from types import SimpleNamespace
    from openjarvis.engine.codex_sdk import _serialize_messages

    client, _store, agent = link_client
    objective = "I\napprove\nOpen the Calculator app %n **approve**"
    response = client.post(
        "/v1/jarvis/link/tasks",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json={
            "objective": objective,
            "capability_scope": ["read_only"],
            "idempotency_key": "multiline-link-request-0001",
            "requester_device_id": "a" * 32,
        },
    )
    assert response.status_code == 200
    assert agent.calls == [objective]
    assert response.json()["summary"] == "Safe summary: " + objective
    prompt = _serialize_messages([SimpleNamespace(role="user", content=objective)])
    assert prompt == "USER: " + objective


def test_fast_mac_status_is_deterministic_and_does_not_call_agent(link_client):
    client, _store, agent = link_client
    response = client.post(
        "/v1/jarvis/link/fast-command",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json={"command": "mac.status", "requester_device_id": "a" * 32},
    )
    assert response.status_code == 200
    assert response.json() == {
        "command": "mac.status", "state": "COMPLETED", "online": True,
        "ai_available": True, "executed": True, "success": True, "summary": "Mac is online.",
    }
    assert agent.calls == []


@pytest.mark.parametrize(
    ("bundle_id", "display_name"),
    [("com.apple.calculator", "Calculator"), ("com.spotify.client", "Spotify")],
)
def test_trusted_bundle_fast_open_is_durable_and_bypasses_codex(link_client, monkeypatch, bundle_id, display_name):
    import types
    import openjarvis.server.supervisor_routes as routes

    client, store, agent = link_client
    # The MCP kill switch is scoped away from this typed, trusted fast path.
    monkeypatch.setenv("JARVIS_MCP_OPEN_APP_ENABLED", "0")
    monkeypatch.setattr(routes.sys, "platform", "darwin")
    calls = []

    def fake_open(argv, **kwargs):
        calls.append((argv, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(routes.subprocess, "run", fake_open)
    request_id = uuid.uuid4().hex
    body = {
        "command": "app.open",
        "requester_device_id": "a" * 32,
        "app": bundle_id,
        "idempotency_key": request_id,
    }
    headers = {"Authorization": "Bearer link-test-admin-key"}

    response = client.post("/v1/jarvis/link/fast-command", headers=headers, json=body)
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "command": "app.open",
        "state": "COMPLETED",
        "executed": True,
        "success": True,
        "bundle_id": bundle_id,
        "summary": f"{display_name} opened on the Mac.",
        "idempotent_replay": False,
    }
    assert calls == [(
        ["/usr/bin/open", "-b", bundle_id],
        {"capture_output": True, "text": True, "timeout": 12, "check": False},
    )]
    assert agent.calls == []
    assert store.pending_approvals(20) == []

    replay = client.post("/v1/jarvis/link/fast-command", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json()["state"] == "COMPLETED"
    assert replay.json()["idempotent_replay"] is True
    assert len(calls) == 1


def test_trusted_bundle_fast_open_failure_is_truthful_and_not_retried(link_client, monkeypatch):
    import types
    import openjarvis.server.supervisor_routes as routes

    client, store, agent = link_client
    monkeypatch.setattr(routes.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or types.SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    body = {
        "command": "app.open", "requester_device_id": "a" * 32,
        "app": "com.spotify.client", "idempotency_key": uuid.uuid4().hex,
    }
    headers = {"Authorization": "Bearer link-test-admin-key"}

    first = client.post("/v1/jarvis/link/fast-command", headers=headers, json=body)
    second = client.post("/v1/jarvis/link/fast-command", headers=headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["state"] == second.json()["state"] == "FAILED"
    assert first.json()["success"] is second.json()["success"] is False
    assert first.json()["executed"] is second.json()["executed"] is False
    assert second.json()["idempotent_replay"] is True
    assert len(calls) == 1
    assert agent.calls == []
    assert store.pending_approvals(20) == []


def test_fast_open_replay_after_stale_inflight_claim_is_indeterminate_not_reexecuted(link_client, monkeypatch):
    import openjarvis.server.supervisor_routes as routes

    client, store, agent = link_client
    monkeypatch.setattr(routes.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(routes.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    request_id = uuid.uuid4().hex
    store.claim_fast_command(
        request_id=request_id, requester="a" * 32,
        command="app.open", target="com.apple.calculator",
    )
    store._db.execute(
        "UPDATE fast_command_requests SET updated_at=? WHERE request_id=?",
        ("2000-01-01T00:00:00+00:00", request_id),
    )
    store._db.commit()
    response = client.post(
        "/v1/jarvis/link/fast-command",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json={
            "command": "app.open", "requester_device_id": "a" * 32,
            "app": "com.apple.calculator", "idempotency_key": request_id,
        },
    )
    assert response.status_code == 200
    assert response.json()["state"] == "INDETERMINATE"
    assert response.json()["executed"] is False
    assert response.json()["success"] is False
    assert calls == []
    assert agent.calls == []


def test_fast_open_rejects_unknown_or_injected_bundle_and_idempotency_conflict(link_client, monkeypatch):
    import openjarvis.server.supervisor_routes as routes

    client, store, agent = link_client
    calls = []
    monkeypatch.setattr(routes.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    headers = {"Authorization": "Bearer link-test-admin-key"}
    request_id = uuid.uuid4().hex
    base = {"command": "app.open", "requester_device_id": "a" * 32, "idempotency_key": request_id}

    unknown = client.post(
        "/v1/jarvis/link/fast-command", headers=headers,
        json={**base, "app": "com.attacker.app;touch /tmp/x"},
    )
    assert unknown.status_code == 422
    assert calls == []

    monkeypatch.setattr(routes.sys, "platform", "darwin")
    monkeypatch.setattr(
        routes.subprocess, "run",
        lambda argv, **kwargs: calls.append(argv) or __import__("types").SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    valid = {**base, "app": "com.apple.calculator"}
    assert client.post("/v1/jarvis/link/fast-command", headers=headers, json=valid).status_code == 200
    conflict = client.post(
        "/v1/jarvis/link/fast-command", headers=headers,
        json={**valid, "requester_device_id": "b" * 32},
    )
    assert conflict.status_code == 409
    assert len(calls) == 1
    assert agent.calls == []
    assert store.pending_approvals(20) == []


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Now drawing from 'AC Power'\n -InternalBattery-0 (id=123) 83%; charging; 0:43 remaining present: true", (83, True)),
        ("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=123) 65%; discharging; 2:18 remaining present: true", (65, False)),
        ("No battery information available.", (None, None)),
    ],
)
def test_pmset_battery_parser_reports_actual_fields(output, expected):
    from openjarvis.server.supervisor_routes import _parse_pmset_battery

    assert _parse_pmset_battery(output) == expected


def test_mac_battery_fast_command_uses_fixed_bounded_provider(link_client, monkeypatch):
    import types
    import openjarvis.server.supervisor_routes as routes

    client, store, agent = link_client
    monkeypatch.setattr(routes.sys, "platform", "darwin")
    calls = []

    def fake_pmset(argv, **kwargs):
        calls.append((argv, kwargs))
        return types.SimpleNamespace(
            returncode=0,
            stdout="Now drawing from 'AC Power'\n -InternalBattery-0 (id=123) 94%; charged; (no estimate) present: true",
            stderr="",
        )

    monkeypatch.setattr(routes.subprocess, "run", fake_pmset)
    response = client.post(
        "/v1/jarvis/link/fast-command",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json={"command": "mac.battery", "requester_device_id": "a" * 32},
    )
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "command": "mac.battery", "state": "COMPLETED", "executed": True, "success": True,
        "battery_percent": 94, "charging": False,
        "summary": "Mac battery is 94% and is not charging.",
    }
    assert calls == [(
        ["/usr/bin/pmset", "-g", "batt"],
        {"capture_output": True, "text": True, "timeout": 3, "check": False},
    )]
    assert agent.calls == []
    assert store.pending_approvals(20) == []


def test_mac_battery_failure_and_non_mac_do_not_fabricate_status(link_client, monkeypatch):
    import types
    import openjarvis.server.supervisor_routes as routes

    client, store, agent = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {"command": "mac.battery", "requester_device_id": "a" * 32}
    calls = []
    monkeypatch.setattr(routes.sys, "platform", "darwin")
    monkeypatch.setattr(
        routes.subprocess, "run",
        lambda argv, **kwargs: calls.append(argv) or types.SimpleNamespace(returncode=1, stdout="", stderr="private provider detail"),
    )
    failed = client.post("/v1/jarvis/link/fast-command", headers=headers, json=body)
    assert failed.status_code == 200
    assert failed.json()["state"] == "FAILED"
    assert failed.json()["success"] is False
    assert failed.json()["battery_percent"] is None
    assert "private provider detail" not in failed.text
    assert calls == [["/usr/bin/pmset", "-g", "batt"]]

    monkeypatch.setattr(routes.sys, "platform", "linux")
    calls.clear()
    desktop = client.post("/v1/jarvis/link/fast-command", headers=headers, json=body)
    assert desktop.status_code == 200
    assert desktop.json()["state"] == "FAILED"
    assert desktop.json()["success"] is False
    assert desktop.json()["battery_percent"] is None
    assert calls == []
    assert agent.calls == []
    assert store.pending_approvals(20) == []


def test_mac_battery_reports_desktop_without_fabricating_percentage(link_client, monkeypatch):
    import types
    import openjarvis.server.supervisor_routes as routes

    client, _, agent = link_client
    monkeypatch.setattr(routes.sys, "platform", "darwin")
    monkeypatch.setattr(
        routes.subprocess, "run",
        lambda argv, **kwargs: types.SimpleNamespace(returncode=0, stdout="No battery information available.", stderr=""),
    )
    response = client.post(
        "/v1/jarvis/link/fast-command",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json={"command": "mac.battery", "requester_device_id": "a" * 32},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "FAILED"
    assert response.json()["executed"] is True
    assert response.json()["success"] is False
    assert response.json()["battery_percent"] is None
    assert response.json()["charging"] is None
    assert "does not report an internal battery" in response.json()["summary"]
    assert agent.calls == []


def test_fast_task_status_is_owner_bound_and_does_not_call_agent(link_client):
    client, store, agent = link_client
    task = store.create_link_task(
        "Read task status", capability_scope=["read_only"],
        idempotency_key="fast-status-owner-check-0001", owner_actor="device:" + "a" * 32,
    )
    headers = {"Authorization": "Bearer link-test-admin-key"}
    allowed = client.post("/v1/jarvis/link/fast-command", headers=headers,
                          json={"command": "task.status", "requester_device_id": "a" * 32, "task_id": task["task_id"]})
    denied = client.post("/v1/jarvis/link/fast-command", headers=headers,
                         json={"command": "task.status", "requester_device_id": "b" * 32, "task_id": task["task_id"]})
    assert allowed.status_code == 200 and allowed.json()["state"] == "CREATED"
    assert denied.status_code == 403
    assert agent.calls == []


def test_fast_task_status_reports_approval_expiry_without_running_the_operation(link_client):
    client, store, agent = link_client
    task = store.create_link_task(
        "Open Calculator on the Mac", capability_scope=["assistant"],
        idempotency_key="fast-status-expiry-owner-0001", owner_actor="device:" + "a" * 32,
    )
    task_id = task["task_id"]
    store.start_link_task(
        task_id, objective_digest=task["objective_digest"], objective_text_digest=task["objective_digest"],
        capability_scope=["assistant"], idempotency_key=task["idempotency_key"],
    )
    approval_id, _ = store.create_approval(
        task_id, "mcp:jarvis_mac:open_app", "Calculator", {"name": "Calculator"},
        "Exact app opening requires approval.", ttl_seconds=600,
    )
    store.mark_link_task_waiting_approval(task_id, approval_id)
    store.expire_approval(approval_id)

    response = client.post(
        "/v1/jarvis/link/fast-command",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json={"command": "task.status", "requester_device_id": "a" * 32, "task_id": task_id},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "EXPIRED"
    assert "expired" in response.json()["summary"].lower()
    # Status inspection does not rewrite task history, consume approval, or execute Calculator.
    assert store.get_task(task_id)["state"] == "WAITING_APPROVAL"
    assert store.approval(approval_id)["status"] == "EXPIRED"
    assert agent.calls == []


def test_fast_calculator_creates_exact_approval_without_invoking_agent(link_client):
    client, store, agent = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {"command": "app.open", "requester_device_id": "c" * 32, "app": "Calculator", "idempotency_key": "d" * 32}
    response = client.post("/v1/jarvis/link/fast-command", headers=headers, json=body)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result["state"] == "WAITING_APPROVAL"
    assert result["approval"]["operation"] == "mcp:jarvis_mac:open_app"
    assert result["approval"]["target"] == "Calculator"
    assert result["approval"]["canonical_arguments_digest"]
    approvals = [row for row in store.pending_approvals(20) if row["task_id"] == result["task_id"]]
    assert len(approvals) == 1
    assert approvals[0]["status"] == "PENDING"
    replay = client.post("/v1/jarvis/link/fast-command", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json()["task_id"] == result["task_id"]
    assert replay.json()["approval"]["approval_id"] == result["approval"]["approval_id"]
    assert agent.calls == []


def test_fast_command_rejects_unapproved_app_and_extra_fields(link_client):
    client, _store, agent = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    base = {"command": "app.open", "requester_device_id": "d" * 32, "idempotency_key": "f" * 32}
    assert client.post("/v1/jarvis/link/fast-command", headers=headers, json={**base, "app": "Terminal"}).status_code == 422
    assert client.post("/v1/jarvis/link/fast-command", headers=headers, json={**base, "app": "Calculator", "shell": "echo bad"}).status_code == 422
    assert agent.calls == []


def test_existing_link_task_header_requires_local_auth_digest_scope_and_idempotency(link_client):
    _, store, _ = link_client
    objective = "Run one exact Codex Link turn"
    idempotency_key = "link-host-idempotency-0001"
    created = store.create_link_task(objective, capability_scope=["read_only"], idempotency_key=idempotency_key)
    task_id = created["task_id"]
    digest = created["objective_digest"]
    headers = {
        "Authorization": "Bearer link-test-admin-key",
        "X-OpenJarvis-Task": task_id,
        "X-OpenJarvis-Objective-Digest": digest,
        "Idempotency-Key": idempotency_key,
    }
    assert _begin_supervised_request(_request(headers), objective) == task_id
    assert store.get_task(task_id)["state"] == "RUNNING"
    with pytest.raises(HTTPException) as replay:
        _begin_supervised_request(_request(headers), objective)
    assert replay.value.status_code == 409

    other = store.create_link_task("Different objective", capability_scope=["read_only"], idempotency_key="link-host-idempotency-0002")
    bad_headers = {**headers, "X-OpenJarvis-Task": other["task_id"], "X-OpenJarvis-Objective-Digest": "0" * 64}
    with pytest.raises(HTTPException) as mismatch:
        _begin_supervised_request(_request(bad_headers), "Different objective")
    assert mismatch.value.status_code == 409

    unauthenticated = {key: value for key, value in headers.items() if key != "Authorization"}
    with pytest.raises(HTTPException) as denied:
        _begin_supervised_request(_request(unauthenticated), objective)
    assert denied.value.status_code == 401


def test_link_engine_context_selects_basic_mcp_mode(monkeypatch):
    from openjarvis.engine.codex_sdk import _approval_handler, _desktop_mode_for_current_task

    assert _desktop_mode_for_current_task() == "control"
    with bind_task("f" * 32, source="jarvis_link", scope="read_only", allowed_capabilities=frozenset({"file:read"})):
        assert _desktop_mode_for_current_task() == "basic"
        handler = _approval_handler("basic")
        assert handler("item/commandExecution/requestApproval", {})["decision"] == "decline"
        assert handler("item/fileChange/requestApproval", {})["decision"] == "decline"
        assert handler("mcpServer/tool/call", {"server": "other", "tool": "get_disk_usage"})["isError"] is True


def test_development_codex_cwd_and_registered_mcp_are_workspace_scoped(tmp_path, monkeypatch):
    from pathlib import Path
    from openjarvis.engine.codex_sdk import _config_overrides, _working_directory_for_current_task

    workspace = tmp_path / "taskflow"
    workspace.mkdir()
    monkeypatch.setenv("JARVIS_DEV_WORKSPACE", str(workspace))
    with bind_task(
        "a" * 32,
        source="jarvis_link",
        scope="development",
        allowed_capabilities=supervisor.LINK_SCOPE_CAPABILITIES["development"],
    ):
        assert Path(_working_directory_for_current_task()) == workspace.resolve()
        overrides = _config_overrides("control")
        assert any("mcp_servers.jarvis_dev.command" in entry for entry in overrides)
        assert not any("mcp_servers.jarvis_mac." in entry for entry in overrides)
        assert "features.shell_tool=false" in overrides
        assert "features.unified_exec=false" in overrides
        assert 'mcp_servers.jarvis_dev.env.JARVIS_DEV_EXECUTION_MODE="vm"' in overrides
        assert "features.unified_exec_tty=false" in overrides

        monkeypatch.setenv("JARVIS_DEV_WORKER_CONFIG", "/tmp/jarvis-worker.json")
        monkeypatch.setenv("JARVIS_SUPERVISOR_DB", "/tmp/jarvis-supervisor.db")
        overrides = _config_overrides("control")
        assert 'mcp_servers.jarvis_dev.env.JARVIS_DEV_WORKER_CONFIG="/tmp/jarvis-worker.json"' in overrides
        assert 'mcp_servers.jarvis_dev.env.JARVIS_SUPERVISOR_DB="/tmp/jarvis-supervisor.db"' in overrides

    with bind_task(
        "b" * 32,
        source="jarvis_link",
        scope="assistant",
        allowed_capabilities=supervisor.LINK_SCOPE_CAPABILITIES["assistant"],
    ):
        assert Path(_working_directory_for_current_task()) == Path.home() / ".openjarvis"
        assistant_overrides = _config_overrides("control")
        assert "features.shell_tool=false" not in assistant_overrides


def test_codex_mcp_callback_routes_only_bound_development_tools(link_client, monkeypatch):
    from openjarvis.engine.codex_sdk import _approval_handler

    _, store, _ = link_client
    owner = "device:" + "d" * 32
    run = store.create_link_task(
        "Create a bounded evaluation project",
        capability_scope=["development"],
        idempotency_key="codex-dev-mcp-callback-0001",
        owner_actor=owner,
    )
    task_id = run["task_id"]
    store.start_link_task(
        task_id,
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["development"],
        idempotency_key=run["idempotency_key"],
    )
    dispatched = []

    def fake_dev_call(tool, arguments, **authorization):
        dispatched.append((tool, dict(arguments), authorization))
        return {"content": [{"type": "text", "text": "isolated dev tool result"}], "isError": False}

    monkeypatch.setattr("openjarvis.engine.codex_sdk._call_local_dev_mcp", fake_dev_call)
    handler = _approval_handler("control")
    with bind_task(
        task_id,
        source="jarvis_link",
        scope="development",
        allowed_capabilities=supervisor.LINK_SCOPE_CAPABILITIES["development"],
    ):
        result = handler(
            "mcpServer/tool/call",
            {"server": "jarvis_dev", "tool": "list_project_files", "arguments": {"dir": "."}},
        )
        denied = handler(
            "mcpServer/tool/call",
            {"server": "jarvis_dev", "tool": "shell_exec", "arguments": {"command": "open -a TextEdit"}},
        )
    assert result["isError"] is False
    assert result["content"][0]["text"] == "isolated dev tool result"
    assert len(dispatched) == 1
    assert dispatched[0][0:2] == ("list_project_files", {"dir": "."})
    assert dispatched[0][2] == {
        "supervisor_task_id": task_id,
        "supervisor_task_source": "jarvis_link",
        "supervisor_task_scope": "development",
    }
    assert denied["isError"] is True
    assert len(dispatched) == 1


def test_link_executor_denies_code_execution_outside_read_only_scope(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    link = store.create_link_task(
        "inspect read-only state",
        capability_scope=["read_only"],
        idempotency_key="link-tool-scope-test-001",
    )
    store.start_link_task(
        link["task_id"],
        objective_digest=link["objective_digest"],
        objective_text_digest=link["objective_digest"],
        capability_scope=["read_only"],
        idempotency_key=link["idempotency_key"],
    )
    shell = FakeShell()
    executor = ToolExecutor([shell], supervisor_store=store)
    with bind_task(
        link["task_id"],
        source="jarvis_link",
        scope="read_only",
        allowed_capabilities=frozenset({"file:read"}),
    ):
        result = executor.execute(ToolCall(id="link-shell", name="shell_exec", arguments=json.dumps({"command": "echo unsafe"})))
    assert not result.success
    assert result.metadata["error_code"] == "LINK_SCOPE_DENIED"
    assert shell.calls == []
    store.close()


def _install_control_agent(client, monkeypatch):
    agent = FakeMacControlAgent()
    client.app.state.agent = agent
    return agent


def test_phone_assistant_scope_waits_for_exact_approval_then_continues(link_client, monkeypatch):
    client, store, _ = link_client
    agent = _install_control_agent(client, monkeypatch)
    dispatched = []

    def fake_mcp_call(tool, arguments, mode, **authorization):
        dispatched.append((tool, dict(arguments), mode, authorization))
        return {"content": [{"type": "text", "text": "Opened TextEdit."}], "isError": False}

    monkeypatch.setattr("openjarvis.engine.codex_sdk._call_local_mcp", fake_mcp_call)
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {
        "objective": "Open TextEdit, then tell me it is open.",
        "capability_scope": ["assistant"],
        "idempotency_key": "phone-assistant-flow-0001",
        "requester_device_id": "a" * 32,
    }
    accepted = client.post("/v1/jarvis/link/tasks", headers=headers, json=body)
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    assert accepted.json()["state"] == "WAITING_APPROVAL"
    assert agent.approval_result["isError"] is True
    assert dispatched == []
    approval = next(item for item in store.pending_approvals(20) if item["task_id"] == task_id)
    assert approval["operation"] == "mcp:jarvis_mac:open_app"
    assert approval["target"] == "TextEdit"
    store.mark_link_task_waiting_approval(task_id, approval["id"])
    assert approval["status"] == "PENDING"
    store.recover_after_restart()
    assert store.get_task(task_id)["state"] == "PAUSED"
    assert store.approval(approval["id"])["status"] == "PENDING"
    assert dispatched == []

    decision = {
        "decision": "approve",
        "task_id": task_id,
        "expected_operation_digest": approval["args_digest"],
        "requester_device_id": "a" * 32,
        "idempotency_key": "phone-approval-once-0001",
        "continuation_objective": body["objective"],
    }
    result = client.post(
        f"/v1/jarvis/approvals/{approval['id']}/decision",
        headers=headers,
        json=decision,
    )
    assert result.status_code == 200
    assert result.json()["status"] == "CONSUMED"
    assert result.json()["executed"] is True
    assert result.json()["success"] is True
    assert result.json()["execution_state"] == "COMPLETED"
    assert result.json()["result_summary"] == "Opened TextEdit."
    assert result.json()["task_state"] == "COMPLETED"
    assert len(dispatched) == 1
    assert dispatched[0][:3] == ("open_app", {"name": "TextEdit"}, "control")
    assert dispatched[0][3]["supervisor_approved"] is True
    assert dispatched[0][3]["supervisor_approval_id"] == approval["id"]
    assert dispatched[0][3]["supervisor_task_id"] == task_id
    assert dispatched[0][3]["supervisor_operation"] == approval["operation"]
    assert dispatched[0][3]["supervisor_target"] == approval["target"]
    assert dispatched[0][3]["supervisor_operation_digest"] == approval["args_digest"]
    assert agent.calls == [body["objective"], body["objective"]]
    assert store.approval(approval["id"])["status"] == "CONSUMED"
    assert store.get_task(task_id)["state"] == "COMPLETED"
    assert store.link_task_run(task_id)["result_summary"] == "TextEdit opened after approval; the task is complete."

    replay = client.post(
        f"/v1/jarvis/approvals/{approval['id']}/decision",
        headers=headers,
        json=decision,
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "CONSUMED"
    assert replay.json()["execution_state"] == "COMPLETED"
    assert replay.json()["executed"] is True
    assert replay.json()["success"] is True
    assert len(dispatched) == 1
    assert len(agent.calls) == 2


def test_replayed_consumed_approval_without_terminal_execution_event_is_indeterminate(link_client, monkeypatch):
    client, store, agent = link_client
    control_agent = _install_control_agent(client, monkeypatch)
    dispatched = []
    monkeypatch.setattr("openjarvis.engine.codex_sdk._call_local_mcp", lambda *args, **kwargs: dispatched.append(args))
    headers = {"Authorization": "Bearer link-test-admin-key"}
    objective = "Open TextEdit, then report the actual result."
    accepted = client.post("/v1/jarvis/link/tasks", headers=headers, json={
        "objective": objective,
        "capability_scope": ["assistant"],
        "idempotency_key": "phone-approval-crash-window-01",
        "requester_device_id": "a" * 32,
    })
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    approval = next(item for item in store.pending_approvals(20) if item["task_id"] == task_id)
    nonce = "phone-approval-crash-window-decision"

    # Simulate a process crash after the one-shot approval is consumed but
    # before the MCP call's durable terminal event is written.
    assert store.decide_approval(
        approval["id"], decision="approve", expected_digest=approval["args_digest"],
        task_id=task_id, actor="device:" + "a" * 32, nonce=nonce,
    )
    assert store.consume_approval(
        approval["id"], task_id=task_id, operation=approval["operation"],
        target=approval["target"], arguments=json.loads(approval["arguments_json"]),
    )
    assert store.approval(approval["id"])["status"] == "CONSUMED"

    replay = client.post(f"/v1/jarvis/approvals/{approval['id']}/decision", headers=headers, json={
        "decision": "approve", "task_id": task_id,
        "expected_operation_digest": approval["args_digest"],
        "requester_device_id": "a" * 32,
        "idempotency_key": nonce, "continuation_objective": objective,
    })
    assert replay.status_code == 200
    assert replay.json()["status"] == "CONSUMED"
    assert replay.json()["execution_state"] == "INDETERMINATE"
    assert replay.json()["executed"] is False
    assert replay.json()["success"] is False
    assert replay.json()["task_state"] == "RUNNING"
    assert dispatched == []
    assert len(control_agent.calls) == 1


def test_approval_replay_reports_persisted_mcp_failure_without_retry(link_client, monkeypatch):
    client, store, _ = link_client
    agent = _install_control_agent(client, monkeypatch)
    dispatched = []

    def fake_mcp_call(tool, arguments, mode, **authorization):
        dispatched.append((tool, dict(arguments), mode))
        return {"content": [{"type": "text", "text": "Activation failed."}], "isError": True}

    monkeypatch.setattr("openjarvis.engine.codex_sdk._call_local_mcp", fake_mcp_call)
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {
        "objective": "Open TextEdit and tell me whether it opened.",
        "capability_scope": ["assistant"],
        "idempotency_key": "phone-approval-failure-0001",
        "requester_device_id": "a" * 32,
    }
    accepted = client.post("/v1/jarvis/link/tasks", headers=headers, json=body)
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    approval = next(item for item in store.pending_approvals(20) if item["task_id"] == task_id)
    decision = {
        "decision": "approve", "task_id": task_id,
        "expected_operation_digest": approval["args_digest"],
        "requester_device_id": "a" * 32,
        "idempotency_key": "phone-approval-failure-decision-0001",
        "continuation_objective": body["objective"],
    }
    first = client.post(f"/v1/jarvis/approvals/{approval['id']}/decision", headers=headers, json=decision)
    replay = client.post(f"/v1/jarvis/approvals/{approval['id']}/decision", headers=headers, json=decision)
    assert first.status_code == replay.status_code == 200
    assert first.json()["execution_state"] == replay.json()["execution_state"] == "FAILED"
    assert first.json()["success"] is replay.json()["success"] is False
    assert replay.json()["executed"] is False
    assert len(dispatched) == 1
    assert agent.calls == [body["objective"]]


def test_phone_assistant_reject_never_dispatches_or_continues(link_client, monkeypatch):
    client, store, _ = link_client
    agent = _install_control_agent(client, monkeypatch)
    dispatched = []
    monkeypatch.setattr("openjarvis.engine.codex_sdk._call_local_mcp", lambda *args, **kwargs: dispatched.append(args))
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {
        "objective": "Open TextEdit only if I approve.",
        "capability_scope": ["assistant"],
        "idempotency_key": "phone-assistant-reject-0001",
        "requester_device_id": "a" * 32,
    }
    accepted = client.post("/v1/jarvis/link/tasks", headers=headers, json=body)
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    approval = next(item for item in store.pending_approvals(20) if item["task_id"] == task_id)
    rejected = client.post(
        f"/v1/jarvis/approvals/{approval['id']}/decision",
        headers=headers,
        json={
            "decision": "reject",
            "task_id": task_id,
            "expected_operation_digest": approval["args_digest"],
            "requester_device_id": "a" * 32,
            "idempotency_key": "phone-approval-reject-0001",
            "continuation_objective": body["objective"],
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["executed"] is False
    assert store.approval(approval["id"])["status"] == "REJECTED"
    assert store.get_task(task_id)["state"] == "PAUSED"
    assert store.link_task_run(task_id)["status"] == "REJECTED"
    assert dispatched == []
    assert len(agent.calls) == 1


def test_disabled_mcp_open_app_blocks_approval_consumption_but_allows_reject(link_client, monkeypatch):
    client, store, _ = link_client
    monkeypatch.setenv("JARVIS_MCP_OPEN_APP_ENABLED", "0")
    device_id = "e" * 32
    objective = "Open an app only if the MCP launch gate permits it."
    run = store.create_link_task(
        objective,
        capability_scope=["assistant"],
        idempotency_key="disabled-mcp-open-app-approval-0001",
        owner_actor="device:" + device_id,
    )
    task_id = run["task_id"]
    store.start_link_task(
        task_id,
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["assistant"],
        idempotency_key=run["idempotency_key"],
    )
    arguments = {"name": "MCP Gate Probe"}
    approval_id, digest = store.create_approval(
        task_id, "mcp:jarvis_mac:open_app", "MCP Gate Probe", arguments, "isolated test",
    )
    store.mark_link_task_waiting_approval(task_id, approval_id)
    headers = {"Authorization": "Bearer link-test-admin-key"}

    blocked = client.post(
        f"/v1/jarvis/approvals/{approval_id}/decision",
        headers=headers,
        json={
            "decision": "approve", "task_id": task_id,
            "expected_operation_digest": digest,
            "requester_device_id": device_id,
            "idempotency_key": "disabled-mcp-open-app-approve-0001",
            "continuation_objective": objective,
        },
    )
    assert blocked.status_code == 409
    assert store.approval(approval_id)["status"] == "PENDING"
    assert store._db.execute(
        "SELECT 1 FROM mcp_approval_dispatches WHERE approval_id=?", (approval_id,),
    ).fetchone() is None

    rejected = client.post(
        f"/v1/jarvis/approvals/{approval_id}/decision",
        headers=headers,
        json={
            "decision": "reject", "task_id": task_id,
            "expected_operation_digest": digest,
            "requester_device_id": device_id,
            "idempotency_key": "disabled-mcp-open-app-reject-0001",
            "continuation_objective": objective,
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert store.approval(approval_id)["status"] == "REJECTED"
    assert store.link_task_run(task_id)["status"] == "REJECTED"
    assert store._db.execute(
        "SELECT 1 FROM mcp_approval_dispatches WHERE approval_id=?", (approval_id,),
    ).fetchone() is None


def test_link_approval_requires_matching_requester_device_before_decision(link_client, monkeypatch):
    client, store, _ = link_client
    agent = _install_control_agent(client, monkeypatch)
    dispatched = []
    monkeypatch.setattr(
        "openjarvis.engine.codex_sdk._call_local_mcp",
        lambda tool, arguments, mode, **kwargs: dispatched.append(tool) or {
            "content": [{"type": "text", "text": "Opened TextEdit."}], "isError": False,
        },
    )
    headers = {"Authorization": "Bearer link-test-admin-key"}
    objective = "Open TextEdit and report the outcome."
    task_body = {
        "objective": objective,
        "capability_scope": ["assistant"],
        "idempotency_key": "link-owner-binding-approval-01",
        "requester_device_id": "a" * 32,
    }
    accepted = client.post("/v1/jarvis/link/tasks", headers=headers, json=task_body)
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    approval = next(item for item in store.pending_approvals(20) if item["task_id"] == task_id)
    base = {
        "decision": "approve", "task_id": task_id,
        "expected_operation_digest": approval["args_digest"],
        "requester_device_id": "a" * 32,
        "idempotency_key": "link-owner-binding-decision-01",
        "continuation_objective": objective,
    }

    missing = client.post(
        f"/v1/jarvis/approvals/{approval['id']}/decision", headers=headers,
        json={key: value for key, value in base.items() if key != "requester_device_id"},
    )
    wrong = client.post(
        f"/v1/jarvis/approvals/{approval['id']}/decision", headers=headers,
        json={**base, "requester_device_id": "b" * 32},
    )
    assert missing.status_code == wrong.status_code == 403
    assert store.approval(approval["id"])["status"] == "PENDING"
    assert store.get_task(task_id)["state"] == "WAITING_APPROVAL"
    assert dispatched == []

    correct = client.post(
        f"/v1/jarvis/approvals/{approval['id']}/decision", headers=headers,
        json={**base, "requester_device_id": "a" * 32},
    )
    assert correct.status_code == 200, correct.json()
    assert correct.json()["execution_state"] == "COMPLETED"
    assert correct.json()["success"] is True
    assert dispatched == ["open_app"]
    assert len(agent.calls) == 2


def test_expired_link_approval_returns_terminal_expired_without_execution(link_client, monkeypatch):
    client, store, _ = link_client
    _install_control_agent(client, monkeypatch)
    dispatched = []
    monkeypatch.setattr("openjarvis.engine.codex_sdk._call_local_mcp", lambda *args, **kwargs: dispatched.append(args))
    headers = {"Authorization": "Bearer link-test-admin-key"}
    objective = "Open TextEdit if the approval is still valid."
    accepted = client.post("/v1/jarvis/link/tasks", headers=headers, json={
        "objective": objective,
        "capability_scope": ["assistant"],
        "idempotency_key": "expired-link-approval-test-01",
        "requester_device_id": "a" * 32,
    })
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    approval = next(item for item in store.pending_approvals(20) if item["task_id"] == task_id)
    store._db.execute(
        "UPDATE operation_approvals SET expires_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", approval["id"]),
    )
    store._db.commit()
    response = client.post(
        f"/v1/jarvis/approvals/{approval['id']}/decision",
        headers=headers,
        json={
            "decision": "approve", "task_id": task_id,
            "expected_operation_digest": approval["args_digest"],
            "requester_device_id": "a" * 32,
            "idempotency_key": "expired-link-approval-decision-01",
            "continuation_objective": objective,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "EXPIRED"
    assert response.json()["execution_state"] == "EXPIRED"
    assert response.json()["executed"] is False
    assert response.json()["success"] is False
    assert store.approval(approval["id"])["status"] == "EXPIRED"
    assert store.get_task(task_id)["state"] == "WAITING_APPROVAL"
    assert dispatched == []


def test_approved_operation_without_authorized_executor_fails_durably(link_client, monkeypatch):
    client, store, _ = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    link = store.create_link_task(
        "Run an unsupported Mac action",
        capability_scope=["assistant"],
        idempotency_key="unsupported-approved-operation-0001",
        owner_actor="device:" + "a" * 32,
    )
    store.start_link_task(
        link["task_id"], objective_digest=link["objective_digest"],
        objective_text_digest=link["objective_digest"], capability_scope=["assistant"],
        idempotency_key=link["idempotency_key"],
    )
    operation = "mcp:jarvis_mac:unknown_tool"
    arguments = {"name": "not-allowlisted"}
    approval_id, digest = store.create_approval(
        link["task_id"], operation, "unknown target", arguments, "Test only.",
    )
    store.mark_link_task_waiting_approval(link["task_id"], approval_id)
    store.decide_approval(
        approval_id, decision="approve", expected_digest=digest,
        task_id=link["task_id"], actor="device:" + "a" * 32, nonce="unsupported-tool-decision-01",
    )

    result = client.post(f"/v1/jarvis/approvals/{approval_id}/decision", headers=headers, json={
        "decision": "approve", "task_id": link["task_id"],
        "expected_operation_digest": digest, "requester_device_id": "a" * 32,
        "idempotency_key": "unsupported-tool-decision-01",
    })
    assert result.status_code == 200, result.json()
    assert result.json()["execution_state"] == "FAILED"
    assert result.json()["executed"] is False
    assert result.json()["success"] is False
    assert result.json()["error_code"] == "NO_AUTHORIZED_DISPATCHER"
    assert store.approval(approval_id)["status"] == "CONSUMED"
    assert store.get_task(link["task_id"])["state"] == "FAILED"
    assert store.link_task_run(link["task_id"])["status"] == "FAILED"
    replay = client.post(f"/v1/jarvis/approvals/{approval_id}/decision", headers=headers, json={
        "decision": "approve", "task_id": link["task_id"],
        "expected_operation_digest": digest, "requester_device_id": "a" * 32,
        "idempotency_key": "unsupported-tool-decision-01",
    })
    assert replay.status_code == 200
    assert replay.json()["execution_state"] == "FAILED"
    assert replay.json()["executed"] is False


def test_link_event_feed_exposes_terminal_approval_state_without_arguments(link_client):
    client, store, _ = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    device_id = "a" * 32
    objective = "Open TextEdit and report the result."
    run = store.create_link_task(
        objective, capability_scope=["assistant"],
        idempotency_key="approval-event-feed-test-0001", owner_actor="device:" + device_id,
    )
    task_id = run["task_id"]
    store.start_link_task(
        task_id, objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"], capability_scope=["assistant"],
        idempotency_key=run["idempotency_key"],
    )
    approval_id, _ = store.create_approval(
        task_id, "mcp:jarvis_mac:open_app", "TextEdit", {"name": "TextEdit"}, "Open the app.",
    )
    store.mark_link_task_waiting_approval(task_id, approval_id)
    store.record_event(task_id, "approval.execution_failed", {
        "approval_id": approval_id, "operation": "mcp:jarvis_mac:open_app",
        "error_code": "APPROVED_OPERATION_FAILED", "success": False,
        "private_arguments": {"name": "TextEdit"},
    })
    response = client.get(
        f"/v1/jarvis/link/tasks/{run['idempotency_key']}/events?after_seq=0",
        headers=headers,
    )
    assert response.status_code == 200
    event = next(row for row in response.json()["events"] if row["event_type"] == "approval.execution_failed")
    assert event["payload"] == {
        "approval_id": approval_id,
        "operation": "mcp:jarvis_mac:open_app",
        "error_code": "APPROVED_OPERATION_FAILED",
        "success": False,
    }


def test_phone_can_cancel_pending_approval_and_revoke_it(link_client, monkeypatch):
    client, store, _ = link_client
    agent = _install_control_agent(client, monkeypatch)
    dispatched = []
    monkeypatch.setattr("openjarvis.engine.codex_sdk._call_local_mcp", lambda *args, **kwargs: dispatched.append(args))
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {
        "objective": "Open TextEdit, then report the result.",
        "capability_scope": ["assistant"],
        "idempotency_key": "phone-assistant-cancel-0001",
        "requester_device_id": "a" * 32,
    }
    accepted = client.post("/v1/jarvis/link/tasks", headers=headers, json=body)
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    approval = next(item for item in store.pending_approvals(20) if item["task_id"] == task_id)

    cancelled = client.post(
        f"/v1/jarvis/link/tasks/{body['idempotency_key']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"
    assert store.get_task(task_id)["state"] == "CANCELLED"
    assert store.link_task_run(task_id)["status"] == "CANCELLED"
    assert store.approval(approval["id"])["status"] == "EXPIRED"
    assert store.pending_approvals(20) == []
    assert dispatched == []

    # Even a correctly signed stale approval cannot make a cancelled task run.
    stale_decision = client.post(
        f"/v1/jarvis/approvals/{approval['id']}/decision",
        headers=headers,
        json={
            "decision": "approve",
            "task_id": task_id,
            "expected_operation_digest": approval["args_digest"],
            "requester_device_id": "a" * 32,
            "idempotency_key": "phone-cancelled-approval-replay-01",
        },
    )
    assert stale_decision.status_code == 409
    assert dispatched == []
    assert agent.calls == [body["objective"]]

    status = client.get(f"/v1/jarvis/link/tasks/{body['idempotency_key']}", headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] == "CANCELLED"


def test_link_task_owner_is_bound_to_paired_device(link_client):
    client, store, _ = link_client
    token_a = "ojd_owner_test_device_a"
    token_b = "ojd_owner_test_device_b"
    device_a = "a" * 32
    device_b = "b" * 32
    now = "2099-01-01T00:00:00+00:00"
    for device_id, token in ((device_a, token_a), (device_b, token_b)):
        store._db.execute(
            "INSERT INTO paired_devices(id,public_key_b64,token_hash,created_at,expires_at,device_name) VALUES(?,?,?,?,?,?)",
            (device_id, "test-public-key", hashlib.sha256(token.encode()).hexdigest(), now, "2099-02-01T00:00:00+00:00", "test device"),
        )
    store._db.commit()
    body = {
        "objective": "A device-bound task",
        "capability_scope": ["read_only"],
        "idempotency_key": "device-owned-link-task-01",
        "requester_device_id": device_a,
    }
    created = client.post("/v1/jarvis/link/tasks", headers={"Authorization": f"Bearer {token_a}"}, json=body)
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    assert store.get_task(task_id)["state"] == "COMPLETED"
    assert client.get(
        f"/v1/jarvis/link/tasks/{body['idempotency_key']}",
        headers={"Authorization": f"Bearer {token_b}"},
    ).status_code == 403
    assert client.post(
        f"/v1/jarvis/link/tasks/{body['idempotency_key']}/cancel",
        headers={"Authorization": f"Bearer {token_b}"},
    ).status_code == 403
    assert store.get_task(task_id)["state"] == "COMPLETED"


def test_cancel_after_mcp_dispatch_claim_stops_continuation_but_reports_possible_action(tmp_path):
    from openjarvis.security.mcp_supervisor_bridge import claim_consumed_mcp_approval

    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    run = store.create_link_task(
        "Open TextEdit, then summarize",
        capability_scope=["assistant"],
        idempotency_key="cancel-after-mcp-claim-0001",
        owner_actor="device:" + "c" * 32,
    )
    task_id = run["task_id"]
    store.start_link_task(
        task_id,
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["assistant"],
        idempotency_key=run["idempotency_key"],
    )
    arguments = {"name": "TextEdit"}
    operation = "mcp:jarvis_mac:open_app"
    target = "TextEdit"
    approval_id, digest = store.create_approval(task_id, operation, target, arguments, "test")
    store.mark_link_task_waiting_approval(task_id, approval_id)
    assert store.decide_approval(
        approval_id, decision="approve", expected_digest=digest, task_id=task_id,
        actor="device:" + "c" * 32, nonce="cancel-after-mcp-decision-01",
    )
    assert store.consume_approval(
        approval_id, task_id=task_id, operation=operation, target=target, arguments=arguments,
    )
    store.resume_link_task_after_approval(task_id, approval_id)
    assert claim_consumed_mcp_approval(
        store, approval_id=approval_id, task_id=task_id, operation=operation,
        target=target, arguments=arguments, expected_digest=digest,
    )

    cancelled = store.cancel_link_task(task_id, actor="device:" + "c" * 32)
    assert cancelled == {"state": "CANCELLED", "cancelled": True, "action_may_have_started": True}
    assert store.link_task_run(task_id)["status"] == "CANCELLED"
    assert store.get_task(task_id)["state"] == "CANCELLED"
    assert not claim_consumed_mcp_approval(
        store, approval_id=approval_id, task_id=task_id, operation=operation,
        target=target, arguments=arguments, expected_digest=digest,
    )
    store.close()


def test_local_mcp_child_approval_is_exact_and_claimed_once(tmp_path):
    from openjarvis.security.mcp_supervisor_bridge import claim_consumed_mcp_approval, create_mcp_approval

    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    task = store.create_link_task(
        "Open TextEdit then summarize its state",
        capability_scope=["assistant"],
        idempotency_key="mcp-child-approval-20261116-01",
    )
    store.start_link_task(
        task["task_id"],
        objective_digest=task["objective_digest"],
        objective_text_digest=task["objective_digest"],
        capability_scope=["assistant"],
        idempotency_key=task["idempotency_key"],
    )
    arguments = {"name": "TextEdit"}
    approval_id, operation, target, digest = create_mcp_approval(
        store,
        task_id=task["task_id"],
        source="jarvis_link",
        scope="assistant",
        tool="open_app",
        arguments=arguments,
    )
    assert operation == "mcp:jarvis_mac:open_app"
    assert target == "TextEdit"
    assert store.approval(approval_id)["status"] == "PENDING"
    assert not claim_consumed_mcp_approval(
        store,
        approval_id=approval_id,
        task_id=task["task_id"],
        operation=operation,
        target=target,
        arguments=arguments,
        expected_digest=digest,
    )

    assert store.decide_approval(
        approval_id,
        decision="approve",
        expected_digest=digest,
        task_id=task["task_id"],
        actor="test-phone",
        nonce="mcp-approval-decision-01",
    )
    assert store.consume_approval(
        approval_id,
        task_id=task["task_id"],
        operation=operation,
        target=target,
        arguments=arguments,
    )
    assert claim_consumed_mcp_approval(
        store,
        approval_id=approval_id,
        task_id=task["task_id"],
        operation=operation,
        target=target,
        arguments=arguments,
        expected_digest=digest,
    )
    assert not claim_consumed_mcp_approval(
        store,
        approval_id=approval_id,
        task_id=task["task_id"],
        operation=operation,
        target=target,
        arguments=arguments,
        expected_digest=digest,
    )
    assert not claim_consumed_mcp_approval(
        store,
        approval_id=approval_id,
        task_id=task["task_id"],
        operation=operation,
        target=target,
        arguments={"name": "Calculator"},
        expected_digest=digest,
    )
    store.close()


def test_mcp_child_store_open_does_not_pause_the_live_supervisor(tmp_path):
    path = str(tmp_path / "supervisor.db")
    supervisor_store = SupervisorStore(path)
    task_id = supervisor_store.create_task("Keep the active service task running")
    supervisor_store.transition(task_id, "RUNNING")
    mcp_child_store = SupervisorStore(path, run_recovery=False)
    try:
        assert supervisor_store.get_task(task_id)["state"] == "RUNNING"
        assert mcp_child_store.get_task(task_id)["state"] == "RUNNING"
    finally:
        mcp_child_store.close()
        supervisor_store.close()


def test_real_mac_mcp_bridge_queues_then_claims_only_the_exact_approval(tmp_path, monkeypatch):
    import importlib.util
    import subprocess
    from pathlib import Path

    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "control")
    monkeypatch.setenv("JARVIS_MCP_OPEN_APP_ENABLED", "1")
    task_id = "f" * 32
    store = SupervisorStore()
    link = store.create_link_task(
        "Open TextEdit and report the result",
        capability_scope=["assistant"],
        idempotency_key="mcp-script-approval-20261116-01",
    )
    task_id = link["task_id"]
    store.start_link_task(
        task_id,
        objective_digest=link["objective_digest"],
        objective_text_digest=link["objective_digest"],
        capability_scope=["assistant"],
        idempotency_key=link["idempotency_key"],
    )
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_ID", task_id)
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SOURCE", "jarvis_link")
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SCOPE", "assistant")
    monkeypatch.setenv("JARVIS_SUPERVISOR_APPROVED_OPERATION", "0")
    spec = importlib.util.spec_from_file_location(
        "jarvis_test_mac_desktop_mcp",
        Path(__file__).resolve().parents[2] / "src" / "openjarvis" / "mac_desktop_mcp.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    arguments = {"name": "TextEdit"}
    pending = module.call_tool("open_app", arguments)
    assert pending["isError"] is True
    assert store.get_task(task_id)["state"] == "WAITING_APPROVAL"
    approval = next(item for item in store.pending_approvals(20) if item["task_id"] == task_id)
    assert approval["operation"] == "mcp:jarvis_mac:open_app"
    assert approval["target"] == "TextEdit"
    store.mark_link_task_waiting_approval(task_id, approval["id"])

    assert store.decide_approval(
        approval["id"], decision="approve", expected_digest=approval["args_digest"],
        task_id=task_id, actor="test-phone", nonce="mcp-script-approved-01",
    )
    assert store.consume_approval(
        approval["id"], task_id=task_id, operation=approval["operation"],
        target=approval["target"], arguments=arguments,
    )
    store.resume_link_task_after_approval(task_id, approval["id"])

    monkeypatch.setenv("JARVIS_SUPERVISOR_APPROVED_OPERATION", "1")
    monkeypatch.setenv("JARVIS_SUPERVISOR_APPROVAL_ID", approval["id"])
    monkeypatch.setenv("JARVIS_SUPERVISOR_OPERATION", approval["operation"])
    monkeypatch.setenv("JARVIS_SUPERVISOR_TARGET", approval["target"])
    monkeypatch.setenv("JARVIS_SUPERVISOR_OPERATION_DIGEST", approval["args_digest"])
    module._SUPERVISOR_APPROVAL_CONSUMED = True
    module._SUPERVISOR_APPROVAL_ID = approval["id"]
    module._SUPERVISOR_OPERATION = approval["operation"]
    module._SUPERVISOR_TARGET = approval["target"]
    module._SUPERVISOR_DIGEST = approval["args_digest"]
    executed = []
    monkeypatch.setattr(module, "run", lambda command, timeout=15: executed.append(list(command)) or subprocess.CompletedProcess(command, 0, "", ""))
    result = module.call_tool("open_app", arguments)
    assert result["isError"] is False
    assert executed == [["/usr/bin/open", "-a", "TextEdit"]]
    replay = module.call_tool("open_app", arguments)
    assert replay["isError"] is True
    assert len(executed) == 1
    boundary_events = [event for event in store.events(task_id) if event["event_type"].startswith("mac.open_app.")]
    stages = {event["event_type"] for event in boundary_events}
    assert {
        "mac.open_app.approval_requested",
        "mac.open_app.dispatch_claim",
        "mac.open_app.execution_attempt",
        "mac.open_app.execution_result",
    }.issubset(stages)
    claim_event = next(event for event in boundary_events if event["event_type"] == "mac.open_app.dispatch_claim")
    assert claim_event["payload"]["task_id"] == task_id
    assert claim_event["payload"]["approval_id"] == approval["id"]
    assert claim_event["payload"]["requested_application"] == "TextEdit"
    assert claim_event["payload"]["dispatch_claim_outcome"] == "claimed"
    assert claim_event["payload"]["execution_attempted"] is False
    attempt_event = next(event for event in boundary_events if event["event_type"] == "mac.open_app.execution_attempt")
    result_event = next(event for event in boundary_events if event["event_type"] == "mac.open_app.execution_result")
    assert attempt_event["payload"]["execution_attempted"] is True
    assert attempt_event["payload"]["process_context"]["pid"] > 0
    assert result_event["payload"]["result"] == "launch_command_accepted"
    assert result_event["payload"]["exit_code"] == 0
    assert not {"stdout", "stderr", "arguments"}.intersection(result_event["payload"])
    module._STORE.close()
    store.close()


@pytest.mark.parametrize("invalid_state", ["rejected", "expired", "unconsumed", "mismatched"])
def test_mac_mcp_open_app_never_calls_launch_handler_without_exact_consumed_approval(
    tmp_path, monkeypatch, invalid_state,
):
    import importlib.util
    import subprocess
    from pathlib import Path

    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "control")
    monkeypatch.setenv("JARVIS_MCP_OPEN_APP_ENABLED", "1")
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    owner = "device:" + "b" * 32
    run = store.create_link_task(
        "Open a test app only after approval",
        capability_scope=["assistant"],
        idempotency_key=f"mcp-invalid-{invalid_state}-0001",
        owner_actor=owner,
    )
    task_id = run["task_id"]
    store.start_link_task(
        task_id,
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["assistant"],
        idempotency_key=run["idempotency_key"],
    )
    arguments = {"name": "MCP Gate Probe"}
    operation = "mcp:jarvis_mac:open_app"
    approval_id, digest = store.create_approval(task_id, operation, "MCP Gate Probe", arguments, "isolated test")
    store.mark_link_task_waiting_approval(task_id, approval_id)
    call_arguments = arguments
    if invalid_state == "rejected":
        assert store.decide_approval(
            approval_id, decision="reject", expected_digest=digest, task_id=task_id,
            actor=owner, nonce=f"mcp-invalid-reject-{invalid_state}",
        )
        store.reject_link_task_after_approval(task_id, approval_id)
    elif invalid_state == "expired":
        store.expire_approval(approval_id, reason="isolated_test_expiry")
    elif invalid_state == "unconsumed":
        assert store.decide_approval(
            approval_id, decision="approve", expected_digest=digest, task_id=task_id,
            actor=owner, nonce=f"mcp-invalid-approve-{invalid_state}",
        )
    else:
        assert store.decide_approval(
            approval_id, decision="approve", expected_digest=digest, task_id=task_id,
            actor=owner, nonce=f"mcp-invalid-approve-{invalid_state}",
        )
        assert store.consume_approval(
            approval_id, task_id=task_id, operation=operation, target="MCP Gate Probe", arguments=arguments,
        )
        store.resume_link_task_after_approval(task_id, approval_id)
        call_arguments = {"name": "Different App"}

    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_ID", task_id)
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SOURCE", "jarvis_link")
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SCOPE", "assistant")
    monkeypatch.setenv("JARVIS_SUPERVISOR_APPROVED_OPERATION", "1")
    monkeypatch.setenv("JARVIS_SUPERVISOR_APPROVAL_ID", approval_id)
    monkeypatch.setenv("JARVIS_SUPERVISOR_OPERATION", operation)
    monkeypatch.setenv("JARVIS_SUPERVISOR_TARGET", "MCP Gate Probe")
    monkeypatch.setenv("JARVIS_SUPERVISOR_OPERATION_DIGEST", digest)
    spec = importlib.util.spec_from_file_location(
        f"jarvis_test_mac_mcp_{invalid_state}",
        Path(__file__).resolve().parents[2] / "src" / "openjarvis" / "mac_desktop_mcp.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module._STORE = store
    module._SUPERVISOR_APPROVAL_CONSUMED = True
    module._SUPERVISOR_APPROVAL_ID = approval_id
    module._SUPERVISOR_TASK_ID = task_id
    module._SUPERVISOR_OPERATION = operation
    module._SUPERVISOR_TARGET = "MCP Gate Probe"
    module._SUPERVISOR_DIGEST = digest
    launches = []
    monkeypatch.setattr(
        module, "run",
        lambda command, timeout=15: launches.append(list(command)) or subprocess.CompletedProcess(command, 0, "", ""),
    )

    result = module.call_tool("open_app", call_arguments)
    assert result["isError"] is True
    assert launches == []
    assert store._db.execute(
        "SELECT 1 FROM mcp_approval_dispatches WHERE approval_id=?", (approval_id,),
    ).fetchone() is None
    boundary = [event for event in store.events(task_id) if event["event_type"] == "mac.open_app.dispatch_claim"]
    assert boundary
    assert boundary[-1]["payload"]["execution_attempted"] is False
    module._STORE = None
    store.close()


def test_mcp_open_app_is_default_off_and_does_not_change_trusted_fast_path(link_client, monkeypatch):
    import importlib.util
    import subprocess
    from pathlib import Path
    from openjarvis.engine.codex_sdk import _approval_handler, _call_local_mcp, _config_overrides

    _, store, _ = link_client
    monkeypatch.setenv("JARVIS_DESKTOP_MODE", "control")
    monkeypatch.delenv("JARVIS_MCP_OPEN_APP_ENABLED", raising=False)
    assert 'mcp_servers.jarvis_mac.env.JARVIS_MCP_OPEN_APP_ENABLED="0"' in _config_overrides("control")

    spec = importlib.util.spec_from_file_location(
        "jarvis_test_mac_mcp_disabled",
        Path(__file__).resolve().parents[2] / "src" / "openjarvis" / "mac_desktop_mcp.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    assert "open_app" not in {tool["name"] for tool in module.tool_list()}
    launches = []
    monkeypatch.setattr(module, "run", lambda command, timeout=15: launches.append(list(command) or []) or subprocess.CompletedProcess(command, 0, "", ""))
    assert module.call_tool("open_app", {"name": "MCP Gate Probe"})["isError"] is True
    assert launches == []

    task = store.create_link_task(
        "Open an app only through the configured MCP gate",
        capability_scope=["assistant"],
        idempotency_key="mcp-open-app-disabled-callback-0001",
        owner_actor="device:" + "c" * 32,
    )
    store.start_link_task(
        task["task_id"], objective_digest=task["objective_digest"],
        objective_text_digest=task["objective_digest"], capability_scope=["assistant"],
        idempotency_key=task["idempotency_key"],
    )
    with bind_task(
        task["task_id"], source="jarvis_link", scope="assistant",
        allowed_capabilities=supervisor.LINK_SCOPE_CAPABILITIES["assistant"],
    ):
        callback_result = _approval_handler("control")(
            "mcpServer/tool/call",
            {"server": "jarvis_mac", "tool": "open_app", "arguments": {"name": "MCP Gate Probe"}},
        )
    assert callback_result["isError"] is True
    assert not [approval for approval in store.pending_approvals(20) if approval["task_id"] == task["task_id"]]

    # Even a direct call through the parent local-MCP bridge must stop before
    # spawning the child while the kill switch is off.
    monkeypatch.setattr(
        "openjarvis.engine.codex_sdk.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("disabled open_app must not spawn the MCP child"),
    )
    blocked = _call_local_mcp("open_app", {"name": "MCP Gate Probe"}, "control", supervisor_approved=True)
    assert blocked["isError"] is True
