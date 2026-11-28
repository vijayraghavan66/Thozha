"""Finite, callback-driven goal step runner.

This engine performs no browser, LLM, shell, payment, or supervisor operation on
its own. A pure planner describes one bounded step; the injected async executor
delegates that step to an existing authorized adapter and returns immediately
with a checkpoint or a wait disposition.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from openjarvis.goals.models import (
    GoalActionKind,
    GoalApprovalBinding,
    GoalDisposition,
    GoalError,
    GoalExecutionView,
    GoalLease,
    GoalPolicy,
    GoalRunReport,
    GoalSnapshot,
    GoalStatus,
    GoalStepContext,
    GoalStepPlan,
    GoalStepResult,
    GoalValidationError,
    GoalWakeReason,
    ProviderPollDecision,
    SupervisorApprovalOutcome,
    normalize_datetime,
    utc_now,
)
from openjarvis.goals.store import GoalStore


StepExecutor = Callable[[GoalStepContext], Awaitable[GoalStepResult]]
AsyncPlanner = Callable[[GoalExecutionView], Awaitable[GoalStepPlan]]
ApprovalVerifier = Callable[[GoalApprovalBinding], SupervisorApprovalOutcome | str]
Jitter = Callable[[float], float]


def retry_delay_seconds(attempt: int, policy: GoalPolicy, *, jitter: Jitter | None = None) -> int:
    """Bounded exponential backoff with injectable jitter for deterministic tests."""
    if attempt < 1:
        raise GoalValidationError("attempt must be positive")
    base = min(policy.retry_max_seconds, policy.retry_base_seconds * (2 ** min(attempt - 1, 20)))
    factor = (jitter or random.uniform)(0.85, 1.15) if jitter is not None else random.uniform(0.85, 1.15)
    if not isinstance(factor, (int, float)) or not 0.5 <= factor <= 1.5:
        raise GoalValidationError("retry jitter factor is outside the allowed range")
    return max(1, min(policy.retry_max_seconds, int(base * factor)))


class GoalEngine:
    """Run at most one finite step per claimed goal and scheduler invocation."""

    def __init__(
        self,
        store: GoalStore,
        *,
        planner: AsyncPlanner,
        executor: StepExecutor,
        worker_id: str,
        approval_verifier: ApprovalVerifier | None = None,
        jitter: Jitter | None = None,
        planner_timeout_seconds: float = 0.25,
    ):
        if not callable(planner) or not callable(executor):
            raise GoalValidationError("planner and executor callbacks are required")
        if not (inspect.iscoroutinefunction(planner) or inspect.iscoroutinefunction(getattr(planner, "__call__", None))):
            raise GoalValidationError("planner must be async so its bounded timeout can be enforced")
        if not (inspect.iscoroutinefunction(executor) or inspect.iscoroutinefunction(getattr(executor, "__call__", None))):
            raise GoalValidationError("step executor must be async and cancellation-cooperative")
        if not 0.01 <= planner_timeout_seconds <= 1.0:
            raise GoalValidationError("planner_timeout_seconds must be between 0.01 and 1")
        self.store = store
        self.planner = planner
        self.executor = executor
        self.worker_id = worker_id
        self.approval_verifier = approval_verifier
        self.jitter = jitter
        self.planner_timeout_seconds = planner_timeout_seconds
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._inflight: dict[str, tuple[GoalLease, str | None]] = {}
        self._wake = asyncio.Event()

    async def run_due_once(self, *, now: datetime | None = None, limit: int = 1) -> list[GoalRunReport]:
        """Claim due goals, execute no more than one bounded step each, then return."""
        self._wake.clear()
        now = normalize_datetime(now or utc_now(), "now")
        leases = self.store.claim_due(
            self.worker_id,
            now=now,
            limit=max(1, min(limit, 10)),
        )
        reports: list[GoalRunReport] = []
        for lease in leases:
            reports.append(await self._run_one(lease, now=now))
        return reports

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        limit: int = 10,
        maximum_idle_seconds: int = 60,
    ) -> None:
        """Run bounded due work and wait without keeping a model/browser active."""
        if not 1 <= maximum_idle_seconds <= 3600:
            raise GoalValidationError("scheduler idle interval is invalid")
        while not stop_event.is_set():
            if self._wake.is_set():
                self._wake.clear()
            await self.run_due_once(limit=limit)
            if stop_event.is_set():
                return
            timeout = self.store.seconds_until_next_due(maximum_seconds=maximum_idle_seconds)
            wake_task = asyncio.create_task(self._wake.wait())
            stop_task = asyncio.create_task(stop_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {wake_task, stop_task}, timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if stop_task in done and stop_event.is_set():
                    return
            finally:
                for task in (wake_task, stop_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(wake_task, stop_task, return_exceptions=True)

    async def _run_one(self, lease: GoalLease, *, now: datetime) -> GoalRunReport:
        goal = lease.goal
        view = goal.to_execution_view()
        try:
            plan = await asyncio.wait_for(self.planner(view), timeout=self.planner_timeout_seconds)
            plan = plan.validated(GoalPolicy(
                max_attempts=goal.max_attempts,
                retry_base_seconds=goal.retry_base_seconds,
                retry_max_seconds=goal.retry_max_seconds,
                max_live_step_seconds=goal.max_live_step_seconds,
            ))
        except Exception as exc:
            snapshot = self.store.fail_unprepared_step(
                lease, error_code="PLANNER_TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else self._error_code(exc, "PLANNER_FAILED"), now=now
            )
            return GoalRunReport(snapshot.goal_id, snapshot.status, None, snapshot.summary, snapshot.next_wake_at)

        try:
            run_key = self.store.prepare_step(lease, plan, now=now)
        except GoalError:
            raise
        cancel_event = asyncio.Event()
        self._cancel_events[goal.goal_id] = cancel_event
        self._inflight[goal.goal_id] = (lease, run_key)
        current = self.store.get_goal(goal.goal_id, goal.owner_actor)
        deadline_seconds = (current.deadline_at - utc_now()).total_seconds()
        if deadline_seconds <= 0:
            try:
                self.store.prepare_step(lease, plan, now=utc_now())
            except Exception:
                pass  # prepare_step commits deadline expiry before raising LeaseLost
            snapshot = self.store.get_goal(goal.goal_id, goal.owner_actor)
            return GoalRunReport(snapshot.goal_id, snapshot.status, plan.step_key, snapshot.summary, snapshot.next_wake_at)
        timeout_seconds = min(plan.max_live_step_seconds, deadline_seconds)
        approved_action = (
            self.store.consumed_approval_binding_for_action(
                goal.goal_id, goal.owner_actor, plan.action_digest, now=utc_now(),
            )
            if plan.action_kind == GoalActionKind.SUPERVISOR_ACTION and plan.action_digest
            else None
        )
        dispatch_state = {"started": False}

        def mark_dispatched(approval_id: str) -> bool:
            if approved_action is None or approval_id != approved_action.approval_id:
                raise GoalValidationError("no consumed supervisor approval matches this action")
            claimed = self.store.mark_supervisor_action_dispatched(
                lease, plan, run_key, approval_id=approval_id, now=utc_now(),
            )
            dispatch_state["started"] = bool(claimed)
            return bool(claimed)

        context = GoalStepContext(
            goal=current.to_execution_view(),
            plan=plan,
            idempotency_key=run_key,
            timeout_seconds=max(1, int(timeout_seconds)),
            cancel_event=cancel_event,
            approved_action=approved_action,
            mark_supervisor_action_dispatched=mark_dispatched if plan.action_kind == GoalActionKind.SUPERVISOR_ACTION else None,
            reserve_provider_poll=lambda provider, interval=300: self.store.reserve_provider_poll(
                goal.owner_actor, provider, minimum_interval_seconds=interval,
            ),
        )
        step_task = asyncio.create_task(self.executor(context))
        cancel_task = asyncio.create_task(cancel_event.wait())
        started = time.monotonic()
        try:
            done, _ = await asyncio.wait(
                {step_task, cancel_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_event.is_set():
                step_task.cancel()
                await asyncio.wait({step_task}, timeout=0.1)
                snapshot = self.store.get_goal(goal.goal_id, goal.owner_actor)
                return GoalRunReport(snapshot.goal_id, snapshot.status, plan.step_key, "Goal cancelled.", snapshot.next_wake_at)
            if step_task not in done:
                step_task.cancel()
                await asyncio.wait({step_task}, timeout=0.1)
                if plan.action_kind == GoalActionKind.SUPERVISOR_ACTION:
                    result = GoalStepResult(
                        GoalDisposition.NEEDS_REVIEW,
                        "Supervisor action timed out with an unknown result; owner review is required before retrying.",
                        error_code="SUPERVISOR_RESULT_UNKNOWN",
                    )
                else:
                    result = GoalStepResult(
                        GoalDisposition.FAILED,
                        "Read-only step timed out and can retry safely with the same idempotency key.",
                        retryable=True,
                        error_code="STEP_TIMEOUT",
                    )
            else:
                result = step_task.result()
                if not isinstance(result, GoalStepResult):
                    raise GoalValidationError("goal executor returned an invalid result")
            if dispatch_state["started"]:
                action_result = result.supervisor_action_result
                if action_result is None:
                    action_result = "completed" if result.disposition == GoalDisposition.COMPLETED else "unknown"
                self.store.record_supervisor_action_result(lease, run_key, result=action_result, now=utc_now())
                if action_result == "unknown" and result.disposition != GoalDisposition.NEEDS_REVIEW:
                    result = GoalStepResult(
                        GoalDisposition.NEEDS_REVIEW,
                        "The approved action outcome is uncertain; owner review is required before retrying.",
                        error_code="SUPERVISOR_ACTION_RESULT_UNKNOWN",
                    )
            policy = GoalPolicy(
                max_attempts=goal.max_attempts,
                retry_base_seconds=goal.retry_base_seconds,
                retry_max_seconds=goal.retry_max_seconds,
                max_live_step_seconds=goal.max_live_step_seconds,
            )
            # Preserve provider Retry-After exactly. If it exceeds the
            # exponential-retry ceiling, schedule a durable wait instead of
            # retrying earlier than the provider allows.
            if result.retryable and result.retry_after_seconds and result.retry_after_seconds > policy.retry_max_seconds:
                result = GoalStepResult(
                    GoalDisposition.WAITING_UNTIL,
                    result.summary,
                    checkpoint=result.checkpoint,
                    progress=result.progress,
                    next_wake_at=utc_now() + timedelta(seconds=result.retry_after_seconds),
                    error_code=result.error_code,
                    evidence=result.evidence,
                )
            delay = (result.retry_after_seconds or retry_delay_seconds(
                current.attempts_used, policy, jitter=self.jitter,
            )) if result.retryable else None
            snapshot = self.store.finish_step(
                lease,
                plan,
                run_key,
                result,
                now=utc_now(),
                duration_ms=min(600_000, int((time.monotonic() - started) * 1000)),
                retry_delay_seconds=delay,
            )
            return GoalRunReport(snapshot.goal_id, snapshot.status, plan.step_key, snapshot.summary, snapshot.next_wake_at)
        except asyncio.CancelledError:
            step_task.cancel()
            raise
        except Exception as exc:
            # The callback is injected. Never put exception messages, objective,
            # provider data, or credentials into the durable event stream.
            if plan.action_kind == GoalActionKind.SUPERVISOR_ACTION:
                if dispatch_state["started"]:
                    try:
                        self.store.record_supervisor_action_result(lease, run_key, result="unknown", now=utc_now())
                    except Exception:
                        pass
                result = GoalStepResult(
                    GoalDisposition.NEEDS_REVIEW,
                    "Supervisor action result is uncertain; owner review is required before retrying.",
                    error_code="SUPERVISOR_RESULT_UNKNOWN",
                )
            else:
                result = GoalStepResult(
                    GoalDisposition.FAILED,
                    "Read-only step failed and will retry within the configured budget.",
                    retryable=True,
                    error_code=self._error_code(exc, "STEP_FAILED"),
                )
            policy = GoalPolicy(
                max_attempts=goal.max_attempts,
                retry_base_seconds=goal.retry_base_seconds,
                retry_max_seconds=goal.retry_max_seconds,
                max_live_step_seconds=goal.max_live_step_seconds,
            )
            delay = (result.retry_after_seconds or retry_delay_seconds(
                current.attempts_used, policy, jitter=self.jitter,
            )) if result.retryable else None
            snapshot = self.store.finish_step(lease, plan, run_key, result, now=utc_now(),
                duration_ms=min(600_000, int((time.monotonic() - started) * 1000)), retry_delay_seconds=delay)
            return GoalRunReport(snapshot.goal_id, snapshot.status, plan.step_key, snapshot.summary, snapshot.next_wake_at)
        finally:
            cancel_task.cancel()
            self._cancel_events.pop(goal.goal_id, None)
            self._inflight.pop(goal.goal_id, None)

    async def cancel_goal(self, goal_id: str, owner_actor: str, *, now: datetime | None = None) -> GoalSnapshot:
        """Cancel storage first, then signal the active cooperative finite callback."""
        snapshot = self.store.cancel_goal(goal_id, owner_actor, now=now)
        event = self._cancel_events.get(goal_id)
        if event is not None:
            event.set()
        self._wake.set()
        return snapshot

    def resolve_supervisor_approval(
        self,
        goal_id: str,
        owner_actor: str,
        approval_id: str,
        *,
        event_id: str,
        now: datetime | None = None,
    ) -> tuple[GoalSnapshot, bool]:
        """Wake only after an injected supervisor verifier confirms the exact decision."""
        if self.approval_verifier is None:
            raise GoalValidationError("a SupervisorStore approval verifier is required")
        binding = self.store.pending_approval_binding(goal_id, owner_actor, approval_id)
        outcome = SupervisorApprovalOutcome(self.approval_verifier(binding))
        return self.store.resolve_approval_binding(
            goal_id,
            owner_actor,
            approval_id,
            supervisor_task_id=binding.supervisor_task_id,
            action_digest=binding.action_digest,
            outcome=outcome,
            event_id=event_id,
            now=now,
        )

    def wake_on_event(
        self,
        goal_id: str,
        owner_actor: str,
        reason: GoalWakeReason | str,
        *,
        event_id: str,
        checkpoint_update: dict[str, object] | None = None,
        summary: str | None = None,
        now: datetime | None = None,
    ) -> tuple[GoalSnapshot, bool]:
        result = self.store.wake_on_event(
            goal_id,
            owner_actor,
            reason,
            event_id=event_id,
            checkpoint_update=checkpoint_update,
            summary=summary,
            now=now,
        )
        self._wake.set()
        return result

    async def wait_for_wake(self, timeout_seconds: float) -> bool:
        """Wait only on scheduler events/timer; it does not hold a goal worker alive."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.01, min(float(timeout_seconds), 3600)))
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _error_code(exc: Exception, fallback: str) -> str:
        name = type(exc).__name__.upper()
        name = re.sub(r"[^A-Z0-9_]", "_", name)[:40].strip("_")
        return name if name and name[0].isalpha() else fallback
