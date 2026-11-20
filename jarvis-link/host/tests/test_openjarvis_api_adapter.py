"""The remote host keeps the OpenJarvis master credential on loopback only."""
from __future__ import annotations

import hashlib
import json
import os
import stat

import httpx
import pytest

from jarvis_link_host.openjarvis_api_adapter import OpenJarvisApiSupervisorAdapter, SupervisorUnavailable
from jarvis_link_host.openjarvis_adapter import SupervisorRefused


def _key(path):
    path.write_text("test-local-supervisor-key\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_submit_and_approval_decision_use_only_authenticated_loopback(tmp_path):
    key = _key(tmp_path / "api-key")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/link/tasks"):
            return httpx.Response(202, json={"task_id": "a" * 32, "state": "WAITING_APPROVAL"})
        return httpx.Response(200, json={"status": "CONSUMED", "executed": True, "success": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    task = adapter.submit_task(
        objective="Open TextEdit then summarize",
        capability_scope=["assistant"],
        idempotency_key="link-message-id-0001",
        requester_device_id="d" * 32,
    )
    assert task["task_id"] == "a" * 32
    assert task["state"] == "WAITING_APPROVAL"
    assert requests[0].url == "http://127.0.0.1:8000/v1/jarvis/link/tasks"
    assert requests[0].headers["authorization"] == "Bearer test-local-supervisor-key"
    assert json.loads(requests[0].content) == {
        "objective": "Open TextEdit then summarize",
        "capability_scope": ["assistant"],
        "idempotency_key": "link-message-id-0001",
        "requester_device_id": "d" * 32,
    }
    assert "test-local-supervisor-key" not in str(requests[0].url)

    result = adapter.decide_approval(
        task_id="a" * 32,
        approval_id="b" * 32,
        decision="approve",
        expected_arguments_digest="c" * 64,
        idempotency_key="approval-message-id-001",
        requester_device_id="d" * 32,
        continuation_objective="Open TextEdit then summarize",
    )
    assert result["status"] == "CONSUMED"
    body = json.loads(requests[1].content)
    assert body["continuation_objective"] == "Open TextEdit then summarize"
    assert body["expected_operation_digest"] == "c" * 64
    assert body["requester_device_id"] == "d" * 32
    assert "test-local-supervisor-key" not in requests[1].content.decode()
    client.close()


def test_fast_command_adapter_sends_only_typed_loopback_requests(tmp_path):
    key = _key(tmp_path / "api-key")
    requests = []
    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        if body["command"] == "mac.status":
            return httpx.Response(200, json={"command": "mac.status", "state": "COMPLETED",
                                             "executed": True, "success": True,
                                             "online": True, "ai_available": True,
                                             "summary": "Mac is online. AI is available."})
        if body["command"] == "task.status":
            return httpx.Response(200, json={"command":"task.status", "state":"COMPLETED", "task_id":body["task_id"], "summary":"done"})
        return httpx.Response(200, json={
            "command": "app.open", "state": "COMPLETED", "executed": True,
            "success": True, "bundle_id": body["app"], "summary": "Calculator opened.",
        })
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    requester = "d" * 32
    assert adapter.fast_command(command="mac.status", requester_device_id=requester) == {
        "state": "COMPLETED", "executed": True, "success": True,
        "online": True, "ai_available": True, "summary": "Mac is online. AI is available.",
    }
    assert adapter.fast_command(command="task.status", requester_device_id=requester, task_id="b" * 32)["summary"] == "done"
    result = adapter.fast_command(command="app.open", requester_device_id=requester,
                                  idempotency_key="e" * 32, app="com.apple.calculator")
    assert result == {
        "state": "COMPLETED", "bundle_id": "com.apple.calculator", "executed": True,
        "success": True, "summary": "Calculator opened.",
    }
    app_request = json.loads(requests[2].content)
    assert app_request == {
        "command": "app.open", "requester_device_id": requester, "app": "com.apple.calculator",
        "idempotency_key": "e" * 32,
    }
    assert not ({"path", "argv", "command_line", "arguments"} & set(app_request))
    assert app_request["requester_device_id"] == requester
    assert json.loads(requests[0].content) == {
        "command": "mac.status", "requester_device_id": requester,
    }
    assert all(r.url == "http://127.0.0.1:8000/v1/jarvis/link/fast-command" for r in requests)
    assert all(r.headers["authorization"] == "Bearer test-local-supervisor-key" for r in requests)
    with pytest.raises(SupervisorRefused):
        adapter.fast_command(command="app.open", requester_device_id=requester, idempotency_key="f" * 32, app="Terminal")
    with pytest.raises(SupervisorRefused, match="canonical trusted bundle ID"):
        adapter.fast_command(command="app.open", requester_device_id=requester,
                             idempotency_key="c" * 32, app="Calculator")
    client.close()


def test_typed_battery_command_requires_fresh_validated_status_fields(tmp_path):
    key = _key(tmp_path / "api-key")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={
            "command": "mac.battery", "state": "COMPLETED", "executed": True,
            "success": True, "battery_percent": 81, "charging": False,
            "summary": "Battery level: 81%.",
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    result = adapter.fast_command(command="mac.battery", requester_device_id="d" * 32)
    assert result == {"state": "COMPLETED", "executed": True, "success": True,
                     "battery_percent": 81, "summary": "Battery level: 81%.", "charging": False}
    assert json.loads(seen[0].content) == {"command": "mac.battery", "requester_device_id": "d" * 32}
    client.close()


@pytest.mark.parametrize("battery_percent", [-1, 101, True, "81"])
def test_typed_battery_command_rejects_invalid_percentages(tmp_path, battery_percent):
    key = _key(tmp_path / "api-key")
    response = {"command": "mac.battery", "state": "COMPLETED", "executed": True,
                "success": True, "battery_percent": battery_percent, "summary": "Battery."}
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    with pytest.raises(SupervisorRefused):
        adapter.fast_command(command="mac.battery", requester_device_id="d" * 32)
    client.close()


@pytest.mark.parametrize("response", [
    {"command": "app.open", "state": "COMPLETED", "executed": False,
     "success": True, "bundle_id": "com.apple.calculator", "summary": "opened"},
    {"command": "app.open", "state": "COMPLETED", "executed": True,
     "success": True, "bundle_id": "com.fake.app", "summary": "opened"},
    {"command": "app.open", "state": "COMPLETED", "executed": True,
     "success": True, "bundle_id": "com.apple.calculator", "summary": ""},
])
def test_fast_app_result_requires_truthful_matching_execution_evidence(tmp_path, response):
    key = _key(tmp_path / "api-key")
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    with pytest.raises(SupervisorRefused):
        adapter.fast_command(command="app.open", requester_device_id="d" * 32,
                             idempotency_key="e" * 32, app="com.apple.calculator")
    client.close()


def test_fast_app_approval_fallback_is_bound_to_bundle_and_task(tmp_path):
    key = _key(tmp_path / "api-key")
    approval = {
        "approval_id": "a" * 32,
        "task_id": "b" * 32,
        "operation": "mcp:jarvis_mac:open_app",
        "target": "com.apple.calculator",
        "canonical_arguments_digest": "c" * 64,
        "expires_at_unix_ms": 1_800_000_000_000,
    }
    data = {"command": "app.open", "state": "WAITING_APPROVAL", "bundle_id": "com.apple.calculator",
            "task_id": approval["task_id"], "approval": approval, "summary": "Approval required."}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    result = adapter.fast_command(command="app.open", requester_device_id="d" * 32,
                                  idempotency_key="e" * 32, app="com.apple.calculator")
    assert result == {"state": "WAITING_APPROVAL", "bundle_id": "com.apple.calculator",
                      "task_id": approval["task_id"], "summary": "Approval required.",
                      "approval": {**approval,
                                   "redacted_summary": "Review the exact app activation before approving."}}
    client.close()


@pytest.mark.parametrize("state", ["EXECUTION_PENDING", "RUNNING", "INDETERMINATE"])
def test_fast_app_inflight_state_is_preserved_without_claiming_completion(tmp_path, state):
    key = _key(tmp_path / "api-key")
    response = {"command": "app.open", "state": state, "bundle_id": "com.apple.calculator",
                "task_id": "b" * 32, "summary": "Jarvis is reconciling the app activation."}
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    result = adapter.fast_command(command="app.open", requester_device_id="d" * 32,
                                  idempotency_key="e" * 32, app="com.apple.calculator")
    assert result == {"state": state, "bundle_id": "com.apple.calculator",
                      "task_id": "b" * 32, "summary": "Jarvis is reconciling the app activation."}
    assert "executed" not in result and "success" not in result
    client.close()


def test_pending_approval_response_is_reduced_to_encrypted_protocol_fields(tmp_path):
    key = _key(tmp_path / "api-key")

    def handler(request):
        assert request.url.path == "/v1/jarvis/approvals/pending"
        return httpx.Response(200, json={"approvals": [
            {
                "id": "b" * 32,
                "task_id": "a" * 32,
                "operation": "mcp:jarvis_mac:open_app",
                "target_resource": "TextEdit",
                "operation_digest": "c" * 64,
                "expires_at": "2026-11-16T20:00:00+00:00",
                "reason": "Open app after approval",
                "canonical_arguments": {"name": "TextEdit"},
                "status": "PENDING",
            },
            {"id": "d" * 32, "task_id": "e" * 32},
        ]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    approvals = adapter.list_pending_approvals("a" * 32)
    assert len(approvals) == 1
    assert approvals[0]["operation"] == "mcp:jarvis_mac:open_app"
    assert approvals[0]["target"] == "TextEdit"
    assert approvals[0]["canonical_arguments_digest"] == "c" * 64
    assert "TextEdit" in approvals[0]["redacted_summary"]
    client.close()


def _development_plan_approval_row():
    plan = {
        "allowed_ops": ["artifact_manifest", "run_tests", "write_file"],
        "allowed_paths": ["src", "tests"],
        "device_id": "d" * 32,
        "max_duration_s": 900,
        "max_file_bytes": 4096,
        "max_repairs": 1,
        "max_test_runs": 2,
        "max_total_bytes": 8192,
        "max_writes": 3,
        "plan_id": "approval-plan-0001",
        "ttl_s": 600,
        "user_id": "device:" + "d" * 32,
        "workspace_root": "/tmp/jarvis-taskflow",
    }
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    plan_digest = hashlib.sha256(canonical(plan).encode()).hexdigest()
    arguments = {"plan": plan, "plan_digest": plan_digest}
    operation = "mcp:jarvis_dev:approve_plan"
    target = f"taskflow:plan:{plan['plan_id']}"
    operation_payload = {"operation": operation, "target": target, "arguments": arguments}
    operation_digest = hashlib.sha256(canonical(operation_payload).encode()).hexdigest()
    return {
        "id": "a" * 32,
        "task_id": "b" * 32,
        "operation": operation,
        "target_resource": target,
        "operation_digest": operation_digest,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "reason": "Review the complete bounded project plan.",
        "canonical_arguments": arguments,
        "status": "PENDING",
    }


def test_development_plan_approval_preserves_complete_verified_details(tmp_path):
    key = _key(tmp_path / "api-key")
    row = _development_plan_approval_row()
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"approvals": [row]})))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    approval = adapter.list_pending_approvals("b" * 32)[0]
    assert approval["development_plan"]["workspace_root"] == "/tmp/jarvis-taskflow"
    assert approval["development_plan"]["allowed_paths"] == ["src", "tests"]
    assert approval["development_plan"]["max_repairs"] == 1
    assert approval["development_plan"]["plan_digest"] == row["canonical_arguments"]["plan_digest"]
    assert "complete bounded development plan" in approval["redacted_summary"]
    assert len(approval["redacted_summary"]) < 200
    client.close()


@pytest.mark.parametrize("mutation", ["missing_plan", "tampered_plan", "wrong_operation_digest", "oversized_plan"])
def test_development_plan_approval_fails_closed_without_complete_digest_bound_details(tmp_path, mutation):
    key = _key(tmp_path / "api-key")
    row = _development_plan_approval_row()
    if mutation == "missing_plan":
        row["canonical_arguments"] = {}
    elif mutation == "tampered_plan":
        row["canonical_arguments"]["plan"]["allowed_paths"] = ["src", "private"]
    elif mutation == "wrong_operation_digest":
        row["operation_digest"] = "0" * 64
    elif mutation == "oversized_plan":
        row["canonical_arguments"]["plan"]["allowed_paths"] = [f"dir{index}" for index in range(25)]
        plan = row["canonical_arguments"]["plan"]
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        row["canonical_arguments"]["plan_digest"] = hashlib.sha256(canonical.encode()).hexdigest()
        arguments = row["canonical_arguments"]
        op = {"operation": row["operation"], "target": row["target_resource"], "arguments": arguments}
        row["operation_digest"] = hashlib.sha256(json.dumps(op, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"approvals": [row]})))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    with pytest.raises(SupervisorRefused, match="development (approval|plan)"):
        adapter.list_pending_approvals("b" * 32)
    client.close()


