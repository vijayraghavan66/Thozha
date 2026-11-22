"""Allowlisted authenticated gateway for the Android companion.

The raw OpenJarvis API stays on loopback. Separate runners may expose this
narrow API over an explicitly configured private transport; binding policy and
TLS are enforced by those runners, never by a catch-all proxy route.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from openjarvis.core.paths import get_config_dir
from openjarvis.server.supervisor_routes import _canonical_pairing_payload, _pairing_server_identity
from openjarvis.tasks.supervisor import SupervisorStore

BACKEND_URL = "http://127.0.0.1:8000"
GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 8001
MAX_BODY_BYTES = 16 * 1024
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_TAILNET_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net$")


class ReadOnlyGatewayStore:
    """Read the shared device table without running task restart recovery."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    def authenticate_device_token(self, token: str) -> str | None:
        path = Path(self.db_path)
        if not path.exists():
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        db_uri = f"{path.resolve().as_uri()}?mode=ro"
        try:
            with sqlite3.connect(db_uri, uri=True, timeout=2.0) as db:
                row = db.execute(
                    "SELECT id FROM paired_devices WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",
                    (token_hash, now),
                ).fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503, detail="Supervisor device store is unavailable") from exc
        return row[0] if row else None


def validate_bind_host(host: str) -> str:
    """Reject any gateway bind that is not loopback."""
    if host.lower() == "localhost":
        return host
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError("Jarvis Companion Gateway may bind only to a loopback address")


def _device_id_for_request(request: Request, store: SupervisorStore) -> tuple[str, str]:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Device bearer token required")
    device_id = store.authenticate_device_token(token)
    if not device_id:
        raise HTTPException(status_code=401, detail="Invalid or expired device token")
    return device_id, token


async def _read_json_body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Expected a JSON object") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    return value


