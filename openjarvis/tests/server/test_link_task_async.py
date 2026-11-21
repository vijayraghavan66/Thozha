"""Durable async Link task acceptance, execution, idempotency, and recovery."""
from __future__ import annotations

import threading
import time
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

import openjarvis.tasks.supervisor as supervisor
from openjarvis.agents._stubs import AgentResult
from openjarvis.server.link_task_runner import LinkTaskExecutionRunner
from openjarvis.server.supervisor_routes import router
from openjarvis.tasks.supervisor import SupervisorStore


class FakeCodexEngine:
    engine_id = "codex_sdk"


class GateAgent:
    _model = "gpt-6-luna"

    def __init__(self, *, fail: bool = False):
        self.calls: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail = fail

    def run(self, text, *, context=None):
        self.calls.append(text)
        self.started.set()
        self.release.wait(timeout=4)
        if self.fail:
            raise RuntimeError("fake executor failure")
        return AgentResult(content="real fake answer: " + text)


def _client(tmp_path, monkeypatch, agent: GateAgent, *, workers: int = 2):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "async-link-test-admin")
    app = FastAPI()
    app.include_router(router)
    app.state.engine = FakeCodexEngine()
    app.state.model = "gpt-6-luna"
    app.state.agent = agent
    runner = LinkTaskExecutionRunner(app=app, store=store, concurrency=workers, poll_interval=0.05)
    app.state.link_task_runner = runner

    @app.on_event("startup")
    async def start_runner():
        await runner.start()

    @app.on_event("shutdown")
    async def stop_runner():
        await runner.stop()

    test_client = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))
    return test_client, store, agent, runner


def _body(key: str, objective: str | None = None):
    return {
        "objective": objective or f"Explain {key}",
        "capability_scope": ["read_only"],
        "idempotency_key": key,
        "requester_device_id": "a" * 32,
    }


def test_async_acceptance_is_fast_concurrent_and_duplicate_safe(tmp_path, monkeypatch):
    agent = GateAgent()
    client, store, _, runner = _client(tmp_path, monkeypatch, agent)
    headers = {"Authorization": "Bearer async-link-test-admin"}
    try:
        with client:
            started = time.monotonic()
            first = client.post("/v1/jarvis/link/tasks?mode=async", headers=headers,
                                json=_body("async-concurrent-request-0001"))
            assert first.status_code == 202
            expected_trace = hashlib.sha256(b"async-concurrent-request-0001").hexdigest()[:12]
            assert first.headers["X-Jarvis-Trace-ID"] == expected_trace
            assert float(first.headers["X-Jarvis-Backend-Duration-Ms"]) >= 0
            assert first.headers["X-Jarvis-Server-Wall-Ms"].isdigit()
            first_task = first.json()["task_id"]
            assert time.monotonic() - started < 0.5
            assert agent.started.wait(timeout=2)
            progress = client.get(
                "/v1/jarvis/link/tasks/async-concurrent-request-0001",
                headers=headers,
            ).json()
            assert progress["state"] == "RUNNING"
            assert progress["summary"] == "The assistant is working on the task."

            second = client.post("/v1/jarvis/link/tasks?mode=async", headers=headers,
                                 json=_body("async-concurrent-request-0002"))
            assert second.status_code == 202
            second_task = second.json()["task_id"]
            assert second_task != first_task

            duplicate = client.post("/v1/jarvis/link/tasks?mode=async", headers=headers,
                                    json=_body("async-concurrent-request-0001"))
            assert duplicate.status_code == 202
            assert duplicate.json()["task_id"] == first_task
            assert duplicate.json()["idempotent_replay"] is True
            assert len(agent.calls) <= 2

            agent.release.set()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                states = [store.get_task(task_id)["state"] for task_id in (first_task, second_task)]
                if states == ["COMPLETED", "COMPLETED"]:
                    break
                time.sleep(0.025)
            assert states == ["COMPLETED", "COMPLETED"]
            assert len(agent.calls) == 2
            for key in ("async-concurrent-request-0001", "async-concurrent-request-0002"):
                row = store.link_task_run_by_idempotency_key(key)
                assert row["status"] == "COMPLETED"
                assert row["result_text"].startswith("real fake answer: Explain")
                assert row["objective_text"] is None
                events = client.get(f"/v1/jarvis/link/tasks/{key}/events?after_seq=0",
                                    headers=headers).json()["events"]
                kinds = [event["event_type"] for event in events]
                assert "link_task.queued" in kinds
                assert "link_task.started" in kinds
                assert any(event.get("payload", {}).get("to") == "COMPLETED" for event in events)
                assert all("objective" not in event.get("payload", {}) for event in events)
    finally:
        agent.release.set()
        client.close()
        store.close()
        monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def test_async_worker_persists_failure_state(tmp_path, monkeypatch):
    agent = GateAgent(fail=True)
    client, store, _, _runner = _client(tmp_path, monkeypatch, agent, workers=1)
    headers = {"Authorization": "Bearer async-link-test-admin"}
    try:
        with client:
            accepted = client.post("/v1/jarvis/link/tasks?mode=async", headers=headers,
                                   json=_body("async-failure-request-00001"))
            assert accepted.status_code == 202
            task_id = accepted.json()["task_id"]
            assert agent.started.wait(timeout=2)
            agent.release.set()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                task = store.get_task(task_id)
                if task["state"] == "FAILED":
                    break
                time.sleep(0.025)
            assert task["state"] == "FAILED"
            assert store.link_task_run(task_id)["status"] == "FAILED"
    finally:
        agent.release.set()
        client.close()
        store.close()
        monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def test_restart_resumes_only_unclaimed_persisted_tasks(tmp_path):
    db_path = str(tmp_path / "recovery.db")
    store = SupervisorStore(db_path)
    queued = store.create_link_task("resume from durable queue", capability_scope=["read_only"],
                                    idempotency_key="async-recovery-queued-0001")
    running = store.create_link_task("do not replay after restart", capability_scope=["read_only"],
                                     idempotency_key="async-recovery-running-0001")
    store.start_link_task(running["task_id"], objective_digest=running["objective_digest"],
                          objective_text_digest=running["objective_digest"],
                          capability_scope=["read_only"], idempotency_key=running["idempotency_key"])
    store.close()

    recovered = SupervisorStore(db_path)
    try:
        queued_rows = recovered.queued_link_tasks()
        assert [row["task_id"] for row in queued_rows] == [queued["task_id"]]
        assert queued_rows[0]["objective_text"] == "resume from durable queue"
        assert recovered.get_task(running["task_id"])["state"] == "PAUSED"
        assert recovered.link_task_run(running["task_id"])["objective_text"] is None
    finally:
        recovered.close()


