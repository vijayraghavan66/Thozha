"""Contract checks for mounting the separately authenticated Web APIs."""

from unittest.mock import MagicMock
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import hashlib

from fastapi.testclient import TestClient

from openjarvis.server.app import create_app


def _app():
    return create_app(MagicMock(), "test-model")


def test_web_api_is_disabled_without_explicit_browser_auth(monkeypatch):
    for name in (
        "THOZHA_WEB_OWNER_ID",
        "THOZHA_WEB_PASSWORD_HASH",
        "THOZHA_WEB_ORIGIN",
        "THOZHA_WEB_SESSION_DB",
    ):
        monkeypatch.delenv(name, raising=False)

    app = _app()
    with TestClient(app, base_url="https://thozha.test") as client:
        response = client.post(
            "/v1/web/session/login",
            headers={"Origin": "https://thozha.test"},
            json={"username": "owner", "password": "not-a-real-secret"},
        )
        assert response.status_code == 503
        assert client.get("/v1/web/conversations").status_code == 404


def test_explicit_web_auth_mounts_owner_scoped_conversation_routes(
    monkeypatch, tmp_path
):
    from openjarvis.server.thozha_web_auth import hash_password

    origin = "https://thozha.test"
    monkeypatch.setenv("THOZHA_WEB_OWNER_ID", "test-owner")
    monkeypatch.setenv(
        "THOZHA_WEB_PASSWORD_HASH",
        hash_password("a-test-only-password-long-enough"),
    )
    monkeypatch.setenv("THOZHA_WEB_ORIGIN", origin)
    monkeypatch.setenv("THOZHA_WEB_SESSION_DB", str(tmp_path / "web" / "sessions.db"))
    monkeypatch.setenv("THOZHA_WEB_MEMORY_DB", str(tmp_path / "web" / "memory.db"))

    app = _app()
    with TestClient(app, base_url=origin) as client:
        login = client.post(
            "/v1/web/session/login",
            headers={"Origin": origin},
            json={
                "username": "test-owner",
                "password": "a-test-only-password-long-enough",
            },
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]

        # Browser auth alone is not a supervisor/device identity. Without an
        # explicit paired-device mapping, privileged task/device routes fail closed.
        assert client.get("/v1/web/tasks").status_code == 403
        assert client.get("/v1/web/devices").status_code == 403

        created = client.post(
            "/v1/web/conversations",
            headers={"Origin": origin, "X-Thozha-CSRF": csrf},
            json={"title": "Test conversation"},
        )
        assert created.status_code == 201
        conversation = created.json()
        assert conversation["title"] == "Test conversation"

        listed = client.get("/v1/web/conversations")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [conversation["id"]]

        memory = client.post(
            "/v1/web/memory",
            headers={
                "Origin": origin,
                "X-Thozha-CSRF": csrf,
                "Idempotency-Key": "web-memory-integration-001",
            },
            json={"content": "Prefers concise status updates", "tags": ["style"]},
        )
        assert memory.status_code == 201
        memory_id = memory.json()["memory"]["id"]
        assert (
            client.get("/v1/web/memory/search?q=concise").json()["items"][0]["id"]
            == memory_id
        )

        logout = client.post(
            "/v1/web/session/logout",
            headers={"Origin": origin, "X-Thozha-CSRF": csrf},
            json={},
        )
        assert logout.status_code == 204
        assert client.get("/v1/web/conversations").status_code == 401


