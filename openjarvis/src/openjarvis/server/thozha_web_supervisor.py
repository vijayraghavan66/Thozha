"""Browser-authenticated, paired-owner scoped supervisor APIs.

These routes do not treat a Web session as a device credential. A deployment
must explicitly map the configured Web owner to active paired-device IDs using
``THOZHA_WEB_ALLOWED_DEVICE_IDS``. Every request rechecks that pairing state;
task ownership remains the existing ``device:<id>`` actor used by Android.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from openjarvis.server.supervisor_routes import (
    _approval_payload,
    _artifact_store,
    _decide_approval_for_actor,
    get_supervisor_store,
)
from openjarvis.tasks.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStoreError,
)
from openjarvis.tasks.supervisor import LINK_SCOPE_CAPABILITIES, is_link_scope

_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_.-]{0,79}$")
_EVENT_PAYLOAD_KEYS = frozenset(
    {
        "from",
        "to",
        "state",
        "step",
        "approval_id",
        "operation",
        "target",
        "reason",
        "error_code",
        "result_summary",
        "tests_ran",
        "failures",
        "errors",
        "bytes_written",
        "manifest_sha256",
        "artifact_sha256",
    }
)


class WebTaskCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    capability_scope: list[str] = Field(min_length=1, max_length=1)
    idempotency_key: str = Field(
        min_length=16, max_length=128, pattern=_IDEMPOTENCY_RE.pattern
    )
    requester_device_id: str = Field(pattern=r"^[0-9a-f]{32}$")

    model_config = ConfigDict(extra="forbid")


class WebApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    task_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    expected_operation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(
        min_length=16, max_length=128, pattern=_IDEMPOTENCY_RE.pattern
    )
    requester_device_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    continuation_objective: str | None = Field(default=None, max_length=4096)

    model_config = ConfigDict(extra="forbid")


def _principal_user_id(principal: Any) -> str:
    user_id = (
        principal.get("user_id")
        if isinstance(principal, dict)
        else getattr(principal, "user_id", None)
    )
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=401, detail="Invalid Web session principal")
    return user_id


def resolve_web_device_ids(
    principal: Any,
    supervisor_store: Any,
    *,
    environ: dict[str, str] | None = None,
) -> set[str]:
    """Return only explicitly mapped, currently active paired device IDs.

    The single-owner Web auth configuration is intentional for v1. A caller
    cannot set this mapping in a request body or header.
    """
    env = os.environ if environ is None else environ
    ids = _mapped_web_device_ids(principal, environ=env)

    db_path = Path(str(getattr(supervisor_store, "db_path", ""))).expanduser()
    if not db_path.is_file():
        raise HTTPException(
            status_code=503, detail="Paired-device store is unavailable"
        )
    now = datetime.now(timezone.utc)
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as db:
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(
                "SELECT id, expires_at, revoked_at FROM paired_devices "
                f"WHERE id IN ({placeholders})",
                sorted(ids),
            ).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503, detail="Paired-device store is unavailable"
        ) from exc

    active: set[str] = set()
    for device_id, expires_at, revoked_at in rows:
        if revoked_at is not None:
            continue
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                continue
        except (TypeError, ValueError):
            continue
        if expiry > now:
            active.add(str(device_id))
    if not active:
        raise HTTPException(
            status_code=403,
            detail="This Web owner has no active paired-device mapping",
        )
    return active


def _mapped_web_device_ids(
    principal: Any, *, environ: dict[str, str] | None = None
) -> set[str]:
    env = os.environ if environ is None else environ
    owner_id = _principal_user_id(principal)
    configured_owner = env.get("THOZHA_WEB_OWNER_ID", "").strip()
    if not configured_owner or owner_id != configured_owner:
        raise HTTPException(
            status_code=403,
            detail="Web session owner is not mapped to paired devices",
        )
    raw = env.get("THOZHA_WEB_ALLOWED_DEVICE_IDS", "")
    if not raw.strip():
        raise HTTPException(
            status_code=403,
            detail="This Web owner has no paired-device supervisor mapping",
        )
    ids = [item.strip() for item in raw.split(",")]
    if any(not _DEVICE_ID_RE.fullmatch(item) for item in ids) or len(set(ids)) != len(
        ids
    ):
        raise HTTPException(
            status_code=503,
            detail="Configured Web paired-device mapping is invalid",
        )
    return set(ids)


def is_web_device_mapped(principal: Any, device_id: str) -> bool:
    """Check explicit mapping membership, even after revocation, for retries.

    Use only for idempotent revoke/cleanup reconciliation. Task, approval and
    device data routes must call ``resolve_web_device_ids`` instead.
    """
    if not _DEVICE_ID_RE.fullmatch(device_id):
        return False
    try:
        return device_id in _mapped_web_device_ids(principal)
    except HTTPException:
        return False


def is_web_device_active(user_id: str, device_id: str, supervisor_store: Any) -> bool:
    """Check the owner mapping and live paired-device state on every request."""
    if (
        not isinstance(user_id, str)
        or not user_id
        or not _DEVICE_ID_RE.fullmatch(device_id)
    ):
        return False
    configured_owner = os.environ.get("THOZHA_WEB_OWNER_ID", "").strip()
    if user_id != configured_owner:
        return False
    try:
        return device_id in resolve_web_device_ids(
            {"user_id": user_id}, supervisor_store
        )
    except HTTPException:
        return False


def _task_metadata(store: Any, task_id: str) -> tuple[dict[str, Any], str]:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        metadata = json.loads(task.get("recovery_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Task not found") from None
    owner_actor = metadata.get("owner_actor")
    if (
        metadata.get("source") != "jarvis_link"
        or not isinstance(owner_actor, str)
        or not owner_actor.startswith("device:")
        or not _DEVICE_ID_RE.fullmatch(owner_actor.removeprefix("device:"))
    ):
        raise HTTPException(status_code=404, detail="Task not found")
    return task, owner_actor


def _require_web_task_owner(
    store: Any, task_id: str, allowed_devices: frozenset[str]
) -> tuple[dict[str, Any], str]:
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    task, actor = _task_metadata(store, task_id)
    if actor.removeprefix("device:") not in allowed_devices:
        raise HTTPException(status_code=404, detail="Task not found")
    return task, actor


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    safe_payload = (
        {
            key: value
            for key, value in payload.items()
            if key in _EVENT_PAYLOAD_KEYS
            and (
                isinstance(value, (bool, int, float))
                or isinstance(value, str)
                and len(value) <= 4000
            )
        }
        if isinstance(payload, dict)
        else {}
    )
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}", event_type
    ):
        event_type = "event"
    return {
        "seq": event.get("seq"),
        "event_type": event_type,
        "created_at": event.get("created_at"),
        "payload": safe_payload,
    }


def _task_result(
    store: Any, task: dict[str, Any], *, replay: bool = False
) -> dict[str, Any]:
    task_id = task["id"]
    run = store.link_task_run(task_id) or {}
    return {
        "task_id": task_id,
        "state": task.get("state"),
        "summary": str(run.get("result_summary") or ""),
        "result_text": run.get("result_text")
        if task.get("state") == "COMPLETED"
        else None,
        "result_digest": run.get("result_digest")
        if task.get("state") == "COMPLETED"
        else None,
        "idempotent_replay": replay,
        "error_code": (
            run.get("last_error")
            if task.get("state") == "FAILED"
            and isinstance(run.get("last_error"), str)
            and _ERROR_CODE_RE.fullmatch(run["last_error"])
            else "task_failed"
            if task.get("state") == "FAILED"
            else None
        ),
    }


def _require_owned_completed_development_task(
    store: Any, task_id: str, device_id: str
) -> None:
    task, owner_actor = _task_metadata(store, task_id)
    if owner_actor != "device:" + device_id:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        capabilities = json.loads(task.get("capabilities_json") or "[]")
        metadata = json.loads(task.get("recovery_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Task not found") from None
    if (
        task.get("state") != "COMPLETED"
        or metadata.get("scope") != "development"
        or metadata.get("capability_scope") != ["development"]
        or capabilities != sorted(LINK_SCOPE_CAPABILITIES["development"])
        or (store.link_task_run(task_id) or {}).get("status") != "COMPLETED"
    ):
        raise HTTPException(status_code=409, detail="Task artifacts are not ready")


def create_web_supervisor_router(
    require_web_session: Callable[..., Any],
    *,
    supervisor_store_provider: Callable[[Request], Any] | None = None,
) -> APIRouter:
    """Create task/approval/artifact routes gated by Web session and device map."""
    if require_web_session is None:
        raise ValueError("require_web_session dependency is required")
    router = APIRouter(prefix="/v1/web", tags=["web-supervisor"])
    stream_slots = asyncio.Semaphore(8)

    def store_for(request: Request):
        if supervisor_store_provider is not None:
            store = supervisor_store_provider(request)
        else:
            store = getattr(request.app.state, "supervisor_store", None)
        if store is None:
            store = get_supervisor_store()
        return store

    def devices_for(request: Request, principal: Any, store: Any) -> frozenset[str]:
        context = getattr(request.app.state, "thozha_web_auth", None)
        config = getattr(context, "config", None)
        if config is None or _principal_user_id(principal) != config.owner_id:
            raise HTTPException(status_code=401, detail="Web owner is not authorized")
        return resolve_web_device_ids(principal, store)

    @router.post("/tasks")
    async def submit_task(
        body: WebTaskCreate,
        request: Request,
        response: Response,
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        if body.requester_device_id not in allowed:
            raise HTTPException(
                status_code=403,
                detail="Requester device is not mapped to this Web owner",
            )
        if not body.objective.strip():
            raise HTTPException(status_code=422, detail="objective must not be blank")
        if not is_link_scope(body.capability_scope):
            raise HTTPException(
                status_code=403, detail="Requested task scope is not supported"
            )
        runner = getattr(request.app.state, "link_task_runner", None)
        if runner is None:
            raise HTTPException(
                status_code=503, detail="Supervisor task worker is unavailable"
            )
        try:
            run = store.create_link_task(
                body.objective,
                capability_scope=body.capability_scope,
                idempotency_key=body.idempotency_key,
                owner_actor="device:" + body.requester_device_id,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=403, detail="Task belongs to another paired device"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409, detail="Task idempotency or scope conflict"
            ) from exc
        if not run.get("created"):
            status = run.get("status")
            if status in {"FAILED", "REJECTED", "CANCELLED"}:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This idempotent task already reached a terminal "
                        "failure or cancellation state"
                    ),
                )
        if run.get("created"):
            runner.wake()
        task = store.get_task(run["task_id"])
        if task is None:
            raise HTTPException(
                status_code=503, detail="Supervisor task record was not created"
            )
        result = _task_result(store, task, replay=not bool(run.get("created")))
        if not run.get("created") and run.get("status") == "COMPLETED":
            return JSONResponse(status_code=200, content=result)
        return JSONResponse(status_code=202, content=result)

    @router.get("/tasks")
    async def list_tasks(
        request: Request,
        response: Response,
        state: str = Query(default="active", pattern="^(active|completed|all)$"),
        limit: int = Query(default=50, ge=1, le=100),
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        active_states = {
            "CREATED",
            "PLANNING",
            "RUNNING",
            "VERIFYING",
            "WAITING_APPROVAL",
            "PAUSED",
        }
        completed_states = {"COMPLETED", "FAILED", "CANCELLED"}
        items: list[dict[str, Any]] = []
        for task in store.recent_tasks(min(limit * 8, 500)):
            task_id = task.get("id")
            if not isinstance(task_id, str):
                continue
            try:
                _, actor = _task_metadata(store, task_id)
            except HTTPException:
                continue
            if actor.removeprefix("device:") not in allowed:
                continue
            if state == "active" and task.get("state") not in active_states:
                continue
            if state == "completed" and task.get("state") not in completed_states:
                continue
            items.append(_task_result(store, task))
            if len(items) >= limit:
                break
        return {"tasks": items}

    @router.get("/tasks/{task_id}")
    async def get_task(
        task_id: str,
        request: Request,
        response: Response,
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        task, _ = _require_web_task_owner(store, task_id, allowed)
        return _task_result(store, task)

    @router.get("/tasks/{task_id}/events")
    async def get_task_events(
        task_id: str,
        request: Request,
        response: Response,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        _require_web_task_owner(store, task_id, allowed)
        events = store.events_after(task_id, after_seq, limit)
        next_seq = events[-1]["seq"] if events else after_seq
        return {
            "task_id": task_id,
            "events": [_safe_event(item) for item in events],
            "next_seq": next_seq,
        }

    @router.get("/tasks/{task_id}/events/stream")
    async def stream_task_events(
        task_id: str,
        request: Request,
        after_seq: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        principal=Depends(require_web_session),
    ) -> StreamingResponse:
        """Replay persisted task events via bounded SSE (not model token stream)."""
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        _, task_actor = _require_web_task_owner(store, task_id, allowed)
        task_device_id = task_actor.removeprefix("device:")
        cursor = after_seq
        if last_event_id is not None:
            if not re.fullmatch(r"[0-9]{1,18}", last_event_id):
                raise HTTPException(
                    status_code=422, detail="Last-Event-ID must be a sequence number"
                )
            cursor = max(cursor, int(last_event_id))
        try:
            await asyncio.wait_for(stream_slots.acquire(), timeout=0.2)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503, detail="Task event streams are at capacity"
            ) from exc

        async def events():
            nonlocal cursor
            idle = 0
            next_identity_check = time.monotonic() + 10
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    if time.monotonic() >= next_identity_check:
                        try:
                            current_devices = devices_for(request, principal, store)
                        except HTTPException:
                            return
                        if task_device_id not in current_devices:
                            return
                        next_identity_check = time.monotonic() + 10
                    page = store.events_after(task_id, cursor, 100)
                    for item in page:
                        safe = _safe_event(item)
                        cursor = int(item["seq"])
                        payload = json.dumps(
                            safe, separators=(",", ":"), ensure_ascii=False
                        )
                        yield (
                            f"id: {cursor}\n"
                            f"event: {safe['event_type']}\n"
                            "data: "
                            f"{payload}\n\n"
                        )
                    if page:
                        idle = 0
                    else:
                        task = store.get_task(task_id) or {}
                        if task.get("state") in {"COMPLETED", "FAILED", "CANCELLED"}:
                            return
                        idle += 1
                        if idle % 15 == 0:
                            yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)
            finally:
                release_slot()

        slot_released = False

        def release_slot() -> None:
            nonlocal slot_released
            if not slot_released:
                slot_released = True
                stream_slots.release()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
            background=BackgroundTask(release_slot),
        )

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(
        task_id: str,
        request: Request,
        response: Response,
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        _, actor = _require_web_task_owner(store, task_id, allowed)
        try:
            result = store.cancel_link_task(task_id, actor=actor)
        except PermissionError as exc:
            raise HTTPException(
                status_code=403, detail="Task belongs to another paired device"
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=409, detail="Task can no longer be cancelled"
            ) from exc
        return {"task_id": task_id, **result}

    @router.get("/approvals/pending")
    async def list_pending_approvals(
        request: Request,
        response: Response,
        limit: int = Query(default=100, ge=1, le=100),
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        approvals = []
        for item in store.pending_approvals(min(limit * 4, 400)):
            try:
                _require_web_task_owner(store, item["task_id"], allowed)
            except HTTPException:
                continue
            approvals.append(_approval_payload(item))
            if len(approvals) >= limit:
                break
        return {"approvals": approvals, "count": len(approvals)}

    @router.get("/approvals/{approval_id}")
    async def get_approval(
        approval_id: str,
        request: Request,
        response: Response,
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        item = store.approval(approval_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        _require_web_task_owner(store, item["task_id"], allowed)
        return _approval_payload(item)

    @router.post("/approvals/{approval_id}/decision")
    async def decide_approval(
        approval_id: str,
        request: Request,
        body: WebApprovalDecision,
        response: Response,
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        if body.requester_device_id not in allowed:
            raise HTTPException(
                status_code=403,
                detail="Requester device is not mapped to this Web owner",
            )
        approval = store.approval(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        _, owner_actor = _require_web_task_owner(store, approval["task_id"], allowed)
        if owner_actor != "device:" + body.requester_device_id:
            raise HTTPException(
                status_code=403, detail="Approval belongs to another paired device"
            )
        # Recheck immediately before entering the shared consume/dispatch path.
        if body.requester_device_id not in devices_for(request, principal, store):
            raise HTTPException(
                status_code=403,
                detail="Requester device is no longer active for this Web owner",
            )
        decision_body = {
            "decision": body.decision,
            "task_id": body.task_id or approval["task_id"],
            "expected_operation_digest": body.expected_operation_digest,
            "idempotency_key": body.idempotency_key,
            "requester_device_id": body.requester_device_id,
            "continuation_objective": body.continuation_objective,
        }
        try:
            # Construct a minimal request with the validated JSON payload. The
            # shared decision helper performs all digest, expiry, idempotency,
            # dispatch-claim, executor and continuation checks.
            return await _decide_approval_for_actor(
                approval_id, request, owner_actor, decision_body=decision_body
            )
        except HTTPException:
            raise

    @router.get("/tasks/{task_id}/artifacts")
    async def list_artifacts(
        task_id: str,
        request: Request,
        response: Response,
        principal=Depends(require_web_session),
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        _, actor = _require_web_task_owner(store, task_id, allowed)
        device_id = actor.removeprefix("device:")
        try:
            _require_owned_completed_development_task(store, task_id, device_id)
            artifact_store = _artifact_store()
            artifacts = artifact_store.list_for_task(task_id, device_id)
        except HTTPException:
            raise
        except ArtifactStoreError as exc:
            raise HTTPException(
                status_code=503, detail="Task artifacts are unavailable"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Task artifacts are unavailable"
            ) from exc
        return {
            "task_id": task_id,
            "artifacts": [
                item.to_wire(
                    download_path=f"/v1/web/tasks/{task_id}/artifacts/{item.artifact_id}/download"
                )
                for item in artifacts
            ],
        }

    @router.get("/tasks/{task_id}/artifacts/{artifact_id}/download")
    async def download_artifact(
        task_id: str,
        artifact_id: str,
        request: Request,
        principal=Depends(require_web_session),
    ) -> Response:
        store = store_for(request)
        allowed = devices_for(request, principal, store)
        _, actor = _require_web_task_owner(store, task_id, allowed)
        device_id = actor.removeprefix("device:")
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise HTTPException(status_code=404, detail="Artifact not found")
        try:
            _require_owned_completed_development_task(store, task_id, device_id)
            artifact, data = _artifact_store().download(task_id, device_id, artifact_id)
        except HTTPException:
            raise
        except ArtifactNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        except ArtifactIntegrityError as exc:
            raise HTTPException(
                status_code=500, detail="Stored artifact integrity check failed"
            ) from exc
        except ArtifactStoreError as exc:
            raise HTTPException(
                status_code=503, detail="Task artifact storage is unavailable"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != artifact.archive_sha256:
            raise HTTPException(
                status_code=500, detail="Stored artifact integrity check failed"
            )
        filename = quote(str(artifact.file_name), safe="._-")
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-SHA256": digest,
                "ETag": f'"{digest}"',
            },
        )

    return router


__all__ = [
    "create_web_supervisor_router",
    "is_web_device_active",
    "is_web_device_mapped",
    "resolve_web_device_ids",
]
