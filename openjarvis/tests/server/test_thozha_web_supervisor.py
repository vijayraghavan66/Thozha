from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from openjarvis.engine import codex_sdk
from openjarvis.server import supervisor_routes
from openjarvis.server.thozha_web_auth import WebPrincipal
from openjarvis.server.thozha_web_supervisor import (
    create_web_supervisor_router,
    is_web_device_active,
    is_web_device_mapped,
    resolve_web_device_ids,
)
from openjarvis.tasks.artifact_store import ArtifactStore
from openjarvis.tasks.supervisor import SupervisorStore

WEB_OWNER = "web-owner"
DEVICE_A = "a" * 32
DEVICE_B = "b" * 32


class FakeRunner:
    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1


def _add_device(
    store: SupervisorStore,
    device_id: str,
    *,
    revoked: bool = False,
    expired: bool = False,
):
    now = datetime.now(timezone.utc)
    expires_at = (
        now - timedelta(minutes=1) if expired else now + timedelta(days=10)
    ).isoformat()
    revoked_at = now.isoformat() if revoked else None
    store._db.execute(
        "INSERT INTO paired_devices("
        "id,public_key_b64,token_hash,created_at,expires_at,revoked_at,device_name) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            device_id,
            "test-public-key",
            hashlib.sha256(device_id.encode()).hexdigest(),
            now.isoformat(),
            expires_at,
            revoked_at,
            "Fixture device",
        ),
    )
    store._db.commit()


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    db_path = tmp_path / "supervisor.db"
    store = SupervisorStore(str(db_path), run_recovery=False)
    monkeypatch.setattr(supervisor_routes, "get_supervisor_store", lambda: store)
    _add_device(store, DEVICE_A)
    _add_device(store, DEVICE_B)
    principal = WebPrincipal(
        WEB_OWNER, "test-session", int(datetime.now(timezone.utc).timestamp()) + 3600
    )
    runner = FakeRunner()
    app = FastAPI()
    app.state.supervisor_store = store
    app.state.link_task_runner = runner
    app.state.thozha_web_auth = SimpleNamespace(
        config=SimpleNamespace(owner_id=WEB_OWNER)
    )
    app.include_router(
        create_web_supervisor_router(
            lambda: principal,
            supervisor_store_provider=lambda _request: store,
        )
    )
    monkeypatch.setenv("THOZHA_WEB_OWNER_ID", WEB_OWNER)
    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", DEVICE_A)
    client = TestClient(app)
    yield client, store, runner
    client.close()
    store.close()


def _task_body(
    objective="Return a short status report.", *, device_id=DEVICE_A, key="r" * 32
):
    return {
        "objective": objective,
        "capability_scope": ["read_only"],
        "idempotency_key": key,
        "requester_device_id": device_id,
    }


def _export(files: dict[str, bytes]) -> dict[str, object]:
    manifest = [
        {
            "path": path,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for path, data in sorted(files.items())
    ]
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "files": len(files),
        "bytes": sum(len(data) for data in files.values()),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "file_manifest": manifest,
        "payloads": files,
    }


def _complete_task(
    store: SupervisorStore,
    *,
    device_id: str = DEVICE_A,
    scope: str = "read_only",
    idempotency_key: str = "complete-task-request-0001",
) -> str:
    objective = "Complete a harmless fixture task."
    run = store.create_link_task(
        objective,
        capability_scope=[scope],
        idempotency_key=idempotency_key,
        owner_actor=f"device:{device_id}",
    )
    store.start_link_task(
        run["task_id"],
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=[scope],
        idempotency_key=idempotency_key,
    )
    store.finish_link_task(run["task_id"], result_text="fixture result")
    store.transition(run["task_id"], "VERIFYING", expected="RUNNING")
    store.transition(run["task_id"], "COMPLETED", expected="VERIFYING")
    return run["task_id"]


def test_web_task_submission_is_owner_bound_idempotent_and_queued(web_client):
    client, store, runner = web_client
    first = client.post("/v1/web/tasks", json=_task_body())
    assert first.status_code == 202, first.text
    task_id = first.json()["task_id"]
    assert first.json()["state"] == "CREATED"
    assert first.json()["idempotent_replay"] is False
    assert runner.wakes == 1
    task = store.get_task(task_id)
    assert json.loads(task["recovery_json"])["owner_actor"] == f"device:{DEVICE_A}"

    replay = client.post("/v1/web/tasks", json=_task_body())
    assert replay.status_code == 202
    assert replay.json()["task_id"] == task_id
    assert replay.json()["idempotent_replay"] is True
    assert runner.wakes == 1

    conflict = client.post(
        "/v1/web/tasks", json=_task_body("Different content, same key.")
    )
    assert conflict.status_code == 409


