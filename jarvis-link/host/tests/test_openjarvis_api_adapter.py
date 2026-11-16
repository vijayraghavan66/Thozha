"""The remote host keeps the OpenJarvis master credential on loopback only."""
from __future__ import annotations

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
    )
    assert task["task_id"] == "a" * 32
    assert task["state"] == "WAITING_APPROVAL"
    assert requests[0].url == "http://127.0.0.1:8000/v1/jarvis/link/tasks"
    assert requests[0].headers["authorization"] == "Bearer test-local-supervisor-key"
    assert json.loads(requests[0].content) == {
        "objective": "Open TextEdit then summarize",
        "capability_scope": ["assistant"],
        "idempotency_key": "link-message-id-0001",
    }
    assert "test-local-supervisor-key" not in str(requests[0].url)

    result = adapter.decide_approval(
        task_id="a" * 32,
        approval_id="b" * 32,
        decision="approve",
        expected_arguments_digest="c" * 64,
        idempotency_key="approval-message-id-001",
        continuation_objective="Open TextEdit then summarize",
    )
    assert result["status"] == "CONSUMED"
    body = json.loads(requests[1].content)
    assert body["continuation_objective"] == "Open TextEdit then summarize"
    assert body["expected_operation_digest"] == "c" * 64
    assert "test-local-supervisor-key" not in requests[1].content.decode()
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


def test_adapter_refuses_non_loopback_targets_and_unsafe_credential_modes(tmp_path):
    key = _key(tmp_path / "api-key")
    for url in ("http://192.168.1.2:8000", "https://api.example", "http://127.0.0.1:8000/path"):
        with pytest.raises(ValueError):
            OpenJarvisApiSupervisorAdapter(key_file=key, base_url=url)
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key)
    with pytest.raises(SupervisorUnavailable):
        adapter.submit_task(objective="read", capability_scope=["read_only"], idempotency_key="link-message-id-0002")
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
    adapter.submit_task(objective="read", capability_scope=["read_only"], idempotency_key="link-message-id-0005")
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
        adapter.submit_task(objective="read only", capability_scope=["read_only"], idempotency_key="link-message-id-0007")
    adapter.close()


def test_loopback_server_503_is_retryable(tmp_path):
    key = _key(tmp_path / "api-key")
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503, json={"detail": "private"})))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    with pytest.raises(SupervisorUnavailable, match="HTTP 503"):
        adapter.submit_task(objective="read", capability_scope=["read_only"], idempotency_key="link-message-id-0003")
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
    )
    assert result["state"] == "WAITING_APPROVAL"
    assert [request.method for request in requests] == ["POST", "GET"]
    assert requests[-1].url.path.endswith("/link/tasks/link-message-id-0004")
    client.close()
