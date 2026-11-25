"""Isolated allowlist, auth, and migration tests for the loopback gateway."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openjarvis.server.companion_gateway import (
    ReadOnlyGatewayStore,
    _TASK_EVENTS_SUBPROTOCOL,
    create_gateway_app,
    validate_bind_host,
)
from openjarvis.server.supervisor_routes import _canonical_pairing_payload
from openjarvis.tasks.supervisor import SupervisorStore
import openjarvis.tasks.supervisor as supervisor_module
from openjarvis.server.supervisor_routes import router as supervisor_router


@pytest.fixture
def gateway(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    token = "ojd_test_device_token_for_gateway"
    device_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    store._db.execute(
        "INSERT INTO paired_devices(id,public_key_b64,token_hash,created_at,expires_at,device_name) VALUES(?,?,?,?,?,?)",
        (
            device_id,
            base64.b64encode(public_der).decode(),
            hashlib.sha256(token.encode()).hexdigest(),
            now.isoformat(),
            (now + timedelta(days=20)).isoformat(),
            "Gateway test phone",
        ),
    )
    store._db.commit()
    upstream_requests = []

    async def upstream_handler(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        if request.url.path.endswith("/events"):
            if "deviceowner-denied0001" in request.url.path:
                return httpx.Response(403, json={"detail": "Task belongs to another paired device"})
            after_seq = int(request.url.params.get("after_seq", "0"))
            if "checkpoint-only" in request.url.path:
                events = []
                next_seq = 3
            elif after_seq == 0:
                events = [{
                    "seq": 1,
                    "event_type": "link_task.queued",
                    "created_at": "2026-11-25T00:00:00Z",
                    "payload": {"state": "CREATED"},
                }]
                next_seq = 3
            elif after_seq in {1, 3}:
                events = [{
                    "seq": 5,
                    "event_type": "link_task.completed",
                    "created_at": "2026-11-25T00:00:01Z",
                    "payload": {"success": True},
                }]
                next_seq = 5
            else:
                events = []
                next_seq = after_seq
            return httpx.Response(200, json={"task_id": "f" * 32, "events": events, "next_seq": next_seq})
        if request.url.path.endswith("/download"):
            digest = "a" * 64
            return httpx.Response(
                200,
                content=b"synthetic archive bytes",
                headers={
                    "Content-Type": "application/zip",
                    "Content-Disposition": 'attachment; filename="archive.zip"',
                    "X-Content-SHA256": digest,
                    "ETag": f'"{digest}"',
                    "X-Internal-Header": "must-not-be-forwarded",
                },
            )
        return httpx.Response(200, json={"path": request.url.path, "method": request.method, "ok": True})

    app = create_gateway_app(
        supervisor_store=store,
        backend_transport=httpx.MockTransport(upstream_handler),
        expected_identity_origin="https://mac.tailnet.ts.net",
    )
    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        yield client, store, token, device_id, key.public_key(), upstream_requests
    store.close()


def test_gateway_allows_only_companion_paths_and_methods(gateway):
    client, _, token, _, _, upstream = gateway
    headers = {"Authorization": f"Bearer {token}", "X-Jarvis-Trace-ID": "a1b2c3d4e5f6"}

    response = client.get("/v1/jarvis/tasks?state=active", headers=headers)
    assert response.status_code == 200
    assert response.json()["path"] == "/v1/jarvis/tasks"
    assert upstream[-1].headers["authorization"] == f"Bearer {token}"
    assert upstream[-1].headers["x-jarvis-trace-id"] == "a1b2c3d4e5f6"

    client.get(
        "/v1/jarvis/tasks?state=active",
        headers={"Authorization": f"Bearer {token}", "X-Jarvis-Trace-ID": "not-a-trace"},
    )
    assert "x-jarvis-trace-id" not in upstream[-1].headers

    status = client.get("/v1/jarvis/system/info", headers=headers)
    assert status.status_code == 200
    assert upstream[-1].url.path == "/v1/jarvis/system/info"
    assert upstream[-1].headers["authorization"] == f"Bearer {token}"

    assert client.get("/v1/jarvis/pair/pending", headers=headers).status_code == 404
    assert client.post("/v1/jarvis/pair/claim", headers=headers, json={}).status_code == 404
    assert client.post("/v1/jarvis/pair/invitations", headers=headers).status_code == 404
    assert client.post("/v1/jarvis/pair/start", headers=headers).status_code == 404
    assert client.post("/v1/jarvis/pair/complete", headers=headers, json={}).status_code == 404
    assert client.get("/v1/jarvis/pair/pending", headers=headers).status_code == 404
    assert client.get("/v1/jarvis/pair/devices", headers=headers).status_code == 404
    assert client.post(
        f"/v1/jarvis/pair/devices/{'a' * 32}/revoke", headers=headers, json={"confirm": True},
    ).status_code == 404
    assert client.post(f"/v1/jarvis/pair/{'a' * 32}/decision", headers=headers, json={}).status_code == 404
    assert client.get("/v1/chat/completions", headers=headers).status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.post("/v1/jarvis/tasks", headers=headers).status_code == 405

    fcm_token = "fcm-registration-token-0123456789-gateway"
    registered = client.post("/v1/jarvis/devices/fcm-token", headers=headers, json={"token": fcm_token})
    assert registered.status_code == 200
    assert upstream[-1].url.path == "/v1/jarvis/devices/fcm-token"
    assert json.loads(upstream[-1].content) == {"token": fcm_token}
    revoked = client.delete("/v1/jarvis/devices/fcm-token", headers=headers)
    assert revoked.status_code == 200
    assert upstream[-1].method == "DELETE"


def _pairing_gateway(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "pairing-supervisor.db"))
    monkeypatch.setattr(supervisor_module, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "pairing-integration-master")
    backend_app = FastAPI()
    backend_app.include_router(supervisor_router)

    forwarded_requests: list[httpx.Request] = []

    async def upstream_handler(request: httpx.Request) -> httpx.Response:
        forwarded_requests.append(request)
        transport = httpx.ASGITransport(app=backend_app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as backend:
            response = await backend.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                content=request.content,
                headers={key: value for key, value in request.headers.items() if key.lower() in {"content-type", "accept"}},
            )
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    app = create_gateway_app(
        supervisor_store=store,
        backend_transport=httpx.MockTransport(upstream_handler),
    )
    client = TestClient(app, base_url="http://127.0.0.1:8001")
    return client, store, forwarded_requests


def _new_pairing(store):
    invitation_id, challenge, expires = store.begin_device_pairing()
    key = ec.generate_private_key(ec.SECP256R1())
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signature = key.sign(challenge.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    body = {
        "device_name": "Test Android",
        "public_key": base64.b64encode(public_der).decode("ascii"),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return invitation_id, key, body


def test_invitation_scoped_gateway_pairing_preserves_approval_expiry_and_replay(tmp_path, monkeypatch):
    client, store, forwarded = _pairing_gateway(tmp_path, monkeypatch)
    invitation_id, key, body = _new_pairing(store)
    headers = {"Content-Type": "application/json"}

    completed = client.post(f"/v1/jarvis/pair/{invitation_id}/complete", headers=headers, json=body)
    assert completed.status_code == 200
    assert completed.json()["status"] == "PENDING_CONFIRMATION"
    assert forwarded[-1].url.path == "/v1/jarvis/pair/complete"
    assert forwarded[-1].headers.get("authorization") is None
    assert set(json.loads(forwarded[-1].content)) == {"invitation_id", "device_name", "public_key", "signature"}
    replayed_complete = client.post(f"/v1/jarvis/pair/{invitation_id}/complete", headers=headers, json=body)
    assert replayed_complete.status_code == 409

    pending = client.get(f"/v1/jarvis/pair/{invitation_id}/status")
    assert pending.status_code == 200
    assert pending.json()["status"] == "PENDING_CONFIRMATION"
    assert "claim_challenge" not in pending.json()
    premature = client.post(
        f"/v1/jarvis/pair/{invitation_id}/claim",
        headers=headers,
        json={"signature": base64.b64encode(b"not a valid device signature" * 3).decode("ascii")},
    )
    assert premature.status_code == 409
    assert "access_token" not in premature.json()

    assert store.decide_device_pairing(invitation_id, "approve") == "APPROVED"
    approved = client.get(f"/v1/jarvis/pair/{invitation_id}/status")
    challenge = approved.json()["claim_challenge"]
    claim_signature = base64.b64encode(key.sign(challenge.encode("utf-8"), ec.ECDSA(hashes.SHA256()))).decode("ascii")
    claimed = client.post(
        f"/v1/jarvis/pair/{invitation_id}/claim", headers=headers, json={"signature": claim_signature},
    )
    assert claimed.status_code == 200
    assert claimed.json()["access_token"].startswith("ojd_")
    replayed_claim = client.post(
        f"/v1/jarvis/pair/{invitation_id}/claim", headers=headers, json={"signature": claim_signature},
    )
    assert replayed_claim.status_code == 409
    store.close()
    monkeypatch.setattr(supervisor_module, "_STORE_INSTANCE", None)


def test_invitation_scoped_gateway_rejects_expired_or_mismatched_proofs(tmp_path, monkeypatch):
    client, store, _forwarded = _pairing_gateway(tmp_path, monkeypatch)
    invitation_id, key, body = _new_pairing(store)
    store._db.execute(
        "UPDATE device_pairings SET expires_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), invitation_id),
    )
    store._db.commit()
    expired = client.post(f"/v1/jarvis/pair/{invitation_id}/complete", json=body)
    assert expired.status_code == 409

    other_id, _, _ = _new_pairing(store)
    mismatched = client.post(f"/v1/jarvis/pair/{other_id}/complete", json=body)
    assert mismatched.status_code == 409
    assert "access_token" not in mismatched.json()

    # Strict path IDs, methods, schemas, and body sizes keep this from becoming a proxy.
    assert client.get("/v1/jarvis/pair/not-an-id/status").status_code == 404
    assert client.get(f"/v1/jarvis/pair/{other_id}/status?debug=1").status_code == 422
    assert client.post(f"/v1/jarvis/pair/{other_id}/claim", json={"signature": "x" * 2000}).status_code == 413
    assert client.post(f"/v1/jarvis/pair/{other_id}/claim", json={"signature": "x" * 80, "token": "extra"}).status_code == 422
    store.close()
    monkeypatch.setattr(supervisor_module, "_STORE_INSTANCE", None)


def test_invitation_scoped_gateway_rejects_owner_rejection_and_expired_claim(tmp_path, monkeypatch):
    client, store, _forwarded = _pairing_gateway(tmp_path, monkeypatch)
    rejected_id, _rejected_key, rejected_body = _new_pairing(store)
    rejected = client.post(f"/v1/jarvis/pair/{rejected_id}/complete", json=rejected_body)
    assert rejected.status_code == 200
    assert store.decide_device_pairing(rejected_id, "reject") == "REJECTED"
    rejected_status = client.get(f"/v1/jarvis/pair/{rejected_id}/status")
    assert rejected_status.json()["status"] == "REJECTED"
    rejected_claim = client.post(
        f"/v1/jarvis/pair/{rejected_id}/claim", json={"signature": base64.b64encode(b"x" * 72).decode("ascii")},
    )
    assert rejected_claim.status_code == 409
    assert "access_token" not in rejected_claim.json()

    expired_id, expired_key, expired_body = _new_pairing(store)
    assert client.post(f"/v1/jarvis/pair/{expired_id}/complete", json=expired_body).status_code == 200
    assert store.decide_device_pairing(expired_id, "approve") == "APPROVED"
    store._db.execute(
        "UPDATE device_pairings SET claim_expires_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), expired_id),
    )
    store._db.commit()
    assert client.get(f"/v1/jarvis/pair/{expired_id}/status").json()["status"] == "EXPIRED"
    expired_claim = client.post(
        f"/v1/jarvis/pair/{expired_id}/claim",
        json={"signature": base64.b64encode(expired_key.sign(b"expired", ec.ECDSA(hashes.SHA256()))).decode("ascii")},
    )
    assert expired_claim.status_code == 409
    assert "access_token" not in expired_claim.json()
    store.close()
    monkeypatch.setattr(supervisor_module, "_STORE_INSTANCE", None)


def test_gateway_allows_only_read_only_link_task_envelopes(gateway):
    client, _, token, device_id, _, upstream = gateway
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "objective": "Explain Java in one sentence",
        "capability_scope": ["read_only"],
        "idempotency_key": "jarvis-link-task-20261116-01",
        "requester_device_id": device_id,
    }
    accepted = client.post("/v1/jarvis/link/tasks", headers=headers, json=body)
    assert accepted.status_code == 200
    assert upstream[-1].url.path == "/v1/jarvis/link/tasks"
    assert json.loads(upstream[-1].content) == body

    assert client.post(
        "/v1/jarvis/link/tasks", headers=headers,
        json={**body, "capability_scope": ["system_admin"]},
    ).status_code == 403
    assert client.post(
        "/v1/jarvis/link/tasks", headers=headers,
        json={**body, "requester_device_id": "a" * 32},
    ).status_code == 403
    assert client.post(
        "/v1/jarvis/link/tasks", headers=headers,
        json={key: value for key, value in body.items() if key != "requester_device_id"},
    ).status_code == 422
    assert client.post(
        "/v1/jarvis/link/tasks", headers=headers,
        json={**body, "command": "rm -rf /"},
    ).status_code == 422
    assert client.get("/v1/jarvis/link/tasks/invalid", headers=headers).status_code == 404
    assert client.get(
        "/v1/jarvis/link/tasks/" + body["idempotency_key"], headers=headers,
    ).status_code == 200
    assert upstream[-1].url.path == "/v1/jarvis/link/tasks/" + body["idempotency_key"]
    events = client.get(
        f"/v1/jarvis/link/tasks/{body['idempotency_key']}/events?after_seq=7&limit=25",
        headers=headers,
    )
    assert events.status_code == 200
    assert upstream[-1].url.path.endswith(f"/link/tasks/{body['idempotency_key']}/events")
    assert dict(upstream[-1].url.params) == {"after_seq": "7", "limit": "25"}
    assert client.get(
        f"/v1/jarvis/link/tasks/{body['idempotency_key']}/events?after_seq=-1",
        headers=headers,
    ).status_code == 422
    artifact_task = "b" * 32
    artifacts = client.get(f"/v1/jarvis/tasks/{artifact_task}/artifacts", headers=headers)
    assert artifacts.status_code == 200
    assert upstream[-1].url.path == f"/v1/jarvis/tasks/{artifact_task}/artifacts"
    archive = client.get(
        f"/v1/jarvis/tasks/{artifact_task}/artifacts/{'a' * 64}/download", headers=headers,
    )
    assert archive.status_code == 200
    assert archive.content == b"synthetic archive bytes"
    assert archive.headers["content-type"] == "application/zip"
    assert archive.headers["x-content-sha256"] == "a" * 64
    assert archive.headers["etag"] == '"' + "a" * 64 + '"'
    assert "x-internal-header" not in archive.headers
    assert client.get(
        f"/v1/jarvis/tasks/{artifact_task}/artifacts/invalid/download", headers=headers,
    ).status_code == 404
    cancelled = client.post(
        f"/v1/jarvis/link/tasks/{body['idempotency_key']}/cancel",
        headers=headers,
        json={},
    )
    assert cancelled.status_code == 200
    assert upstream[-1].url.path.endswith(f"/link/tasks/{body['idempotency_key']}/cancel")
    assert json.loads(upstream[-1].content) == {}
    assert client.post(
        f"/v1/jarvis/link/tasks/{body['idempotency_key']}/cancel",
        headers=headers,
        json={"task_id": "caller-controlled"},
    ).status_code == 422


def test_gateway_requires_device_auth_for_companion_data(gateway):
    client, _, _, _, _, upstream = gateway
    before = len(upstream)
    assert client.get("/v1/jarvis/tasks").status_code == 401
    assert client.get("/v1/jarvis/system/info").status_code == 401
    assert client.get("/v1/jarvis/approvals/pending", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert len(upstream) == before


def test_gateway_wss_task_events_replay_ack_and_reconnect_catchup(gateway):
    client, _, token, _, _, upstream = gateway
    key = "wss-catchup-request-0001"
    headers = {"Authorization": f"Bearer {token}"}
    endpoint = f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=0"

    with client.websocket_connect(
        endpoint, headers=headers, subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
    ) as websocket:
        ready = websocket.receive_json()
        assert ready == {"type": "ready", "protocol": 1, "after_seq": 0, "latest_seq": 3}
        frame = websocket.receive_json()
        assert frame == {
            "type": "event",
            "protocol": 1,
            "task_id": "f" * 32,
            "event": {
                "seq": 1,
                "event_type": "link_task.queued",
                "created_at": "2026-11-25T00:00:00Z",
                "payload": {"state": "CREATED"},
            },
        }
        checkpoint = websocket.receive_json()
        assert checkpoint == {"type": "checkpoint", "protocol": 1, "task_id": "f" * 32, "seq": 3}
        # The caller sends a cumulative ACK only after persisting the cursor.
        ack = {"type": "ack", "protocol": 1, "task_id": "f" * 32, "seq": 3}
        websocket.send_json(ack)
        websocket.send_json(ack)  # duplicate ACK is idempotent
        websocket.close()

    with client.websocket_connect(
        f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=3",
        headers=headers,
        subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
    ) as websocket:
        assert websocket.receive_json() == {"type": "ready", "protocol": 1, "after_seq": 3, "latest_seq": 5}
        replay = websocket.receive_json()
        assert replay["type"] == "event" and replay["task_id"] == "f" * 32
        assert replay["event"]["seq"] == 5
        assert replay["event"]["event_type"] == "link_task.completed"
        websocket.send_json({"type": "ack", "protocol": 1, "task_id": "f" * 32, "seq": 5})
        websocket.close()

    requests = [item for item in upstream if item.url.path.endswith("/events")]
    assert [dict(item.url.params)["after_seq"] for item in requests[:2]] == ["0", "3"]
    assert all(item.headers["authorization"] == f"Bearer {token}" for item in requests[:2])


def test_gateway_wss_resume_before_checkpoint_ack_catches_up_without_loss(gateway):
    client, _, token, _, _, upstream = gateway
    key = "wss-checkpoint-reconnect-0001"
    headers = {"Authorization": f"Bearer {token}"}

    with client.websocket_connect(
        f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=0",
        headers=headers,
        subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
    ) as websocket:
        websocket.receive_json()  # ready
        assert websocket.receive_json()["event"]["seq"] == 1
        assert websocket.receive_json()["seq"] == 3  # checkpoint was not ACKed
        websocket.close()

    with client.websocket_connect(
        f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=1",
        headers=headers,
        subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
    ) as websocket:
        ready = websocket.receive_json()
        assert ready["after_seq"] == 1
        assert ready["latest_seq"] == 5
        event = websocket.receive_json()
        assert event["type"] == "event" and event["event"]["seq"] == 5
        websocket.send_json({"type": "ack", "protocol": 1, "task_id": "f" * 32, "seq": 5})
        websocket.close()

    requests = [item for item in upstream if item.url.path.endswith("/events")]
    assert [dict(item.url.params)["after_seq"] for item in requests[-2:]] == ["0", "1"]


def test_gateway_wss_checkpoint_covers_filtered_rows_and_rejects_backwards_ack(gateway):
    client, _, token, _, _, _ = gateway
    key = "wss-checkpoint-only-request-0001"
    headers = {"Authorization": f"Bearer {token}"}
    with client.websocket_connect(
        f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=0",
        headers=headers,
        subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
    ) as websocket:
        ready = websocket.receive_json()
        assert ready["latest_seq"] == 3
        checkpoint = websocket.receive_json()
        assert checkpoint == {"type": "checkpoint", "protocol": 1, "task_id": "f" * 32, "seq": 3}
        ack = {"type": "ack", "protocol": 1, "task_id": "f" * 32, "seq": 3}
        websocket.send_json(ack)
        websocket.send_json(ack)
        websocket.send_json({"type": "ack", "protocol": 1, "task_id": "f" * 32, "seq": 2})
        with pytest.raises(WebSocketDisconnect) as rejected:
            websocket.receive_json()
    assert rejected.value.code == 1008


def test_gateway_wss_rejects_bad_auth_query_subprotocol_and_ack(gateway):
    client, _, token, _, _, _ = gateway
    key = "wss-protocol-request-0002"

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=0",
            subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
        ) as websocket:
            websocket.receive_json()

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=0&token={token}",
            headers={"Authorization": f"Bearer {token}"},
            subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
        ) as websocket:
            websocket.receive_json()

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=0",
            headers={"Authorization": f"Bearer {token}"},
            subprotocols=["unsupported.v1"],
        ) as websocket:
            websocket.receive_json()

    with client.websocket_connect(
        f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=0",
        headers={"Authorization": f"Bearer {token}"},
        subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
    ) as websocket:
        websocket.receive_json()  # ready
        websocket.send_json({"type": "ack", "protocol": 1, "task_id": "f" * 32, "seq": 999})
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()

    with client.websocket_connect(
        f"/v1/jarvis/link/tasks/{key}/events/ws?after_seq=0",
        headers={"Authorization": f"Bearer {token}"},
        subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
    ) as websocket:
        websocket.receive_json()  # ready
        websocket.send_bytes(b"not-an-ack-frame")
        with pytest.raises(WebSocketDisconnect) as rejected:
            websocket.receive_json()
    assert rejected.value.code == 1008


def test_gateway_wss_preserves_backend_owner_binding(gateway):
    client, _, token, _, _, _ = gateway
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(
            "/v1/jarvis/link/tasks/deviceowner-denied0001/events/ws?after_seq=0",
            headers={"Authorization": f"Bearer {token}"},
            subprotocols=[_TASK_EVENTS_SUBPROTOCOL],
        ) as websocket:
            websocket.receive_json()
    assert denied.value.code == 1008


def test_decision_body_is_bounded_and_allowlisted(gateway):
    client, _, token, _, _, upstream = gateway
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    approval_id = uuid.uuid4().hex
    base = {"decision": "approve", "expected_operation_digest": "a" * 64, "idempotency_key": "request-123"}

    bad = client.post(f"/v1/jarvis/approvals/{approval_id}/decision", headers=headers, json={**base, "tool": "shell_exec"})
    assert bad.status_code == 422
    assert client.post(f"/v1/jarvis/approvals/{approval_id}/decision", headers={"Authorization": headers["Authorization"]}, content=json.dumps(base)).status_code == 415
    ok = client.post(f"/v1/jarvis/approvals/{approval_id}/decision", headers=headers, json=base)
    assert ok.status_code == 200
    assert upstream[-1].url.path.endswith(f"/approvals/{approval_id}/decision")
    assert json.loads(upstream[-1].content) == base


def test_device_migration_bootstrap_hmac_uses_current_token(gateway):
    client, store, token, device_id, _, _ = gateway
    nonce = "N" * 32
    response = client.post(
        "/v1/jarvis/devices/migration-bootstrap",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"nonce": nonce},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["device_id"] == device_id
    assert body["nonce"] == nonce
    assert "access_token" not in body
    public_der = base64.b64decode(body["server_public_key"], validate=True)
    fingerprint = "sha256:" + hashlib.sha256(public_der).hexdigest()
    assert body["server_fingerprint"] == fingerprint
    message = f"openjarvis-migration-bootstrap:v1\n{nonce}\n{device_id}\n{fingerprint}\n{body['server_public_key']}".encode()
    expected = hmac.new(hashlib.sha256(token.encode()).digest(), message, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(body["proof_hmac"], expected)
    assert store.authenticate_device_token(token) == device_id


def test_identity_challenge_signs_only_canonical_https_tailnet_origin(gateway):
    client, _, _, _, _, upstream = gateway
    nonce = "migrationNonce_123456789"
    body = {"nonce": nonce, "target_origin": "https://mac.tailnet.ts.net"}
    response = client.post("/v1/jarvis/identity/challenge", headers={"Host": "mac.tailnet.ts.net"}, json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["nonce"] == nonce
    assert payload["target_origin"] == body["target_origin"]
    assert len(upstream) == 0  # Challenge is served locally, not proxied.

    signature = base64.b64decode(payload.pop("signature"), validate=True)
    public_der = base64.b64decode(payload["server_public_key"], validate=True)
    public_key = serialization.load_der_public_key(public_der)
    assert payload["server_fingerprint"] == "sha256:" + hashlib.sha256(public_der).hexdigest()
    public_key.verify(signature, _canonical_pairing_payload(payload), ec.ECDSA(hashes.SHA256()))

    for bad_origin in (
        "http://mac.tailnet.ts.net",
        "https://mac.example",
        "https://mac.tailnet.ts.net:8443",
        "https://mac.tailnet.ts.net/path",
    ):
        bad = client.post("/v1/jarvis/identity/challenge", headers={"Host": "mac.tailnet.ts.net"}, json={"nonce": nonce, "target_origin": bad_origin})
        assert bad.status_code == 422, bad_origin

    other_tailnet = client.post(
        "/v1/jarvis/identity/challenge",
        headers={"Host": "mac.tailnet.ts.net"},
        json={"nonce": nonce, "target_origin": "https://attacker.ts.net"},
    )
    assert other_tailnet.status_code == 403

    wrong_host = client.post(
        "/v1/jarvis/identity/challenge",
        headers={"Host": "attacker.ts.net"},
        json=body,
    )
    assert wrong_host.status_code == 403


def test_identity_challenge_fails_closed_without_configured_origin(gateway):
    _, store, _, _, _, _ = gateway
    app = create_gateway_app(supervisor_store=store, expected_identity_origin="")
    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        response = client.post(
            "/v1/jarvis/identity/challenge",
            headers={"Host": "attacker.ts.net"},
            json={"nonce": "migrationNonce_123456789", "target_origin": "https://attacker.ts.net"},
        )
    assert response.status_code == 503


def test_gateway_body_and_bind_restrictions(gateway):
    client, _, token, _, _, _ = gateway
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post(
        "/v1/jarvis/devices/migration-bootstrap",
        headers={**auth, "Content-Type": "application/json"},
        json={"nonce": "short"},
    ).status_code == 422
    assert client.post(
        "/v1/jarvis/devices/migration-bootstrap",
        headers={**auth, "Content-Type": "application/json"},
        json={"nonce": "N" * 32, "secret": "unexpected"},
    ).status_code == 422
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("::1") == "::1"
    with pytest.raises(ValueError):
        validate_bind_host("0.0.0.0")


def test_gateway_auth_store_does_not_run_task_restart_recovery(tmp_path):
    db_path = tmp_path / "supervisor.db"
    store = SupervisorStore(str(db_path))
    task_id = store.create_task("gateway must not recover other process work")
    store.transition(task_id, "RUNNING")
    store.close()

    # Constructing/authenticating through the gateway's read-only store does
    # not instantiate SupervisorStore or pause the task as a restart would.
    view = ReadOnlyGatewayStore(db_path)
    assert view.authenticate_device_token("no-such-token") is None
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        assert db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "RUNNING"
