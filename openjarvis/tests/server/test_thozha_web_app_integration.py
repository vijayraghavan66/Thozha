"""Contract checks for mounting the separately authenticated Web APIs."""

from unittest.mock import MagicMock

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
