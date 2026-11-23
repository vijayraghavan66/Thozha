"""Durable, bounded background execution for Jarvis Link AI tasks.

Task acceptance is committed in SupervisorStore before this runner sees work.
Only CREATED rows with a stored objective may be claimed. Once claimed, the
objective is cleared transactionally; a process restart pauses an in-flight
task and never replays a possibly side-effecting Codex turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import traceback
from typing import Any

from openjarvis.server.performance import PERF_LOGGER

logger = logging.getLogger(__name__)


def _perf(trace: str, event: str, started_ns: int | None = None, **fields: Any) -> None:
    elapsed = "" if started_ns is None else f" elapsed_ms={(time.perf_counter_ns() - started_ns) / 1_000_000:.3f}"
    safe = " ".join(
        f"{key}={str(value)[:80]}"
        for key, value in fields.items()
        if key in {"task_id", "state", "scope"} and isinstance(value, (str, int, bool))
    )
    PERF_LOGGER.info(
        "perf trace=%s event=%s wall_ms=%d mono_ns=%d%s%s",
        trace, event, int(time.time() * 1000), time.perf_counter_ns(), elapsed,
        (" " + safe) if safe else "",
    )


class LinkTaskExecutionRunner:
    def __init__(self, *, app: Any, store: Any, concurrency: int = 2, poll_interval: float = 0.25) -> None:
        if not 1 <= concurrency <= 8:
            raise ValueError("Link task concurrency must be between 1 and 8")
        if not 0.05 <= poll_interval <= 5.0:
            raise ValueError("Link task poll interval must be between 0.05 and 5 seconds")
        self.app = app
        self.store = store
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self._active: dict[str, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._loop_task is None:
            self._stopping = False
            self._loop_task = asyncio.create_task(self._run(), name="jarvis-link-task-runner")
            self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopping:
            self._prune()
            capacity = self.concurrency - len(self._active)
            if capacity > 0:
                try:
                    rows = await asyncio.to_thread(self.store.queued_link_tasks, min(32, capacity * 4))
                except Exception:
                    logger.exception("Link task queue read failed")
                    rows = []
                for row in rows:
                    task_id = row.get("task_id")
                    if not isinstance(task_id, str) or task_id in self._active:
                        continue
                    if len(self._active) >= self.concurrency:
                        break
                    worker = asyncio.create_task(self._execute(row), name=f"jarvis-link-{task_id[:8]}")
                    self._active[task_id] = worker
            self._wake.clear()
            if self._active:
                try:
                    await asyncio.wait(list(self._active.values()), timeout=self.poll_interval, return_when=asyncio.FIRST_COMPLETED)
                except asyncio.CancelledError:
                    raise
            else:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        self._prune()

    def _prune(self) -> None:
        for task_id, worker in list(self._active.items()):
            if not worker.done():
                continue
            try:
                worker.result()
            except Exception:
                logger.exception("Link task worker failed unexpectedly task=%s", task_id)
            self._active.pop(task_id, None)

    async def _execute(self, row: dict[str, Any]) -> None:
        from openjarvis.server.models import ChatCompletionRequest, ChatMessage
        from openjarvis.server.routes import _run_supervised_agent

        task_id = str(row["task_id"])
        objective = row.get("objective_text")
        idempotency_key = row.get("idempotency_key")
        trace = hashlib.sha256(str(idempotency_key or task_id).encode("utf-8")).hexdigest()[:12]
        started_ns = time.perf_counter_ns()
        _perf(trace, "runner_claim_started", started_ns, task_id=task_id[:8])
        objective_digest = row.get("objective_digest")
        claimed = False
        try:
            if not isinstance(objective, str) or not objective.strip() or not isinstance(idempotency_key, str):
                raise ValueError("persisted Link task prompt is unavailable")
            if hashlib.sha256(objective.strip().encode("utf-8")).hexdigest() != objective_digest:
                raise ValueError("persisted Link task objective digest does not match")
            task = self.store.get_task(task_id)
            metadata = json.loads(task.get("recovery_json") or "{}") if task else {}
            scope = metadata.get("capability_scope")
            if not isinstance(scope, list) or scope != json.loads(row["scope"]):
                raise ValueError("persisted Link task scope is invalid")
            claim_result = await asyncio.to_thread(
                self.store.start_link_task,
                task_id,
                objective_digest=objective_digest,
                objective_text_digest=objective_digest,
                capability_scope=scope,
                idempotency_key=idempotency_key,
            )
            if claim_result.get("replay"):
                _perf(trace, "runner_duplicate_claim_ignored", started_ns, task_id=task_id[:8])
                return
            claimed = True
            _perf(trace, "runner_claimed", started_ns, task_id=task_id[:8], scope=scope[0] if scope else "")
            agent = getattr(self.app.state, "agent", None)
            model = getattr(self.app.state, "model", "") or getattr(agent, "_model", "")
            if agent is None or not model:
                raise RuntimeError("Link task runner lost its configured agent")
            request = ChatCompletionRequest(
                model=model,
                messages=[ChatMessage(role="user", content=objective.strip())],
                stream=False,
            )
            model_started_ns = time.perf_counter_ns()
            _perf(trace, "agent_execution_started", model_started_ns, task_id=task_id[:8])
            await asyncio.to_thread(
                _run_supervised_agent,
                task_id,
                None,
                agent,
                model,
                request,
                None,
                trace_store=getattr(self.app.state, "trace_store", None),
                bus=getattr(self.app.state, "bus", None),
            )
            _perf(trace, "agent_execution_returned", model_started_ns, task_id=task_id[:8])
            current = self.store.get_task(task_id) or {}
            if current.get("state") == "WAITING_APPROVAL":
                pending = [item for item in self.store.pending_approvals(100) if item.get("task_id") == task_id]
                run = self.store.link_task_run(task_id) or {}
                if pending and run.get("status") != "WAITING_APPROVAL":
                    self.store.mark_link_task_waiting_approval(task_id, pending[-1]["id"])
            _perf(trace, "runner_task_state", started_ns, task_id=task_id[:8], state=str(current.get("state") or "UNKNOWN"))
        except Exception as exc:
            frames = ",".join(
                f"{frame.name}:{frame.lineno}"
                for frame in traceback.extract_tb(exc.__traceback__)[-6:]
            )
            logger.error(
                "Link task execution failed task=%s error=%s frames=%s",
                task_id[:8], type(exc).__name__, frames[:360],
            )
            run = self.store.link_task_run(task_id) or {}
            if not claimed and run.get("status") == "CREATED":
                try:
                    self.store.fail_queued_link_task(task_id, error_code=type(exc).__name__)
                except (KeyError, RuntimeError, ValueError):
                    logger.exception("Could not persist queued Link task failure")
            elif claimed and run.get("status") == "RUNNING":
                try:
                    self.store.finish_link_task(task_id, error=type(exc).__name__)
                except (KeyError, RuntimeError):
                    pass
                current = self.store.get_task(task_id) or {}
                if current.get("state") in {"RUNNING", "VERIFYING", "PLANNING"}:
                    try:
                        self.store.transition(task_id, "FAILED", expected=current["state"], error=type(exc).__name__)
                    except (KeyError, RuntimeError, ValueError):
                        pass

    async def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            self._prune()
            queued = await asyncio.to_thread(self.store.queued_link_tasks, 1)
            if not self._active and not queued:
                return True
            self.wake()
            await asyncio.sleep(0.02)
        return False

    async def stop(self, *, drain_timeout: float = 2.0) -> None:
        self._stopping = True
        self.wake()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=drain_timeout)
            except asyncio.TimeoutError:
                self._loop_task.cancel()
                await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        if self._active:
            # Do not cancel threads running Codex/MCP calls. A restart will pause
            # their supervisor records and refuse to replay them automatically.
            done, _ = await asyncio.wait(list(self._active.values()), timeout=drain_timeout)
            for task in done:
                try:
                    task.result()
                except Exception:
                    logger.exception("Link task failed while draining")
            self._active = {key: task for key, task in self._active.items() if not task.done()}
