"""Plan-only assistant MCP is bound to the task and cannot authorize execution."""

from __future__ import annotations

import json

import pytest

from openjarvis.tasks import jarvis_plan_mcp
from openjarvis.tasks.dev_capability import (
    PLAN_APPROVAL_OPERATION,
    CapabilityStore,
    build_spec,
    plan_approval_arguments,
    plan_approval_target,
)
from openjarvis.tasks.dev_capability_adapter import DevelopmentCapabilityMCPAdapter
from openjarvis.tasks.jarvis_plan_mcp import handle, propose_plan
from openjarvis.tasks.supervisor import SupervisorStore, operation_digest

DEVICE = "6fa42c7bf11d478e9293ab23150e91db"
ACTOR = f"device:{DEVICE}"
PLAN = {
    "plan_id": "taskflow-plan-2026",
    "allowed_ops": ["write_file", "run_tests", "artifact_manifest"],
    "allowed_paths": ["src", "tests", "README.md"],
    "max_file_bytes": 8192,
    "max_total_bytes": 30000,
    "max_writes": 4,
    "max_test_runs": 2,
    "max_repairs": 1,
}


@pytest.fixture
def assistant_world(tmp_path, monkeypatch):
    workspace = tmp_path / "taskflow"
    workspace.mkdir()
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    run = store.create_link_task(
        "Create a task manager",
        capability_scope=["assistant"],
        idempotency_key="assistant-plan-test-" + "x" * 16,
        owner_actor=ACTOR,
    )
    store.start_link_task(
        run["task_id"],
        objective_digest=run["objective_digest"],
        objective_text_digest=run["objective_digest"],
        capability_scope=["assistant"],
        idempotency_key=run["idempotency_key"],
    )
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_ID", run["task_id"])
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SOURCE", "jarvis_link")
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SCOPE", "assistant")
    monkeypatch.setenv("JARVIS_DEV_WORKSPACE", str(workspace))
    monkeypatch.setenv("JARVIS_DEV_CAPABILITY_DB", str(tmp_path / "capabilities.db"))
    yield {
        "store": store,
        "task_id": run["task_id"],
        "workspace": workspace,
        "tmp": tmp_path,
    }
    store.close()


def _content(result):
    return json.loads(result["content"][0]["text"])


def test_assistant_can_propose_but_cannot_approve_or_issue_capability(
    assistant_world, monkeypatch
):
    monkeypatch.setattr(
        "openjarvis.tasks.jarvis_plan_mcp._workspace_root",
        lambda: assistant_world["workspace"],
    )
    store = assistant_world["store"]
    result = _content(propose_plan({"plan": PLAN}, store=store))

    assert result["status"] == "PLAN_PROPOSED"
    assert result["task_id"] == assistant_world["task_id"]
    assert result["authorization_state"] == "REQUIRED"
    assert result["execution_authorized"] is False
    assert result["approval_created"] is False
    assert result["capability_issued"] is False
    assert result["plan"]["workspace_root"] == str(assistant_world["workspace"])
    assert result["plan"]["device_id"] == DEVICE
    assert result["plan"]["user_id"] == ACTOR

    spec = build_spec(
        plan=PLAN,
        device_id=DEVICE,
        user_id=ACTOR,
        workspace_root=str(assistant_world["workspace"]),
        ttl_s=600,
        max_duration_s=900,
    )
    args = plan_approval_arguments(spec)
    assert result["plan_digest"] == spec.plan_digest
    assert result["approval_operation_digest"] == operation_digest(
        PLAN_APPROVAL_OPERATION, plan_approval_target(spec), args
    )
    events = store.events(assistant_world["task_id"])
    proposals = [
        event for event in events if event["event_type"] == "development.plan_proposed"
    ]
    assert len(proposals) == 1
    assert proposals[0]["payload"]["plan_digest"] == spec.plan_digest
    assert proposals[0]["payload"]["execution_authorized"] is False
    assert proposals[0]["payload"]["approval_created"] is False
    assert store.pending_approvals() == []
    assert not (assistant_world["tmp"] / "capabilities.db").exists()


def test_same_proposal_is_idempotent_and_modified_plan_is_rejected(
    assistant_world, monkeypatch
):
    monkeypatch.setattr(
        "openjarvis.tasks.jarvis_plan_mcp._workspace_root",
        lambda: assistant_world["workspace"],
    )
    store = assistant_world["store"]
    first = _content(propose_plan({"plan": PLAN}, store=store))
    second = _content(propose_plan({"plan": PLAN}, store=store))
    assert second["proposal_id"] == first["proposal_id"]
    assert second["plan_digest"] == first["plan_digest"]
    assert (
        len(
            [
                e
                for e in store.events(assistant_world["task_id"])
                if e["event_type"] == "development.plan_proposed"
            ]
        )
        == 1
    )

    changed = {**PLAN, "max_writes": 3}
    rejected = propose_plan({"plan": changed}, store=store)
    assert rejected["isError"] is True
    assert "different plan proposal" in rejected["content"][0]["text"]
    assert store.pending_approvals() == []


