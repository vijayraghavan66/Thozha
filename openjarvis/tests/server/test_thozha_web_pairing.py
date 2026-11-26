"""Authenticated Web-owner QR setup uses the same signed Mac/Android protocol."""

from __future__ import annotations

import base64
import hashlib
import sqlite3

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import openjarvis.server.supervisor_routes as supervisor_routes
import openjarvis.tasks.supervisor as supervisor_module
from openjarvis.server.companion_gateway import create_gateway_app
from openjarvis.server.supervisor_routes import _canonical_pairing_payload, router
from openjarvis.server.thozha_web_devices import create_web_devices_router
from openjarvis.server.thozha_web_supervisor import (
    is_web_device_active,
    is_web_device_mapped,
    resolve_web_device_ids,
)
from openjarvis.tasks.supervisor import SupervisorStore


OWNER = "web-owner-test"
OTHER_OWNER = "another-web-owner"
OWNER_PASSWORD = "isolated-web-owner-password"
ORIGIN = "https://thozha.test"


def _pairing_app(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "web-pairing.db"), run_recovery=False)
    monkeypatch.setattr(supervisor_module, "_STORE_INSTANCE", store)
    monkeypatch.setenv("THOZHA_WEB_OWNER_ID", OWNER)
    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", "")
    app = FastAPI()
    from openjarvis.server.thozha_web_auth import (
        WebAuthConfig,
        WebSessionStore,
        hash_password,
        install_web_auth,
        require_web_session,
    )

    session_store = WebSessionStore(tmp_path / "web-sessions.db")
    web_auth = install_web_auth(
        app,
        config=WebAuthConfig(
            owner_id=OWNER,
            password_hash=hash_password(
                OWNER_PASSWORD,
                salt=b"isolated-pairing-test-salt",
                iterations=100_000,
            ),
            origin=ORIGIN,
            database_path=tmp_path / "web-sessions.db",
        ),
        store=session_store,
    )
    app.state.supervisor_store = store
    app.include_router(
        create_web_devices_router(
            require_web_session,
            resolve_web_device_ids,
            is_web_device_active,
            is_web_device_mapped,
            store_provider=lambda _request: store,
        )
    )
    app.include_router(router)
    monkeypatch.setattr(
        supervisor_routes,
        "_local_wifi_pairing_parameters",
        lambda _store: {
            "server_url": "https://thozha-test.local:8444",
            "loopback_url": "http://127.0.0.1:8000",
            "gateway_address": "192.168.1.44",
            "gateway_port": "8444",
            "gateway_certificate_sha256": "sha256:" + "1" * 64,
        },
    )
    client = TestClient(app, base_url=ORIGIN)
    return app, client, store, web_auth


def _login_owner(client):
    login = client.post(
        "/v1/web/session/login",
        headers={"Origin": ORIGIN},
        json={"username": OWNER, "password": OWNER_PASSWORD},
    )
    assert login.status_code == 200
    return {"Origin": ORIGIN, "X-Thozha-CSRF": login.json()["csrf_token"]}


def _new_device_proof(challenge: str):
    key = ec.generate_private_key(ec.SECP256R1())
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signature = key.sign(challenge.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return key, base64.b64encode(public_der).decode("ascii"), base64.b64encode(signature).decode("ascii")


def test_supervisor_upgrade_adds_owner_mapping_state_without_changing_pairing_rows(tmp_path):
    db_path = tmp_path / "older-supervisor.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE device_pairings(id TEXT PRIMARY KEY,challenge TEXT NOT NULL,"
            "created_at TEXT NOT NULL,expires_at TEXT NOT NULL,used_at TEXT)"
        )
        db.execute(
            "INSERT INTO device_pairings(id,challenge,created_at,expires_at) VALUES(?,?,?,?)",
            ("a" * 32, "opaque-challenge", "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
        )
    store = SupervisorStore(str(db_path), run_recovery=False)
    try:
        columns = {row["name"] for row in store._db.execute("PRAGMA table_info(device_pairings)")}
        assert "web_owner_id" in columns
        row = store._db.execute(
            "SELECT id,status,challenge,web_owner_id FROM device_pairings WHERE id=?",
            ("a" * 32,),
        ).fetchone()
        assert tuple(row) == ("a" * 32, "INVITED", "opaque-challenge", None)
        assert store.web_device_mapping_ids(OWNER) == set()
    finally:
        store.close()


def _gateway_for_app(app, store):
    async def forward(request: httpx.Request) -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
        ) as backend:
            return await backend.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                content=request.content,
                headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() in {"content-type", "accept"}
                },
            )

    return create_gateway_app(
        supervisor_store=store,
        backend_transport=httpx.MockTransport(forward),
    )