def test_adapter_refuses_non_loopback_targets_and_unsafe_credential_modes(tmp_path):
    key = _key(tmp_path / "api-key")
    for url in ("http://192.168.1.2:8000", "https://api.example", "http://127.0.0.1:8000/path"):
        with pytest.raises(ValueError):
            OpenJarvisApiSupervisorAdapter(key_file=key, base_url=url)
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key)
    with pytest.raises(SupervisorUnavailable):
        adapter.submit_task(objective="read", capability_scope=["read_only"], idempotency_key="link-message-id-0002",
                            requester_device_id="d" * 32)
    adapter.close()


def test_adapter_can_read_api_key_from_local_openjarvis_config_only(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[server.auth]\napi_key = "test-config-key"\n', encoding="utf-8")
    os.chmod(config, 0o600)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"task_id": "a" * 32, "state": "COMPLETED", "summary": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(
        key_file=tmp_path / "missing-key-file",
        config_file=config,
        http_client=client,
    )
    adapter.submit_task(objective="read", capability_scope=["read_only"], idempotency_key="link-message-id-0005",
                        requester_device_id="d" * 32)
    assert seen[0].headers["authorization"] == "Bearer test-config-key"
    client.close()


def test_adapter_refuses_group_or_world_readable_config_credentials(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[server.auth]\napi_key = "must-not-be-used"\n', encoding="utf-8")
    os.chmod(config, 0o644)
    adapter = OpenJarvisApiSupervisorAdapter(
        key_file=tmp_path / "missing-key-file",
        config_file=config,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"task_id": "a" * 32, "state": "COMPLETED"}))),
    )
    with pytest.raises(SupervisorUnavailable, match="configuration permissions"):
        adapter.submit_task(objective="read only", capability_scope=["read_only"], idempotency_key="link-message-id-0007",
                            requester_device_id="d" * 32)
    adapter.close()


