"""Fail-closed browser session primitives for the Thozha Web Workspace.

This module is intentionally not mounted by the general OpenJarvis app. The
integrator must call install_web_auth() after choosing a same-origin HTTPS
deployment. It never accepts or returns Android paired-device bearer tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

SESSION_COOKIE = "thozha_web_session"
CSRF_COOKIE = "thozha_web_csrf"
SESSION_PATH = "/v1/web"
PASSWORD_HASH_SCHEME = "pbkdf2_sha256"
MIN_PASSWORD_HASH_ITERATIONS = 100_000
MAX_PASSWORD_HASH_ITERATIONS = 2_000_000
DEFAULT_PASSWORD_HASH_ITERATIONS = 600_000
MAX_LOGIN_BODY_BYTES = 4096
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CSRF_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class WebAuthConfigurationError(ValueError):
    """Invalid or incomplete browser-auth configuration."""


@dataclass(frozen=True)
class WebPrincipal:
    user_id: str
    session_id: str
    expires_at: int


@dataclass(frozen=True)
class WebAuthConfig:
    owner_id: str
    password_hash: str
    origin: str
    session_ttl_seconds: int = 8 * 60 * 60
    allow_insecure_loopback_dev: bool = False
    login_attempts: int = 10
    login_window_seconds: int = 300
    database_path: Path | None = None

    def __post_init__(self) -> None:
        if (
            not self.owner_id
            or len(self.owner_id) > 128
            or any(ord(c) < 0x20 for c in self.owner_id)
        ):
            raise WebAuthConfigurationError("THOZHA_WEB_OWNER_ID is invalid")
        _parse_password_hash(self.password_hash)
        _validate_config_origin(
            self.origin, allow_loopback_http=self.allow_insecure_loopback_dev
        )
        if not 60 <= self.session_ttl_seconds <= 24 * 60 * 60:
            raise WebAuthConfigurationError(
                "THOZHA_WEB_SESSION_TTL_SECONDS must be 60..86400"
            )
        if (
            not 3 <= self.login_attempts <= 100
            or not 10 <= self.login_window_seconds <= 3600
        ):
            raise WebAuthConfigurationError(
                "Web login rate-limit configuration is invalid"
            )

    @classmethod
    def from_environment(
        cls, environ: dict[str, str] | None = None
    ) -> WebAuthConfig | None:
        env = environ if environ is not None else os.environ
        owner = env.get("THOZHA_WEB_OWNER_ID", "").strip()
        password_hash = env.get("THOZHA_WEB_PASSWORD_HASH", "").strip()
        origin = env.get("THOZHA_WEB_ORIGIN", "").strip()
        if not owner and not password_hash and not origin:
            return None
        if not owner or not password_hash or not origin:
            raise WebAuthConfigurationError(
                "Browser authentication is only enabled with complete configuration"
            )
        try:
            ttl = int(env.get("THOZHA_WEB_SESSION_TTL_SECONDS", str(8 * 60 * 60)))
            attempts = int(env.get("THOZHA_WEB_LOGIN_ATTEMPTS", "10"))
            window = int(env.get("THOZHA_WEB_LOGIN_WINDOW_SECONDS", "300"))
        except ValueError as exc:
            raise WebAuthConfigurationError(
                "Web authentication numeric configuration is invalid"
            ) from exc
        allow_loopback_http = (
            env.get("THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV", "") == "1"
        )
        database_path = (
            Path(env["THOZHA_WEB_SESSION_DB"]).expanduser()
            if env.get("THOZHA_WEB_SESSION_DB")
            else None
        )
        return cls(
            owner,
            password_hash,
            origin,
            ttl,
            allow_loopback_http,
            attempts,
            window,
            database_path,
        )


@dataclass(frozen=True)
class _Session:
    user_id: str
    csrf_hash: bytes
    expires_at: int


class WebSessionStore:
    """Opaque session IDs are hashed at rest; only session metadata is stored."""

    def __init__(self, database_path: str | Path, *, clock: Any = time.time) -> None:
        self.path = Path(database_path).expanduser()
        if self.path.is_symlink():
            raise WebAuthConfigurationError(
                "Web session database must not be a symlink"
            )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS web_sessions(
                session_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                csrf_hash BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked_at INTEGER
            )"""
        )
        self._db.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def create(
        self, *, session_id: str, user_id: str, csrf_token: str, expires_at: int
    ) -> None:
        if not _SESSION_ID_RE.fullmatch(session_id) or not _CSRF_TOKEN_RE.fullmatch(
            csrf_token
        ):
            raise ValueError("Web session identifier is invalid")
        session_hash = _sha256_hex(session_id.encode("ascii"))
        csrf_hash = hashlib.sha256(csrf_token.encode("ascii")).digest()
        now = int(self._clock())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """INSERT INTO web_sessions(
                        session_hash,user_id,csrf_hash,created_at,expires_at,revoked_at
                    ) VALUES(?,?,?,?,?,NULL)""",
                    (session_hash, user_id, csrf_hash, now, expires_at),
                )
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def get(self, session_id: str) -> _Session | None:
        if not _SESSION_ID_RE.fullmatch(session_id):
            return None
        digest = _sha256_hex(session_id.encode("ascii"))
        now = int(self._clock())
        with self._lock:
            row = self._db.execute(
                """SELECT user_id,csrf_hash,expires_at,revoked_at
                   FROM web_sessions WHERE session_hash=?""",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if row[3] is not None or int(row[2]) <= now:
                if row[3] is None:
                    self._db.execute(
                        """UPDATE web_sessions SET revoked_at=?
                           WHERE session_hash=? AND revoked_at IS NULL""",
                        (now, digest),
                    )
                    self._db.commit()
                return None
            return _Session(str(row[0]), bytes(row[1]), int(row[2]))

    def revoke(self, session_id: str) -> None:
        if not _SESSION_ID_RE.fullmatch(session_id):
            return
        digest = _sha256_hex(session_id.encode("ascii"))
        with self._lock:
            self._db.execute(
                """UPDATE web_sessions SET revoked_at=?
                   WHERE session_hash=? AND revoked_at IS NULL""",
                (int(self._clock()), digest),
            )
            self._db.commit()

    def rotate_csrf(self, session_id: str, csrf_token: str) -> bool:
        if not _SESSION_ID_RE.fullmatch(session_id) or not _CSRF_TOKEN_RE.fullmatch(
            csrf_token
        ):
            return False
        digest = _sha256_hex(session_id.encode("ascii"))
        csrf_hash = hashlib.sha256(csrf_token.encode("ascii")).digest()
        now = int(self._clock())
        with self._lock:
            cursor = self._db.execute(
                """UPDATE web_sessions SET csrf_hash=?
                   WHERE session_hash=? AND revoked_at IS NULL AND expires_at>?""",
                (csrf_hash, digest, now),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def verify_csrf(self, session_id: str, csrf_token: str) -> bool:
        if not _SESSION_ID_RE.fullmatch(session_id) or not _CSRF_TOKEN_RE.fullmatch(
            csrf_token
        ):
            return False
        session = self.get(session_id)
        return bool(
            session
            and hmac.compare_digest(
                session.csrf_hash, hashlib.sha256(csrf_token.encode("ascii")).digest()
            )
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()


class LoginRateLimiter:
    """Bounded process-local limiter; stores a digest, never a raw address."""

    def __init__(
        self, *, max_attempts: int, window_seconds: int, clock: Any = time.monotonic
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.clock = clock
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, address: str) -> tuple[bool, int]:
        key = _sha256_hex(address.encode("utf-8", "replace"))
        now = self.clock()
        with self._lock:
            if key not in self._attempts and len(self._attempts) >= 1024:
                oldest = min(
                    self._attempts,
                    key=lambda item: (
                        self._attempts[item][-1] if self._attempts[item] else now
                    ),
                )
                self._attempts.pop(oldest, None)
            attempts = self._attempts.setdefault(key, deque())
            while attempts and now - attempts[0] >= self.window_seconds:
                attempts.popleft()
            if len(attempts) >= self.max_attempts:
                return False, max(1, int(self.window_seconds - (now - attempts[0])))
            attempts.append(now)
            return True, 0

    def clear(self, address: str) -> None:
        key = _sha256_hex(address.encode("utf-8", "replace"))
        with self._lock:
            self._attempts.pop(key, None)


@dataclass
class WebAuthContext:
    config: WebAuthConfig | None
    store: WebSessionStore | None
    rate_limiter: LoginRateLimiter | None
    clock: Any = time.time


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = DEFAULT_PASSWORD_HASH_ITERATIONS,
) -> str:
    """Create a config-file value; callers must store it outside source control."""
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("Web password must be 12..1024 characters")
    if not MIN_PASSWORD_HASH_ITERATIONS <= iterations <= MAX_PASSWORD_HASH_ITERATIONS:
        raise ValueError("PBKDF2 iteration count is outside the accepted range")
    salt = salt or secrets.token_bytes(16)
    if not 16 <= len(salt) <= 64:
        raise ValueError("PBKDF2 salt must be 16..64 bytes")
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=32
    )
    return "$".join(
        (PASSWORD_HASH_SCHEME, str(iterations), _b64url(salt), _b64url(digest))
    )


def _parse_password_hash(value: str) -> tuple[int, bytes, bytes]:
    parts = value.split("$")
    if len(parts) != 4 or parts[0] != PASSWORD_HASH_SCHEME:
        raise WebAuthConfigurationError("Web password hash format is invalid")
    try:
        iterations = int(parts[1])
        salt = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        digest = base64.urlsafe_b64decode(parts[3] + "=" * (-len(parts[3]) % 4))
    except (ValueError, TypeError) as exc:
        raise WebAuthConfigurationError("Web password hash format is invalid") from exc
    if (
        not MIN_PASSWORD_HASH_ITERATIONS <= iterations <= MAX_PASSWORD_HASH_ITERATIONS
        or not 16 <= len(salt) <= 64
        or len(digest) != 32
    ):
        raise WebAuthConfigurationError("Web password hash parameters are invalid")
    return iterations, salt, digest


def verify_password(password: str, password_hash: str) -> bool:
    try:
        iterations, salt, expected = _parse_password_hash(password_hash)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations, dklen=32
        )
    except (ValueError, TypeError, WebAuthConfigurationError):
        return False
    return hmac.compare_digest(actual, expected)


