"""Task scheduler — cron/interval/once execution with background polling."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from openjarvis.scheduler.store import SchedulerStore

logger = logging.getLogger(__name__)

# Event type strings (avoids editing core EventType enum)
SCHEDULER_TASK_START = "scheduler_task_start"
SCHEDULER_TASK_END = "scheduler_task_end"
WEB_SOURCE = "web"
WEB_OWNER_KEY = "thozha_web_owner_id"
WEB_DEVICE_KEY = "thozha_web_device_id"
WEB_IDEMPOTENCY_KEY = "thozha_web_idempotency_key"
WEB_SCOPE_KEY = "capability_scope"
_WEB_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class WebScheduleConflict(ValueError):
    """A Web schedule idempotency key was reused with modified content."""


def _validate_web_schedule(
    schedule_type: str, schedule_value: str, timezone_name: str | None = None
) -> None:
    if schedule_type not in {"once", "interval", "cron"}:
        raise ValueError("unsupported Web automation schedule type")
    if timezone_name:
        ZoneInfo(timezone_name)
    if schedule_type == "interval":
        seconds = float(schedule_value)
        if not 300 <= seconds <= 31_536_000:
            raise ValueError("Web automation interval must be 300..31536000 seconds")
    elif schedule_type == "once":
        when = datetime.fromisoformat(schedule_value.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("one-time Web automation requires a timezone")
        delta = when.astimezone(timezone.utc) - datetime.now(timezone.utc)
        if not 60 <= delta.total_seconds() <= 365 * 24 * 60 * 60:
            raise ValueError(
                "one-time Web automation must be 60 seconds to one year away"
            )
    else:
        from croniter import croniter

        reference = (
            datetime.now(ZoneInfo(timezone_name))
            if timezone_name
            else datetime.now(timezone.utc)
        )
        croniter(schedule_value, reference).get_next(datetime)


@dataclass(slots=True)
class ScheduledTask:
    """A task scheduled for future or recurring execution."""

    id: str
    prompt: str
    schedule_type: str  # "cron" | "interval" | "once"
    schedule_value: str  # cron expression, interval seconds, ISO datetime
    context_mode: str = "isolated"
    status: str = "active"
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    agent: str = "simple"
    tools: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict for store persistence."""
        return {
            "id": self.id,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value,
            "context_mode": self.context_mode,
            "status": self.status,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "agent": self.agent,
            "tools": self.tools,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ScheduledTask:
        """Deserialize from a plain dict."""
        return cls(
            id=d["id"],
            prompt=d["prompt"],
            schedule_type=d["schedule_type"],
            schedule_value=d["schedule_value"],
            context_mode=d.get("context_mode", "isolated"),
            status=d.get("status", "active"),
            next_run=d.get("next_run"),
            last_run=d.get("last_run"),
            agent=d.get("agent", "simple"),
            tools=d.get("tools", ""),
            metadata=d.get("metadata", {}),
        )


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class TaskScheduler:
    """Scheduler that polls for due tasks and executes them.

    Parameters
    ----------
    store:
        The persistence backend.
    system:
        Optional ``JarvisSystem`` instance for executing prompts.
    poll_interval:
        Seconds between poll cycles (default 60).
    bus:
        Optional event bus for publishing scheduler events.
    """

    def __init__(
        self,
        store: SchedulerStore,
        system: Any = None,
        *,
        poll_interval: int = 60,
        bus: Any = None,
        web_supervisor_dispatcher: Callable[[Dict[str, Any], str], Dict[str, Any]]
        | None = None,
    ) -> None:
        self._store = store
        self._system = system
        self._poll_interval = poll_interval
        self._bus = bus
        self._web_supervisor_dispatcher = web_supervisor_dispatcher
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._closed = False

    # -- Public API ----------------------------------------------------------

    def start(self) -> None:
        """Start the background polling daemon thread."""
        if self._closed:
            raise RuntimeError("scheduler is closed")
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="jarvis-scheduler"
        )
        self._thread.start()
        logger.info("Scheduler started (poll_interval=%ds)", self._poll_interval)

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 5)
            self._thread = None
        logger.info("Scheduler stopped")

    def close(self) -> None:
        """Stop scheduler polling and close its persistent store."""
        if self._closed:
            return
        self.stop()
        self._store.close()
        self._closed = True

    def create_task(
        self,
        prompt: str,
        schedule_type: str,
        schedule_value: str,
        **kwargs: Any,
    ) -> ScheduledTask:
        """Create and persist a new scheduled task."""
        task = ScheduledTask(
            id=uuid.uuid4().hex[:16],
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            agent=kwargs.get("agent", "simple"),
            tools=kwargs.get("tools", ""),
            context_mode=kwargs.get("context_mode", "isolated"),
            metadata=kwargs.get("metadata", {}),
        )
        task.next_run = self._compute_next_run(task)
        with self._lock:
            self._store.save_task(task.to_dict())
        return task

    @property
    def web_supervisor_dispatcher_ready(self) -> bool:
        dispatcher = self._web_supervisor_dispatcher
        if dispatcher is None:
            return False
        is_ready = getattr(dispatcher, "is_ready", None)
        return bool(is_ready()) if callable(is_ready) else True

    def configure_web_supervisor_dispatcher(
        self, dispatcher: Callable[[Dict[str, Any], str], Dict[str, Any]] | None
    ) -> None:
        """Install the app-owned callback that creates normal durable Link tasks.

        The callback must re-check that the stored paired device is active, bind
        the task to ``device:<id>``, and submit only through SupervisorStore so
        LinkTaskExecutionRunner and normal approvals remain authoritative.
        """
        with self._lock:
            self._web_supervisor_dispatcher = dispatcher

    def create_web_task(
        self,
        prompt: str,
        schedule_type: str,
        schedule_value: str,
        *,
        owner_id: str,
        device_id: str,
        idempotency_key: str,
        timezone_name: str | None = None,
    ) -> tuple[ScheduledTask, bool]:
        """Persist a read-only Web schedule.

        Execution requires the supervisor adapter.
        """
        if not self.web_supervisor_dispatcher_ready:
            raise RuntimeError("supervisor-backed Web scheduler is unavailable")
        if (
            not owner_id
            or not device_id
            or not _WEB_IDEMPOTENCY_RE.fullmatch(idempotency_key or "")
        ):
            raise ValueError("Web schedule identity or idempotency key is invalid")
        prompt = prompt.strip()
        if not prompt or len(prompt) > 2000:
            raise ValueError("Web schedule prompt is invalid")
        _validate_web_schedule(schedule_type, schedule_value, timezone_name)
        from openjarvis.tasks.supervisor import is_link_scope

        scope = ["read_only"]
        if not is_link_scope(scope):
            raise RuntimeError("read-only Link scope is not enabled")
        metadata = {
            WEB_OWNER_KEY: owner_id,
            WEB_DEVICE_KEY: device_id,
            "thozha_web_source": WEB_SOURCE,
            WEB_IDEMPOTENCY_KEY: idempotency_key,
            WEB_SCOPE_KEY: scope,
            **({"timezone": timezone_name} if timezone_name else {}),
        }
        with self._lock:
            existing = self._store.find_web_task(owner_id, idempotency_key)
            if existing is not None:
                expected = (
                    prompt,
                    schedule_type,
                    schedule_value,
                    device_id,
                    scope,
                    timezone_name,
                )
                actual = (
                    existing["prompt"],
                    existing["schedule_type"],
                    existing["schedule_value"],
                    (existing.get("metadata") or {}).get(WEB_DEVICE_KEY),
                    (existing.get("metadata") or {}).get(WEB_SCOPE_KEY),
                    (existing.get("metadata") or {}).get("timezone"),
                )
                if expected != actual:
                    raise WebScheduleConflict(
                        "Web schedule idempotency key conflicts with an existing "
                        "schedule"
                    )
                return ScheduledTask.from_dict(existing), False
            task = ScheduledTask(
                id=uuid.uuid4().hex[:16],
                prompt=prompt,
                schedule_type=schedule_type,
                schedule_value=schedule_value,
                agent="simple",
                tools="",
                metadata=metadata,
            )
            task.next_run = self._compute_next_run(task)
            existing, created = self._store.create_web_task(task.to_dict())
            expected = (
                prompt,
                schedule_type,
                schedule_value,
                device_id,
                scope,
                timezone_name,
            )
            actual = (
                existing["prompt"],
                existing["schedule_type"],
                existing["schedule_value"],
                (existing.get("metadata") or {}).get(WEB_DEVICE_KEY),
                (existing.get("metadata") or {}).get(WEB_SCOPE_KEY),
                (existing.get("metadata") or {}).get("timezone"),
            )
            if expected != actual:
                raise WebScheduleConflict(
                    "Web schedule idempotency key conflicts with an existing schedule"
                )
            return ScheduledTask.from_dict(existing), created

    def list_tasks(self, *, status: Optional[str] = None) -> List[ScheduledTask]:
        """Return tasks, optionally filtered by *status*."""
        with self._lock:
            rows = self._store.list_tasks(status=status)
        return [ScheduledTask.from_dict(r) for r in rows]

    def get_task(self, task_id: str) -> Dict[str, Any] | None:
        """Read one persisted schedule; the API layer performs owner checks."""
        return self._store.get_task(task_id)

    def get_run_logs(self, task_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("history limit must be between 1 and 100")
        return self._store.get_run_logs(task_id, limit=limit)

    def pause_task(self, task_id: str) -> None:
        """Pause an active task."""
        with self._lock:
            d = self._store.get_task(task_id)
            if d is None:
                raise KeyError(f"Task not found: {task_id}")
            d["status"] = "paused"
            if (d.get("metadata") or {}).get("thozha_web_source") == WEB_SOURCE:
                if not self._store.update_web_task(d):
                    raise KeyError(f"Web task not found: {task_id}")
            else:
                self._store.update_task(d)

    def resume_task(self, task_id: str) -> None:
        """Resume a paused task."""
        with self._lock:
            d = self._store.get_task(task_id)
            if d is None:
                raise KeyError(f"Task not found: {task_id}")
            d["status"] = "active"
            # Recompute next_run from now
            task = ScheduledTask.from_dict(d)
            task.next_run = self._compute_next_run(task)
            updated = task.to_dict()
            if (d.get("metadata") or {}).get("thozha_web_source") == WEB_SOURCE:
                if not self._store.update_web_task(updated):
                    raise KeyError(f"Web task not found: {task_id}")
            else:
                self._store.update_task(updated)

    def cancel_task(self, task_id: str) -> None:
        """Cancel a task (sets status to cancelled)."""
        with self._lock:
            d = self._store.get_task(task_id)
            if d is None:
                raise KeyError(f"Task not found: {task_id}")
            d["status"] = "cancelled"
            d["next_run"] = None
            if (d.get("metadata") or {}).get("thozha_web_source") == WEB_SOURCE:
                if not self._store.update_web_task(d):
                    raise KeyError(f"Web task not found: {task_id}")
            else:
                self._store.update_task(d)

    def update_task(
        self,
        task_id: str,
        *,
        prompt: str | None = None,
        schedule_type: str | None = None,
        schedule_value: str | None = None,
        timezone_name: str | None = None,
    ) -> ScheduledTask:
        """Update only a Web schedule; caller must already pass the supervisor gate."""
        with self._lock:
            d = self._store.get_task(task_id)
            if d is None:
                raise KeyError(f"Task not found: {task_id}")
            metadata = d.get("metadata") or {}
            if metadata.get("thozha_web_source") != WEB_SOURCE:
                raise PermissionError(
                    "Only Web-owned schedules are mutable through this operation"
                )
            original_type = d["schedule_type"]
            original_value = d["schedule_value"]
            if prompt is not None:
                prompt = prompt.strip()
                if not prompt or len(prompt) > 2000:
                    raise ValueError("Web schedule prompt is invalid")
                d["prompt"] = prompt
            if schedule_type is not None:
                d["schedule_type"] = schedule_type
            if schedule_value is not None:
                d["schedule_value"] = schedule_value
            schedule_changed = (
                d["schedule_type"] != original_type
                or d["schedule_value"] != original_value
            )
            if timezone_name is not None:
                metadata["timezone"] = timezone_name
            _validate_web_schedule(
                d["schedule_type"],
                d["schedule_value"],
                timezone_name
                if timezone_name is not None
                else metadata.get("timezone"),
            )
            task = ScheduledTask.from_dict(d)
            task.metadata = metadata
            if schedule_changed:
                task.last_run = None
            task.next_run = self._compute_next_run(task)
            d = task.to_dict()
            if schedule_changed:
                d["last_run"] = None
            d["next_run"] = task.next_run
            if not self._store.update_web_task(d):
                raise KeyError(f"Web task not found: {task_id}")
            return task

    def delete_task(self, task_id: str) -> None:
        """Soft-delete a Web schedule; retained run history is not erased."""
        with self._lock:
            d = self._store.get_task(task_id)
            if d is None:
                raise KeyError(f"Task not found: {task_id}")
            if (d.get("metadata") or {}).get("thozha_web_source") != WEB_SOURCE:
                raise PermissionError(
                    "Only Web-owned schedules are mutable through this operation"
                )
            d["status"] = "cancelled"
            d["next_run"] = None
            if not self._store.update_web_task(d):
                raise KeyError(f"Web task not found: {task_id}")

    def run_task_once(self, task_id: str, *, idempotency_key: str) -> Dict[str, Any]:
        """Manually dispatch one Web occurrence through Link's supervisor lane."""
        if not _WEB_IDEMPOTENCY_RE.fullmatch(idempotency_key or ""):
            raise ValueError("manual Web run requires an Idempotency-Key")
        with self._lock:
            raw = self._store.get_task(task_id)
            if raw is None:
                raise KeyError(f"Task not found: {task_id}")
            task = ScheduledTask.from_dict(raw)
            if (task.metadata or {}).get("thozha_web_source") != WEB_SOURCE:
                raise PermissionError("Only Web-owned schedules can be manually run")
            if task.status == "cancelled":
                raise RuntimeError("Cancelled schedules cannot be manually run")
        occurrence_key = f"manual:{idempotency_key}"
        result = self._dispatch_web_occurrence(task, occurrence_key)
        if result.get("supervisor_task_id"):
            with self._lock:
                current = self._store.get_task(task_id)
                if current is not None:
                    metadata = current.get("metadata") or {}
                    metadata["last_manual_supervisor_task_id"] = result[
                        "supervisor_task_id"
                    ]
                    metadata["last_manual_run_at"] = _now_iso()
                    current["metadata"] = metadata
                    self._store.update_task(current)
        return result

    # -- Background loop -----------------------------------------------------

    def _poll_loop(self) -> None:
        """Poll for due tasks and execute them until stopped."""
        while not self._stop_event.is_set():
            try:
                now = _now_iso()
                with self._lock:
                    due = self._store.get_due_tasks(now)
                for task_dict in due:
                    task = ScheduledTask.from_dict(task_dict)
                    self._execute_task(task)
            except Exception:
                logger.exception("Scheduler poll error")
            self._stop_event.wait(timeout=self._poll_interval)

    def _execute_task(self, task: ScheduledTask) -> None:
        """Execute a single due task and log the result."""
        if (task.metadata or {}).get("thozha_web_source") == WEB_SOURCE:
            self._execute_web_schedule(task)
            return

        started_at = _now_iso()

        # Publish start event
        if self._bus is not None:
            self._bus.publish(
                SCHEDULER_TASK_START,
                {"task_id": task.id, "prompt": task.prompt},
            )

        success = False
        result_text = ""
        error_text = ""

        try:
            if self._system is not None:
                raw_tools = (
                    task.tools
                    if isinstance(task.tools, list)
                    else task.tools.split(",")
                )
                tools_list = (
                    [t.strip() for t in raw_tools if t.strip()] if task.tools else []
                )
                ask_kwargs: Dict[str, Any] = {
                    "agent": task.agent,
                    "tools": tools_list if tools_list else None,
                }
                meta = task.metadata or {}
                if meta.get("operator_id"):
                    ask_kwargs["system_prompt"] = meta.get("system_prompt", "")
                    ask_kwargs["operator_id"] = meta["operator_id"]
                result_text = self._system.ask(
                    task.prompt,
                    **ask_kwargs,
                )
            else:
                raise RuntimeError("scheduler execution engine is not configured")
            success = True
        except Exception as exc:
            error_text = str(exc)
            logger.error("Task %s failed: %s", task.id, exc)

        finished_at = _now_iso()

        # Log the run
        with self._lock:
            self._store.log_run(
                task_id=task.id,
                started_at=started_at,
                finished_at=finished_at,
                success=success,
                result=result_text,
                error=error_text,
            )

            # Update task state
            d = self._store.get_task(task.id)
            if d is not None:
                d["last_run"] = finished_at
                next_run = self._compute_next_run(ScheduledTask.from_dict(d))
                d["next_run"] = next_run
                if next_run is None:
                    d["status"] = "completed"
                self._store.update_task(d)

        # Publish end event
        if self._bus is not None:
            self._bus.publish(
                SCHEDULER_TASK_END,
                {
                    "task_id": task.id,
                    "success": success,
                    "result": result_text,
                    "error": error_text,
                },
            )

    def _execute_web_schedule(self, task: ScheduledTask) -> None:
        """Dispatch one due Web occurrence into the durable Link supervisor queue."""
        occurrence = task.next_run or _now_iso()
        if self._bus is not None:
            self._bus.publish(
                SCHEDULER_TASK_START,
                {"task_id": task.id, "source": WEB_SOURCE},
            )
        failure_type: str | None = None
        try:
            result = self._dispatch_web_occurrence(task, occurrence)
            if result.get("replayed"):
                return
            state = result.get("state", "accepted")
            supervisor_task_id = result.get("supervisor_task_id")
            success = state not in {"failed", "cancelled"}
        except Exception as exc:
            current = self._store.get_task(task.id)
            if (
                current is None
                or current.get("status") != "active"
                or current.get("next_run") != occurrence
            ):
                # A concurrent pause, delete, or reschedule won the claim.
                return
            state = "failed"
            supervisor_task_id = None
            success = False
            failure_type = type(exc).__name__
            logger.warning(
                "Web automation supervisor dispatch failed "
                "schedule_id=%s error_type=%s",
                task.id,
                type(exc).__name__,
            )
        finished_at = _now_iso()
        with self._lock:
            current = self._store.get_task(task.id)
            if current is None:
                return
            current["last_run"] = finished_at
            if supervisor_task_id:
                metadata = current.get("metadata") or {}
                metadata["last_supervisor_task_id"] = supervisor_task_id
                current["metadata"] = metadata
            next_run = self._compute_next_run(ScheduledTask.from_dict(current))
            current["next_run"] = next_run
            if failure_type == "PermissionError":
                current["status"] = "paused"
                current["next_run"] = None
                metadata = current.get("metadata") or {}
                metadata["web_last_error"] = "paired_device_or_scope_not_authorized"
                current["metadata"] = metadata
            if next_run is None and current.get("status") == "active":
                current["status"] = "completed"
            self._store.update_task(current)
        if self._bus is not None:
            # Never publish browser prompt text to the general event bus.
            self._bus.publish(
                SCHEDULER_TASK_END,
                {
                    "task_id": task.id,
                    "state": state,
                    "success": success,
                    "supervisor_task_id": supervisor_task_id,
                },
            )

    def _dispatch_web_occurrence(
        self, task: ScheduledTask, occurrence_key: str
    ) -> Dict[str, Any]:
        metadata = task.metadata or {}
        owner_id = metadata.get(WEB_OWNER_KEY)
        device_id = metadata.get(WEB_DEVICE_KEY)
        scope = metadata.get(WEB_SCOPE_KEY)
        if (
            metadata.get("thozha_web_source") != WEB_SOURCE
            or not isinstance(owner_id, str)
            or not owner_id
            or not isinstance(device_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", device_id)
            or scope != ["read_only"]
        ):
            raise PermissionError("Web schedule identity or read-only scope is invalid")
        if not isinstance(occurrence_key, str) or not occurrence_key:
            raise ValueError("schedule occurrence key is required")
        link_idempotency_key = hashlib.sha256(
            f"thozha-web-automation-v1:{task.id}:{occurrence_key}".encode("utf-8")
        ).hexdigest()
        run, _created = self._store.claim_run(
            task.id,
            occurrence_key=occurrence_key,
            idempotency_key=link_idempotency_key,
            started_at=_now_iso(),
            expected_next_run=(
                occurrence_key if not occurrence_key.startswith("manual:") else None
            ),
            allowed_statuses=(
                ("active", "paused", "completed")
                if occurrence_key.startswith("manual:")
                else None
            ),
        )
        if run.get("idempotency_key") != link_idempotency_key:
            raise ValueError("schedule occurrence idempotency conflict")
        if run["state"] in {"accepted", "completed", "failed", "cancelled"}:
            return {
                "state": run["state"],
                "supervisor_task_id": run.get("supervisor_task_id"),
                "result": run.get("result", ""),
                "replayed": True,
            }
        try:
            dispatcher = self._web_supervisor_dispatcher
            if dispatcher is None:
                raise RuntimeError("supervisor-backed Web scheduler is unavailable")
            # Dispatcher must revalidate the device binding and call
            # SupervisorStore.create_link_task with owner_actor=device:<id>.
            response = dispatcher(task.to_dict(), link_idempotency_key)
            if not isinstance(response, dict):
                raise RuntimeError("supervisor dispatcher returned an invalid result")
            supervisor_task_id = response.get("task_id")
            state = response.get("state") or response.get("status") or "CREATED"
            if not isinstance(supervisor_task_id, str) or not re.fullmatch(
                r"[0-9a-f]{32}", supervisor_task_id
            ):
                raise RuntimeError("supervisor dispatcher did not return a task ID")
            if state not in {
                "CREATED",
                "RUNNING",
                "WAITING_APPROVAL",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            }:
                raise RuntimeError(
                    "supervisor dispatcher returned an unknown task state"
                )
            run_state = {
                "CREATED": "accepted",
                "RUNNING": "accepted",
                "WAITING_APPROVAL": "waiting_approval",
            }.get(state, state.lower())
            accepted = state not in {"FAILED", "CANCELLED"}
            result_summary = (
                "Supervisor task accepted."
                if accepted
                else "Supervisor task was not accepted."
            )
            self._store.finish_run(
                run["id"],
                finished_at=_now_iso(),
                state=run_state,
                success=accepted,
                result=result_summary,
                error="" if accepted else state.lower(),
                supervisor_task_id=supervisor_task_id,
            )
            return {
                "state": run_state,
                "supervisor_task_id": supervisor_task_id,
                "result": result_summary,
                "replayed": False,
            }
        except Exception as exc:
            self._store.finish_run(
                run["id"],
                finished_at=_now_iso(),
                state="failed",
                success=False,
                error=type(exc).__name__,
            )
            raise

    def _compute_next_run(self, task: ScheduledTask) -> Optional[str]:
        """Compute the next run time for a task.

        Returns an ISO 8601 string, or ``None`` if the task should not run again.
        """
        now = datetime.now(timezone.utc)

        if task.schedule_type == "once":
            # If already run, no more runs
            if task.last_run is not None:
                return None
            # Otherwise the schedule_value is the target ISO datetime
            return task.schedule_value

        if task.schedule_type == "interval":
            seconds = float(task.schedule_value)
            next_time = now + timedelta(seconds=seconds)
            return next_time.isoformat()

        if task.schedule_type == "cron":
            timezone_name = str((task.metadata or {}).get("timezone") or "") or None
            return self._compute_next_cron(task.schedule_value, now, timezone_name)

        return None

    @staticmethod
    def _compute_next_cron(
        cron_expr: str,
        now: datetime,
        timezone_name: str | None = None,
    ) -> Optional[str]:
        """Compute the next run time from a cron expression.

        Evaluate in the configured IANA timezone and persist the result in UTC.
        Missing cron support is fatal instead of silently changing semantics.
        """
        try:
            from croniter import croniter  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "croniter is required for cron schedules; reinstall OpenJarvis"
            ) from exc

        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local_now = now.astimezone(ZoneInfo(timezone_name)) if timezone_name else now
        next_local = croniter(cron_expr, local_now).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=local_now.tzinfo)
        return next_local.astimezone(timezone.utc).isoformat()


