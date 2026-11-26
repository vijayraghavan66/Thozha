"""Allowlisted authenticated gateway for the Android companion.

The raw OpenJarvis API stays on loopback. Separate runners may expose this
narrow API over an explicitly configured private transport; binding policy and
TLS are enforced by those runners, never by a catch-all proxy route.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
_TASK_EVENTS_SUBPROTOCOL = "thozha.task-events.v1"
_TASK_EVENTS_MAX_CONNECTIONS = 8
_TASK_EVENTS_MAX_UNACKED = 32
_TASK_EVENTS_ACK_TIMEOUT_SECONDS = 15.0
_TASK_EVENTS_POLL_SECONDS = 0.25
_TASK_EVENTS_REAUTH_SECONDS = 10.0
_TASK_EVENTS_HEARTBEAT_SECONDS = 15.0


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


async def _read_json_body(request: Request, *, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="Request body is too large")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Expected a JSON object") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    return value


async def _proxy_invitation(
    request: Request,
    *,
    backend_path: str,
    method: str,
    body: bytes | None = None,
) -> Response:
    """Forward only one invitation-scoped bootstrap request without device credentials."""
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(
            base_url=request.app.state.backend_url,
            transport=request.app.state.backend_transport,
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            upstream = await client.request(method, backend_path, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Local pairing service is unavailable") from exc
    content_type = upstream.headers.get("content-type", "application/json")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={"Content-Type": content_type, "Cache-Control": "no-store"},
    )


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
    # Preserve only the content-free trace token used for end-to-end timing.
    trace_id = request.headers.get("x-jarvis-trace-id", "")
    if re.fullmatch(r"[0-9a-f]{12}", trace_id):
        headers["X-Jarvis-Trace-ID"] = trace_id
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
    safe_headers = {"Content-Type": content_type, "Cache-Control": "no-store"}
    for name in ("Content-Disposition", "ETag", "X-Content-SHA256"):
        value = upstream.headers.get(name)
        if value is not None and len(value) <= 256 and all(ord(char) >= 0x20 and ord(char) != 0x7f for char in value):
            safe_headers[name] = value
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=safe_headers,
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
    app.state.task_event_ws_slots = asyncio.Semaphore(_TASK_EVENTS_MAX_CONNECTIONS)
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

    @app.post("/v1/jarvis/pair/{invitation_id}/complete")
    async def complete_invitation_pairing(invitation_id: str, request: Request):
        """Complete only a QR-scoped device proof; never accepts a token or arbitrary route."""
        if request.query_params:
            raise HTTPException(status_code=422, detail="Unexpected pairing query parameters")
        if not _ID_RE.fullmatch(invitation_id):
            raise HTTPException(status_code=404, detail="Pairing invitation not found")
        body = await _read_json_body(request, max_bytes=2048)
        if set(body) != {"device_name", "public_key", "signature"}:
            raise HTTPException(status_code=422, detail="Unexpected pairing completion fields")
        device_name, public_key, signature = body.get("device_name"), body.get("public_key"), body.get("signature")
        if (
            not isinstance(device_name, str) or not device_name.strip() or len(device_name) > 80
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in device_name)
            or not isinstance(public_key, str) or not 80 <= len(public_key) <= 256
            or not isinstance(signature, str) or not 40 <= len(signature) <= 1024
        ):
            raise HTTPException(status_code=422, detail="Pairing proof fields are invalid")
        forwarded = json.dumps(
            {"invitation_id": invitation_id, "device_name": device_name, "public_key": public_key, "signature": signature},
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return await _proxy_invitation(
            request, backend_path="/v1/jarvis/pair/complete", method="POST", body=forwarded,
        )

    @app.get("/v1/jarvis/pair/{invitation_id}/status")
    async def get_invitation_pairing_status(invitation_id: str, request: Request):
        """Expose only the lifecycle of the invitation named by the QR."""
        if request.query_params:
            raise HTTPException(status_code=422, detail="Unexpected pairing query parameters")
        if not _ID_RE.fullmatch(invitation_id):
            raise HTTPException(status_code=404, detail="Pairing invitation not found")
        return await _proxy_invitation(
            request, backend_path=f"/v1/jarvis/pair/status/{invitation_id}", method="GET",
        )

    @app.post("/v1/jarvis/pair/{invitation_id}/claim")
    async def claim_invitation_pairing(invitation_id: str, request: Request):
        """Issue a paired-device token only after the store's approved one-time proof."""
        if request.query_params:
            raise HTTPException(status_code=422, detail="Unexpected pairing query parameters")
        if not _ID_RE.fullmatch(invitation_id):
            raise HTTPException(status_code=404, detail="Pairing invitation not found")
        body = await _read_json_body(request, max_bytes=1024)
        if set(body) != {"signature"} or not isinstance(body.get("signature"), str) or not 40 <= len(body["signature"]) <= 1024:
            raise HTTPException(status_code=422, detail="Pairing claim signature is invalid")
        forwarded = json.dumps(
            {"invitation_id": invitation_id, "signature": body["signature"]},
            separators=(",", ":"),
        ).encode("utf-8")
        return await _proxy_invitation(
            request, backend_path="/v1/jarvis/pair/claim", method="POST", body=forwarded,
        )

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

    @app.get("/v1/jarvis/tasks/{task_id}/artifacts")
    async def list_task_artifacts(task_id: str, request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _ID_RE.fullmatch(task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        return await _proxy(request, backend_path=f"/v1/jarvis/tasks/{task_id}/artifacts")

    @app.get("/v1/jarvis/tasks/{task_id}/artifacts/{artifact_id}/download")
    async def download_task_artifact(task_id: str, artifact_id: str, request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _ID_RE.fullmatch(task_id) or not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
            raise HTTPException(status_code=404, detail="Artifact not found")
        return await _proxy(
            request,
            backend_path=f"/v1/jarvis/tasks/{task_id}/artifacts/{artifact_id}/download",
        )

    @app.get("/v1/jarvis/system/info")
    async def get_system_info(request: Request):
        """Return only the existing authenticated, read-only Mac status fields."""
        _device_id_for_request(request, request.app.state.supervisor_store)
        return await _proxy(request, backend_path="/v1/jarvis/system/info")

    @app.post("/v1/jarvis/link/tasks")
    async def submit_link_task(request: Request, mode: str = ""):
        authenticated_device_id, _ = _device_id_for_request(request, request.app.state.supervisor_store)
        if set(request.query_params.keys()) - {"mode"} or len(request.query_params.getlist("mode")) > 1:
            raise HTTPException(status_code=422, detail="Only one mode query parameter is supported")
        if mode not in {"", "async"}:
            raise HTTPException(status_code=422, detail="mode must be async when supplied")
        body = await _read_json_body(request)
        required_fields = {"objective", "capability_scope", "idempotency_key", "requester_device_id"}
        optional_fields = {"conversation_id"}
        if not required_fields.issubset(body) or set(body) - required_fields - optional_fields:
            raise HTTPException(status_code=422, detail="Unexpected Link task fields")
        objective = body.get("objective")
        scope = body.get("capability_scope")
        key = body.get("idempotency_key")
        requester_device_id = body.get("requester_device_id")
        conversation_id = body.get("conversation_id")
        if conversation_id is not None and (
            not isinstance(conversation_id, str) or not _ID_RE.fullmatch(conversation_id)
        ):
            raise HTTPException(status_code=422, detail="Invalid conversation ID")
        if not isinstance(objective, str) or not objective.strip() or len(objective.encode("utf-8")) > 4096:
            raise HTTPException(status_code=422, detail="objective must be non-empty and under 4096 bytes")
        if scope not in (["read_only"], ["assistant"], ["development"]):
            raise HTTPException(
                status_code=403,
                detail="Only read_only, supervisor-gated assistant, or plan-gated development scope is enabled",
            )
        if not isinstance(key, str) or not _IDEMPOTENCY_RE.fullmatch(key):
            raise HTTPException(status_code=422, detail="Invalid idempotency key")
        if not isinstance(requester_device_id, str) or not _ID_RE.fullmatch(requester_device_id):
            raise HTTPException(status_code=422, detail="Invalid requester device ID")
        if requester_device_id != authenticated_device_id:
            raise HTTPException(status_code=403, detail="Authenticated device does not match the Link task requester")
        forwarded_body = {
            "objective": objective.strip(),
            "capability_scope": scope,
            "idempotency_key": key,
            "requester_device_id": requester_device_id,
        }
        if conversation_id is not None:
            forwarded_body["conversation_id"] = conversation_id
        raw = json.dumps(
            forwarded_body,
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        params = {"mode": "async"} if mode == "async" else None
        return await _proxy(request, backend_path="/v1/jarvis/link/tasks", params=params, body=raw)

    @app.get("/v1/jarvis/link/tasks/{idempotency_key}")
    async def get_link_task(idempotency_key: str, request: Request):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise HTTPException(status_code=404, detail="Link task not found")
        return await _proxy(request, backend_path=f"/v1/jarvis/link/tasks/{idempotency_key}")

    @app.get("/v1/jarvis/link/tasks/{idempotency_key}/events")
    async def get_link_task_events(
        idempotency_key: str,
        request: Request,
        after_seq: int = 0,
        limit: int = 100,
    ):
        _device_id_for_request(request, request.app.state.supervisor_store)
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise HTTPException(status_code=404, detail="Link task not found")
        if after_seq < 0 or not 1 <= limit <= 200:
            raise HTTPException(status_code=422, detail="Invalid task event cursor")
        return await _proxy(
            request,
            backend_path=f"/v1/jarvis/link/tasks/{idempotency_key}/events",
            params={"after_seq": str(after_seq), "limit": str(limit)},
        )

    @app.websocket("/v1/jarvis/link/tasks/{idempotency_key}/events/ws")
    async def stream_link_task_events(websocket: WebSocket, idempotency_key: str):
        """Stream only owner-authorized, persisted task events over pinned TLS.

        The TLS frontend is a byte-for-byte proxy to this loopback-only ASGI
        app, so WebSocket Upgrade does not create a second listener or raw API
        path. The paired-device bearer remains an Authorization header and is
        revalidated against the read-only device table every ten seconds.
        """
        query = websocket.query_params
        if (
            not _IDEMPOTENCY_RE.fullmatch(idempotency_key)
            or set(query.keys()) - {"after_seq"}
            or len(query.getlist("after_seq")) > 1
        ):
            await websocket.close(code=1008)
            return
        raw_cursor = query.get("after_seq", "0")
        if not re.fullmatch(r"0|[1-9][0-9]{0,18}", raw_cursor):
            await websocket.close(code=1008)
            return
        cursor = int(raw_cursor)
        if cursor > 9_223_372_036_854_775_807:
            await websocket.close(code=1008)
            return
        if _TASK_EVENTS_SUBPROTOCOL not in websocket.scope.get("subprotocols", []):
            await websocket.close(code=1008)
            return
        try:
            device_id, _ = _device_id_for_request(websocket, websocket.app.state.supervisor_store)
        except HTTPException:
            await websocket.close(code=1008)
            return

        slots: asyncio.Semaphore = websocket.app.state.task_event_ws_slots
        try:
            await asyncio.wait_for(slots.acquire(), timeout=0.05)
        except TimeoutError:
            await websocket.close(code=1013)
            return

        authorization = websocket.headers.get("authorization", "")
        try:
            async with httpx.AsyncClient(
                base_url=websocket.app.state.backend_url,
                transport=websocket.app.state.backend_transport,
                timeout=3.0,
                follow_redirects=False,
                trust_env=False,
            ) as preflight:
                initial_response = await preflight.get(
                    f"/v1/jarvis/link/tasks/{idempotency_key}/events",
                    params={"after_seq": str(cursor), "limit": "100"},
                    headers={"Authorization": authorization, "Accept": "application/json"},
                )
        except httpx.HTTPError:
            slots.release()
            await websocket.close(code=1011)
            return
        if initial_response.status_code in {401, 403, 404}:
            slots.release()
            await websocket.close(code=1008)
            return
        if initial_response.status_code != 200:
            slots.release()
            await websocket.close(code=1011)
            return
        try:
            initial_page = initial_response.json()
        except ValueError:
            slots.release()
            await websocket.close(code=1011)
            return
        if (
            not isinstance(initial_page, dict)
            or not isinstance(initial_page.get("events"), list)
            or not isinstance(initial_page.get("task_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", initial_page["task_id"])
            or isinstance(initial_page.get("next_seq"), bool)
            or not isinstance(initial_page.get("next_seq"), int)
            or initial_page["next_seq"] < cursor
        ):
            slots.release()
            await websocket.close(code=1011)
            return

        last_scanned = cursor
        highest_sent = cursor
        last_acked = cursor
        unacked: list[int] = []
        last_ack_at = time.monotonic()
        next_poll = 0.0
        pending_page: dict[str, Any] | None = initial_page
        resolved_task_id = initial_page["task_id"]
        next_reauth = time.monotonic() + _TASK_EVENTS_REAUTH_SECONDS
        next_heartbeat = time.monotonic() + _TASK_EVENTS_HEARTBEAT_SECONDS
        try:
            await websocket.accept(subprotocol=_TASK_EVENTS_SUBPROTOCOL)
            await websocket.send_json({
                "type": "ready",
                "protocol": 1,
                "after_seq": cursor,
                "latest_seq": initial_page["next_seq"],
            })
            async with httpx.AsyncClient(
                base_url=websocket.app.state.backend_url,
                transport=websocket.app.state.backend_transport,
                timeout=3.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                while True:
                    now = time.monotonic()
                    if now >= next_reauth:
                        try:
                            current_id, _ = _device_id_for_request(
                                websocket, websocket.app.state.supervisor_store,
                            )
                        except HTTPException:
                            await websocket.close(code=1008)
                            return
                        if current_id != device_id:
                            await websocket.close(code=1008)
                            return
                        next_reauth = now + _TASK_EVENTS_REAUTH_SECONDS

                    if unacked and now - last_ack_at > _TASK_EVENTS_ACK_TIMEOUT_SECONDS:
                        await websocket.close(code=1013)
                        return

                    # Process one small ACK/control frame at a time. The cursor
                    # is cumulative so a persisted high-water mark can cover
                    # event-sequence gaps caused by intentionally filtered rows.
                    try:
                        incoming = await asyncio.wait_for(websocket.receive(), timeout=0.025)
                    except TimeoutError:
                        incoming = None
                    except WebSocketDisconnect:
                        return
                    if incoming is not None:
                        if incoming.get("type") == "websocket.disconnect":
                            return
                        raw = incoming.get("text")
                        if incoming.get("type") != "websocket.receive" or not isinstance(raw, str):
                            await websocket.close(code=1008)
                            return
                        if len(raw.encode("utf-8")) > 256:
                            await websocket.close(code=1008)
                            return
                        try:
                            control = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            await websocket.close(code=1008)
                            return
                        if (
                            not isinstance(control, dict)
                            or set(control) != {"type", "protocol", "task_id", "seq"}
                            or control.get("type") != "ack"
                            or control.get("protocol") != 1
                            or control.get("task_id") != resolved_task_id
                        ):
                            await websocket.close(code=1008)
                            return
                        acknowledged = control.get("seq")
                        if (
                            isinstance(acknowledged, bool)
                            or not isinstance(acknowledged, int)
                            or acknowledged < last_acked
                            or acknowledged > highest_sent
                            or (acknowledged > last_acked and acknowledged not in unacked)
                        ):
                            await websocket.close(code=1008)
                            return
                        if acknowledged > last_acked:
                            last_acked = acknowledged
                            unacked = [seq for seq in unacked if seq > acknowledged]
                            last_ack_at = now

                    if now >= next_heartbeat:
                        await websocket.send_json({"type": "heartbeat", "protocol": 1})
                        next_heartbeat = now + _TASK_EVENTS_HEARTBEAT_SECONDS

                    if now < next_poll or len(unacked) >= _TASK_EVENTS_MAX_UNACKED:
                        await asyncio.sleep(0.025)
                        continue

                    if pending_page is not None:
                        page = pending_page
                        pending_page = None
                    else:
                        try:
                            upstream = await client.get(
                                f"/v1/jarvis/link/tasks/{idempotency_key}/events",
                                params={"after_seq": str(last_scanned), "limit": "100"},
                                headers={"Authorization": authorization, "Accept": "application/json"},
                            )
                        except httpx.HTTPError:
                            await websocket.close(code=1011)
                            return
                        if upstream.status_code in {401, 403, 404}:
                            await websocket.close(code=1008)
                            return
                        if upstream.status_code != 200:
                            await websocket.close(code=1011)
                            return
                        try:
                            page = upstream.json()
                        except ValueError:
                            await websocket.close(code=1011)
                            return
                    if (
                        not isinstance(page, dict)
                        or not isinstance(page.get("events"), list)
                        or page.get("task_id") != resolved_task_id
                        or isinstance(page.get("next_seq"), bool)
                        or not isinstance(page.get("next_seq"), int)
                        or page["next_seq"] < last_scanned
                    ):
                        await websocket.close(code=1011)
                        return
                    events = page["events"]
                    for event in events:
                        if len(unacked) >= _TASK_EVENTS_MAX_UNACKED:
                            break
                        if not isinstance(event, dict):
                            await websocket.close(code=1011)
                            return
                        sequence = event.get("seq")
                        if (
                            isinstance(sequence, bool)
                            or not isinstance(sequence, int)
                            or sequence <= last_scanned
                            or sequence > page["next_seq"]
                            or not isinstance(event.get("event_type"), str)
                            or not isinstance(event.get("created_at"), str)
                            or not isinstance(event.get("payload"), dict)
                        ):
                            await websocket.close(code=1011)
                            return
                        await websocket.send_json({
                            "type": "event",
                            "protocol": 1,
                            "task_id": resolved_task_id,
                            "event": {
                                "seq": sequence,
                                "event_type": event["event_type"],
                                "created_at": event["created_at"],
                                "payload": event["payload"],
                            },
                        })
                        if not unacked:
                            last_ack_at = time.monotonic()
                        last_scanned = sequence
                        highest_sent = sequence
                        unacked.append(sequence)

                    # GET /events intentionally filters private event types. A
                    # checkpoint advances over those persisted rows without
                    # exposing them or replaying them forever.
                    scanned = page["next_seq"]
                    if scanned > last_scanned and len(unacked) < _TASK_EVENTS_MAX_UNACKED:
                        if not unacked:
                            last_ack_at = time.monotonic()
                        await websocket.send_json({
                            "type": "checkpoint",
                            "protocol": 1,
                            "task_id": resolved_task_id,
                            "seq": scanned,
                        })
                        last_scanned = scanned
                        highest_sent = scanned
                        unacked.append(scanned)
                    next_poll = time.monotonic() + _TASK_EVENTS_POLL_SECONDS
        finally:
            slots.release()

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