def test_loopback_server_503_is_retryable(tmp_path):
    key = _key(tmp_path / "api-key")
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503, json={"detail": "private"})))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    with pytest.raises(SupervisorUnavailable, match="HTTP 503"):
        adapter.submit_task(objective="read", capability_scope=["read_only"], idempotency_key="link-message-id-0003",
                            requester_device_id="d" * 32)
    client.close()


def test_task_202_is_polled_until_supervisor_approval_state(tmp_path, monkeypatch):
    import jarvis_link_host.openjarvis_api_adapter as api_adapter

    key = _key(tmp_path / "api-key")
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": "f" * 32, "state": "RUNNING"})
        return httpx.Response(200, json={"task_id": "f" * 32, "state": "WAITING_APPROVAL", "summary": ""})

    monkeypatch.setattr(api_adapter.time, "sleep", lambda _delay: None)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    result = adapter.submit_task(
        objective="Open TextEdit after one approval",
        capability_scope=["assistant"],
        idempotency_key="link-message-id-0004",
        requester_device_id="d" * 32,
    )
    assert result["state"] == "WAITING_APPROVAL"
    assert [request.method for request in requests] == ["POST", "GET"]
    assert requests[-1].url.path.endswith("/link/tasks/link-message-id-0004")
    client.close()