def make_web_supervisor_dispatcher(
    supervisor_store: Any,
    *,
    is_web_device_active: Callable[[str, str], bool],
    is_link_runner_ready: Callable[[], bool],
) -> Callable[[Dict[str, Any], str], Dict[str, Any]]:
    """Bind Web schedules to normal Link task creation and approval policy.

    The LinkTaskExecutionRunner already polls durable CREATED Link rows. This
    adapter never invokes ``System.ask`` or a tool itself. Device pairing is
    revalidated for every occurrence, and only read-only scope is accepted.
    """

    def dispatch(task: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        metadata = task.get("metadata") or {}
        owner_id = metadata.get(WEB_OWNER_KEY)
        device_id = metadata.get(WEB_DEVICE_KEY)
        scope = metadata.get(WEB_SCOPE_KEY)
        if (
            metadata.get("thozha_web_source") != WEB_SOURCE
            or not isinstance(owner_id, str)
            or not owner_id
            or not isinstance(device_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", device_id)
            or scope != ["read_only"]
        ):
            raise PermissionError("Web schedule identity or scope is invalid")
        if not is_web_device_active(owner_id, device_id):
            raise PermissionError(
                "Web schedule paired device is inactive or no longer authorized"
            )
        if not is_link_runner_ready():
            raise RuntimeError("durable Link task runner is not running")
        run = supervisor_store.create_link_task(
            task["prompt"],
            capability_scope=["read_only"],
            idempotency_key=idempotency_key,
            owner_actor="device:" + device_id,
        )
        return {"task_id": run["task_id"], "state": run.get("status", "CREATED")}

    dispatch.is_ready = is_link_runner_ready  # type: ignore[attr-defined]
    return dispatch


__all__ = [
    "SCHEDULER_TASK_END",
    "SCHEDULER_TASK_START",
    "ScheduledTask",
    "TaskScheduler",
    "make_web_supervisor_dispatcher",
]
