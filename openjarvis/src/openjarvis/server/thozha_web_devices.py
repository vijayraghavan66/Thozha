"""Browser-safe status and revocation for explicitly mapped paired devices.

This router never enrolls devices. The injected resolver must return only
paired-device IDs explicitly mapped to the authenticated Web principal; each
request then rechecks activity against the supervisor database.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from openjarvis.tasks.supervisor import SupervisorStore

logger = logging.getLogger(__name__)
_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class DeviceRevokeRequest(BaseModel):
    confirm: StrictBool

    model_config = ConfigDict(extra="forbid")


class PairingInvitationRequest(BaseModel):
    transport: Literal["wifi"]

    model_config = ConfigDict(extra="forbid")


class PairingDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]

    model_config = ConfigDict(extra="forbid")


class PairingCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _web_user_id(principal: Any) -> str:
    user_id = (
        principal.get("user_id")
        if isinstance(principal, dict)
        else getattr(principal, "user_id", None)
    )
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=401, detail="Invalid Web session principal")
    return user_id


def _require_supervisor_owner(request: Request, principal: Any) -> str:
    """Require the authenticated Web principal to be the owner of this Mac store."""
    context = getattr(request.app.state, "thozha_web_auth", None)
    config = getattr(context, "config", None)
    owner_id = getattr(config, "owner_id", None)
    user_id = _web_user_id(principal)
    if not isinstance(owner_id, str) or not owner_id:
        raise HTTPException(status_code=503, detail="Web pairing owner is unavailable")
    if user_id != owner_id:
        # Keep installation ownership opaque; do not reveal another account's status.
        raise HTTPException(status_code=404, detail="Pairing request not found")
    return user_id


def _active_mapped_devices(
    principal: Any,
    store: SupervisorStore,
    resolver: Callable[[Any, SupervisorStore], set[str]],
    active_check: Callable[[str, str, SupervisorStore], bool] | None = None,
) -> list[dict[str, str]]:
    user_id = _web_user_id(principal)
    try:
        mapped = resolver(principal, store)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Web device mapping unavailable error_type=%s", type(exc).__name__
        )
        raise HTTPException(
            status_code=503, detail="Web device mapping is unavailable"
        ) from exc
    if not isinstance(mapped, (set, frozenset, list, tuple)):
        raise HTTPException(status_code=503, detail="Web device mapping is invalid")
    device_ids = sorted(
        {
            item
            for item in mapped
            if isinstance(item, str) and _DEVICE_ID_RE.fullmatch(item)
        }
    )
    if not device_ids:
        return []

    placeholders = ",".join("?" for _ in device_ids)
    now = datetime.now(timezone.utc).isoformat()
    db_path = Path(store.db_path).resolve()
    db_uri = f"{db_path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(db_uri, uri=True, timeout=2.0) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT id,device_name,created_at,expires_at FROM paired_devices "
                f"WHERE id IN ({placeholders}) AND revoked_at IS NULL AND expires_at>? "
                "ORDER BY created_at DESC",
                (*device_ids, now),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(
            "Web device status unavailable error_type=%s", type(exc).__name__
        )
        raise HTTPException(
            status_code=503, detail="Paired-device status is unavailable"
        ) from exc

    devices: list[dict[str, str]] = []
    for row in rows:
        if active_check is not None:
            try:
                if not active_check(user_id, str(row["id"]), store):
                    continue
            except Exception as exc:
                logger.warning(
                    "Web device active-check unavailable error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=503, detail="Paired-device status is unavailable"
                ) from exc
        name = (
            " ".join(
                "".join(
                    char
                    for char in str(row["device_name"] or "Android companion")
                    if char >= " " and char != "\x7f"
                ).split()
            )[:80]
            or "Android companion"
        )
        devices.append(
            {
                "device_id": str(row["id"]),
                "device_name": name,
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"]),
                "status": "active",
            }
        )
    return devices


def create_web_devices_router(
    require_web_session: Callable[..., Any],
    resolve_web_device_ids: Callable[[Any, SupervisorStore], set[str]],
    is_web_device_active: Callable[[str, str, SupervisorStore], bool],
    is_web_device_mapped: Callable[[Any, str, SupervisorStore], bool],
    *,
    store_provider: Callable[..., SupervisorStore] | None = None,
) -> APIRouter:
    """Create Web session routes for safe metadata and confirmed revocation."""
    if require_web_session is None:
        raise ValueError("require_web_session dependency is required")
    if (
        resolve_web_device_ids is None
        or is_web_device_active is None
        or is_web_device_mapped is None
    ):
        raise ValueError("Web device identity callbacks are required")
    router = APIRouter(prefix="/v1/web/devices", tags=["web-devices"])

    def get_store(request) -> SupervisorStore:
        if store_provider is not None:
            store = store_provider(request)
        else:
            store = getattr(request.app.state, "supervisor_store", None)
        if store is None or not callable(getattr(store, "revoke_device", None)):
            raise HTTPException(
                status_code=503, detail="Supervisor device store is unavailable"
            )
        return store

    @router.get("")
    async def list_devices(
        request: Request,
        response: Response,
        principal=Depends(require_web_session),
    ):
        store = get_store(request)
        devices = _active_mapped_devices(
            principal,
            store,
            resolve_web_device_ids,
            is_web_device_active,
        )
        response.headers["Cache-Control"] = "no-store"
        return {"devices": devices, "count": len(devices)}

    @router.get("/pairing-requests")
    async def list_pairing_requests(
        request: Request,
        response: Response,
        limit: int = 50,
        principal=Depends(require_web_session),
    ):
        _require_supervisor_owner(request, principal)
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
        store = get_store(request)
        requests = store.pending_device_pairings(limit)
        response.headers["Cache-Control"] = "no-store"
        return {"requests": requests, "count": len(requests)}

    @router.get("/{device_id}")
    async def get_device(
        device_id: str,
        request: Request,
        response: Response,
        principal=Depends(require_web_session),
    ):
        if not _DEVICE_ID_RE.fullmatch(device_id):
            raise HTTPException(status_code=404, detail="Paired device not found")
        store = get_store(request)
        devices = _active_mapped_devices(
            principal,
            store,
            resolve_web_device_ids,
            is_web_device_active,
        )
        device = next(
            (item for item in devices if item["device_id"] == device_id), None
        )
        if device is None:
            raise HTTPException(status_code=404, detail="Paired device not found")
        response.headers["Cache-Control"] = "no-store"
        return device

    @router.post("/pairing-invitations")
    async def create_pairing_invitation(
        request: Request,
        body: PairingInvitationRequest,
        response: Response,
        principal=Depends(require_web_session),
    ):
        _require_supervisor_owner(request, principal)
        store = get_store(request)
        from openjarvis.server.supervisor_routes import create_signed_pairing_qr

        qr = create_signed_pairing_qr(store, request, transport=body.transport)
        response.headers["Cache-Control"] = "no-store"
        return qr

    @router.post("/pairing-requests/{invitation_id}/decision")
    async def decide_pairing_request(
        invitation_id: str,
        request: Request,
        body: PairingDecisionRequest,
        response: Response,
        principal=Depends(require_web_session),
    ):
        owner_id = _require_supervisor_owner(request, principal)
        if not _DEVICE_ID_RE.fullmatch(invitation_id):
            raise HTTPException(status_code=404, detail="Pairing request not found")
        store = get_store(request)
        try:
            status = store.decide_device_pairing(
                invitation_id,
                body.decision,
                web_owner_id=owner_id if body.decision == "approve" else None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Pairing request not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail="Pairing request is no longer pending") from exc
        response.headers["Cache-Control"] = "no-store"
        return {"invitation_id": invitation_id, "status": status}

    @router.post("/pairing-invitations/{invitation_id}/cancel")
    async def cancel_pairing_invitation(
        invitation_id: str,
        request: Request,
        body: PairingCancelRequest,
        response: Response,
        principal=Depends(require_web_session),
    ):
        _require_supervisor_owner(request, principal)
        if not _DEVICE_ID_RE.fullmatch(invitation_id):
            raise HTTPException(status_code=404, detail="Pairing invitation not found")
        store = get_store(request)
        try:
            status = store.cancel_device_pairing(invitation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Pairing invitation not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail="Pairing invitation was already claimed") from exc
        response.headers["Cache-Control"] = "no-store"
        return {"invitation_id": invitation_id, "status": status}

    @router.get("/pairing-invitations/{invitation_id}")
    async def get_pairing_invitation(
        invitation_id: str,
        request: Request,
        response: Response,
        principal=Depends(require_web_session),
    ):
        _require_supervisor_owner(request, principal)
        if not _DEVICE_ID_RE.fullmatch(invitation_id):
            raise HTTPException(status_code=404, detail="Pairing invitation not found")
        store = get_store(request)
        try:
            status = store.device_pairing_status(invitation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Pairing invitation not found") from exc
        # The Web owner sees status only. The temporary claim challenge remains
        # available exclusively through the invitation-scoped Android API.
        response.headers["Cache-Control"] = "no-store"
        return {"invitation_id": invitation_id, "status": status["status"]}

    @router.post("/{device_id}/revoke")
    async def revoke_device(
        device_id: str,
        request: Request,
        body: DeviceRevokeRequest,
        response: Response,
        principal=Depends(require_web_session),
    ):
        if not _DEVICE_ID_RE.fullmatch(device_id):
            raise HTTPException(status_code=404, detail="Paired device not found")
        if body.confirm is not True:
            raise HTTPException(
                status_code=422, detail="Explicit {confirm: true} is required"
            )
        store = get_store(request)
        user_id = _web_user_id(principal)
        try:
            mapped = is_web_device_mapped(principal, device_id, store)
        except Exception as exc:
            logger.warning(
                "Web device mapping check unavailable error_type=%s", type(exc).__name__
            )
            raise HTTPException(
                status_code=503, detail="Web device mapping is unavailable"
            ) from exc
        if not mapped:
            raise HTTPException(status_code=404, detail="Paired device not found")
        devices = _active_mapped_devices(
            principal,
            store,
            resolve_web_device_ids,
            is_web_device_active,
        )
        active = any(item["device_id"] == device_id for item in devices)
        already_revoked = not active
        if active:
            if not store.revoke_device(device_id):
                raise HTTPException(
                    status_code=409, detail="Device revocation could not be confirmed"
                )
        else:
            # A prior revoke may have succeeded while cleanup failed. Permit a
            # mapped owner to retry FCM cleanup, but never to access device data.
            with store._lock:
                row = store._db.execute(
                    "SELECT revoked_at FROM paired_devices WHERE id=?", (device_id,)
                ).fetchone()
            if row is None or row["revoked_at"] is None:
                raise HTTPException(
                    status_code=404, detail="Active paired device not found"
                )

        registration_status = "none"
        try:
            from openjarvis.server.fcm_notifications import FCMRegistrationStore

            registrations = FCMRegistrationStore(store.db_path)
            try:
                if registrations.revoke(device_id):
                    registration_status = "revoked"
            finally:
                registrations.close()
        except Exception as exc:
            logger.error(
                "Web device revoked but FCM cleanup failed "
                "user=%s device=%s error_type=%s",
                hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12],
                device_id[:8],
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Device access is revoked, but notification cleanup needs "
                    "retry through local Mac management"
                ),
            ) from exc

        logger.info(
            "Web device revoked user=%s device=%s fcm_cleanup=%s",
            hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12],
            device_id[:8],
            registration_status,
        )
        response.headers["Cache-Control"] = "no-store"
        return {
            "status": "already_revoked" if already_revoked else "revoked",
            "device_id": device_id,
            "notification_token_cleanup": registration_status,
        }

    return router


__all__ = [
    "DeviceRevokeRequest",
    "PairingDecisionRequest",
    "PairingInvitationRequest",
    "create_web_devices_router",
]
