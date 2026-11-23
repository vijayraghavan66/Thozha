from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.server import thozha_web_auth
from openjarvis.server.thozha_web_auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    LoginRateLimiter,
    WebAuthConfig,
    WebAuthConfigurationError,
    WebAuthContext,
    WebSessionStore,
    hash_password,
    install_web_auth,
)

ORIGIN = "https://thozha.test"
OWNER = "owner-local"


def _app(tmp_path: Path, *, attempts: int = 10, clock=None):
    now = clock or (lambda: time.time())
    config = WebAuthConfig(
        owner_id=OWNER,
        password_hash=hash_password(
            "a sufficiently long local passphrase", salt=b"s" * 16, iterations=100_000
        ),
        origin=ORIGIN,
        session_ttl_seconds=120,
        login_attempts=attempts,
        login_window_seconds=60,
        database_path=tmp_path / "web_sessions.db",
    )
    store = WebSessionStore(config.database_path, clock=now)
    app = FastAPI()
    app.state.thozha_web_auth = WebAuthContext(
        config,
        store,
        LoginRateLimiter(
            max_attempts=config.login_attempts,
            window_seconds=config.login_window_seconds,
        ),
        now,
    )
    install_web_auth(app, config=config, store=store, clock=now)
    return app, store


def _login(client: TestClient, **extra):
    headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    payload = {
        "username": OWNER,
        "password": "a sufficiently long local passphrase",
        **extra,
    }
    return client.post("/v1/web/session/login", headers=headers, json=payload)


def test_missing_configuration_fails_closed(tmp_path):
    app = FastAPI()
    install_web_auth(app, config=None, store=None)
    with TestClient(app, base_url=ORIGIN) as client:
        assert (
            client.post(
                "/v1/web/session/login",
                headers={"Origin": ORIGIN},
                json={"username": OWNER, "password": "anything"},
            ).status_code
            == 503
        )
        assert client.get("/v1/web/session").status_code == 503


def test_session_database_and_parent_are_owner_only(tmp_path):
    database = tmp_path / "web_sessions.db"
    store = WebSessionStore(database)
    try:
        assert stat.S_IMODE(tmp_path.stat().st_mode) & 0o077 == 0
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
    finally:
        store.close()


def test_unsafe_existing_database_parent_is_rejected(tmp_path):
    os.chmod(tmp_path, 0o750)
    try:
        with pytest.raises(
            WebAuthConfigurationError, match="directory must be owner-only"
        ):
            WebSessionStore(tmp_path / "web_sessions.db")
    finally:
        os.chmod(tmp_path, 0o700)


def test_database_chmod_failure_fails_closed_and_cleans_new_database(
    tmp_path, monkeypatch
):
    database = tmp_path / "web_sessions.db"
    actual_chmod = os.chmod

    def fail_database_chmod(path, mode):
        if Path(path) == database:
            raise PermissionError("test permission failure")
        actual_chmod(path, mode)

    monkeypatch.setattr(thozha_web_auth.os, "chmod", fail_database_chmod)
    with pytest.raises(WebAuthConfigurationError, match="restricted to mode 0600"):
        WebSessionStore(database)
    assert not database.exists()


def test_password_hash_verification_is_constant_contract():
    password_hash = hash_password(
        "a sufficiently long local passphrase", salt=b"s" * 16, iterations=100_000
    )
    assert password_hash.startswith("pbkdf2_sha256$100000$")
    assert (
        hash_password(
            "a sufficiently long local passphrase", salt=b"s" * 16, iterations=100_000
        )
        == password_hash
    )
    from openjarvis.server.thozha_web_auth import verify_password

    assert verify_password("a sufficiently long local passphrase", password_hash)
    assert not verify_password("wrong passphrase", password_hash)