def _validate_config_origin(origin: str, *, allow_loopback_http: bool) -> str:
    try:
        parsed = urlsplit(origin)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise WebAuthConfigurationError("THOZHA_WEB_ORIGIN is invalid") from exc
    loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    scheme_ok = parsed.scheme == "https" or (
        allow_loopback_http and loopback and parsed.scheme == "http"
    )
    if (
        not scheme_ok
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
        or origin != f"{parsed.scheme}://{parsed.netloc}"
    ):
        raise WebAuthConfigurationError(
            "THOZHA_WEB_ORIGIN must be a canonical HTTPS origin"
        )
    return origin


def _context(request: Request) -> WebAuthContext:
    context = getattr(request.app.state, "thozha_web_auth", None)
    if (
        not isinstance(context, WebAuthContext)
        or context.config is None
        or context.store is None
        or context.rate_limiter is None
    ):
        raise HTTPException(
            status_code=503, detail="Web authentication is not configured"
        )
    return context


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _validate_request_origin(request: Request, config: WebAuthConfig) -> None:
    request_origin = f"{request.url.scheme}://{request.url.netloc}"
    if request_origin != config.origin:
        raise HTTPException(status_code=403, detail="Origin rejected")
    if request.headers.get("origin") != config.origin:
        raise HTTPException(status_code=403, detail="Origin rejected")
    if request.url.scheme != "https":
        peer = request.client.host if request.client else ""
        if not config.allow_insecure_loopback_dev or peer not in {
            "127.0.0.1",
            "::1",
            "testclient",
        }:
            raise HTTPException(
                status_code=403, detail="Insecure browser transport is disabled"
            )
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site != "same-origin":
        raise HTTPException(status_code=403, detail="Origin rejected")


