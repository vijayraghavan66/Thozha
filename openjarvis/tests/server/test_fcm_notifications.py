"""FCM token storage, optional delivery, and device-authenticated API tests."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openjarvis.server.fcm_notifications as fcm
import openjarvis.server.supervisor_routes as supervisor_routes
import openjarvis.tasks.supervisor as supervisor
from openjarvis.server.fcm_notifications import FCMNotificationSender, FCMRegistrationStore
from openjarvis.server.supervisor_routes import router
from openjarvis.tasks.supervisor import SupervisorStore


def _add_device(store: SupervisorStore, token: str) -> str:
    device_id = "d" * 32
    now = datetime.now(timezone.utc)
    store._db.execute(
        "INSERT INTO paired_devices(id,public_key_b64,token_hash,created_at,expires_at,device_name) VALUES(?,?,?,?,?,?)",
        (device_id, "public-key-not-used-by-fcm", hashlib.sha256(token.encode()).hexdigest(), now.isoformat(), (now + timedelta(days=30)).isoformat(), "FCM test phone"),
    )
    store._db.commit()
    return device_id


@pytest.fixture
def fcm_api(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    token = "ojd_device_test_token_for_fcm"
    device_id = _add_device(store, token)
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "test-admin-key")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, store, token, device_id
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def test_registration_requires_device_auth_refreshes_and_revokes(fcm_api, tmp_path):
    client, store, device_token, device_id = fcm_api
    first = "fcm-registration-token-0123456789-first"
    refreshed = "fcm-registration-token-0123456789-refreshed"
    endpoint = "/v1/jarvis/devices/fcm-token"
    assert client.post(endpoint, json={"token": first}).status_code == 401
    assert client.post(endpoint, headers={"Authorization": "Bearer test-admin-key"}, json={"token": first}).status_code == 403

    headers = {"Authorization": f"Bearer {device_token}"}
    registered = client.post(endpoint, headers=headers, json={"token": first})
    assert registered.status_code == 200
    assert registered.json() == {"status": "registered"}
    assert first not in registered.text

    store_tokens = FCMRegistrationStore(str(store.db_path))
    assert store_tokens.registration_token(device_id) == first
    row = store_tokens._db.execute("SELECT token_ciphertext,nonce,revoked_at FROM companion_push_tokens WHERE device_id=?", (device_id,)).fetchone()
    assert first.encode() not in bytes(row["token_ciphertext"])
    assert stat.S_IMODE(store_tokens.key_path.stat().st_mode) == 0o600

    assert client.post(endpoint, headers=headers, json={"token": refreshed}).status_code == 200
    assert store_tokens.registration_token(device_id) == refreshed
    assert client.delete(endpoint, headers=headers).json() == {"status": "revoked"}
    assert store_tokens.registration_token(device_id) is None
    store_tokens.close()


def test_sender_missing_credentials_fails_closed_without_network(tmp_path):
    db = SupervisorStore(str(tmp_path / "supervisor.db"))
    device_id = _add_device(db, "ojd_token_for_missing_credentials")
    tokens = FCMRegistrationStore(str(db.db_path))
    tokens.register(device_id, "fcm-registration-token-0123456789-missing")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"name": "should-not-be-called"})

    sender = FCMNotificationSender(
        tokens,
        credentials_file=tmp_path / "missing-service-account.json",
        transport=httpx.MockTransport(handler),
        access_token_provider=lambda _: (_ for _ in ()).throw(AssertionError("credentials provider must not run")),
    )
    result = sender.send_approval_pending(device_id, "a" * 32)
    assert result.status == "disabled"
    assert requests == []
    db.close()
    tokens.close()


def test_sender_payload_is_minimal_and_deduplicated(tmp_path):
    db = SupervisorStore(str(tmp_path / "supervisor.db"))
    device_id = _add_device(db, "ojd_token_for_mock_fcm_sender")
    tokens = FCMRegistrationStore(str(db.db_path))
    tokens.register(device_id, "fcm-registration-token-0123456789-payload")
    credentials = tmp_path / "firebase-service-account.json"
    credentials.write_text(json.dumps({"project_id": "test-project"}), encoding="utf-8")
    sent_requests = []

    def handler(request):
        sent_requests.append(request)
        return httpx.Response(200, json={"name": "fake-message-id"})

    sender = FCMNotificationSender(
        tokens,
        credentials_file=credentials,
        transport=httpx.MockTransport(handler),
        access_token_provider=lambda _: "mock-oauth-access-token",
    )
    approval_id = "a" * 32
    assert sender.send_approval_pending(device_id, approval_id).status == "sent"
    assert sender.send_approval_pending(device_id, approval_id).status == "duplicate"
    assert len(sent_requests) == 1
    body = json.loads(sent_requests[0].content)
    assert set(body) == {"message"}
    assert set(body["message"]) == {"token", "data"}
    assert body["message"]["data"] == {"approval_id": approval_id, "type": "approval_pending"}
    assert "command" not in sent_requests[0].content.decode()
    assert "arguments" not in sent_requests[0].content.decode()
    db.close()
    tokens.close()


def test_sender_retries_transient_failed_approval_delivery(tmp_path):
    db = SupervisorStore(str(tmp_path / "supervisor.db"))
    device_id = _add_device(db, "ojd_token_for_fcm_retry")
    tokens = FCMRegistrationStore(str(db.db_path))
    tokens.register(device_id, "fcm-registration-token-0123456789-retry")
    credentials = tmp_path / "firebase-service-account.json"
    credentials.write_text(json.dumps({"project_id": "test-project"}), encoding="utf-8")
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(503 if len(sent) == 1 else 200, json={})

    sender = FCMNotificationSender(
        tokens,
        credentials_file=credentials,
        transport=httpx.MockTransport(handler),
        access_token_provider=lambda _: "mock-oauth-access-token",
    )
    approval_id = "b" * 32
    assert sender.send_approval_pending(device_id, approval_id).status == "failed"
    assert sender.send_approval_pending(device_id, approval_id).status == "sent"
    assert len(sent) == 2
    db.close()
    tokens.close()


def test_sender_uses_application_default_credentials_when_configured(tmp_path, monkeypatch):
    db = SupervisorStore(str(tmp_path / "supervisor.db"))
    device_id = _add_device(db, "ojd_token_for_adc_fcm_sender")
    tokens = FCMRegistrationStore(str(db.db_path))
    tokens.register(device_id, "fcm-registration-token-0123456789-adc")
    adc = tmp_path / "application_default_credentials.json"
    adc.write_text(json.dumps({"type": "authorized_user", "project_id": "firebase-test-project"}), encoding="utf-8")

    class Credentials:
        token = None

        def refresh(self, _request):
            self.token = "adc-oauth-token"

    credentials = Credentials()
    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (credentials, "firebase-test-project"))
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"name": "adc-message"})

    sender = FCMNotificationSender(tokens, credentials_file=adc, transport=httpx.MockTransport(handler))
    result = sender.send_approval_pending(device_id, "c" * 32)
    assert result.status == "sent"
    assert sent[0].url.path.endswith("/projects/firebase-test-project/messages:send")
    assert sent[0].headers["authorization"] == "Bearer adc-oauth-token"
    db.close()
    tokens.close()


def test_new_approval_auto_dispatches_only_when_credentials_exist(tmp_path, monkeypatch):
    db_path = str(tmp_path / "supervisor.db")
    store = SupervisorStore(db_path)
    task_id = store.create_task("approve a harmless task")
    store.transition(task_id, "RUNNING")
    event = threading.Event()
    dispatched = []
    monkeypatch.setattr(fcm, "service_account_path", lambda: Path("/test/fcm-service-account.json"))

    def fake_dispatch(path: str, approval_id: str) -> None:
        dispatched.append((path, approval_id))
        event.set()

    monkeypatch.setattr(fcm, "dispatch_pending_approval", fake_dispatch)
    approval_id, _ = store.create_approval(
        task_id,
        "shell_exec",
        "/tmp",
        {"command": "echo harmless"},
        "explicit approval test",
    )
    assert event.wait(2)
    assert dispatched == [(db_path, approval_id)]
    store.close()


def test_new_approval_does_not_dispatch_without_credentials(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    task_id = store.create_task("no fcm credentials")
    store.transition(task_id, "RUNNING")
    monkeypatch.setattr(fcm, "service_account_path", lambda: None)
    monkeypatch.setattr(fcm, "dispatch_pending_approval", lambda *_: pytest.fail("must not dispatch"))
    store.create_approval(task_id, "shell_exec", "/tmp", {"command": "echo safe"}, "test")
    store.close()
