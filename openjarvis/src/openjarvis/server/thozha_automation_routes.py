"""Authenticated Web Workspace automation routes.

This module is deliberately an adapter, not an automation executor.  Read
operations can inspect only scheduler rows tagged with the authenticated Web
owner.  Every mutation is delegated to an injected ``supervised_action``
callback; without that callback the route fails closed.  The callback is the
integration point for OpenJarvis' supervisor/capability policy and must persist
``owner_id`` and ``idempotency_key`` with newly created scheduler records.

The existing scheduler currently has no safe manual-trigger operation and may
be configured as a store-only scheduler.  This API never runs scheduler tasks,
workflow TOOL nodes, MCP tools, or shell commands itself.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

_OWNER_METADATA_KEY = "thozha_web_owner_id"
_SOURCE_METADATA_KEY = "thozha_web_source"
_IDEMPOTENCY_METADATA_KEY = "thozha_web_idempotency_key"
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_SUPPORTED_SCHEDULES = {"once", "interval", "cron"}


class AutomationCreate(BaseModel):
    """Fields accepted for a supervised scheduled automation."""

    prompt: str = Field(min_length=1, max_length=2000)
    schedule_type: str
    schedule_value: str = Field(min_length=1, max_length=200)
    timezone: Optional[str] = Field(default=None, max_length=80)

    model_config = ConfigDict(extra="forbid")


class AutomationUpdate(BaseModel):
    """Partial update. The supervisor adapter applies and persists it."""

    prompt: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    schedule_type: Optional[str] = None
    schedule_value: Optional[str] = Field(default=None, min_length=1, max_length=200)
    timezone: Optional[str] = Field(default=None, max_length=80)

    model_config = ConfigDict(extra="forbid")


def validate_schedule(
    schedule_type: str,
    schedule_value: str,
    timezone_name: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Validate supported scheduler values before dispatching to the gate."""
    if schedule_type not in _SUPPORTED_SCHEDULES:
        raise HTTPException(422, "schedule_type must be once, interval, or cron")
    if timezone_name:
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(422, "timezone must be a valid IANA timezone") from exc

    if schedule_type == "interval":
        try:
            seconds = float(schedule_value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "interval schedule_value must be seconds") from exc
        if not 300 <= seconds <= 31_536_000:
            raise HTTPException(
                422, "interval must be between 300 and 31536000 seconds"
            )
    elif schedule_type == "once":
        try:
            run_at = datetime.fromisoformat(schedule_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                422, "once schedule_value must be an ISO-8601 datetime"
            ) from exc
        if run_at.tzinfo is None:
            raise HTTPException(422, "once schedule_value must include a timezone")
        reference = now or datetime.now(timezone.utc)
        if run_at.astimezone(timezone.utc) < reference + timedelta(seconds=60):
            raise HTTPException(
                422, "one-time automation must be at least 60 seconds in the future"
            )
        if run_at.astimezone(timezone.utc) > reference + timedelta(days=365):
            raise HTTPException(
                422, "one-time automation cannot be more than 365 days away"
            )
    else:
        # Use the scheduler's own dependency and parser so accepted expressions
        # have the same semantics as the eventual runner.
        try:
            from croniter import croniter

            croniter(schedule_value, now or datetime.now(timezone.utc)).get_next(
                datetime
            )
        except ImportError as exc:
            raise HTTPException(
                503, "cron schedules unavailable: croniter is not installed"
            ) from exc
        except Exception as exc:
            raise HTTPException(422, "invalid cron expression") from exc


def scheduler_state_for_create(
    scheduler: Any, supervised_action: Optional[Callable[..., Any]]
) -> bool:
    return (
        supervised_action is not None
        and callable(getattr(scheduler, "create_task", None))
        and (not hasattr(scheduler, "_system") or scheduler._system is not None)
    )


def scheduler_unavailable_reasons(
    scheduler: Any,
    supervised_action: Optional[Callable[..., Any]],
) -> Dict[str, str]:
    reasons: Dict[str, str] = {}
    methods = {
        "update": "update_task",
        "delete": "delete_task",
        "manual_trigger": "run_task_once",
    }
    if supervised_action is None:
        for operation in (
            "create",
            "update",
            "enable",
            "disable",
            "pause",
            "delete",
            "manual_trigger",
        ):
            reasons[operation] = "supervisor-backed action adapter is not configured"
    for operation, method in methods.items():
        if not callable(getattr(scheduler, method, None)):
            reasons[operation] = f"scheduler has no public {method} method"
    if hasattr(scheduler, "_system") and scheduler._system is None:
        reasons["create"] = "scheduler is store-only and cannot execute automations"
    return reasons