def test_login_cookie_flags_and_authenticated_session(tmp_path):
    app, store = _app(tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            response = _login(client)
            assert response.status_code == 200
            assert response.json()["user_id"] == OWNER
            assert "access_token" not in response.json()
            cookie_headers = response.headers.get_list("set-cookie")
            session_cookie = next(
                value
                for value in cookie_headers
                if value.startswith(SESSION_COOKIE + "=")
            )
            csrf_cookie = next(
                value for value in cookie_headers if value.startswith(CSRF_COOKIE + "=")
            )
            assert "httponly" in session_cookie.lower()
            assert "secure" in session_cookie.lower()
            assert "samesite=strict" in session_cookie.lower()
            assert "path=/v1/web" in session_cookie.lower()
            assert "httponly" not in csrf_cookie.lower()
            assert "secure" in csrf_cookie.lower()
            assert response.headers["cache-control"] == "no-store"

            session = client.get("/v1/web/session")
            assert session.status_code == 200
            assert session.json()["user_id"] == OWNER
            assert session.json()["csrf_token"]
            assert "access_token" not in session.text
            assert "access-control-allow-origin" not in session.headers
    finally:
        store.close()


def test_missing_and_rotated_session_cookies_are_rejected_or_refreshed(tmp_path):
    app, store = _app(tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            assert client.get("/v1/web/session").status_code == 401
            login = _login(client)
            assert login.status_code == 200
            session_id = client.cookies.get(SESSION_COOKIE)
            old_csrf = login.json()["csrf_token"]

            client.cookies.delete(CSRF_COOKIE)
            refreshed = client.get("/v1/web/session")
            assert refreshed.status_code == 200
            new_csrf = refreshed.json()["csrf_token"]
            assert new_csrf != old_csrf
            assert client.cookies.get(CSRF_COOKIE) == new_csrf

            stale_csrf = client.post(
                "/v1/web/session/logout",
                headers={
                    "Origin": ORIGIN,
                    "Sec-Fetch-Site": "same-origin",
                    "X-Thozha-CSRF": old_csrf,
                },
                json={},
            )
            assert stale_csrf.status_code == 403
            assert store.get(session_id) is not None
    finally:
        store.close()


def test_login_rejects_wrong_origin_extra_fields_and_paired_tokens(tmp_path):
    app, store = _app(tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            wrong_origin = client.post(
                "/v1/web/session/login",
                headers={"Origin": "https://attacker.test"},
                json={
                    "username": OWNER,
                    "password": "a sufficiently long local passphrase",
                },
            )
            assert wrong_origin.status_code == 403
            extra = _login(client, device_token="paired-token-must-not-be-accepted")
            assert extra.status_code == 422
            wrong_password = _login(client, password="not the configured passphrase")
            assert wrong_password.status_code == 401
            assert "paired" not in wrong_password.text.lower()
    finally:
        store.close()


def test_mutations_require_same_origin_and_csrf_token(tmp_path):
    app, store = _app(tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            login = _login(client)
            assert login.status_code == 200
            csrf = login.json()["csrf_token"]
            no_csrf = client.post(
                "/v1/web/session/logout",
                headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
                json={},
            )
            assert no_csrf.status_code == 403
            non_json = client.post(
                "/v1/web/session/logout",
                headers={
                    "Origin": ORIGIN,
                    "Sec-Fetch-Site": "same-origin",
                    "X-Thozha-CSRF": csrf,
                    "Content-Type": "text/plain",
                },
                content="logout",
            )
            assert non_json.status_code == 415
            wrong_origin = client.post(
                "/v1/web/session/logout",
                headers={"Origin": "https://attacker.test", "X-Thozha-CSRF": csrf},
                json={},
            )
            assert wrong_origin.status_code == 403
            valid = client.post(
                "/v1/web/session/logout",
                headers={
                    "Origin": ORIGIN,
                    "Sec-Fetch-Site": "same-origin",
                    "X-Thozha-CSRF": csrf,
                },
                json={},
            )
            assert valid.status_code == 204
            assert "max-age=0" in ";".join(valid.headers.get_list("set-cookie")).lower()
            assert client.get("/v1/web/session").status_code == 401
    finally:
        store.close()


def test_login_rotates_previous_session(tmp_path):
    app, store = _app(tmp_path)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            first = _login(client)
            assert first.status_code == 200
            first_cookie = client.cookies.get(SESSION_COOKIE)
            second = _login(client)
            assert second.status_code == 200
            second_cookie = client.cookies.get(SESSION_COOKIE)
            assert first_cookie and second_cookie and first_cookie != second_cookie
            assert store.get(first_cookie) is None
            assert client.get("/v1/web/session").status_code == 200
    finally:
        store.close()


def test_expired_session_is_revoked_and_rejected(tmp_path):
    clock = [1_700_000_000.0]
    app, store = _app(tmp_path, clock=lambda: clock[0])
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            assert _login(client).status_code == 200
            clock[0] += 121
            assert client.get("/v1/web/session").status_code == 401
    finally:
        store.close()


def test_login_rate_limit_is_bounded_and_returns_retry_after(tmp_path):
    app, store = _app(tmp_path, attempts=3)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            for _ in range(3):
                assert _login(client, password="wrong passphrase").status_code == 401
            locked = _login(client, password="wrong passphrase")
            assert locked.status_code == 429
            assert int(locked.headers["retry-after"]) > 0
    finally:
        store.close()
