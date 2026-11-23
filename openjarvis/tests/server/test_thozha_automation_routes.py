"""Focused tests for the owner-scoped, supervisor-delegating Web API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.scheduler.scheduler import TaskScheduler
from openjarvis.scheduler.store import SchedulerStore
from openjarvis.server.thozha_automation_routes import create_automation_router


@pytest.fixture
def harness(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    scheduler = TaskScheduler(store)
    # Test-only execution binding. Production SystemBuilder currently leaves
    # this None, which the API correctly reports as unavailable.
    scheduler._system = object()
    current = {"user_id": "owner-a"}
    calls: list[dict[str, Any]] = []
    idem: dict[tuple[str, str], str] = {}
    dispatch_behavior = {"mode": "normal"}

    async def require_session():
        return {
            "user_id": current["user_id"],
            "session_id": "test",
            "expires_at": "later",
        }

    def scheduler_provider(_request):
        return scheduler

    def supervised_action(principal, action):
        """Deterministic test double; production work is delegated to supervisor."""
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
            task = scheduler.create_task(
                data["prompt"],
                data["schedule_type"],
                data["schedule_value"],
                agent="simple",
                tools="",
                metadata=data["metadata"],
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
            scheduler.cancel_task(action["automation_id"])
            return {"status": "deleted"}
        if operation == "update":
            raise NotImplementedError("not wired in test adapter")
        if operation == "manual_trigger":
            raise NotImplementedError("not supported by existing scheduler")
        raise AssertionError(f"unexpected operation {operation}")

    app = FastAPI()
    app.include_router(
        create_automation_router(
            require_web_session=require_session,
            scheduler_provider=scheduler_provider,
            supervised_action=supervised_action,
        )
    )
    client = TestClient(app)
    yield client, scheduler, store, current, calls, idem, dispatch_behavior
    store.close()


def _create(client: TestClient, prompt="Check the weather", key="request-0001"):
    return client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": key},
        json={
            "prompt": prompt,
            "schedule_type": "interval",
            "schedule_value": "3600",
        },
    )


def test_create_is_supervised_owner_scoped_and_idempotent(harness):
    client, scheduler, _store, current, calls, _idem, _behavior = harness
    response = _create(client)
    assert response.status_code == 202, response.text
    automation = response.json()["automation"]
    task_id = automation["id"]
    row = scheduler._store.get_task(task_id)
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


def test_request_cannot_choose_owner_and_idempotency_key_is_required(harness):
    client, _scheduler, _store, _current, calls, _idem, _behavior = harness
    forged = client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0001"},
        json={
            "owner_id": "owner-b",
            "prompt": "Do work",
            "schedule_type": "interval",
            "schedule_value": "3600",
        },
    )
    assert forged.status_code == 422
    missing_key = client.post(
        "/v1/web/automations",
        json={
            "prompt": "Do work",
            "schedule_type": "interval",
            "schedule_value": "3600",
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
    client, _scheduler, _store, _current, calls, _idem, _behavior = harness
    response = client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0002"},
        json={
            "prompt": "Do bounded work",
            "schedule_type": schedule_type,
            "schedule_value": value,
        },
    )
    assert response.status_code in (422, 503)
    assert calls == []


def test_schedule_cron_and_timezone_validation(harness):
    client, _scheduler, _store, _current, calls, _idem, _behavior = harness
    response = client.post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0003"},
        json={
            "prompt": "Check status",
            "schedule_type": "interval",
            "schedule_value": "3600",
            "timezone": "Not/AZone",
        },
    )
    assert response.status_code == 422
    assert calls == []


def test_history_and_lifecycle_are_owner_scoped(harness):
    client, scheduler, _store, current, calls, _idem, _behavior = harness
    created = _create(client).json()["automation"]
    task_id = created["id"]
    scheduler._store.log_run(task_id, "start", "end", True, result="completed")

    history = client.get(f"/v1/web/automations/{task_id}/history")
    assert history.status_code == 200
    assert history.json()["items"][0]["result"] == "completed"

    paused = client.post(f"/v1/web/automations/{task_id}/pause")
    assert paused.status_code == 200
    assert scheduler._store.get_task(task_id)["status"] == "paused"
    assert calls[-1]["operation"] == "pause"

    current["user_id"] = "owner-b"
    assert client.get(f"/v1/web/automations/{task_id}/history").status_code == 404
    assert client.post(f"/v1/web/automations/{task_id}/enable").status_code == 404


def test_unavailable_manual_trigger_is_reported_without_execution(harness):
    client, _scheduler, _store, _current, calls, _idem, _behavior = harness
    task_id = _create(client).json()["automation"]["id"]
    before = len(calls)
    result = client.post(f"/v1/web/automations/{task_id}/trigger")
    assert result.status_code == 501
    assert result.json()["detail"]["status"] == "unavailable"
    assert "run_task_once" in result.json()["detail"]["reason"]
    assert len(calls) == before


def test_update_and_delete_are_explicitly_unavailable_when_core_methods_missing(
    harness,
):
    client, _scheduler, _store, _current, calls, _idem, _behavior = harness
    task_id = _create(client).json()["automation"]["id"]
    before = len(calls)
    update = client.patch(f"/v1/web/automations/{task_id}", json={"prompt": "new"})
    delete = client.delete(f"/v1/web/automations/{task_id}")
    assert update.status_code == delete.status_code == 501
    assert len(calls) == before


def test_capabilities_report_current_core_scheduler_gaps(harness):
    client, _scheduler, _store, _current, _calls, _idem, _behavior = harness
    response = client.get("/v1/web/automations/capabilities")
    assert response.status_code == 200
    operations = response.json()["operations"]
    assert operations["create"] is True  # test harness binds an execution fake
    assert operations["list"] is True
    assert operations["history"] is True
    assert operations["update"] is False
    assert operations["delete"] is False
    assert operations["manual_trigger"] is False


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
        )
    )
    response = TestClient(app).post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0004"},
        json={
            "prompt": "Do work",
            "schedule_type": "interval",
            "schedule_value": "3600",
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
        )
    )
    response = TestClient(app).post(
        "/v1/web/automations",
        headers={"Idempotency-Key": "request-0005"},
        json={
            "prompt": "Do work",
            "schedule_type": "interval",
            "schedule_value": "3600",
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
    client, _scheduler, _store, _current, _calls, _idem, behavior = harness
    task_id = _create(client).json()["automation"]["id"]
    behavior["mode"] = mode
    response = client.post(f"/v1/web/automations/{task_id}/pause")
    assert response.status_code == status
    assert response.json()["detail"] == expected_detail
    assert "must-not-leak" not in response.text


def test_foreign_legacy_scheduler_rows_are_not_exposed(harness):
    client, scheduler, _store, _current, _calls, _idem, _behavior = harness
    scheduler.create_task(
        "private other task", "interval", "3600", metadata={"operator_id": "legacy"}
    )
    assert client.get("/v1/web/automations").json()["items"] == []