def create_automation_router(
    *,
    require_web_session: Callable[..., Any],
    scheduler_provider: Optional[Callable[..., Any]] = None,
    supervised_action: Optional[Callable[..., Any]] = None,
) -> APIRouter:
    """Create the versioned Web automation router.

    ``require_web_session`` is injected by the browser-auth module.  It must
    return a principal with a non-empty ``user_id``. ``supervised_action`` is
    intentionally mandatory for all mutations and receives ``(principal,
    action_dict)``. It must apply the existing supervisor/capability policy and
    perform the operation through the established scheduler integration. For
    create, it must persist owner/source/idempotency metadata on the scheduler
    row and return its task ID. No request body can set the owner.
    """
    router = APIRouter(prefix="/v1/web/automations", tags=["web-automations"])

    def owner(principal: Any) -> str:
        user_id = (
            principal.get("user_id")
            if isinstance(principal, dict)
            else getattr(principal, "user_id", None)
        )
        if not isinstance(user_id, str) or not user_id.strip():
            raise HTTPException(401, "authenticated Web principal is invalid")
        return user_id

    def scheduler_from(request: Request) -> Any:
        if scheduler_provider is not None:
            scheduler = scheduler_provider(request)
        else:
            system = getattr(request.app.state, "system", None)
            scheduler = getattr(system, "scheduler", None)
        if scheduler is None or not hasattr(scheduler, "list_tasks"):
            raise HTTPException(
                503,
                detail={
                    "status": "unavailable",
                    "reason": "task scheduler is not configured",
                },
            )
        return scheduler

    def unavailable() -> None:
        raise HTTPException(
            503,
            detail={
                "status": "unavailable",
                "reason": "supervisor-backed automation actions are not configured",
            },
        )

    def require_scheduler_operation(scheduler: Any, operation: str) -> None:
        methods = {
            "create": "create_task",
            "update": "update_task",
            "delete": "delete_task",
            "enable": "resume_task",
            "disable": "pause_task",
            "pause": "pause_task",
            # Do not reach into private _execute_task as a manual trigger.
            "manual_trigger": "run_task_once",
        }
        method = methods[operation]
        if not callable(getattr(scheduler, method, None)):
            raise HTTPException(
                501,
                detail={
                    "status": "unavailable",
                    "reason": f"scheduler does not implement public {method} operation",
                },
            )
        # The production TaskScheduler created by SystemBuilder currently has
        # _system=None, causing its due-task loop to persist a fake dry-run as
        # successful. Refuse creation against that known non-executing runner.
        if (
            operation == "create"
            and hasattr(scheduler, "_system")
            and scheduler._system is None
        ):
            raise HTTPException(
                503,
                detail={
                    "status": "unavailable",
                    "reason": "scheduler is store-only and cannot execute automations",
                },
            )

    async def dispatch(principal: Any, action: Dict[str, Any]) -> Any:
        if supervised_action is None:
            unavailable()
        try:
            result = supervised_action(principal, action)
            if inspect.isawaitable(result):
                result = await result
            return result
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(403, "supervisor denied automation operation") from exc
        except KeyError as exc:
            raise HTTPException(404, "automation not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except NotImplementedError as exc:
            raise HTTPException(501, "automation operation is unavailable") from exc
        except Exception as exc:
            # Do not expose backend exception contents, prompts, or credentials.
            raise HTTPException(503, "supervisor automation operation failed") from exc

    def owned_task(
        scheduler: Any,
        task_id: str,
        user_id: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            task = scheduler._store.get_task(task_id)
        except Exception as exc:
            raise HTTPException(503, "scheduler persistence is unavailable") from exc
        metadata = (task or {}).get("metadata") or {}
        if (
            task is None
            or metadata.get(_OWNER_METADATA_KEY) != user_id
            or metadata.get(_SOURCE_METADATA_KEY) != "web"
            or (
                idempotency_key is not None
                and metadata.get(_IDEMPOTENCY_METADATA_KEY) != idempotency_key
            )
        ):
            # Deliberately do not distinguish foreign IDs from missing IDs.
            raise HTTPException(404, "automation not found")
        return task

    def public_task(task: Dict[str, Any]) -> Dict[str, Any]:
        metadata = task.get("metadata") or {}
        return {
            "id": task["id"],
            "prompt": task["prompt"],
            "schedule_type": task["schedule_type"],
            "schedule_value": task["schedule_value"],
            "timezone": metadata.get("timezone"),
            "status": task["status"],
            "next_run": task.get("next_run"),
            "last_run": task.get("last_run"),
        }

    async def perform_mutation(
        principal: Any,
        action: Dict[str, Any],
        *,
        scheduler: Any = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = await dispatch(principal, action)
        # The supervised adapter is authoritative for state changes, but we
        # verify persisted ownership before returning a newly created ID.
        if task_id is not None:
            if scheduler is None:
                raise HTTPException(503, "scheduler is unavailable")
            task = owned_task(scheduler, task_id, owner(principal))
            return public_task(task)
        if isinstance(result, dict):
            safe_result = {
                key: result[key]
                for key in ("status", "approval_id")
                if key in result
                and isinstance(result[key], (str, int, bool, type(None)))
            }
            return safe_result or {"status": "accepted"}
        return {"status": "accepted"}

    @router.get("")
    async def list_automations(
        request: Request,
        principal: Any = Depends(require_web_session),
        status: Optional[str] = Query(
            default=None, pattern="^(active|paused|completed|cancelled)$"
        ),
    ):
        user_id = owner(principal)
        scheduler = scheduler_from(request)
        try:
            tasks = scheduler.list_tasks(status=status)
            rows = [
                task.to_dict() if hasattr(task, "to_dict") else task for task in tasks
            ]
        except Exception as exc:
            raise HTTPException(503, "scheduler listing failed") from exc
        return {
            "items": [
                public_task(row)
                for row in rows
                if (
                    (row.get("metadata") or {}).get(_OWNER_METADATA_KEY) == user_id
                    and (row.get("metadata") or {}).get(_SOURCE_METADATA_KEY) == "web"
                )
            ]
        }

    @router.get("/capabilities")
    async def automation_capabilities(
        request: Request,
        principal: Any = Depends(require_web_session),
    ):
        owner(principal)
        scheduler = scheduler_from(request)
        return {
            "status": "available",
            "operations": {
                "list": True,
                "get": True,
                "history": callable(
                    getattr(getattr(scheduler, "_store", None), "get_run_logs", None)
                ),
                "create": scheduler_state_for_create(scheduler, supervised_action),
                "update": callable(getattr(scheduler, "update_task", None))
                and supervised_action is not None,
                "enable": callable(getattr(scheduler, "resume_task", None))
                and supervised_action is not None,
                "disable": callable(getattr(scheduler, "pause_task", None))
                and supervised_action is not None,
                "pause": callable(getattr(scheduler, "pause_task", None))
                and supervised_action is not None,
                "delete": callable(getattr(scheduler, "delete_task", None))
                and supervised_action is not None,
                "manual_trigger": callable(getattr(scheduler, "run_task_once", None))
                and supervised_action is not None,
            },
            "unavailable_reasons": scheduler_unavailable_reasons(
                scheduler, supervised_action
            ),
        }

    @router.post("", status_code=202)
    async def create_automation(
        body: AutomationCreate,
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        principal: Any = Depends(require_web_session),
    ):
        if not idempotency_key or not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise HTTPException(400, "a valid Idempotency-Key header is required")
        validate_schedule(body.schedule_type, body.schedule_value, body.timezone)
        user_id = owner(principal)
        scheduler = scheduler_from(request)
        require_scheduler_operation(scheduler, "create")
        if supervised_action is None:
            unavailable()
        action = {
            "operation": "create",
            "owner_id": user_id,
            "idempotency_key": idempotency_key,
            "automation": {
                **body.model_dump(),
                # No caller-controlled tools, agent, owner, or status fields.
                "agent": "simple",
                "tools": [],
                "metadata": {
                    _OWNER_METADATA_KEY: user_id,
                    _SOURCE_METADATA_KEY: "web",
                    _IDEMPOTENCY_METADATA_KEY: idempotency_key,
                    **({"timezone": body.timezone} if body.timezone else {}),
                },
            },
        }
        result = await dispatch(principal, action)
        task_id = result.get("task_id") if isinstance(result, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise HTTPException(
                503, "supervisor did not return a persisted automation ID"
            )
        task = owned_task(
            scheduler,
            task_id,
            user_id,
            idempotency_key=idempotency_key,
        )
        return {"status": "created", "automation": public_task(task)}

    @router.get("/{automation_id}")
    async def get_automation(
        automation_id: str,
        request: Request,
        principal: Any = Depends(require_web_session),
    ):
        task = owned_task(scheduler_from(request), automation_id, owner(principal))
        return {"automation": public_task(task)}

    @router.get("/{automation_id}/history")
    async def automation_history(
        automation_id: str,
        request: Request,
        principal: Any = Depends(require_web_session),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        scheduler = scheduler_from(request)
        owned_task(scheduler, automation_id, owner(principal))
        try:
            logs = scheduler._store.get_run_logs(automation_id, limit=limit)
        except Exception as exc:
            raise HTTPException(503, "scheduler history is unavailable") from exc
        return {
            "items": [
                {
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "success": bool(row["success"]),
                    "result": row["result"],
                    "error": row["error"],
                }
                for row in logs
            ]
        }

    @router.patch("/{automation_id}")
    async def update_automation(
        automation_id: str,
        body: AutomationUpdate,
        request: Request,
        principal: Any = Depends(require_web_session),
    ):
        user_id = owner(principal)
        scheduler = scheduler_from(request)
        owned_task(scheduler, automation_id, user_id)
        patch = body.model_dump(exclude_unset=True)
        if not any(value is not None for value in patch.values()):
            raise HTTPException(422, "at least one update field is required")
        require_scheduler_operation(scheduler, "update")
        if "schedule_type" in patch or "schedule_value" in patch or "timezone" in patch:
            current = scheduler._store.get_task(automation_id)
            schedule_type = patch.get("schedule_type", current["schedule_type"])
            schedule_value = patch.get("schedule_value", current["schedule_value"])
            timezone_name = patch.get(
                "timezone", (current.get("metadata") or {}).get("timezone")
            )
            validate_schedule(schedule_type, schedule_value, timezone_name)
        return await perform_mutation(
            principal,
            {
                "operation": "update",
                "owner_id": user_id,
                "automation_id": automation_id,
                "changes": patch,
            },
            scheduler=scheduler,
        )

    async def lifecycle_action(
        automation_id: str, request: Request, principal: Any, operation: str
    ):
        user_id = owner(principal)
        scheduler = scheduler_from(request)
        owned_task(scheduler, automation_id, user_id)
        require_scheduler_operation(scheduler, operation)
        return await perform_mutation(
            principal,
            {
                "operation": operation,
                "owner_id": user_id,
                "automation_id": automation_id,
            },
            scheduler=scheduler,
        )

    @router.post("/{automation_id}/enable")
    async def enable_automation(
        automation_id: str,
        request: Request,
        principal: Any = Depends(require_web_session),
    ):
        return await lifecycle_action(automation_id, request, principal, "enable")

    @router.post("/{automation_id}/disable")
    async def disable_automation(
        automation_id: str,
        request: Request,
        principal: Any = Depends(require_web_session),
    ):
        return await lifecycle_action(automation_id, request, principal, "disable")

    @router.post("/{automation_id}/pause")
    async def pause_automation(
        automation_id: str,
        request: Request,
        principal: Any = Depends(require_web_session),
    ):
        return await lifecycle_action(automation_id, request, principal, "pause")

    @router.post("/{automation_id}/trigger")
    async def trigger_automation(
        automation_id: str,
        request: Request,
        principal: Any = Depends(require_web_session),
    ):
        return await lifecycle_action(
            automation_id, request, principal, "manual_trigger"
        )

    @router.delete("/{automation_id}")
    async def delete_automation(
        automation_id: str,
        request: Request,
        principal: Any = Depends(require_web_session),
    ):
        return await lifecycle_action(automation_id, request, principal, "delete")

    return router


__all__ = [
    "AutomationCreate",
    "AutomationUpdate",
    "create_automation_router",
    "validate_schedule",
]
