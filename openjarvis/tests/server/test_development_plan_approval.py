"""API handoff for one exact development plan approval and safe capability issuance."""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openjarvis.tasks.supervisor as supervisor
import openjarvis.server.supervisor_routes as supervisor_routes
from openjarvis.server.supervisor_routes import router
from openjarvis.tasks.dev_capability import CapabilityStore
from openjarvis.tasks.dev_capability_adapter import DevelopmentCapabilityMCPAdapter
from openjarvis.tasks.supervisor import SupervisorStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "test-key-with-sufficient-entropy")
    monkeypatch.setenv("JARVIS_DEV_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("JARVIS_DEV_CAPABILITY_DB", str(tmp_path / "capabilities.db"))
    monkeypatch.setenv("JARVIS_SUPERVISOR_DB", store.db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)), store, workspace
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def test_api_persists_plan_intent_before_consuming_and_continues_task(client, tmp_path, monkeypatch):
    http, store, workspace = client
    device_id = "d" * 32
    actor = f"device:{device_id}"
    token = "ojd_development_plan_api_test_token"
    now = "2099-01-01T00:00:00+00:00"
    store._db.execute(
        "INSERT INTO paired_devices(id,public_key_b64,token_hash,created_at,expires_at,device_name) VALUES(?,?,?,?,?,?)",
        (device_id, "test-public-key", hashlib.sha256(token.encode()).hexdigest(), now, "2099-02-01T00:00:00+00:00", "test device"),
    )
    store._db.commit()

    objective = "Create a small project and run the fixed tests."
    objective_digest = hashlib.sha256(objective.encode()).hexdigest()
    key = "development-plan-api-test-0001"
    run = store.create_link_task(
        objective, capability_scope=["development"], idempotency_key=key, owner_actor=actor
    )
    task_id = run["task_id"]
    store.start_link_task(
        task_id,
        objective_digest=objective_digest,
        objective_text_digest=objective_digest,
        capability_scope=["development"],
        idempotency_key=key,
    )
    plan = {
        "plan_id": "api-plan-0001",
        "allowed_ops": ["write_file", "run_tests", "artifact_manifest"],
        "allowed_paths": ["src", "tests"],
        "max_file_bytes": 4096,
        "max_total_bytes": 8192,
        "max_writes": 3,
        "max_test_runs": 1,
        "max_repairs": 1,
    }
    capabilities = CapabilityStore(str(tmp_path / "capabilities.db"))
    adapter = DevelopmentCapabilityMCPAdapter(
        store, capabilities, task_id=task_id, user_id=actor, workspace_root=str(workspace)
    )
    card = adapter.request_plan_approval(plan, reason="One bounded project and fixed tests.")
    store.mark_link_task_waiting_approval(task_id, card.approval_id)
    capabilities.close()

    from openjarvis.engine import codex_sdk
    from openjarvis.server import routes as agent_routes
    dispatches = []

    def dispatch_plan(tool, arguments, **kwargs):
        assert tool == "approve_plan"
        assert kwargs["supervisor_approved"] is True
        assert kwargs["supervisor_operation"] == "mcp:jarvis_dev:approve_plan"
        path = tmp_path / "capabilities.db"
        check = CapabilityStore(str(path))
        try:
            intent = check._issuance_intent(card.approval_id)
            assert intent is not None
            assert check._issuance(card.approval_id) is None
        finally:
            check.close()
        assert store.approval(card.approval_id)["status"] == "CONSUMED"
        dispatches.append((tool, arguments))
        return {"content": [{"type": "text", "text": "Approved bounded plan capability issued."}], "isError": False}

    def continue_agent(continued_task_id, _run_id, _agent, _model, request, *_args, **_kwargs):
        assert continued_task_id == task_id
        assert request.messages[-1].content == objective
        assert store.link_task_run(task_id)["status"] == "RUNNING"
        store.finish_link_task(task_id, result_text="Project completed.")
        store.transition(task_id, "VERIFYING", step="development_project_verified")
        store.transition(task_id, "COMPLETED", evidence={"development_project_verified": True})

    class Agent:
        _model = "gpt-6-luna"

    http.app.state.agent = Agent()
    http.app.state.model = "gpt-6-luna"
    monkeypatch.setattr(codex_sdk, "_call_local_dev_mcp", dispatch_plan)
    monkeypatch.setattr(agent_routes, "_run_supervised_agent", continue_agent)
    response = http.post(
        f"/v1/jarvis/approvals/{card.approval_id}/decision",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "decision": "approve",
            "task_id": task_id,
            "expected_operation_digest": card.args_digest,
            "requester_device_id": device_id,
            "continuation_objective": objective,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["success"] is True
    assert result["continued"] is True
    assert result["task_state"] == "COMPLETED"
    assert result["result_summary"] == "Project completed."
    assert result["result_text"] == "Project completed."
    assert dispatches and dispatches[0][0] == "approve_plan"