def _request_cookie_session(
    request: Request, context: WebAuthContext
) -> tuple[str, _Session]:
    assert context.config is not None and context.store is not None
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=401, detail="Web session required")
    session = context.store.get(session_id)
    if session is None or session.user_id != context.config.owner_id:
        raise HTTPException(status_code=401, detail="Web session required")
    return session_id, session


def require_web_session(request: Request) -> WebPrincipal:
    """FastAPI dependency; paired device tokens are never accepted."""
    context = _context(request)
    assert context.config is not None and context.store is not None
    session_id, session = _request_cookie_session(request, context)
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            raise HTTPException(
                status_code=415, detail="Content-Type must be application/json"
            )
        _validate_request_origin(request, context.config)
        csrf = request.headers.get("x-thozha-csrf", "")
        if not csrf or not context.store.verify_csrf(session_id, csrf):
            raise HTTPException(status_code=403, detail="CSRF check failed")
    return WebPrincipal(session.user_id, session_id, session.expires_at)


def create_web_auth_context(
    *,
    config: WebAuthConfig | None = None,
    store: WebSessionStore | None = None,
    clock: Any = time.time,
) -> WebAuthContext:
    """Create an app-state object. No filesystem/database is touched when disabled."""
    if config is None:
        try:
            config = WebAuthConfig.from_environment()
        except WebAuthConfigurationError:
            return WebAuthContext(None, None, None, clock)
    if config is None:
        return WebAuthContext(None, None, None, clock)
    if store is None:
        if config.database_path is None:
            from openjarvis.core.paths import get_config_dir

            db_path = get_config_dir() / "thozha_web_sessions.db"
        else:
            db_path = config.database_path
        store = WebSessionStore(db_path, clock=clock)
    limiter = LoginRateLimiter(
        max_attempts=config.login_attempts, window_seconds=config.login_window_seconds
    )
    return WebAuthContext(config, store, limiter, clock)