async def _proxy(
    request: Request,
    *,
    backend_path: str,
    params: dict[str, str] | None = None,
    body: bytes | None = None,
) -> Response:
    """Forward one prevalidated companion request to the fixed local server."""
    _device_id_for_request(request, request.app.state.supervisor_store)
    headers = {"Authorization": request.headers["authorization"], "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        transport = request.app.state.backend_transport
        async with httpx.AsyncClient(
            base_url=request.app.state.backend_url,
            transport=transport,
            timeout=15.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            upstream = await client.request(request.method, backend_path, params=params, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Local Jarvis API is unavailable") from exc
    content_type = upstream.headers.get("content-type", "application/json")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={"Content-Type": content_type, "Cache-Control": "no-store"},
    )


def _allowed_origin(value: str) -> str:
    """Accept only a canonical HTTPS MagicDNS origin for identity migration."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid target_origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not _TAILNET_HOST_RE.fullmatch(host.lower())
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or value != value.strip()
    ):
        raise HTTPException(status_code=422, detail="target_origin must be an HTTPS *.ts.net origin")
    return f"https://{host.lower()}"


def create_gateway_app(
    *,
    backend_url: str = BACKEND_URL,
    supervisor_store: SupervisorStore | ReadOnlyGatewayStore | None = None,
    backend_transport: httpx.AsyncBaseTransport | None = None,
    expected_identity_origin: str | None = None,
) -> FastAPI:
    """Create the strict companion-only ASGI app; no catch-all routes exist."""
    backend = urlsplit(backend_url)
    if backend.scheme != "http" or backend.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Gateway backend must be an HTTP loopback URL")
    app = FastAPI(
        title="Jarvis Companion Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.backend_url = backend_url
    app.state.backend_transport = backend_transport
    # The gateway is a second process. Constructing SupervisorStore here would
    # run restart recovery and pause work owned by the still-running API on 8000.
    app.state.supervisor_store = supervisor_store or ReadOnlyGatewayStore(get_config_dir() / "supervisor.db")
    configured_origin = expected_identity_origin if expected_identity_origin is not None else os.environ.get("JARVIS_TAILSCALE_ORIGIN", "")
    if configured_origin:
        try:
            canonical_origin = _allowed_origin(configured_origin)
        except HTTPException as exc:
            raise ValueError("JARVIS_TAILSCALE_ORIGIN must be a canonical HTTPS *.ts.net origin") from exc
        if canonical_origin != configured_origin:
            raise ValueError("JARVIS_TAILSCALE_ORIGIN must be canonical (no trailing slash or explicit :443)")
    else:
        canonical_origin = ""
    app.state.expected_identity_origin = canonical_origin

    @app.get("/v1/jarvis/tasks")
    async def list_tasks(request: Request, state: str = "active", limit: int = 100):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if state not in {"active", "completed", "all"} or not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="Invalid task query")
        return await _proxy(request, backend_path="/v1/jarvis/tasks", params={"state": state, "limit": str(limit)})

    @app.get("/v1/jarvis/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _ID_RE.fullmatch(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        return await _proxy(request, backend_path=f"/v1/jarvis/tasks/{task_id}")

    @app.get("/v1/jarvis/system/info")
    async def get_system_info(request: Request):
        """Return only the existing authenticated, read-only Mac status fields."""
        _device_id_for_request(request, request.app.state.supervisor_store)
        return await _proxy(request, backend_path="/v1/jarvis/system/info")

    @app.post("/v1/jarvis/link/tasks")
    async def submit_link_task(request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        body = await _read_json_body(request)
        if set(body) != {"objective", "capability_scope", "idempotency_key"}:
            raise HTTPException(status_code=422, detail="Unexpected Link task fields")
        objective = body.get("objective")
        scope = body.get("capability_scope")
        key = body.get("idempotency_key")
        if not isinstance(objective, str) or not objective.strip() or len(objective.encode("utf-8")) > 4096:
            raise HTTPException(status_code=422, detail="objective must be non-empty and under 4096 bytes")
        if scope not in (["read_only"], ["assistant"]):
            raise HTTPException(status_code=403, detail="Only read_only or supervisor-gated assistant scope is enabled")
        if not isinstance(key, str) or not _IDEMPOTENCY_RE.fullmatch(key):
            raise HTTPException(status_code=422, detail="Invalid idempotency key")
        raw = json.dumps(
            {"objective": objective.strip(), "capability_scope": scope, "idempotency_key": key},
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return await _proxy(request, backend_path="/v1/jarvis/link/tasks", body=raw)

    @app.get("/v1/jarvis/link/tasks/{idempotency_key}")
    async def get_link_task(idempotency_key: str, request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise HTTPException(status_code=404, detail="Link task not found")
        return await _proxy(request, backend_path=f"/v1/jarvis/link/tasks/{idempotency_key}")

    @app.post("/v1/jarvis/link/tasks/{idempotency_key}/cancel")
    async def cancel_link_task(idempotency_key: str, request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise HTTPException(status_code=404, detail="Link task not found")
        body = await _read_json_body(request)
        if body:
            raise HTTPException(status_code=422, detail="Unexpected cancellation fields")
        return await _proxy(
            request,
            backend_path=f"/v1/jarvis/link/tasks/{idempotency_key}/cancel",
            body=b"{}",
        )

    @app.get("/v1/jarvis/approvals/pending")
    async def list_pending_approvals(request: Request, limit: int = 100):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="Invalid approval limit")
        return await _proxy(request, backend_path="/v1/jarvis/approvals/pending", params={"limit": str(limit)})

    @app.get("/v1/jarvis/approvals/{approval_id}")
    async def get_approval(approval_id: str, request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _ID_RE.fullmatch(approval_id):
            raise HTTPException(status_code=404, detail="Approval not found")
        return await _proxy(request, backend_path=f"/v1/jarvis/approvals/{approval_id}")

    @app.post("/v1/jarvis/approvals/{approval_id}/decision")
    async def decide_approval(approval_id: str, request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _ID_RE.fullmatch(approval_id):
            raise HTTPException(status_code=404, detail="Approval not found")
        body = await _read_json_body(request)
        allowed = {"decision", "task_id", "expected_operation_digest", "idempotency_key", "continuation_objective"}
        if set(body) - allowed:
            raise HTTPException(status_code=422, detail="Unexpected decision field")
        if body.get("decision") not in {"approve", "reject"}:
            raise HTTPException(status_code=422, detail="decision must be approve or reject")
        if not isinstance(body.get("expected_operation_digest"), str) or not _DIGEST_RE.fullmatch(body["expected_operation_digest"]):
            raise HTTPException(status_code=422, detail="expected_operation_digest must be a SHA-256 hex digest")
        task_id = body.get("task_id")
        if task_id is not None and (not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id)):
            raise HTTPException(status_code=422, detail="task_id must be a task ID")
        continuation = body.get("continuation_objective")
        if continuation is not None and (
            not isinstance(continuation, str) or not continuation.strip() or len(continuation.encode("utf-8")) > 4096
        ):
            raise HTTPException(status_code=422, detail="continuation_objective is invalid")
        nonce = body.get("idempotency_key")
        if nonce is not None and (not isinstance(nonce, str) or not 1 <= len(nonce) <= 128 or "\n" in nonce or "\r" in nonce):
            raise HTTPException(status_code=422, detail="Invalid idempotency key")
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return await _proxy(request, backend_path=f"/v1/jarvis/approvals/{approval_id}/decision", body=raw)

    @app.post("/v1/jarvis/devices/fcm-token")
    async def register_fcm_token(request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        body = await _read_json_body(request)
        token = body.get("token")
        if set(body) != {"token"} or not isinstance(token, str) or not 20 <= len(token) <= 4096:
            raise HTTPException(status_code=422, detail="token is invalid")
        if any(ord(char) < 0x20 or ord(char) == 0x7f for char in token):
            raise HTTPException(status_code=422, detail="token is invalid")
        raw = json.dumps({"token": token}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return await _proxy(request, backend_path="/v1/jarvis/devices/fcm-token", body=raw)

    @app.delete("/v1/jarvis/devices/fcm-token")
    async def revoke_fcm_token(request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if request.headers.get("content-length", "0") not in {"", "0"}:
            raise HTTPException(status_code=400, detail="DELETE registration requests must not contain a body")
        return await _proxy(request, backend_path="/v1/jarvis/devices/fcm-token")

    @app.post("/v1/jarvis/devices/migration-bootstrap")
    async def migration_bootstrap(request: Request):
        device_id, bearer = _device_id_for_request(request, request.app.state.supervisor_store)
        body = await _read_json_body(request)
        if set(body) != {"nonce"} or not isinstance(body.get("nonce"), str) or not _NONCE_RE.fullmatch(body["nonce"]):
            raise HTTPException(status_code=422, detail="nonce must be a URL-safe 16–128 character challenge")
        private_key, public_der, fingerprint = _pairing_server_identity(request.app.state.supervisor_store)
        del private_key
        public_b64 = base64.b64encode(public_der).decode("ascii")
        message = f"openjarvis-migration-bootstrap:v1\n{body['nonce']}\n{device_id}\n{fingerprint}\n{public_b64}".encode("utf-8")
        mac_key = hashlib.sha256(bearer.encode("utf-8")).digest()
        mac = hmac.new(mac_key, message, hashlib.sha256).hexdigest()
        return {
            "device_id": device_id,
            "nonce": body["nonce"],
            "server_public_key": public_b64,
            "server_fingerprint": fingerprint,
            "proof_hmac": mac,
        }

    @app.post("/v1/jarvis/identity/challenge")
    async def identity_challenge(request: Request):
        expected_origin = request.app.state.expected_identity_origin
        if not expected_origin:
            raise HTTPException(status_code=503, detail="Tailnet identity origin is not configured")
        body = await _read_json_body(request)
        if set(body) != {"nonce", "target_origin"}:
            raise HTTPException(status_code=422, detail="nonce and target_origin are required")
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
            raise HTTPException(status_code=422, detail="Invalid nonce")
        target_origin = body.get("target_origin")
        if not isinstance(target_origin, str):
            raise HTTPException(status_code=422, detail="Invalid target_origin")
        origin = _allowed_origin(target_origin)
        if origin != target_origin or origin != expected_origin:
            raise HTTPException(status_code=403, detail="target_origin does not match the configured Mac Serve hostname")
        host_header = request.headers.get("host", "")
        try:
            host_parts = urlsplit("//" + host_header)
            host = (host_parts.hostname or "").lower()
            host_port = host_parts.port
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Request Host does not match the configured Mac hostname") from exc
        expected_host = urlsplit(expected_origin).hostname or ""
        if host != expected_host or host_port not in {None, 443} or host_parts.username or host_parts.password:
            raise HTTPException(status_code=403, detail="Request Host does not match the configured Mac hostname")
        private_key, public_der, fingerprint = _pairing_server_identity(request.app.state.supervisor_store)
        public_b64 = base64.b64encode(public_der).decode("ascii")
        payload = {
            "protocol": "openjarvis-identity-challenge-v1",
            "nonce": nonce,
            "target_origin": origin,
            "server_public_key": public_b64,
            "server_fingerprint": fingerprint,
        }
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        signature = private_key.sign(_canonical_pairing_payload(payload), ec.ECDSA(hashes.SHA256()))
        payload["signature"] = base64.b64encode(signature).decode("ascii")
        return payload

    return app


def main() -> None:
    """Run the opt-in gateway locally; no service or tunnel is configured."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the loopback-only Jarvis Companion Gateway")
    parser.add_argument("--host", default=GATEWAY_HOST)
    parser.add_argument("--port", type=int, default=GATEWAY_PORT)
    args = parser.parse_args()
    host = validate_bind_host(args.host)
    if args.port != GATEWAY_PORT:
        parser.error(f"The companion gateway uses fixed port {GATEWAY_PORT}")
    uvicorn.run(create_gateway_app(), host=host, port=GATEWAY_PORT, log_level="info")


if __name__ == "__main__":
    main()
