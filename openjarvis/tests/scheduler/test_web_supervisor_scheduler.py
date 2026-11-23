from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from openjarvis.scheduler.scheduler import (
    ScheduledTask,
    TaskScheduler,
    make_web_supervisor_dispatcher,
)
from openjarvis.scheduler.store import SchedulerStore
from openjarvis.tasks.supervisor import SupervisorStore

OWNER = "web-owner"
DEVICE = "a" * 32
KEY = "web-schedule-idempotency-0001"


def _scheduler(store: SchedulerStore, supervisor: SupervisorStore) -> TaskScheduler:
    scheduler = TaskScheduler(store, poll_interval=1)
    scheduler.configure_web_supervisor_dispatcher(
        make_web_supervisor_dispatcher(
            supervisor,
            is_web_device_active=lambda owner, device: (
                owner == OWNER and device == DEVICE
            ),
            is_link_runner_ready=lambda: True,
        )
    )
    return scheduler


def _create(scheduler: TaskScheduler, prompt: str = "Summarize today's weather"):
    return scheduler.create_web_task(
        prompt,
        "interval",
        "3600",
        owner_id=OWNER,
        device_id=DEVICE,
        idempotency_key=KEY,
    )


def test_duplicate_create_with_two_database_connections_returns_one_schedule(tmp_path):
    path = tmp_path / "scheduler.db"
    store_a = SchedulerStore(path)
    store_b = SchedulerStore(path)
    supervisor = SupervisorStore(tmp_path / "supervisor.db", run_recovery=False)
    scheduler_a = _scheduler(store_a, supervisor)
    scheduler_b = _scheduler(store_b, supervisor)
    barrier = threading.Barrier(2)

    original_a = store_a.find_web_task
    original_b = store_b.find_web_task

    def wait_then_find_a(owner_id, key):
        barrier.wait(timeout=5)
        return original_a(owner_id, key)

    def wait_then_find_b(owner_id, key):
        barrier.wait(timeout=5)
        return original_b(owner_id, key)

    store_a.find_web_task = wait_then_find_a
    store_b.find_web_task = wait_then_find_b
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_create, scheduler_a),
                executor.submit(_create, scheduler_b),
            ]
            results = [future.result(timeout=10) for future in futures]
        assert results[0][0].id == results[1][0].id
        assert sorted(result[1] for result in results) == [False, True]
        assert len(store_a.list_tasks()) == 1
    finally:
        store_a.close()
        store_b.close()
        supervisor.close()


def test_idempotency_key_conflict_does_not_replace_existing_schedule(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    supervisor = SupervisorStore(tmp_path / "supervisor.db", run_recovery=False)
    scheduler = _scheduler(store, supervisor)
    try:
        created, is_new = _create(scheduler, "First prompt")
        assert is_new
        try:
            _create(scheduler, "Different prompt")
        except ValueError as exc:
            assert "idempotency key conflicts" in str(exc)
        else:
            raise AssertionError("modified request must conflict")
        assert store.get_task(created.id)["prompt"] == "First prompt"
        assert len(store.list_tasks()) == 1
    finally:
        store.close()
        supervisor.close()


def test_scheduler_database_requires_owner_only_directory_and_file(tmp_path):
    path = tmp_path / "private" / "scheduler.db"
    store = SchedulerStore(path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0
    finally:
        store.close()

    os.chmod(path.parent, 0o755)
    try:
        try:
            SchedulerStore(path)
        except PermissionError as exc:
            assert "directory must be owner-only" in str(exc)
        else:
            raise AssertionError("permissive schedule directory must be rejected")
    finally:
        os.chmod(path.parent, 0o700)


def test_legacy_scheduler_database_migrates_web_occurrence_columns(tmp_path):
    path = tmp_path / "legacy" / "scheduler.db"
    path.parent.mkdir(mode=0o700)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY, prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL, schedule_value TEXT NOT NULL,
                context_mode TEXT NOT NULL DEFAULT 'isolated',
                status TEXT NOT NULL DEFAULT 'active', next_run TEXT,
                last_run TEXT, agent TEXT NOT NULL DEFAULT 'simple',
                tools TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE task_run_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT,
                success INTEGER NOT NULL DEFAULT 0,
                result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
            );
            """
        )
    store = SchedulerStore(path)
    try:
        task_columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(scheduled_tasks)")
        }
        run_columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(task_run_logs)")
        }
        assert {"web_owner_id", "web_idempotency_key"} <= task_columns
        assert {
            "state",
            "occurrence_key",
            "idempotency_key",
            "supervisor_task_id",
        } <= run_columns
    finally:
        store.close()


def test_restart_after_supervisor_acceptance_recovers_same_link_task(tmp_path):
    scheduler_path = tmp_path / "scheduler.db"
    supervisor = SupervisorStore(tmp_path / "supervisor.db", run_recovery=False)
    store = SchedulerStore(scheduler_path)
    scheduler = _scheduler(store, supervisor)
    task, _ = _create(scheduler)
    due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    persisted = store.get_task(task.id)
    persisted["next_run"] = due
    store.update_task(persisted)

    occurrence_key = due
    link_key = hashlib.sha256(
        f"thozha-web-automation-v1:{task.id}:{occurrence_key}".encode()
    ).hexdigest()
    claim, is_new = store.claim_run(
        task.id,
        occurrence_key=occurrence_key,
        idempotency_key=link_key,
        started_at=datetime.now(timezone.utc).isoformat(),
        expected_next_run=due,
    )
    assert is_new
    # Simulate a crash after durable Link submission and before the schedule
    # run-history row could be marked ACCEPTED.
    supervisor.create_link_task(
        task.prompt,
        capability_scope=["read_only"],
        idempotency_key=link_key,
        owner_actor=f"device:{DEVICE}",
    )
    store.close()

    recovered_store = SchedulerStore(scheduler_path)
    recovered = _scheduler(recovered_store, supervisor)
    try:
        recovered._execute_task(
            ScheduledTask.from_dict(recovered_store.get_task(task.id))
        )
        logs = recovered.get_run_logs(task.id)
        assert len(logs) == 1
        assert logs[0]["state"] == "accepted"
        assert len(supervisor.recent_tasks()) == 1
        run = supervisor.link_task_run_by_idempotency_key(link_key)
        assert run is not None
        assert run["status"] == "CREATED"
        assert logs[0]["supervisor_task_id"] == run["task_id"]
    finally:
        recovered_store.close()
        supervisor.close()
