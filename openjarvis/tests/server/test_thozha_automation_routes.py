"""Focused tests for the owner-scoped, supervisor-delegating Web API."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.scheduler.scheduler import (
    ScheduledTask,
    TaskScheduler,
    make_web_supervisor_dispatcher,
)
from openjarvis.scheduler.store import SchedulerStore
from openjarvis.server.thozha_automation_routes import create_automation_router
from openjarvis.tasks.supervisor import SupervisorStore

DEVICE = "a" * 32


@pytest.fixture
def harness(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    scheduler = TaskScheduler(store)
    supervisor_store = SupervisorStore(tmp_path / "supervisor.db", run_recovery=False)
    current = {"user_id": "owner-a"}
    calls: list[dict[str, Any]] = []
    idem: dict[tuple[str, str], str] = {}
    dispatch_behavior = {"mode": "normal"}
    active_devices = {("owner-a", DEVICE)}

    scheduler.configure_web_supervisor_dispatcher(
        make_web_supervisor_dispatcher(
            supervisor_store,
            is_web_device_active=lambda owner_id, device_id: (
                (
                    owner_id,
                    device_id,
                )
                in active_devices
            ),
            is_link_runner_ready=lambda: dispatch_behavior.get("runner_ready", True),
        )
    )

    async def require_session():
        return {
            "user_id": current["user_id"],
            "session_id": "test",
            "expires_at": "later",
        }

    def scheduler_provider(_request):
        return scheduler

    def supervised_action(principal, action):
        """Test adapter using the real scheduler and SupervisorStore APIs."""
        calls.append(action)
        if dispatch_behavior["mode"] == "forbidden":
            raise PermissionError("denied")
        if dispatch_behavior["mode"] == "error":
            raise RuntimeError("must-not-leak")
        operation = action["operation"]
        if operation == "create":
            owner_id = action["owner_id"]
            key = action["idempotency_key"]
            identity = (owner_id, key)
            if identity in idem:
                return {"task_id": idem[identity]}
            data = action["automation"]
            task, _created = scheduler.create_web_task(
                data["prompt"],
                data["schedule_type"],
                data["schedule_value"],
                owner_id=owner_id,
                device_id=data["device_id"],
                idempotency_key=key,
                timezone_name=data.get("timezone"),
            )
            idem[identity] = task.id
            return {"task_id": task.id}
        if operation == "pause":
            scheduler.pause_task(action["automation_id"])
            return {"status": "paused"}
        if operation == "disable":
            scheduler.pause_task(action["automation_id"])
            return {"status": "disabled"}
        if operation == "enable":
            scheduler.resume_task(action["automation_id"])
            return {"status": "enabled"}
        if operation == "delete":
            scheduler.delete_task(action["automation_id"])
            return {"status": "deleted"}
        if operation == "update":
            changes = action["changes"]
            task = scheduler.update_task(
                action["automation_id"],
                prompt=changes.get("prompt"),
                schedule_type=changes.get("schedule_type"),
                schedule_value=changes.get("schedule_value"),
                timezone_name=changes.get("timezone"),
            )
            return {"status": "updated", "task_id": task.id}
        if operation == "manual_trigger":
            return scheduler.run_task_once(
                action["automation_id"],
                idempotency_key=action["idempotency_key"],
            )
        raise AssertionError(f"unexpected operation {operation}")

    app = FastAPI()
    app.include_router(
        create_automation_router(
            require_web_session=require_session,
            scheduler_provider=scheduler_provider,
            supervised_action=supervised_action,
            resolve_web_device_ids=lambda principal: (
                {DEVICE} if principal["user_id"] == "owner-a" else set()
            ),
            is_web_device_active=lambda owner_id, device_id: (
                (
                    owner_id,
                    device_id,
                )
                in active_devices
            ),
            supervisor_store_provider=lambda _request: supervisor_store,
        )
    )
    client = TestClient(app)
    yield (
        client,
        scheduler,
        store,
        current,
        calls,
        idem,
        dispatch_behavior,
        supervisor_store,
        active_devices,
    )
    store.close()
    supervisor_store.close()


def _create(client: TestClient, prompt="Check the weather", key="request-0001"):
    return client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": key},
        json={
            "prompt": prompt,
            "schedule_type": "interval",
            "schedule_value": "3600",
            "device_id": DEVICE,
        },
    )


def test_create_is_supervised_owner_scoped_and_idempotent(harness):
    (
        client,
        scheduler,
        _store,
        current,
        calls,
        _idem,
        _behavior,
        supervisor,
        _active,
    ) = harness
    response = _create(client)
    assert response.status_code == 202, response.text
    automation = response.json()["automation"]
    task_id = automation["id"]
    row = scheduler.get_task(task_id)
    assert row["metadata"]["thozha_web_owner_id"] == "owner-a"
    assert row["metadata"]["thozha_web_source"] == "web"
    assert calls[-1]["owner_id"] == "owner-a"

    again = _create(client)
    assert again.status_code == 202
    assert again.json()["automation"]["id"] == task_id
    assert len(scheduler.list_tasks()) == 1

    current["user_id"] = "owner-b"
    assert client.get("/v1/web/automations").json()["items"] == []
    assert client.get(f"/v1/web/automations/{task_id}").status_code == 404


def test_unmapped_device_cannot_create_automation(harness):
    (
        client,
        _scheduler,
        _store,
        _current,
        calls,
        _idem,
        _behavior,
        supervisor,
        _active,
    ) = harness
    response = client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-unmapped-01"},
        json={
            "prompt": "Check a read-only fact",
            "schedule_type": "interval",
            "schedule_value": "3600",
            "device_id": "c" * 32,
        },
    )
    assert response.status_code == 404
    assert calls == []


def test_request_cannot_choose_owner_and_idempotency_key_is_required(harness):
    (
        client,
        _scheduler,
        _store,
        _current,
        calls,
        _idem,
        _behavior,
        _supervisor,
        _active,
    ) = harness
    forged = client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0001"},
        json={
            "owner_id": "owner-b",
            "prompt": "Do work",
            "schedule_type": "interval",
            "schedule_value": "3600",
            "device_id": DEVICE,
        },
    )
    assert forged.status_code == 422
    missing_key = client.post(
        "/v1/web/automations",
        json={
            "prompt": "Do work",
            "schedule_type": "interval",
            "schedule_value": "3600",
            "device_id": DEVICE,
        },
    )
    assert missing_key.status_code == 400
    assert calls == []


@pytest.mark.parametrize(
    "schedule_type,value",
    [
        ("interval", "2"),
        ("interval", "not-seconds"),
        ("once", "2020-01-01T00:00:00Z"),
        (
            "once",
            (datetime.now(timezone.utc) + timedelta(minutes=2))
            .replace(tzinfo=None)
            .isoformat(),
        ),
        ("cron", "not a cron"),
    ],
)
def test_invalid_schedule_is_rejected_before_supervisor_dispatch(
    harness, schedule_type, value
):
    (
        client,
        _scheduler,
        _store,
        _current,
        calls,
        _idem,
        _behavior,
        _supervisor,
        _active,
    ) = harness
    response = client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0002"},
        json={
            "prompt": "Do bounded work",
            "schedule_type": schedule_type,
            "schedule_value": value,
            "device_id": DEVICE,
        },
    )
    assert response.status_code in (422, 503)
    assert calls == []


def test_schedule_cron_and_timezone_validation(harness):
    (
        client,
        _scheduler,
        _store,
        _current,
        calls,
        _idem,
        _behavior,
        _supervisor,
        _active,
    ) = harness
    response = client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0003"},
        json={
            "prompt": "Check status",
            "schedule_type": "interval",
            "schedule_value": "3600",
            "timezone": "Not/AZone",
            "device_id": DEVICE,
        },
    )
    assert response.status_code == 422
    assert calls == []


def test_history_and_lifecycle_are_owner_scoped(harness):
    client, scheduler, _store, current, calls, _idem, _behavior, supervisor, _active = (
        harness
    )
    created = _create(client).json()["automation"]
    task_id = created["id"]
    response = client.post(
        f"/v1/web/automations/{task_id}/trigger",
        headers={"Idempotency-Key": "history-run-0001"},
    )
    assert response.status_code == 200
    supervisor_task_id = response.json()["supervisor_task_id"]
    link_run = supervisor.link_task_run(supervisor_task_id)
    supervisor.start_link_task(
        supervisor_task_id,
        objective_digest=link_run["objective_digest"],
        objective_text_digest=link_run["objective_digest"],
        capability_scope=["read_only"],
        idempotency_key=link_run["idempotency_key"],
    )
    supervisor.finish_link_task(
        supervisor_task_id, result_text="A verified read-only result."
    )
    supervisor.transition(supervisor_task_id, "VERIFYING", expected="RUNNING")
    supervisor.transition(
        supervisor_task_id,
        "COMPLETED",
        expected="VERIFYING",
        evidence={"test": "read_only_complete"},
    )

    history = client.get(f"/v1/web/automations/{task_id}/history")
    assert history.status_code == 200
    run = history.json()["items"][0]
    assert run["state"] == "accepted"
    assert run["supervisor_task"]["state"] == "COMPLETED"
    assert run["supervisor_task"]["result_text"] == "A verified read-only result."

    paused = client.post(f"/v1/web/automations/{task_id}/pause")
    assert paused.status_code == 200
    assert scheduler.get_task(task_id)["status"] == "paused"
    assert calls[-1]["operation"] == "pause"

    current["user_id"] = "owner-b"
    assert client.get(f"/v1/web/automations/{task_id}/history").status_code == 404
    assert client.post(f"/v1/web/automations/{task_id}/enable").status_code == 404


def test_manual_trigger_creates_idempotent_supervisor_task(harness):
    (
        client,
        scheduler,
        _store,
        _current,
        calls,
        _idem,
        _behavior,
        supervisor,
        _active,
    ) = harness
    task_id = _create(client).json()["automation"]["id"]
    key = "manual-run-0001"
    result = client.post(
        f"/v1/web/automations/{task_id}/trigger",
        headers={"Idempotency-Key": key},
    )
    assert result.status_code == 200, result.text
    supervisor_task_id = result.json()["supervisor_task_id"]
    task = supervisor.get_task(supervisor_task_id)
    assert task is not None
    assert task["state"] == "CREATED"
    assert task["recovery_json"].find("device:" + DEVICE) >= 0
    replay = client.post(
        f"/v1/web/automations/{task_id}/trigger",
        headers={"Idempotency-Key": key},
    )
    assert replay.status_code == 200
    assert replay.json()["supervisor_task_id"] == supervisor_task_id
    runs = scheduler.get_run_logs(task_id)
    assert len([run for run in runs if run["occurrence_key"] == f"manual:{key}"]) == 1


def test_due_occurrence_queues_read_only_link_task_and_duplicate_is_suppressed(harness):
    (
        client,
        scheduler,
        _store,
        _current,
        _calls,
        _idem,
        _behavior,
        supervisor,
        _active,
    ) = harness
    automation_id = _create(client).json()["automation"]["id"]
    due = scheduler.get_task(automation_id)
    due["next_run"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    scheduler._store.update_task(due)
    due_task = ScheduledTask.from_dict(due)

    scheduler._execute_task(due_task)
    logs = scheduler.get_run_logs(automation_id)
    assert len(logs) == 1
    assert logs[0]["state"] == "accepted"
    assert logs[0]["supervisor_task_id"]
    supervisor_task = supervisor.get_task(logs[0]["supervisor_task_id"])
    assert supervisor_task is not None
    assert supervisor_task["state"] == "CREATED"
    metadata = __import__("json").loads(supervisor_task["recovery_json"])
    assert metadata["source"] == "jarvis_link"
    assert metadata["owner_actor"] == f"device:{DEVICE}"
    assert metadata["capability_scope"] == ["read_only"]
    assert supervisor.pending_approvals(100) == []

    scheduler._execute_task(due_task)
    assert len(scheduler.get_run_logs(automation_id)) == 1
    assert len(supervisor.recent_tasks()) == 1


def test_dispatch_rechecks_pairing_and_persists_failure(harness):
    (
        client,
        scheduler,
        _store,
        _current,
        _calls,
        _idem,
        _behavior,
        _supervisor,
        active,
    ) = harness
    automation_id = _create(client).json()["automation"]["id"]
    task = scheduler.get_task(automation_id)
    task["next_run"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    scheduler._store.update_task(task)
    active.remove(("owner-a", DEVICE))

    scheduler._execute_task(ScheduledTask.from_dict(task))
    runs = scheduler.get_run_logs(automation_id)
    assert len(runs) == 1
    assert runs[0]["state"] == "failed"
    assert runs[0]["error"] == "PermissionError"
    disabled = scheduler.get_task(automation_id)
    assert disabled["status"] == "paused"
    assert disabled["next_run"] is None
    assert (
        disabled["metadata"]["web_last_error"]
        == "paired_device_or_scope_not_authorized"
    )


def test_concurrent_dispatch_of_one_occurrence_uses_one_supervisor_task(harness):
    (
        client,
        scheduler,
        _store,
        _current,
        _calls,
        _idem,
        _behavior,
        supervisor,
        _active,
    ) = harness
    automation_id = _create(client).json()["automation"]["id"]
    occurrence = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    persisted = scheduler.get_task(automation_id)
    persisted["next_run"] = occurrence
    scheduler._store.update_task(persisted)
    task = ScheduledTask.from_dict(persisted)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: scheduler._dispatch_web_occurrence(task, occurrence),
                range(8),
            )
        )

    ids = {result["supervisor_task_id"] for result in results}
    assert len(ids) == 1
    assert len(supervisor.recent_tasks()) == 1
    assert len(scheduler.get_run_logs(automation_id)) == 1


def test_runner_loss_creates_failure_history_not_dry_run_success(harness):
    (
        client,
        scheduler,
        _store,
        _current,
        _calls,
        _idem,
        behavior,
        _supervisor,
        _active,
    ) = harness
    automation_id = _create(client).json()["automation"]["id"]
    task = scheduler.get_task(automation_id)
    task["next_run"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    scheduler._store.update_task(task)
    behavior["runner_ready"] = False

    scheduler._execute_task(ScheduledTask.from_dict(task))
    run = scheduler.get_run_logs(automation_id)[0]
    assert run["success"] == 0
    assert run["state"] == "failed"
    assert run["error"] == "RuntimeError"
    assert run["supervisor_task_id"] is None


def test_update_and_delete_use_supervised_public_methods_and_keep_history(harness):
    (
        client,
        scheduler,
        _store,
        _current,
        calls,
        _idem,
        _behavior,
        supervisor,
        _active,
    ) = harness
    task_id = _create(client).json()["automation"]["id"]
    update = client.patch(f"/v1/web/automations/{task_id}", json={"prompt": "new"})
    delete = client.delete(f"/v1/web/automations/{task_id}")
    assert update.status_code == 200, update.text
    assert delete.status_code == 200, delete.text
    assert calls[-2]["operation"] == "update"
    assert calls[-1]["operation"] == "delete"
    assert scheduler.get_task(task_id)["status"] == "cancelled"
    assert client.get(f"/v1/web/automations/{task_id}/history").status_code == 200
    cancelled = scheduler.get_task(task_id)
    cancelled["next_run"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    scheduler._store.update_task(cancelled)
    scheduler._execute_task(ScheduledTask.from_dict(cancelled))
    assert supervisor.recent_tasks() == []


def test_update_rejects_explicit_null_fields_without_mutating(harness):
    (
        client,
        scheduler,
        _store,
        _current,
        calls,
        _idem,
        _behavior,
        _supervisor,
        _active,
    ) = harness
    task_id = _create(client).json()["automation"]["id"]
    before = scheduler.get_task(task_id)
    action_count = len(calls)
    response = client.patch(
        f"/v1/web/automations/{task_id}",
        json={"prompt": "A valid new prompt", "timezone": None},
    )
    assert response.status_code == 422
    assert scheduler.get_task(task_id)["prompt"] == before["prompt"]
    assert len(calls) == action_count


def test_capabilities_report_verified_read_only_dispatch_features(harness):
    (
        client,
        _scheduler,
        _store,
        _current,
        _calls,
        _idem,
        _behavior,
        _supervisor,
        _active,
    ) = harness
    response = client.get("/v1/web/automations/capabilities")
    assert response.status_code == 200
    operations = response.json()["operations"]
    assert operations["create"] is True  # test harness binds an execution fake
    assert operations["list"] is True
    assert operations["history"] is True
    assert operations["update"] is True
    assert operations["delete"] is True
    assert operations["manual_trigger"] is True


def test_mutations_fail_closed_without_supervisor_adapter(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    scheduler = TaskScheduler(store)

    async def auth():
        return {"user_id": "owner-a"}

    app = FastAPI()
    app.include_router(
        create_automation_router(
            require_web_session=auth,
            scheduler_provider=lambda _request: scheduler,
            supervised_action=None,
            resolve_web_device_ids=lambda _principal: {DEVICE},
            is_web_device_active=lambda _owner, _device: True,
        )
    )
    response = TestClient(app).post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0004"},
        json={
            "prompt": "Do work",
            "schedule_type": "interval",
            "schedule_value": "3600",
            "device_id": DEVICE,
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "unavailable"
    assert scheduler.list_tasks() == []
    store.close()


def test_store_only_production_scheduler_refuses_automation_creation(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    scheduler = TaskScheduler(store)
    calls = []

    async def auth():
        return {"user_id": "owner-a"}

    def supervised_action(_principal, action):
        calls.append(action)
        return {"task_id": "should-not-be-created"}

    app = FastAPI()
    app.include_router(
        create_automation_router(
            require_web_session=auth,
            scheduler_provider=lambda _request: scheduler,
            supervised_action=supervised_action,
            resolve_web_device_ids=lambda _principal: {DEVICE},
            is_web_device_active=lambda _owner, _device: True,
        )
    )
    response = TestClient(app).post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0005"},
        json={
            "prompt": "Do work",
            "schedule_type": "interval",
            "schedule_value": "3600",
            "device_id": DEVICE,
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "unavailable"
    assert calls == []
    assert scheduler.list_tasks() == []
    store.close()


@pytest.mark.parametrize(
    "mode,status,expected_detail",
    [
        ("forbidden", 403, "supervisor denied automation operation"),
        ("error", 503, "supervisor automation operation failed"),
    ],
)
def test_supervisor_errors_are_safe_and_not_leaked(
    harness, mode, status, expected_detail
):
    (
        client,
        _scheduler,
        _store,
        _current,
        _calls,
        _idem,
        behavior,
        _supervisor,
        _active,
    ) = harness
    task_id = _create(client).json()["automation"]["id"]
    behavior["mode"] = mode
    response = client.post(f"/v1/web/automations/{task_id}/pause")
    assert response.status_code == status
    assert response.json()["detail"] == expected_detail
    assert "must-not-leak" not in response.text


def test_foreign_legacy_scheduler_rows_are_not_exposed(harness):
    (
        client,
        scheduler,
        _store,
        _current,
        _calls,
        _idem,
        _behavior,
        _supervisor,
        _active,
    ) = harness
    scheduler.create_task(
        "private other task", "interval", "3600", metadata={"operator_id": "legacy"}
    )
    assert client.get("/v1/web/automations").json()["items"] == []