def test_automation_routes_are_opt_in_and_fail_closed_without_link_runner_or_mapping(
    monkeypatch, tmp_path
):
    from openjarvis.server.thozha_web_auth import hash_password

    origin = "https://thozha.test"
    monkeypatch.setenv("THOZHA_WEB_OWNER_ID", "test-owner")
    monkeypatch.setenv(
        "THOZHA_WEB_PASSWORD_HASH",
        hash_password("a-test-only-password-long-enough"),
    )
    monkeypatch.setenv("THOZHA_WEB_ORIGIN", origin)
    monkeypatch.setenv("THOZHA_WEB_SESSION_DB", str(tmp_path / "web" / "sessions.db"))
    monkeypatch.setenv("THOZHA_WEB_MEMORY_DB", str(tmp_path / "web" / "memory.db"))
    monkeypatch.setenv(
        "THOZHA_WEB_AUTOMATION_DB", str(tmp_path / "web" / "automation.db")
    )
    monkeypatch.setenv("THOZHA_WEB_AUTOMATION_ENABLED", "1")
    monkeypatch.delenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", raising=False)

    app = _app()
    with TestClient(app, base_url=origin) as client:
        login = client.post(
            "/v1/web/session/login",
            headers={"Origin": origin},
            json={
                "username": "test-owner",
                "password": "a-test-only-password-long-enough",
            },
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        capabilities = client.get("/v1/web/automations/capabilities")
        assert capabilities.status_code == 200
        operations = capabilities.json()["operations"]
        assert operations["list"] is True
        assert operations["create"] is False
        assert operations["manual_trigger"] is False
        readiness = client.get("/v1/web/supervisor/readiness").json()
        assert readiness["status"] == "degraded"
        assert readiness["components"]["device_mapping"]["state"] == "unavailable"
        assert "auth.json" not in str(readiness)
        assert "token_hash" not in str(readiness)
        rejected = client.post(
            "/v1/web/automations",
            headers={
                "Origin": origin,
                "Content-Type": "application/json",
                "X-Thozha-CSRF": csrf,
                "Idempotency-Key": "automation-test-key-0001",
            },
            json={
                "prompt": "Read-only synthetic status",
                "schedule_type": "interval",
                "schedule_value": "300",
                "device_id": "a" * 32,
            },
        )
        assert rejected.status_code in {403, 404}


def test_ollama_only_web_profile_reports_runner_blocker_and_never_queues_task(
    monkeypatch, tmp_path
):
    from openjarvis.server.thozha_web_auth import hash_password

    origin = "https://thozha.test"
    device_id = "d" * 32
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "openjarvis"))
    monkeypatch.setenv("THOZHA_WEB_OWNER_ID", "test-owner")
    monkeypatch.setenv(
        "THOZHA_WEB_PASSWORD_HASH",
        hash_password("a-test-only-password-long-enough"),
    )
    monkeypatch.setenv("THOZHA_WEB_ORIGIN", origin)
    monkeypatch.setenv("THOZHA_WEB_SESSION_DB", str(tmp_path / "web" / "sessions.db"))
    monkeypatch.setenv("THOZHA_WEB_MEMORY_DB", str(tmp_path / "web" / "memory.db"))
    monkeypatch.setenv("THOZHA_WEB_ALLOWED_DEVICE_IDS", device_id)

    engine = SimpleNamespace(engine_id="ollama", health=lambda: True)
    agent = SimpleNamespace(_engine=engine, _model="qwen3.5:4b")
    monkeypatch.setattr(
        "openjarvis.server.routes._engine_key_for_model",
        lambda _engine, _model: "ollama",
    )
    app = create_app(engine, "qwen3.5:4b", agent=agent)
    store = app.state.supervisor_store
    now = datetime.now(timezone.utc)
    store._db.execute(
        "INSERT INTO paired_devices(id,public_key_b64,token_hash,created_at,expires_at,device_name) "
        "VALUES(?,?,?,?,?,?)",
        (
            device_id,
            "synthetic-public-key",
            hashlib.sha256(b"synthetic-only").hexdigest(),
            now.isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            "isolated test fixture",
        ),
    )
    store._db.commit()

    with TestClient(app, base_url=origin) as client:
        login = client.post(
            "/v1/web/session/login",
            headers={"Origin": origin},
            json={
                "username": "test-owner",
                "password": "a-test-only-password-long-enough",
            },
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        readiness = client.get("/v1/web/supervisor/readiness").json()
        assert readiness["components"]["supervisor_worker"] == {
            "state": "unavailable",
            "reason": "unsupported_engine",
        }
        assert client.get("/v1/web/tasks").json()["tasks"] == []
        rejected = client.post(
            "/v1/web/tasks",
            headers={"Origin": origin, "X-Thozha-CSRF": csrf},
            json={
                "objective": "Read the Mac version and uptime.",
                "capability_scope": ["mac_read"],
                "idempotency_key": "unsupported-ollama-task-001",
                "requester_device_id": device_id,
            },
        )
        assert rejected.status_code == 503
        assert rejected.json()["detail"] == "Supervisor task worker is unavailable"
        assert client.get("/v1/web/tasks").json()["tasks"] == []
