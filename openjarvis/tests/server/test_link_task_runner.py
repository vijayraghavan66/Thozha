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
    body = {"objective": "read safely", "capability_scope": ["read_only"], "idempotency_key": "link-run-unique-0001"}
    assert client.post("/v1/jarvis/link/tasks", json=body).status_code == 401
    assert client.post(
        "/v1/jarvis/link/tasks",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json={**body, "capability_scope": ["desktop_observation"]},
    ).status_code == 403

    remote = TestClient(client.app, base_url="http://127.0.0.1:8000", client=("192.168.1.7", 12345))
    assert remote.post(
        "/v1/jarvis/link/tasks",
        headers={"Authorization": "Bearer link-test-admin-key"},
        json=body,
    ).status_code == 403


def test_link_runner_rejects_conflicting_idempotency_reuse(link_client):
    client, _, agent = link_client
    headers = {"Authorization": "Bearer link-test-admin-key"}
    body = {"objective": "first objective", "capability_scope": ["read_only"], "idempotency_key": "link-run-conflict-0001"}
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
        json={"command": "mac.status", "requester": "a" * 32},
    )
    assert response.status_code == 200
    assert response.json() == {"command": "mac.status", "state": "ONLINE", "online": True, "ai_available": True}
    assert agent.calls == []


def test_fast_task_status_is_owner_bound_and_does_not_call_agent(link_client):
    client, store, agent = link_client
    task = store.create_link_task(
        "Read task status", capability_scope=["read_only"],
        idempotency_key="fast-status-owner-check-0001", owner_actor="device:" + "a" * 32,
    )
    headers = {"Authorization": "Bearer link-test-admin-key"}
    allowed = client.post("/v1/jarvis/link/fast-command", headers=headers,
                          json={"command": "task.status", "requester": "a" * 32, "task_id": task["task_id"]})
    denied = client.post("/v1/jarvis/link/fast-command", headers=headers,
                         json={"command": "task.status", "requester": "b" * 32, "task_id": task["task_id"]})
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
        json={"command": "task.status", "requester": "a" * 32, "task_id": task_id},
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
    body = {"command": "app.open", "requester": "c" * 32, "app": "Calculator", "idempotency_key": "d" * 32}
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
    base = {"command": "app.open", "requester": "d" * 32, "idempotency_key": "fast-invalid-app-request-0001"}
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
    assert len(dispatched) == 1
    assert len(agent.calls) == 2


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
        Path.home() / ".openjarvis" / "mcp" / "mac_desktop_mcp.py",
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
    module._STORE.close()
    store.close()
