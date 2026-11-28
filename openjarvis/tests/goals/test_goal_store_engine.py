from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from openjarvis.goals.engine import GoalEngine
from openjarvis.goals.models import (
    GoalActionKind,
    GoalConflict,
    GoalCreate,
    GoalDisposition,
    GoalPolicy,
    GoalStatus,
    GoalStepPlan,
    GoalStepResult,
    GoalValidationError,
    GoalWakeReason,
    SupervisorApprovalOutcome,
    utc_now,
)
from openjarvis.goals.store import GoalStore


OWNER = "device:" + "a" * 32
DIGEST = "b" * 64
APPROVAL = "c" * 32
TASK = "d" * 32


def _store(tmp_path) -> GoalStore:
    return GoalStore(tmp_path / "goals.sqlite3")


def _create(store: GoalStore, *, key: str = "goal-request-00000001"):
    now = utc_now()
    request = GoalCreate(
        owner_actor=OWNER,
        idempotency_key=key,
        title="Track a Chennai movie show",
        objective="Find read-only showtime and seat-map information.",
        deadline_at=now + timedelta(hours=2),
        policy=GoalPolicy(max_attempts=5, retry_base_seconds=5, retry_max_seconds=60),
        next_wake_at=now,
    )
    return store.create_goal(request, now=now)[0], now


def _plan(kind=GoalActionKind.READ_ONLY):
    return GoalStepPlan(
        step_key="discover.showtimes",
        action_kind=kind,
        description="Read current showtime availability",
        max_live_step_seconds=20,
        action_digest=DIGEST if kind == GoalActionKind.SUPERVISOR_ACTION else None,
    )


def _waiting_approval(store: GoalStore):
    goal, now = _create(store)
    lease = store.claim_due("worker-1", now=now)[0]
    plan = _plan(GoalActionKind.SUPERVISOR_ACTION)
    run_key = store.prepare_step(lease, plan, now=now)
    result = GoalStepResult(
        GoalDisposition.WAITING_APPROVAL,
        "Review the exact seat hold proposal.",
        approval_id=APPROVAL,
        supervisor_task_id=TASK,
        approval_expires_at=now + timedelta(minutes=10),
    )
    snapshot = store.finish_step(lease, plan, run_key, result, now=now)
    return snapshot, plan, run_key, now


def test_consumed_approval_resumes_same_bound_action_once(tmp_path):
    store = _store(tmp_path)
    try:
        waiting, plan, run_key, now = _waiting_approval(store)
        with pytest.raises(GoalConflict):
            store.wake_on_event(
                waiting.goal_id, OWNER, GoalWakeReason.USER_REQUESTED,
                event_id="user-wake-00000001", now=now + timedelta(seconds=1),
            )

        resolved, changed = store.resolve_approval_binding(
            waiting.goal_id, OWNER, APPROVAL,
            supervisor_task_id=TASK, action_digest=DIGEST,
            outcome=SupervisorApprovalOutcome.CONSUMED,
            event_id="supervisor-event-0000001", now=now + timedelta(seconds=1),
        )
        assert changed and resolved.status == GoalStatus.ACTIVE
        assert store.resolve_approval_binding(
            waiting.goal_id, OWNER, APPROVAL,
            supervisor_task_id=TASK, action_digest=DIGEST,
            outcome=SupervisorApprovalOutcome.CONSUMED,
            event_id="supervisor-event-0000001", now=now + timedelta(seconds=2),
        )[1] is False

        lease = store.claim_due("worker-2", now=now + timedelta(seconds=2))[0]
        assert store.prepare_step(lease, plan, now=now + timedelta(seconds=2)) == run_key
        binding = store.consumed_approval_binding_for_action(waiting.goal_id, OWNER, DIGEST, now=now + timedelta(seconds=2))
        assert binding is not None and binding.approval_id == APPROVAL
        assert store.mark_supervisor_action_dispatched(
            lease, plan, run_key, approval_id=APPROVAL, now=now + timedelta(seconds=2),
        )
        with pytest.raises(GoalConflict):
            store.mark_supervisor_action_dispatched(
                lease, plan, run_key, approval_id=APPROVAL, now=now + timedelta(seconds=2),
            )
        store.record_supervisor_action_result(lease, run_key, result="completed", now=now + timedelta(seconds=3))
        completed = store.finish_step(
            lease, plan, run_key,
            GoalStepResult(GoalDisposition.COMPLETED, "Approved action completed.", supervisor_action_result="completed"),
            now=now + timedelta(seconds=3),
        )
        assert completed.status == GoalStatus.COMPLETED
    finally:
        store.close()


