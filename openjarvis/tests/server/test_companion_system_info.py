"""Device-token-only fixed read-only Mac system information endpoint tests."""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openjarvis.tasks.supervisor as supervisor
import openjarvis.server.supervisor_routes as routes
from openjarvis.server.supervisor_routes import router
from openjarvis.tasks.supervisor import SupervisorStore


@pytest.fixture
def system_info_client(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    token = "ojd_device_readonly_system_info_test"
    device_id = "b" * 32
    now = datetime.now(timezone.utc)
    store._db.execute(
        "INSERT INTO paired_devices(id,public_key_b64,token_hash,created_at,expires_at,device_name) "
        "VALUES(?,?,?,?,?,?)",
        (
            device_id,
            "fixture-public-key",
            hashlib.sha256(token.encode()).hexdigest(),
            now.isoformat(),
            (now + timedelta(days=1)).isoformat(),
            "fixture Samsung",
        ),
    )
    store._db.commit()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "system-info-test-admin-key")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        yield client, store, token
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def test_system_info_is_limited_to_authenticated_paired_device(system_info_client, monkeypatch):
    client, _store, token = system_info_client
    calls: list[list[str]] = []
    boot_epoch = int(time.time()) - 3_661

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv == ["/usr/bin/pmset", "-g", "batt"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Now drawing from 'Battery Power'\n -InternalBattery-0: 84%; discharging; 0:42 remaining",
            )
        if argv == ["/usr/sbin/sysctl", "-n", "kern.boottime"]:
            return SimpleNamespace(returncode=0, stdout=f"{{ sec = {boot_epoch}, usec = 0 }}\n")
        raise AssertionError(f"unexpected process invocation: {argv!r}")

    monkeypatch.setattr(routes.sys, "platform", "darwin")
    monkeypatch.setattr(routes.subprocess, "run", fake_run)
    endpoint = "/v1/jarvis/system/info"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers={"Authorization": "Bearer system-info-test-admin-key"}).status_code == 403

    response = client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["online"] is True
    assert response.json()["battery_percent"] == 84
    assert response.json()["charging"] is False
    assert response.json()["uptime_seconds"] >= 3_661
    assert "1 hour" in response.json()["uptime"]
    assert calls == [["/usr/bin/pmset", "-g", "batt"], ["/usr/sbin/sysctl", "-n", "kern.boottime"]]


def test_system_info_fails_closed_when_mac_provider_fails(system_info_client, monkeypatch):
    client, _store, token = system_info_client
    monkeypatch.setattr(routes.sys, "platform", "darwin")
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="sensitive diagnostic"),
    )
    response = client.get("/v1/jarvis/system/info", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Mac battery or uptime information could not be read"}
    assert "sensitive diagnostic" not in response.text