def test_web_owner_creates_wifi_qr_and_android_pairs_through_real_gateway_routes(tmp_path, monkeypatch):
    app, web, store, web_auth = _pairing_app(tmp_path, monkeypatch)
    headers = _login_owner(web)

    assert web.post(
        "/v1/web/devices/pairing-invitations",
        headers={"Origin": ORIGIN},
        json={"transport": "wifi"},
    ).status_code == 403
    other_login = web.post(
        "/v1/web/session/login",
        headers={"Origin": ORIGIN},
        json={"username": OTHER_OWNER, "password": OWNER_PASSWORD},
    )
    assert other_login.status_code == 401
    assert web.post(
        "/v1/web/devices/pairing-invitations",
        headers={**headers, "Origin": "https://attacker.example"},
        json={"transport": "wifi"},
    ).status_code == 403
    unsupported = web.post(
        "/v1/web/devices/pairing-invitations",
        headers=headers,
        json={"transport": "loopback"},
    )
    assert unsupported.status_code == 422
    assert store._db.execute("SELECT COUNT(*) FROM device_pairings").fetchone()[0] == 0

    response = web.post(
        "/v1/web/devices/pairing-invitations",
        headers=headers,
        json={"transport": "wifi"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    qr = response.json()["qr_payload"]
    assert qr["protocol"] == "openjarvis-pairing-v2"
    assert qr["server_url"] == "https://thozha-test.local:8444"
    assert qr["gateway_address"] == "192.168.1.44"
    assert qr["gateway_certificate_sha256"] == "sha256:" + "1" * 64
    server_der = base64.b64decode(qr["server_public_key"], validate=True)
    server_key = serialization.load_der_public_key(server_der)
    assert qr["server_fingerprint"] == "sha256:" + hashlib.sha256(server_der).hexdigest()
    signed_payload = dict(qr)
    server_signature = base64.b64decode(signed_payload.pop("server_signature"), validate=True)
    server_key.verify(
        server_signature,
        _canonical_pairing_payload(signed_payload),
        ec.ECDSA(hashes.SHA256()),
    )
    invitation_id = qr["invitation_id"]

    gateway = TestClient(_gateway_for_app(app, store), base_url="http://127.0.0.1:8001")
    phone_key, phone_public, phone_signature = _new_device_proof(qr["challenge"])
    complete = gateway.post(
        f"/v1/jarvis/pair/{invitation_id}/complete",
        json={
            "device_name": "Isolated Web-paired Android",
            "public_key": phone_public,
            "signature": phone_signature,
        },
    )
    assert complete.status_code == 200
    assert complete.json()["status"] == "PENDING_CONFIRMATION"
    assert "access_token" not in complete.json()

    pending = web.get("/v1/web/devices/pairing-requests")
    assert pending.status_code == 200
    assert pending.json()["count"] == 1
    assert pending.json()["requests"][0]["id"] == invitation_id
    assert pending.json()["requests"][0]["device_name"] == "Isolated Web-paired Android"
    assert "public_key" not in pending.text and "access_token" not in pending.text

    decision = web.post(
        f"/v1/web/devices/pairing-requests/{invitation_id}/decision",
        headers=headers,
        json={"decision": "approve"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "APPROVED"
    assert store.web_device_mapping_ids(OWNER) == set()
    web_status = web.get(
        f"/v1/web/devices/pairing-invitations/{invitation_id}",
        headers={"Origin": ORIGIN},
    )
    assert web_status.status_code == 200
    assert web_status.json() == {"invitation_id": invitation_id, "status": "APPROVED"}
    assert "claim_challenge" not in web_status.text

    phone_status = gateway.get(f"/v1/jarvis/pair/{invitation_id}/status")
    assert phone_status.status_code == 200
    assert phone_status.json()["status"] == "APPROVED"
    challenge = phone_status.json()["claim_challenge"]
    claim_signature = base64.b64encode(
        phone_key.sign(challenge.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    ).decode("ascii")
    claimed = gateway.post(
        f"/v1/jarvis/pair/{invitation_id}/claim",
        json={"signature": claim_signature},
    )
    assert claimed.status_code == 200
    token = claimed.json()["access_token"]
    device_id = claimed.json()["device_id"]
    assert store.authenticate_device_token(token) == device_id
    assert store.web_device_mapping_ids(OWNER) == {device_id}
    owner_devices = web.get("/v1/web/devices", headers=headers)
    assert owner_devices.status_code == 200
    assert [item["device_id"] for item in owner_devices.json()["devices"]] == [device_id]
    try:
        resolve_web_device_ids({"user_id": OTHER_OWNER}, store, environ={
            "THOZHA_WEB_OWNER_ID": OWNER,
            "THOZHA_WEB_ALLOWED_DEVICE_IDS": "",
        })
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("a different Web owner must not resolve this paired device")

    # Web status remains safe after the phone consumes the one-time claim.
    terminal = web.get(
        f"/v1/web/devices/pairing-invitations/{invitation_id}",
        headers={"Origin": ORIGIN},
    )
    assert terminal.json() == {"invitation_id": invitation_id, "status": "TOKEN_ISSUED"}
    assert "access_token" not in terminal.text and "claim_challenge" not in terminal.text
    store.close()
    web_auth.store.close()


def test_web_pairing_owner_can_cancel_and_reject_without_claiming(tmp_path, monkeypatch):
    _app, client, store, web_auth = _pairing_app(tmp_path, monkeypatch)
    headers = _login_owner(client)
    cancel_invitation = client.post(
        "/v1/web/devices/pairing-invitations",
        headers=headers,
        json={"transport": "wifi"},
    ).json()["qr_payload"]
    cancelled = client.post(
        f"/v1/web/devices/pairing-invitations/{cancel_invitation['invitation_id']}/cancel",
        headers=headers,
        json={},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"

    rejected_invitation = client.post(
        "/v1/web/devices/pairing-invitations",
        headers=headers,
        json={"transport": "wifi"},
    ).json()["qr_payload"]
    _phone_key, public_key, signature = _new_device_proof(rejected_invitation["challenge"])
    pending = client.post("/v1/jarvis/pair/complete", json={
        "invitation_id": rejected_invitation["invitation_id"],
        "device_name": "Reject-path test phone",
        "public_key": public_key,
        "signature": signature,
    })
    assert pending.status_code == 200
    rejected = client.post(
        f"/v1/web/devices/pairing-requests/{rejected_invitation['invitation_id']}/decision",
        headers=headers,
        json={"decision": "reject"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert client.get(
        f"/v1/web/devices/pairing-invitations/{rejected_invitation['invitation_id']}",
        headers={"Origin": ORIGIN},
    ).json()["status"] == "REJECTED"
    assert store._db.execute("SELECT COUNT(*) FROM paired_devices").fetchone()[0] == 0
    assert store.web_device_mapping_ids(OWNER) == set()
    store.close()
    web_auth.store.close()
