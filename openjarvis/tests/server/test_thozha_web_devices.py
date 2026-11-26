"""Browser device management remains limited to explicitly mapped companions."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from openjarvis.server.thozha_web_devices import create_web_devices_router
from openjarvis.tasks.supervisor import SupervisorStore

OWNER_DEVICE = "a" * 32
OTHER_DEVICE = "b" * 32
UNMAPPED_DEVICE = "c" * 32


def _active_device(store: SupervisorStore, device_id: str, name: str, *, days=10):
    now = datetime.now(timezone.utc)
    token_hash = hashlib.sha256(("fixture-token-" + device_id).encode()).hexdigest()
    store._db.execute(
        "INSERT INTO paired_devices("
        "id,public_key_b64,token_hash,created_at,expires_at,device_name) "
        "VALUES(?,?,?,?,?,?)",
        (
            device_id,
            "synthetic-public-key-must-not-be-returned",
            token_hash,
            now.isoformat(),
            (now + timedelta(days=days)).isoformat(),
            name,
        ),
    )
    store._db.commit()


def _client(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    _active_device(store, OWNER_DEVICE, "Owner phone")
    _active_device(store, OTHER_DEVICE, "Other phone")
    expired_id = "d" * 32
    _active_device(store, expired_id, "Expired phone", days=-1)
    configured = {"alice": {OWNER_DEVICE, expired_id}, "bob": {OTHER_DEVICE}}

    def resolve(principal, _store):
        user_id = (
            principal["user_id"] if isinstance(principal, dict) else principal.user_id
        )
        return set(configured.get(user_id, set()))

    def active(user_id, device_id, db):
        return device_id in _active_device_ids(db)

    def mapped(principal, device_id, _store):
        user_id = (
            principal["user_id"] if isinstance(principal, dict) else principal.user_id
        )
        return device_id in configured.get(user_id, set())

    def require_session(request: Request):
        user_id = request.headers.get("x-test-user")
        if user_id not in configured:
            raise HTTPException(status_code=401, detail="Web session required")
        return {
            "user_id": user_id,
            "session_id": "test-session",
            "expires_at": int(datetime.now(timezone.utc).timestamp()) + 600,
        }

    app = FastAPI()
    app.include_router(
        create_web_devices_router(
            require_session,
            resolve,
            active,
            mapped,
            store_provider=lambda _request: store,
        )
    )
    return TestClient(app), store, expired_id


def _active_device_ids(store: SupervisorStore) -> set[str]:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True) as db:
        return {
            row[0]
            for row in db.execute(
                "SELECT id FROM paired_devices "
                "WHERE revoked_at IS NULL AND expires_at>?",
                (now,),
            )
        }


def test_list_and_get_return_only_mapped_safe_active_metadata(tmp_path):
    client, store, expired_id = _client(tmp_path)
    headers = {"x-test-user": "alice"}
    response = client.get("/v1/web/devices", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    devices = response.json()["devices"]
    assert [item["device_id"] for item in devices] == [OWNER_DEVICE]
    assert response.json()["count"] == 1
    assert set(devices[0]) == {
        "device_id",
        "device_name",
        "created_at",
        "expires_at",
        "status",
    }
    assert devices[0]["status"] == "active"
    assert "token_hash" not in response.text
    assert "synthetic-public-key" not in response.text

    assert (
        client.get(f"/v1/web/devices/{OWNER_DEVICE}", headers=headers).status_code
        == 200
    )
    assert (
        client.get(f"/v1/web/devices/{OTHER_DEVICE}", headers=headers).status_code
        == 404
    )
    assert (
        client.get(f"/v1/web/devices/{UNMAPPED_DEVICE}", headers=headers).status_code
        == 404
    )
    assert (
        client.get(f"/v1/web/devices/{expired_id}", headers=headers).status_code == 404
    )
    store.close()


def test_owner_isolation_and_session_requirement(tmp_path):
    client, store, _ = _client(tmp_path)
    assert client.get("/v1/web/devices").status_code == 401
    alice_list = client.get("/v1/web/devices", headers={"x-test-user": "alice"}).json()[
        "devices"
    ]
    bob_list = client.get("/v1/web/devices", headers={"x-test-user": "bob"}).json()[
        "devices"
    ]
    assert {item["device_id"] for item in alice_list} == {OWNER_DEVICE}
    assert {item["device_id"] for item in bob_list} == {OTHER_DEVICE}
    assert (
        client.get("/v1/web/devices", headers={"x-test-user": "mallory"}).status_code
        == 401
    )
    store.close()


def test_revoke_requires_explicit_confirmation_and_preserves_record(tmp_path):
    client, store, _ = _client(tmp_path)
    path = f"/v1/web/devices/{OWNER_DEVICE}/revoke"
    headers = {"x-test-user": "alice"}
    assert client.post(path, headers=headers, json={}).status_code == 422
    assert (
        client.post(path, headers=headers, json={"confirm": False}).status_code == 422
    )
    assert (
        client.post(
            path, headers=headers, json={"confirm": True, "device_id": OTHER_DEVICE}
        ).status_code
        == 422
    )

    revoked = client.post(path, headers=headers, json={"confirm": True})
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["device_id"] == OWNER_DEVICE
    row = store._db.execute(
        "SELECT revoked_at FROM paired_devices WHERE id=?", (OWNER_DEVICE,)
    ).fetchone()
    assert row["revoked_at"] is not None
    # Pairing/audit row is retained; a replay cannot perform a second mutation.
    assert (
        store._db.execute(
            "SELECT COUNT(*) FROM paired_devices WHERE id=?", (OWNER_DEVICE,)
        ).fetchone()[0]
        == 1
    )
    replay = client.post(path, headers=headers, json={"confirm": True})
    assert replay.status_code == 200
    assert replay.json()["status"] == "already_revoked"
    store.close()


def test_unmapped_device_cannot_be_revoked(tmp_path):
    client, store, _ = _client(tmp_path)
    response = client.post(
        f"/v1/web/devices/{OTHER_DEVICE}/revoke",
        headers={"x-test-user": "alice"},
        json={"confirm": True},
    )
    assert response.status_code == 404
    assert (
        store._db.execute(
            "SELECT revoked_at FROM paired_devices WHERE id=?", (OTHER_DEVICE,)
        ).fetchone()["revoked_at"]
        is None
    )
    store.close()