def test_expired_approval_never_dispatches(tmp_path):
    store = _store(tmp_path)
    try:
        waiting, _, _, now = _waiting_approval(store)
        expired, changed = store.resolve_approval_binding(
            waiting.goal_id, OWNER, APPROVAL,
            supervisor_task_id=TASK, action_digest=DIGEST,
            outcome=SupervisorApprovalOutcome.CONSUMED,
            event_id="late-supervisor-event", now=now + timedelta(minutes=11),
        )
        assert changed and expired.status == GoalStatus.ACTIVE
        assert expired.last_error_code == "APPROVAL_EXPIRED"
        assert store.consumed_approval_binding_for_action(waiting.goal_id, OWNER, DIGEST, now=now + timedelta(minutes=11)) is None
    finally:
        store.close()


def test_cancel_pending_approval_closes_goal_binding_and_step_run(tmp_path):
    store = _store(tmp_path)
    try:
        waiting, _, run_key, now = _waiting_approval(store)
        cancelled = store.cancel_goal(waiting.goal_id, OWNER, now=now + timedelta(seconds=1))
        assert cancelled.status == GoalStatus.CANCELLED
        binding = store._db.execute("SELECT status FROM goal_approval_bindings WHERE approval_id=?", (APPROVAL,)).fetchone()
        run = store._db.execute("SELECT status FROM goal_step_runs WHERE run_key=?", (run_key,)).fetchone()
        assert binding["status"] == "cancelled"
        assert run["status"] == "cancelled"
        with pytest.raises(GoalConflict):
            store.resolve_approval_binding(
                waiting.goal_id, OWNER, APPROVAL,
                supervisor_task_id=TASK, action_digest=DIGEST,
                outcome=SupervisorApprovalOutcome.CONSUMED,
                event_id="approval-after-cancel", now=now + timedelta(seconds=2),
            )
    finally:
        store.close()


def test_cancel_after_dispatch_requires_review(tmp_path):
    store = _store(tmp_path)
    try:
        waiting, plan, run_key, now = _waiting_approval(store)
        store.resolve_approval_binding(
            waiting.goal_id, OWNER, APPROVAL, supervisor_task_id=TASK,
            action_digest=DIGEST, outcome=SupervisorApprovalOutcome.CONSUMED,
            event_id="approved-for-cancel", now=now + timedelta(seconds=1),
        )
        lease = store.claim_due("worker-2", now=now + timedelta(seconds=2))[0]
        store.prepare_step(lease, plan, now=now + timedelta(seconds=2))
        store.mark_supervisor_action_dispatched(lease, plan, run_key, approval_id=APPROVAL, now=now + timedelta(seconds=2))
        cancelled = store.cancel_goal(waiting.goal_id, OWNER, now=now + timedelta(seconds=3))
        assert cancelled.status == GoalStatus.NEEDS_REVIEW
        assert cancelled.last_error_code == "SUPERVISOR_ACTION_RESULT_UNKNOWN"
        assert store.cancel_goal(waiting.goal_id, OWNER, now=now + timedelta(seconds=4)).status == GoalStatus.NEEDS_REVIEW
    finally:
        store.close()


def test_read_only_interrupted_step_recovers_with_same_idempotency_key(tmp_path):
    store = _store(tmp_path)
    try:
        goal, now = _create(store)
        lease = store.claim_due("worker-1", now=now)[0]
        plan = _plan()
        first_key = store.prepare_step(lease, plan, now=now)
        recovered = store.claim_due("worker-2", now=lease.lease_expires_at + timedelta(seconds=1))
        assert len(recovered) == 1
        assert recovered[0].goal.status == GoalStatus.ACTIVE
        assert store.prepare_step(recovered[0], plan, now=lease.lease_expires_at + timedelta(seconds=2)) == first_key
    finally:
        store.close()


def test_provider_poll_retry_after_survives_store_restart(tmp_path):
    path = tmp_path / "goals.sqlite3"
    store = GoalStore(path)
    now = utc_now()
    try:
        reservation = store.reserve_provider_poll(
            OWNER, "district", minimum_interval_seconds=60,
            retry_after_seconds=300, policy_source="provider_retry_after", now=now,
        )
        assert reservation.allowed
        assert reservation.next_allowed_at == now + timedelta(seconds=300)
    finally:
        store.close()
    restarted = GoalStore(path)
    try:
        denied = restarted.reserve_provider_poll(
            OWNER, "district", minimum_interval_seconds=60, now=now + timedelta(seconds=90),
        )
        assert not denied.allowed
        assert abs((denied.next_allowed_at - (now + timedelta(seconds=300))).total_seconds()) < 0.002
    finally:
        restarted.close()