def test_async_202_persists_idempotency_mapping_before_receive_loop_returns(tmp_path):
    key = _key(tmp_path / "api-key")
    ledger = tmp_path / "submission-ledger.json"
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/jarvis/link/tasks"
        assert request.url.params["mode"] == "async"
        return httpx.Response(202, json={"task_id": "9" * 32, "state": "CREATED", "summary": "Queued"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(
        key_file=key,
        http_client=client,
        async_submission=True,
        submission_ledger=ledger,
        strict_recovery=True,
    )
    result = adapter.submit_task(
        objective="a slow but safe task",
        capability_scope=["read_only"],
        idempotency_key="async-ledger-owner-0001",
        requester_device_id="d" * 32,
    )
    saved = json.loads(ledger.read_text())
    assert result["state"] == "CREATED" and result["task_id"] == "9" * 32
    assert saved["async-ledger-owner-0001"]["task_id"] == "9" * 32
    assert len(requests) == 1
    client.close()


def test_task_event_page_uses_authenticated_owner_scoped_cursor(tmp_path):
    key = _key(tmp_path / "api-key")
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/jarvis/link/tasks/event-owner-key-0001/events"
        assert dict(request.url.params) == {"after_seq": "7", "limit": "20"}
        return httpx.Response(200, json={
            "task_id": "a" * 32,
            "events": [{"seq": 8, "event_type": "task.state", "payload": {"to": "RUNNING", "step": "codex_link"}}],
            "next_seq": 8,
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    page = adapter.task_events("event-owner-key-0001", after_seq=7, limit=20)
    assert page == {
        "task_id": "a" * 32,
        "events": [{"seq": 8, "event_type": "task.state", "payload": {"to": "RUNNING", "step": "codex_link"}}],
        "next_seq": 8,
    }
    assert seen[0].headers["authorization"] == "Bearer test-local-supervisor-key"
    client.close()


def test_task_event_page_rejects_invalid_cursor_before_request(tmp_path):
    key = _key(tmp_path / "api-key")
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: pytest.fail("unexpected request")))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    with pytest.raises(SupervisorRefused, match="cursor"):
        adapter.task_events("event-owner-key-0001", after_seq=-1)
    client.close()


def test_host_progress_mapping_emits_only_user_facing_supervisor_stages(tmp_path):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "link_host_admin.py"
    spec = importlib.util.spec_from_file_location("jarvis_link_progress_mapping", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._progress_label({
        "event_type": "task.state",
        "payload": {"to": "RUNNING", "step": "codex_link", "reasoning": "private model analysis"},
    }) == ("RUNNING", "Working on your request")
    assert module._progress_label({
        "event_type": "link_task.waiting_approval",
        "payload": {"approval_id": "a" * 32},
    }) == ("WAITING_APPROVAL", "Waiting for your approval")
    assert module._progress_label({
        "event_type": "link_task.completed",
        "payload": {"result_text": "private assistant answer"},
    }) is None
    assert module._progress_label({
        "event_type": "development.plan_approval_requested",
        "payload": {"approval_id": "private-to-owner"},
    }) == ("WAITING_APPROVAL", "Waiting for your approval of the bounded project plan")
    assert module._progress_label({
        "event_type": "development.operation_started",
        "payload": {"operation": "run_tests", "command": "private command"},
    }) == ("RUNNING", "Running the approved project tests")
    assert module._progress_label({
        "event_type": "development.tests_completed",
        "payload": {"passed": False, "output": "private test output"},
    }) == ("RUNNING", "Project tests found failures; checking within the approved repair limit")


def test_host_publishes_new_durable_events_and_advances_checkpoint(tmp_path, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "link_host_admin.py"
    spec = importlib.util.spec_from_file_location("jarvis_link_progress_publish", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    watch_path = tmp_path / "watch.json"
    watch_path.write_text(json.dumps([{
        "task_id": "a" * 32, "idempotency_key": "progress-task-key-0001",
        "companion_id": "b" * 32, "published": False, "progress_seq": 3,
    }]))
    monkeypatch.setattr(module, "TASK_WATCH_PATH", watch_path)
    monkeypatch.setattr(module, "_companion_certificate", lambda _client, device_id: {"device_id": device_id})

    class Adapter:
        def task_status(self, _key):
            return {"task_id": "a" * 32, "state": "RUNNING"}

        def task_events(self, _key, *, after_seq, limit):
            assert after_seq == 3 and limit == 100
            return {"task_id": "a" * 32, "events": [
                {"seq": 4, "event_type": "link_task.started", "payload": {"scope": "assistant"}},
                {"seq": 5, "event_type": "task.state", "payload": {"to": "RUNNING", "step": "codex_link"}},
            ]}

        def list_pending_approvals(self, _task_id):
            return []

    class Runtime:
        sent = []

        def send_task_progress(self, **payload):
            self.sent.append(payload)

    runtime = Runtime()
    assert module._publish_task_updates({}, object(), None, Adapter(), runtime) == 2
    assert [item["event_seq"] for item in runtime.sent] == [4, 5]
    assert all(item["state"] == "RUNNING" for item in runtime.sent)
    saved = json.loads(watch_path.read_text())[0]
    assert saved["progress_seq"] == 5


def test_host_publishes_persisted_terminal_answer_after_async_acceptance(tmp_path, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "link_host_admin.py"
    spec = importlib.util.spec_from_file_location("jarvis_link_terminal_publish", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    watch_path = tmp_path / "watch.json"
    watch_path.write_text(json.dumps([{
        "task_id": "a" * 32, "idempotency_key": "terminal-task-key-0001",
        "companion_id": "b" * 32, "published": False,
    }]))
    monkeypatch.setattr(module, "TASK_WATCH_PATH", watch_path)
    monkeypatch.setattr(module, "_companion_certificate", lambda _client, device_id: {"device_id": device_id})

    class Adapter:
        def task_status(self, _key):
            return {
                "task_id": "a" * 32, "state": "COMPLETED", "summary": "answer preview",
                "result_text": "Complete verified answer", "result_digest": "c" * 64,
            }

        def list_pending_approvals(self, _task_id):
            return []

    class Runtime:
        sent = []

        def send_task_update(self, **payload):
            self.sent.append(payload)

    runtime = Runtime()
    assert module._publish_task_updates({}, object(), None, Adapter(), runtime) == 1
    assert runtime.sent[0]["result_text"] == "Complete verified answer"
    assert runtime.sent[0]["state"] == "COMPLETED"
    assert json.loads(watch_path.read_text())[0]["published"] is True


def test_paused_task_watches_use_the_slow_recovery_poll_cadence(tmp_path, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "link_host_admin.py"
    spec = importlib.util.spec_from_file_location("jarvis_link_paused_watch", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    watch_path = tmp_path / "watch.json"
    watch_path.write_text(json.dumps([{"task_id": "a" * 32, "published": False, "paused_reported": True}]))
    monkeypatch.setattr(module, "TASK_WATCH_PATH", watch_path)
    assert module._has_open_task_watch() is False
    assert module._has_open_task_watch(include_paused=True) is True


def test_strict_recovery_keeps_unknown_conflict_unacknowledged_after_restart(tmp_path):
    key = _key(tmp_path / "key")
    ledger = tmp_path / "ledger.json"
    calls = []

    def lost_response(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("simulated lost response", request=request)

    first_client = httpx.Client(transport=httpx.MockTransport(lost_response))
    first = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=first_client,
                                           submission_ledger=ledger, strict_recovery=True)
    args = dict(objective="read safely", capability_scope=["read_only"], idempotency_key="lost-reply-unique-0001",
                requester_device_id="d" * 32)
    with pytest.raises(SupervisorUnavailable):
        first.submit_task(**args)
    first_client.close()

    def conflict(request):
        calls.append(request.method)
        if request.method == "GET":
            pytest.fail("unverified task status cannot establish matching arguments")
        return httpx.Response(409, json={"detail": "conflict"})

    second_client = httpx.Client(transport=httpx.MockTransport(conflict))
    second = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=second_client,
                                            submission_ledger=ledger, strict_recovery=True)
    with pytest.raises(SupervisorUnavailable, match="uncertain"):
        second.submit_task(**args)
    assert calls == ["POST", "POST"]
    second_client.close()


def test_strict_cancel_uses_only_known_submission_mapping_and_checks_task_id(tmp_path):
    key = _key(tmp_path / "api-key")
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"task_id": "a" * 32, "state": "CANCELLED", "action_may_have_started": False})
        return httpx.Response(200, json={"task_id": "a" * 32, "state": "COMPLETED"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client, strict_recovery=True)
    args = dict(objective="safe", capability_scope=["read_only"], idempotency_key="owned-submission-0001",
                requester_device_id="d" * 32)
    adapter.submit_task(**args)
    assert adapter.cancel_task("a" * 32, "cancel-key")["state"] == "CANCELLED"
    assert adapter.cancel_task("a" * 32, "cancel-key")["state"] == "CANCELLED"
    assert seen[-1][1] == "/v1/jarvis/link/tasks/owned-submission-0001/cancel"
    with pytest.raises(SupervisorRefused, match="mapping"):
        adapter.cancel_task("b" * 32, "cancel-key")
    client.close()