def test_completed_submission_replay_returns_terminal_result_without_waking_worker(
    web_client,
):
    client, store, runner = web_client
    body = _task_body(key="completed-task-replay-0001")
    accepted = client.post("/v1/web/tasks", json=body)
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    run = store.link_task_run(task_id)
    store.start_link_task(
        task_id,
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["read_only"],
        idempotency_key=body["idempotency_key"],
    )
    store.finish_link_task(task_id, result_text="Real stored fixture result.")
    store.transition(task_id, "VERIFYING", expected="RUNNING")
    store.transition(task_id, "COMPLETED", expected="VERIFYING")

    replay = client.post("/v1/web/tasks", json=body)
    assert replay.status_code == 200
    assert replay.json()["task_id"] == task_id
    assert replay.json()["result_text"] == "Real stored fixture result."
    assert replay.json()["idempotent_replay"] is True
    assert runner.wakes == 1


def test_unmapped_or_inactive_device_fails_closed(web_client, monkeypatch):
    client, store, _ = web_client
    unauthorized = client.post("/v1/web/tasks", json=_task_body(device_id=DEVICE_B))
    assert unauthorized.status_code == 403

    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", "")
    assert client.get("/v1/web/tasks").status_code == 403

    _add_device(store, "c" * 32, revoked=True)
    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", "c" * 32)
    assert client.get("/v1/web/tasks").status_code == 403


def test_mapping_helper_separates_mapped_from_active(web_client, monkeypatch):
    _, store, _ = web_client
    revoked_id = "e" * 32
    _add_device(store, revoked_id, revoked=True)
    principal = WebPrincipal(
        WEB_OWNER, "test-session", int(datetime.now(timezone.utc).timestamp()) + 3600
    )
    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", revoked_id)
    assert is_web_device_mapped(principal, revoked_id) is True
    assert is_web_device_active(WEB_OWNER, revoked_id, store) is False
    with pytest.raises(HTTPException):
        resolve_web_device_ids(principal, store)


def test_inactive_paired_device_is_not_accepted(web_client, monkeypatch, tmp_path):
    client, store, _ = web_client
    expired_id = "d" * 32
    _add_device(store, expired_id, expired=True)
    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", expired_id)
    response = client.post("/v1/web/tasks", json=_task_body(device_id=expired_id))
    assert response.status_code == 403


def test_cross_device_task_reads_are_hidden(web_client, monkeypatch):
    client, store, _ = web_client
    objective = "A device-owned read-only task."
    task = store.create_link_task(
        objective,
        capability_scope=["read_only"],
        idempotency_key="cross-owner-request-0001",
        owner_actor=f"device:{DEVICE_B}",
    )
    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", DEVICE_A)
    assert client.get(f"/v1/web/tasks/{task['task_id']}").status_code == 404
    assert client.get(f"/v1/web/tasks/{task['task_id']}/events").status_code == 404