def test_retry_after_extends_existing_persisted_poll_deadline(tmp_path):
    store = _store(tmp_path)
    now = utc_now()
    try:
        store.reserve_provider_poll(
            OWNER, "district", minimum_interval_seconds=60,
            retry_after_seconds=300, policy_source="provider_retry_after", now=now,
        )
        extended = store.reserve_provider_poll(
            OWNER, "district", minimum_interval_seconds=60,
            retry_after_seconds=600, policy_source="provider_retry_after",
            now=now + timedelta(seconds=90),
        )
        assert not extended.allowed
        assert abs((extended.next_allowed_at - (now + timedelta(seconds=690))).total_seconds()) < 0.002
    finally:
        store.close()


def test_scheduler_idle_delay_tracks_durable_due_time(tmp_path):
    store = _store(tmp_path)
    try:
        goal, now = _create(store)
        assert store.seconds_until_next_due(now=now, maximum_seconds=60) == 0.05
        lease = store.claim_due("worker-1", now=now)[0]
        assert abs(store.seconds_until_next_due(now=now, maximum_seconds=100) - 75) < 0.002
        assert abs((lease.lease_expires_at - (now + timedelta(seconds=75))).total_seconds()) < 0.002
    finally:
        store.close()


def test_objective_checkpoint_and_event_redact_or_reject_credentials(tmp_path):
    store = _store(tmp_path)
    try:
        goal, now = _create(store)
        assert "objective" not in goal.to_wire()
        assert "checkpoint" not in goal.to_wire()
        with pytest.raises(GoalValidationError):
            GoalCreate(
                owner_actor=OWNER, idempotency_key="credential-0000001", title="Track",
                objective="My OTP is 123456", deadline_at=now + timedelta(hours=1),
                next_wake_at=now,
            ).validated()
        assert store.list_goals("device:" + "e" * 32) == []
    finally:
        store.close()


def test_engine_runs_one_finite_step_and_persists_retry_after(tmp_path):
    store = _store(tmp_path)
    seen = {}

    async def planner(view):
        seen["has_owner"] = hasattr(view, "owner_actor")
        return _plan()

    async def executor(context):
        seen["timeout"] = context.timeout_seconds
        return GoalStepResult(
            GoalDisposition.FAILED, "District asked us to wait before another read.",
            retryable=True, retry_after_seconds=30, error_code="PROVIDER_RATE_LIMITED",
        )

    try:
        _create(store)
        engine = GoalEngine(store, planner=planner, executor=executor, worker_id="test-worker")
        before = utc_now()
        reports = asyncio.run(engine.run_due_once())
        assert len(reports) == 1
        assert reports[0].status == GoalStatus.ACTIVE
        assert seen["has_owner"] is False
        assert 1 <= seen["timeout"] <= 20
        assert reports[0].next_wake_at is not None
        assert abs((reports[0].next_wake_at - (before + timedelta(seconds=30))).total_seconds()) < 3
    finally:
        store.close()


def test_engine_waits_full_retry_after_when_provider_delay_exceeds_retry_budget(tmp_path):
    store = _store(tmp_path)

    async def planner(_view):
        return _plan()

    async def executor(_context):
        return GoalStepResult(
            GoalDisposition.FAILED, "The provider asked for a longer wait.",
            retryable=True, retry_after_seconds=300, error_code="PROVIDER_RATE_LIMITED",
        )

    try:
        _create(store)
        engine = GoalEngine(store, planner=planner, executor=executor, worker_id="long-wait-worker")
        before = utc_now()
        report = asyncio.run(engine.run_due_once())[0]
        assert report.status == GoalStatus.WAITING_UNTIL
        assert report.next_wake_at is not None
        assert abs((report.next_wake_at - (before + timedelta(seconds=300))).total_seconds()) < 3
    finally:
        store.close()


def test_engine_cancellation_stops_cooperative_finite_callback(tmp_path):
    store = _store(tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def planner(_view):
        return _plan()

    async def executor(context):
        started.set()
        try:
            await context.cancel_event.wait()
            return GoalStepResult(GoalDisposition.FAILED, "Cancelled read.", error_code="CANCELLED")
        finally:
            stopped.set()

    async def scenario():
        engine = GoalEngine(store, planner=planner, executor=executor, worker_id="cancel-worker")
        goal, _ = _create(store)
        task = asyncio.create_task(engine.run_due_once())
        await asyncio.wait_for(started.wait(), timeout=2)
        cancelled = await engine.cancel_goal(goal.goal_id, OWNER)
        reports = await asyncio.wait_for(task, timeout=2)
        assert cancelled.status == GoalStatus.CANCELLED
        assert reports[0].status == GoalStatus.CANCELLED
        assert stopped.is_set()  # The executor's cancellation cleanup ran.

    try:
        asyncio.run(scenario())
    finally:
        store.close()
