"""Loopback-only paired-device listing and revocation tests."""
from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openjarvis.tasks.supervisor as supervisor_module
from openjarvis.server import supervisor_routes
from openjarvis.server.supervisor_routes import router
from openjarvis.tasks.supervisor import SupervisorStore


class FakeFCMRegistrationStore:
    calls: list[tuple[str, str]] = []
    failure = False

    def __init__(self, _db_path: str):
        self.closed = False

    def revoke(self, device_id: str) -> None:
        self.calls.append(("revoke", device_id))
        if self.failure:
            raise RuntimeError("synthetic cleanup failure")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def local_devices(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    monkeypatch.setattr(supervisor_module, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "test-local-device-admin")
    monkeypatch.setattr(supervisor_routes, "FCMRegistrationStore", FakeFCMRegistrationStore)
    FakeFCMRegistrationStore.calls = []
    FakeFCMRegistrationStore.failure = False
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 32100)) as client:
        yield client, store
    store.close()
    monkeypatch.setattr(supervisor_module, "_STORE_INSTANCE", None)


def _add_device(store: SupervisorStore, *, device_id: str | None = None, name: str = "Phone", status: str = "active"):
    device_id = device_id or uuid.uuid4().hex
    key = ec.generate_private_key(ec.SECP256R1())
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    token = f"test-token-{device_id}"
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=30)).isoformat()
    revoked_at = None
    if status == "expired":
        expires = (now - timedelta(seconds=1)).isoformat()
    elif status == "revoked":
        revoked_at = (now - timedelta(seconds=1)).isoformat()
    store._db.execute(
        "INSERT INTO paired_devices(id,public_key_b64,token_hash,created_at,expires_at,revoked_at,device_name) "
        "VALUES(?,?,?,?,?,?,?)",
        (device_id, base64.b64encode(public_der).decode("ascii"), token_hash,
         (now - timedelta(days=1)).isoformat(), expires, revoked_at, name),
    )
    store._db.commit()
    return device_id, token, public_der


def test_list_is_loopback_origin_guarded_and_redacts_credentials(local_devices):
    client, store = local_devices
    active_id, active_token, active_public = _add_device(store, name="  Samsung\n Phone\x00 ")
    _add_device(store, name="Expired", status="expired")
    _add_device(store, name="Revoked", status="revoked")

    response = client.get("/v1/jarvis/pair/devices", headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["count"] == 3
    active = next(device for device in payload["devices"] if device["device_id"] == active_id)
    assert set(active) == {
        "device_id", "device_name", "public_key_fingerprint", "created_at", "expires_at", "revoked_at", "status",
    }
    assert active["device_name"] == "Samsung Phone"
    assert active["status"] == "active"
    assert active["public_key_fingerprint"] == "SHA256:" + hashlib.sha256(active_public).hexdigest()
    statuses = {device["device_name"]: device["status"] for device in payload["devices"]}
    assert statuses == {"Samsung Phone": "active", "Expired": "expired", "Revoked": "revoked"}

    serialized = response.text
    token_hash = hashlib.sha256(active_token.encode("utf-8")).hexdigest()
    public_b64 = base64.b64encode(active_public).decode("ascii")
    for forbidden in (active_token, token_hash, public_b64, "token_hash", "public_key_b64", "fcm_token"):
        assert forbidden not in serialized

    assert client.get("/v1/jarvis/pair/devices").status_code == 403
    assert client.get(
        "/v1/jarvis/pair/devices", headers={"Origin": "https://attacker.example"},
    ).status_code == 403
    remote = TestClient(app=client.app, base_url="http://192.168.1.10:8000", client=("192.168.1.10", 32100))
    assert remote.get("/v1/jarvis/pair/devices", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 403


def test_local_revoke_requires_exact_confirmation_revokes_token_and_fcm(local_devices):
    client, store = local_devices
    device_id, token, _ = _add_device(store)
    url = f"/v1/jarvis/pair/devices/{device_id}/revoke"
    headers = {"Origin": "http://127.0.0.1:8000", "Content-Type": "application/json"}

    assert client.post(url, headers=headers, json={"confirm": False}).status_code == 422
    assert client.post(url, headers=headers, json={}).status_code == 422
    assert client.post(url, headers=headers, json={"confirm": True, "device_id": device_id}).status_code == 422
    assert store.authenticate_device_token(token) == device_id
    assert FakeFCMRegistrationStore.calls == []
    oversized = client.post(url, headers=headers, content=b"{" + b" " * 257 + b"}")
    assert oversized.status_code == 413

    revoked = client.post(url, headers=headers, json={"confirm": True})
    assert revoked.status_code == 200
    assert revoked.json() == {"status": "revoked", "device_id": device_id}
    assert store.authenticate_device_token(token) is None
    assert FakeFCMRegistrationStore.calls == [("revoke", device_id)]
    assert "token" not in revoked.text
    assert "fcm" not in revoked.text.lower()

    # Idempotent retry still lets FCM cleanup be retried if a prior cleanup failed.
    retried = client.post(url, headers=headers, json={"confirm": True})
    assert retried.status_code == 200
    assert FakeFCMRegistrationStore.calls == [("revoke", device_id), ("revoke", device_id)]
    assert client.get("/v1/jarvis/pair/devices", headers={"Origin": "http://127.0.0.1:8000"}).json()["devices"][0]["status"] == "revoked"


def test_local_revoke_enforces_ui_origin_and_valid_identifier(local_devices):
    client, store = local_devices
    device_id, _, _ = _add_device(store)
    url = f"/v1/jarvis/pair/devices/{device_id}/revoke"
    assert client.post(url, json={"confirm": True}).status_code == 403
    assert client.post(
        url, headers={"Origin": "https://attacker.example"}, json={"confirm": True},
    ).status_code == 403
    remote = TestClient(app=client.app, base_url="http://192.168.1.10:8000", client=("192.168.1.10", 32100))
    assert remote.post(
        url, headers={"Origin": "http://127.0.0.1:8000"}, json={"confirm": True},
    ).status_code == 403
    assert client.post(
        "/v1/jarvis/pair/devices/not-a-device/revoke",
        headers={"Origin": "http://127.0.0.1:8000"}, json={"confirm": True},
    ).status_code == 404
    assert store.authenticate_device_token(_add_device(store)[1]) is not None


def test_existing_master_only_device_revoke_route_remains_available(local_devices):
    client, store = local_devices
    device_id, token, _ = _add_device(store)
    url = f"/v1/jarvis/devices/{device_id}/revoke"
    assert client.post(url).status_code == 401
    existing = client.post(url, headers={"Authorization": "Bearer test-local-device-admin"})
    assert existing.status_code == 200
    assert store.authenticate_device_token(token) is None
    assert FakeFCMRegistrationStore.calls[-1] == ("revoke", device_id)


def test_fcm_cleanup_failure_is_retryable_without_restoring_device_access(local_devices):
    client, store = local_devices
    device_id, token, _ = _add_device(store)
    FakeFCMRegistrationStore.failure = True
    url = f"/v1/jarvis/pair/devices/{device_id}/revoke"
    headers = {"Origin": "http://127.0.0.1:8000", "Content-Type": "application/json"}
    failed_cleanup = client.post(url, headers=headers, json={"confirm": True})
    assert failed_cleanup.status_code == 503
    assert store.authenticate_device_token(token) is None
    assert token not in failed_cleanup.text

    FakeFCMRegistrationStore.failure = False
    retried = client.post(url, headers=headers, json={"confirm": True})
    assert retried.status_code == 200
    assert store.authenticate_device_token(token) is None