def test_cancelled_queued_task_is_not_recovered_or_executed(tmp_path):
    db_path = str(tmp_path / "cancel-queued.db")
    store = SupervisorStore(db_path)
    queued = store.create_link_task("cancel before start", capability_scope=["read_only"],
                                    idempotency_key="async-cancel-queued-0001")
    result = store.cancel_link_task(queued["task_id"], actor="api:test")
    assert result["cancelled"] is True
    assert store.queued_link_tasks() == []
    assert store.link_task_run(queued["task_id"])["objective_text"] is None
    store.close()

    recovered = SupervisorStore(db_path)
    try:
        assert recovered.get_task(queued["task_id"])["state"] == "CANCELLED"
        assert recovered.queued_link_tasks() == []
    finally:
        recovered.close()


def test_worker_race_does_not_fail_an_already_claimed_task(tmp_path):
    store = SupervisorStore(str(tmp_path / "claim-race.db"))
    queued = store.create_link_task("run exactly once", capability_scope=["read_only"],
                                    idempotency_key="async-claim-race-0001")
    store.start_link_task(queued["task_id"], objective_digest=queued["objective_digest"],
                          objective_text_digest=queued["objective_digest"],
                          capability_scope=["read_only"], idempotency_key=queued["idempotency_key"])
    assert store.fail_queued_link_task(queued["task_id"], error_code="RuntimeError") is False
    assert store.get_task(queued["task_id"])["state"] == "RUNNING"
    assert store.link_task_run(queued["task_id"])["status"] == "RUNNING"
    store.close()


def test_corrupt_queued_task_is_failed_and_prompt_erased(tmp_path):
    store = SupervisorStore(str(tmp_path / "corrupt-queue.db"))
    queued = store.create_link_task("correct text", capability_scope=["read_only"],
                                    idempotency_key="async-corrupt-queue-0001")
    with store._tx() as db:
        db.execute("UPDATE link_task_runs SET objective_text=? WHERE task_id=?", ("changed text", queued["task_id"]))
    assert store.fail_queued_link_task(queued["task_id"], error_code="ObjectiveDigestMismatch") is True
    assert store.get_task(queued["task_id"])["state"] == "FAILED"
    assert store.link_task_run(queued["task_id"])["objective_text"] is None
    assert store.queued_link_tasks() == []
    store.close()


def test_existing_supervisor_database_migrates_queue_prompt_column(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy-supervisor.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,request_digest TEXT,state TEXT,current_step TEXT,capabilities_json TEXT,recovery_json TEXT,verification_json TEXT,created_at TEXT,updated_at TEXT,last_error TEXT)")
    db.execute("CREATE TABLE link_task_runs(task_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,objective_digest TEXT,scope TEXT,scope_digest TEXT,status TEXT,result_digest TEXT,result_summary TEXT,created_at TEXT,started_at TEXT,completed_at TEXT,last_error TEXT)")
    db.commit()
    db.close()
    store = SupervisorStore(str(db_path))
    try:
        columns = {row["name"] for row in store._db.execute("PRAGMA table_info(link_task_runs)")}
        assert "objective_text" in columns
    finally:
        store.close()


def test_queue_request_rejects_missing_worker_without_creating_a_task(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "no-worker.db"))
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "async-link-test-admin")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        response = client.post("/v1/jarvis/link/tasks?mode=async",
                               headers={"Authorization": "Bearer async-link-test-admin"},
                               json=_body("async-no-worker-request-0001"))
    assert response.status_code == 503
    assert store.recent_tasks() == []
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)