def test_task_status_events_and_cancel_are_persisted(web_client):
    client, store, _ = web_client
    submitted = client.post("/v1/web/tasks", json=_task_body())
    task_id = submitted.json()["task_id"]
    detail = client.get(f"/v1/web/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["state"] == "CREATED"

    events = client.get(f"/v1/web/tasks/{task_id}/events?after_seq=0&limit=20")
    assert events.status_code == 200
    assert events.json()["events"]
    assert all(
        "objective" not in event.get("payload", {}) for event in events.json()["events"]
    )

    cancelled = client.post(f"/v1/web/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"
    assert store.get_task(task_id)["state"] == "CANCELLED"


def test_sse_replays_durable_sanitized_task_events(web_client):
    client, store, _ = web_client
    task_id = _complete_task(store)
    response = client.get(f"/v1/web/tasks/{task_id}/events/stream?after_seq=0")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert "id: " in response.text
    assert "event: task.state" in response.text
    assert '"to":"COMPLETED"' in response.text
    assert "objective_text" not in response.text


def test_web_artifacts_require_owned_completed_development_task(
    web_client, monkeypatch, tmp_path
):
    client, store, _ = web_client
    task_id = _complete_task(
        store,
        scope="development",
        idempotency_key="development-artifact-task-0001",
    )
    files = {"README.md": b"safe fixture", "src/main.py": b"print('ok')"}
    artifact_store = ArtifactStore(
        tmp_path / "artifact-root", tmp_path / "artifacts.db"
    )
    artifact = artifact_store.create_bundle(
        task_id=task_id, device_id=DEVICE_A, export=_export(files)
    )
    monkeypatch.setattr(
        "openjarvis.server.thozha_web_supervisor._artifact_store",
        lambda: artifact_store,
    )
    try:
        listed = client.get(f"/v1/web/tasks/{task_id}/artifacts")
        assert listed.status_code == 200
        wire = listed.json()["artifacts"][0]
        assert wire["archive_sha256"] == artifact.archive_sha256
        downloaded = client.get(wire["download_path"])
        assert downloaded.status_code == 200
        assert hashlib.sha256(downloaded.content).hexdigest() == artifact.archive_sha256
        with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
            assert archive.read("src/main.py") == files["src/main.py"]
        monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", DEVICE_B)
        cross_owner = client.get(
            f"/v1/web/tasks/{task_id}/artifacts/{artifact.artifact_id}/download",
        )
        assert cross_owner.status_code == 404
        monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", DEVICE_A)
        (artifact_store.root / task_id / artifact.file_name).write_bytes(b"tampered")
        corrupted = client.get(
            f"/v1/web/tasks/{task_id}/artifacts/{artifact.artifact_id}/download"
        )
        assert corrupted.status_code == 500
        assert corrupted.json()["detail"] == "Stored artifact integrity check failed"
    finally:
        artifact_store.close()


def _waiting_approval(
    store: SupervisorStore,
    *,
    device_id=DEVICE_A,
    idempotency_key="approval-request-0001",
    operation="mcp:jarvis_mac:open_app",
    target="Calculator",
    arguments=None,
):
    objective = "Open Calculator on the Mac."
    run = store.create_link_task(
        objective,
        capability_scope=["assistant"],
        idempotency_key=idempotency_key,
        owner_actor=f"device:{device_id}",
    )
    store.start_link_task(
        run["task_id"],
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["assistant"],
        idempotency_key=idempotency_key,
    )
    approval_id, digest = store.create_approval(
        run["task_id"],
        operation,
        target,
        arguments or {"name": "Calculator"},
        "Open Calculator after owner approval.",
    )
    store.mark_link_task_waiting_approval(run["task_id"], approval_id)
    return run["task_id"], approval_id, digest


def test_approval_review_and_reject_use_exact_shared_decision_path(web_client):
    client, store, _ = web_client
    task_id, approval_id, digest = _waiting_approval(store)
    listed = client.get("/v1/web/approvals/pending")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["approvals"]] == [approval_id]
    card = client.get(f"/v1/web/approvals/{approval_id}")
    assert card.status_code == 200
    assert card.json()["operation_digest"] == digest
    assert card.json()["canonical_arguments"] == {"name": "Calculator"}

    decision = client.post(
        f"/v1/web/approvals/{approval_id}/decision",
        json={
            "decision": "reject",
            "task_id": task_id,
            "expected_operation_digest": digest,
            "idempotency_key": "web-decision-reject-0001",
            "requester_device_id": DEVICE_A,
        },
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "REJECTED"
    assert store.approval(approval_id)["status"] == "REJECTED"
    assert store.link_task_run(task_id)["status"] == "REJECTED"

    replay = client.post(
        f"/v1/web/approvals/{approval_id}/decision",
        json={
            "decision": "reject",
            "task_id": task_id,
            "expected_operation_digest": digest,
            "idempotency_key": "web-decision-reject-0001",
            "requester_device_id": DEVICE_A,
        },
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "REJECTED"


def test_mismatched_or_expired_approval_never_dispatches(web_client):
    client, store, _ = web_client
    task_id, approval_id, digest = _waiting_approval(
        store,
        idempotency_key="approval-request-0002",
        operation="mcp:jarvis_mac:click",
        target="screen-coordinate",
        arguments={"x": 1, "y": 1},
    )
    mismatch = client.post(
        f"/v1/web/approvals/{approval_id}/decision",
        json={
            "decision": "approve",
            "task_id": task_id,
            "expected_operation_digest": "0" * 64,
            "idempotency_key": "web-decision-mismatch-01",
            "requester_device_id": DEVICE_A,
        },
    )
    assert mismatch.status_code == 409
    assert store.approval(approval_id)["status"] == "PENDING"

    store._db.execute(
        "UPDATE operation_approvals SET expires_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), approval_id),
    )
    store._db.commit()
    expired = client.post(
        f"/v1/web/approvals/{approval_id}/decision",
        json={
            "decision": "approve",
            "task_id": task_id,
            "expected_operation_digest": digest,
            "idempotency_key": "web-decision-expired-01",
            "requester_device_id": DEVICE_A,
        },
    )
    assert expired.status_code == 200, expired.text
    assert expired.json()["status"] == "EXPIRED"
    assert expired.json()["executed"] is False
    assert store.link_task_run(task_id)["status"] == "FAILED"


def test_web_approval_cannot_decide_for_another_mapped_device(web_client, monkeypatch):
    client, store, _ = web_client
    task_id, approval_id, digest = _waiting_approval(
        store, device_id=DEVICE_B, idempotency_key="approval-request-0003"
    )
    response = client.post(
        f"/v1/web/approvals/{approval_id}/decision",
        json={
            "decision": "reject",
            "task_id": task_id,
            "expected_operation_digest": digest,
            "idempotency_key": "web-decision-owner-0003",
            "requester_device_id": DEVICE_B,
        },
    )
    assert response.status_code == 403
    assert store.approval(approval_id)["status"] == "PENDING"


def test_approved_call_routes_through_shared_exact_executor(tmp_path, monkeypatch):
    """Exercise actual shared continuation with an inert fake MCP result."""
    store = SupervisorStore(str(tmp_path / "approval.db"), run_recovery=False)
    monkeypatch.setattr(supervisor_routes, "get_supervisor_store", lambda: store)
    _add_device(store, DEVICE_A)
    principal = WebPrincipal(
        WEB_OWNER, "session", int(datetime.now(timezone.utc).timestamp()) + 3600
    )
    runner = FakeRunner()
    app = FastAPI()
    app.state.supervisor_store = store
    app.state.link_task_runner = runner
    app.state.agent = None
    app.state.model = "test-model"
    app.state.thozha_web_auth = SimpleNamespace(
        config=SimpleNamespace(owner_id=WEB_OWNER)
    )
    app.include_router(
        create_web_supervisor_router(
            lambda: principal, supervisor_store_provider=lambda _: store
        )
    )
    monkeypatch.setenv("THOZHA_WEB_OWNER_ID", WEB_OWNER)
    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", DEVICE_A)
    monkeypatch.setattr(
        codex_sdk,
        "_call_local_mcp",
        lambda *args, **kwargs: {
            "isError": False,
            "content": [{"type": "text", "text": "inert test success"}],
        },
    )
    client = TestClient(app)
    try:
        task_id, approval_id, digest = _waiting_approval(
            store,
            idempotency_key="approval-request-0004",
            operation="mcp:jarvis_mac:click",
            target="screen-coordinate",
            arguments={"x": 1, "y": 1},
        )
        approved = client.post(
            f"/v1/web/approvals/{approval_id}/decision",
            json={
                "decision": "approve",
                "task_id": task_id,
                "expected_operation_digest": digest,
                "idempotency_key": "web-decision-approve-0004",
                "requester_device_id": DEVICE_A,
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "CONSUMED"
        assert approved.json()["success"] is True
        assert approved.json()["execution_state"] == "COMPLETED"
        assert store.approval(approval_id)["status"] == "CONSUMED"
        assert store.get_task(task_id)["state"] == "COMPLETED"
        replay = client.post(
            f"/v1/web/approvals/{approval_id}/decision",
            json={
                "decision": "approve",
                "task_id": task_id,
                "expected_operation_digest": digest,
                "idempotency_key": "web-decision-approve-0004",
                "requester_device_id": DEVICE_A,
            },
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "CONSUMED"
        assert (
            store.approval(approval_id)["decision_nonce"] == "web-decision-approve-0004"
        )
    finally:
        client.close()
        store.close()
