"""A replayed task submit is recognised through the submission ledger, never by trusting the key alone."""
from __future__ import annotations

import json
import os

import httpx
import pytest

from jarvis_link_host.openjarvis_api_adapter import OpenJarvisApiSupervisorAdapter, SupervisorUnavailable
from jarvis_link_host.openjarvis_adapter import SupervisorRefused

TASK = "d" * 32
KEY = "link-message-id-replay-0001"


def _key(path):
    path.write_text("test-local-supervisor-key\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _submit(adapter, objective="Open TextEdit then summarize"):
    return adapter.submit_task(objective=objective, capability_scope=["assistant"], idempotency_key=KEY)


def test_replay_of_the_same_request_returns_the_existing_task(tmp_path):
    key = _key(tmp_path / "api-key")
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            if len(posts) == 1:
                return httpx.Response(202, json={"task_id": TASK, "state": "WAITING_APPROVAL"})
            return httpx.Response(409, json={"detail": "idempotency key already used"})
        return httpx.Response(200, json={"task_id": TASK, "state": "WAITING_APPROVAL", "summary": ""})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client)
    first = _submit(adapter)
    replay = _submit(adapter)
    assert first["task_id"] == TASK and "replayed" not in first
    assert replay["task_id"] == TASK
    assert replay["state"] == "WAITING_APPROVAL"
    assert replay["replayed"] is True
    client.close()


def test_retry_after_lost_response_recovers_the_task_across_a_restart(tmp_path):
    key = _key(tmp_path / "api-key")
    ledger = tmp_path / "submissions.json"
    calls = {"post": 0}

    def lost_then_replayed(request):
        if request.method == "POST":
            calls["post"] += 1
            if calls["post"] == 1:
                # The supervisor created the task, but the response never reached the adapter.
                raise httpx.ConnectError("connection reset")
            return httpx.Response(409, json={"detail": "idempotency key already used"})
        return httpx.Response(200, json={"task_id": TASK, "state": "RUNNING", "summary": ""})

    client = httpx.Client(transport=httpx.MockTransport(lost_then_replayed))
    with pytest.raises(SupervisorUnavailable):
        _submit(OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client, submission_ledger=ledger))

    # A fresh adapter, as after a host restart, reads the persisted digest and recovers the task.
    replay = _submit(OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client, submission_ledger=ledger))
    assert replay["task_id"] == TASK
    assert replay["replayed"] is True
    stored = json.loads(ledger.read_text(encoding="utf-8"))
    assert set(stored[KEY]) == {"digest", "task_id"}
    assert len(stored[KEY]["digest"]) == 64
    assert "Open TextEdit" not in ledger.read_text(encoding="utf-8")
    client.close()


def test_replay_of_a_key_this_host_never_submitted_is_refused_and_not_recorded(tmp_path):
    key = _key(tmp_path / "api-key")
    ledger = tmp_path / "submissions.json"
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(409, json={"detail": "exists"}) if request.method == "POST"
        else httpx.Response(200, json={"task_id": TASK, "state": "COMPLETED", "summary": ""})
    ))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client, submission_ledger=ledger)
    with pytest.raises(SupervisorRefused, match="could not be verified"):
        _submit(adapter)
    # Nothing was recorded, so a later retry cannot borrow an unrelated task.
    assert not ledger.exists() or KEY not in json.loads(ledger.read_text(encoding="utf-8"))
    with pytest.raises(SupervisorRefused, match="could not be verified"):
        _submit(adapter)
    client.close()


def test_same_key_with_a_different_objective_is_refused_before_any_request(tmp_path):
    key = _key(tmp_path / "api-key")
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(202, json={"task_id": TASK, "state": "WAITING_APPROVAL"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenJarvisApiSupervisorAdapter(key_file=key, http_client=client,
                                             submission_ledger=tmp_path / "submissions.json")
    _submit(adapter, objective="Open TextEdit then summarize")
    with pytest.raises(SupervisorRefused, match="different request"):
        _submit(adapter, objective="Delete everything in Downloads")
    assert len(posts) == 1
    client.close()