router = APIRouter(prefix="/v1/web/session", tags=["thozha-web-session"])


async def _read_json(request: Request) -> dict[str, Any]:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(
            status_code=415, detail="Content-Type must be application/json"
        )
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_LOGIN_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length"
            ) from exc
    body = await request.body()
    if len(body) > MAX_LOGIN_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        data = __import__("json").loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    return data


def _set_session_cookies(
    response: Response, context: WebAuthContext, session_id: str, csrf: str, ttl: int
) -> None:
    assert context.config is not None
    secure = urlsplit(context.config.origin).scheme == "https"
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=ttl,
        path=SESSION_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=ttl,
        path=SESSION_PATH,
        secure=secure,
        httponly=False,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_session_cookies(response: Response, context: WebAuthContext) -> None:
    assert context.config is not None
    secure = urlsplit(context.config.origin).scheme == "https"
    response.delete_cookie(
        SESSION_COOKIE,
        path=SESSION_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE, path=SESSION_PATH, secure=secure, httponly=False, samesite="strict"
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("/login")
async def login(request: Request) -> Response:
    context = _context(request)
    assert (
        context.config is not None
        and context.store is not None
        and context.rate_limiter is not None
    )
    _validate_request_origin(request, context.config)
    if request.headers.get("sec-fetch-site") not in {None, "same-origin"}:
        raise HTTPException(status_code=403, detail="Origin rejected")
    address = _client_address(request)
    allowed, retry_after = context.rate_limiter.allow(address)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )
    body = await _read_json(request)
    if (
        set(body) != {"username", "password"}
        or not isinstance(body.get("username"), str)
        or not isinstance(body.get("password"), str)
    ):
        raise HTTPException(
            status_code=422, detail="username and password are required"
        )
    username_ok = hmac.compare_digest(
        body["username"].encode("utf-8"), context.config.owner_id.encode("utf-8")
    )
    password_ok = verify_password(body["password"], context.config.password_hash)
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    context.rate_limiter.clear(address)

    old_id = request.cookies.get(SESSION_COOKIE)
    if old_id and len(old_id) <= 128:
        context.store.revoke(old_id)
    session_id = _b64url(secrets.token_bytes(32))
    csrf = _b64url(secrets.token_bytes(32))
    expires_at = int(context.clock()) + context.config.session_ttl_seconds
    context.store.create(
        session_id=session_id,
        user_id=context.config.owner_id,
        csrf_token=csrf,
        expires_at=expires_at,
    )
    response = JSONResponse(
        {
            "user_id": context.config.owner_id,
            "expires_at": expires_at,
            "csrf_token": csrf,
        },
        headers={"Cache-Control": "no-store"},
    )
    _set_session_cookies(
        response, context, session_id, csrf, context.config.session_ttl_seconds
    )
    return response


@router.get("")
async def get_session(
    request: Request,
    response: Response,
    principal: WebPrincipal = Depends(require_web_session),
) -> dict[str, Any]:
    context = _context(request)
    session_id, session = _request_cookie_session(request, context)
    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
    if not csrf_cookie or not context.store.verify_csrf(session_id, csrf_cookie):
        csrf_cookie = _b64url(secrets.token_bytes(32))
        if not context.store.rotate_csrf(session_id, csrf_cookie):
            raise HTTPException(status_code=401, detail="Web session required")
        _set_csrf_cookie(
            response,
            context,
            csrf_cookie,
            max(1, principal.expires_at - int(context.clock())),
        )
    response.headers["Cache-Control"] = "no-store"
    return {
        "user_id": principal.user_id,
        "expires_at": principal.expires_at,
        "csrf_token": csrf_cookie,
    }


@router.post("/logout")
async def logout(
    request: Request, principal: WebPrincipal = Depends(require_web_session)
) -> Response:
    context = _context(request)
    context.store.revoke(principal.session_id)
    response = Response(content=b"", status_code=204)
    _clear_session_cookies(response, context)
    return response


def install_web_auth(
    app: Any,
    *,
    config: WebAuthConfig | None = None,
    store: WebSessionStore | None = None,
    clock: Any = time.time,
) -> WebAuthContext:
    """Mount routes; absent config disables login and session access."""
    context = create_web_auth_context(config=config, store=store, clock=clock)
    app.state.thozha_web_auth = context
    app.include_router(router)
    return context


def _set_csrf_cookie(
    response: Response, context: WebAuthContext, token: str, ttl: int
) -> None:
    assert context.config is not None
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=ttl,
        path=SESSION_PATH,
        secure=urlsplit(context.config.origin).scheme == "https",
        httponly=False,
        samesite="strict",
    )