def test_only_an_explicit_development_task_can_create_the_real_plan_approval(
    assistant_world, monkeypatch
):
    monkeypatch.setattr(
        "openjarvis.tasks.jarvis_plan_mcp._workspace_root",
        lambda: assistant_world["workspace"],
    )
    store = assistant_world["store"]
    proposal = _content(propose_plan({"plan": PLAN}, store=store))

    dev_run = store.create_link_task(
        "Create a task manager",
        capability_scope=["development"],
        idempotency_key="development-plan-test-" + "y" * 16,
        owner_actor=ACTOR,
    )
    store.start_link_task(
        dev_run["task_id"],
        objective_digest=dev_run["objective_digest"],
        objective_text_digest=dev_run["objective_digest"],
        capability_scope=["development"],
        idempotency_key=dev_run["idempotency_key"],
    )
    cap_store = CapabilityStore(assistant_world["tmp"] / "capabilities.db")
    adapter = DevelopmentCapabilityMCPAdapter(
        store,
        cap_store,
        task_id=dev_run["task_id"],
        user_id=ACTOR,
        workspace_root=str(assistant_world["workspace"]),
        capability_ttl_s=600,
        max_duration_s=900,
    )
    try:
        approval = adapter.request_plan_approval(
            PLAN, reason="Owner review of exact taskflow plan"
        )
        record = store.approval(approval.approval_id)
        assert record["task_id"] == dev_run["task_id"]
        assert record["operation"] == PLAN_APPROVAL_OPERATION
        assert record["status"] == "PENDING"
        assert approval.plan_digest == proposal["plan_digest"]
        # No capability exists until a real owner decision is consumed.
        assert len(store.pending_approvals()) == 1
        assert (
            cap_store._db.execute("SELECT count(*) FROM dev_capabilities").fetchone()[0]
            == 0
        )
    finally:
        cap_store.close()


def test_assistant_scope_mismatch_or_unbounded_plan_fails_closed(
    assistant_world, monkeypatch
):
    monkeypatch.setattr(
        "openjarvis.tasks.jarvis_plan_mcp._workspace_root",
        lambda: assistant_world["workspace"],
    )
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SCOPE", "development")
    wrong_scope = propose_plan({"plan": PLAN}, store=assistant_world["store"])
    assert wrong_scope["isError"] is True
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_SCOPE", "assistant")

    extra = {**PLAN, "workspace_root": str(assistant_world["workspace"])}
    malformed = propose_plan({"plan": extra}, store=assistant_world["store"])
    assert malformed["isError"] is True
    broad = propose_plan(
        {"plan": {**PLAN, "allowed_paths": [""]}},
        store=assistant_world["store"],
    )
    assert broad["isError"] is True
    top_level_extra = propose_plan(
        {"plan": PLAN, "task_id": assistant_world["task_id"]},
        store=assistant_world["store"],
    )
    assert top_level_extra["isError"] is True
    assert assistant_world["store"].pending_approvals() == []


def test_plan_mcp_exposes_only_one_non_authorizing_tool():
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["propose_plan"]
    schema = tools[0]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["plan"]["additionalProperties"] is False
    assert "does not create an approval" in tools[0]["description"].lower()
    unknown = handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "approve_plan", "arguments": {"plan": PLAN}},
        }
    )
    assert unknown["result"]["isError"] is True


def test_plan_workspace_is_exact_precreated_taskflow_root(tmp_path, monkeypatch):
    monkeypatch.setattr(jarvis_plan_mcp, "REAL_HOME", tmp_path)
    allowed_parent = tmp_path / "Documents" / "Jarvis-Lab" / "agent-evaluation"
    taskflow = allowed_parent / "taskflow"
    taskflow.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_DEV_WORKSPACE", str(taskflow))
    assert jarvis_plan_mcp._workspace_root() == taskflow.resolve()

    other = allowed_parent / "other"
    other.mkdir()
    monkeypatch.setenv("JARVIS_DEV_WORKSPACE", str(other))
    with pytest.raises(PermissionError):
        jarvis_plan_mcp._workspace_root()

    alias = allowed_parent / "taskflow-alias"
    alias.symlink_to(taskflow, target_is_directory=True)
    monkeypatch.setenv("JARVIS_DEV_WORKSPACE", str(alias))
    with pytest.raises(PermissionError):
        jarvis_plan_mcp._workspace_root()


@pytest.mark.parametrize(
    "symlink_component", ["Documents", "Jarvis-Lab", "agent-evaluation", "taskflow"]
)
def test_plan_workspace_rejects_symlink_at_each_path_component(
    tmp_path, monkeypatch, symlink_component
):
    home = tmp_path / "home"
    home.mkdir()
    target_root = tmp_path / "real-tree"
    components = ["Documents", "Jarvis-Lab", "agent-evaluation", "taskflow"]
    target_path = target_root.joinpath(*components)
    target_path.mkdir(parents=True)

    current = home
    link_target = target_root
    for component in components:
        if component == symlink_component:
            (current / component).symlink_to(
                link_target / component, target_is_directory=True
            )
            break
        current = current / component
        current.mkdir()
        link_target = link_target / component

    monkeypatch.setattr(jarvis_plan_mcp, "REAL_HOME", home)
    monkeypatch.setenv(
        "JARVIS_DEV_WORKSPACE",
        str(home / "Documents" / "Jarvis-Lab" / "agent-evaluation" / "taskflow"),
    )
    with pytest.raises(PermissionError, match="symlink"):
        jarvis_plan_mcp._workspace_root()
