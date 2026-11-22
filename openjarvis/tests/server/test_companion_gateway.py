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
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from openjarvis.server.companion_gateway import ReadOnlyGatewayStore, create_gateway_app, validate_bind_host
from openjarvis.server.supervisor_routes import _canonical_pairing_payload
from openjarvis.tasks.supervisor import SupervisorStore


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


def test_gateway_allows_only_read_only_link_task_envelopes(gateway):
    client, _, token, _, _, upstream = gateway
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "objective": "Explain Java in one sentence",
        "capability_scope": ["read_only"],
        "idempotency_key": "jarvis-link-task-20261116-01",
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
        json={**body, "command": "rm -rf /"},
    ).status_code == 422
    assert client.get("/v1/jarvis/link/tasks/invalid", headers=headers).status_code == 404
    assert client.get(
        "/v1/jarvis/link/tasks/" + body["idempotency_key"], headers=headers,
    ).status_code == 200
    assert upstream[-1].url.path == "/v1/jarvis/link/tasks/" + body["idempotency_key"]
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
