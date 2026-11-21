"""MCP facade tests use only temporary supervisor stores and workspaces."""

from __future__ import annotations

import hashlib
import json
import sys

import pytest

from openjarvis.tasks.dev_capability import (
    CapabilityError,
    CapabilityStore,
    build_spec,
    plan_approval_arguments,
    plan_approval_target,
)
from openjarvis.tasks.dev_capability_adapter import DevelopmentCapabilityMCPAdapter
from openjarvis.tasks.supervisor import SupervisorStore

DEVICE = "8f31ca48f4bc2ea8b6bc88b4aad9f6ae"
ACTOR = f"device:{DEVICE}"
PLAN = {
    "plan_id": "adapter-plan-0001",
    "allowed_ops": ["write_file", "run_tests", "artifact_manifest"],
    "allowed_paths": ["src", "tests"],
    "max_file_bytes": 4096,
    "max_total_bytes": 8192,
    "max_writes": 2,
    "max_test_runs": 1,
    "max_repairs": 0,
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def world(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    supervisor = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    capability = CapabilityStore(tmp_path / "capability.db", clock=Clock())
    task = supervisor.create_link_task(
        "Implement a bounded local code change",
        capability_scope=["development"],
        idempotency_key="adapter-task-" + "x" * 16,
        owner_actor=ACTOR,
    )
    task_id = task["task_id"]
    supervisor.transition(task_id, "PLANNING")
    adapter = DevelopmentCapabilityMCPAdapter(
        supervisor,
        capability,
        task_id=task_id,
        user_id="priya",
        workspace_root=str(workspace),
        capability_ttl_s=600,
        max_duration_s=900,
    )
    yield {
        "tmp": tmp_path,
        "workspace": workspace,
        "supervisor": supervisor,
        "capability": capability,
        "task_id": task_id,
        "adapter": adapter,
    }
    capability.close()
    supervisor.close()


def approve(world, approval_id):
    approval = world["supervisor"].approval(approval_id)
    assert world["supervisor"].decide_approval(
        approval_id,
        decision="approve",
        expected_digest=approval["args_digest"],
        task_id=world["task_id"],
        actor=ACTOR,
        nonce="approval-nonce-" + approval_id,
    )


def issue(world, plan=PLAN):
    card = world["adapter"].request_plan_approval(
        plan, reason="Bounded change and fixed tests"
    )
    approve(world, card.approval_id)
    return card.approval_id, world["adapter"].issue_approved_plan(card.approval_id)


def test_plan_approval_is_exact_single_and_derived_from_supervisor_task(world):
    adapter = world["adapter"]
    card = adapter.request_plan_approval(PLAN, reason="Bounded change and fixed tests")
    repeated = adapter.request_plan_approval(PLAN, reason="Repeated request")
    assert repeated.approval_id == card.approval_id
    assert repeated.args_digest == card.args_digest
    assert world["supervisor"].approval(card.approval_id)["task_id"] == world["task_id"]
    assert (
        world["supervisor"].approval(card.approval_id)["operation"]
        == "mcp:jarvis_dev:approve_plan"
    )
    assert world["supervisor"].approval(card.approval_id)["status"] == "PENDING"
    arguments = json.loads(
        world["supervisor"].approval(card.approval_id)["arguments_json"]
    )
    assert arguments["plan"]["device_id"] == DEVICE
    assert arguments["plan"]["user_id"] == "priya"
    assert arguments["plan"]["workspace_root"] == str(world["workspace"])
    assert arguments["plan"]["allowed_ops"] == sorted(PLAN["allowed_ops"])
    assert arguments["plan_digest"] == card.plan_digest
    assert len(world["supervisor"].pending_approvals()) == 1
    assert world["supervisor"].approval(card.approval_id)["decided_by"] is None


def test_one_task_cannot_replace_its_plan_or_select_another_approval(world):
    adapter = world["adapter"]
    first = adapter.request_plan_approval(PLAN, reason="Plan")
    changed = {**PLAN, "max_writes": 1}
    with pytest.raises(CapabilityError) as changed_plan:
        adapter.request_plan_approval(changed, reason="Changed plan")
    assert changed_plan.value.code == "PLAN_ALREADY_BOUND"
    with pytest.raises(CapabilityError) as wrong_approval:
        adapter.issue_approved_plan("f" * 32)
    assert wrong_approval.value.code == "APPROVAL_MISMATCH"
    assert world["supervisor"].approval(first.approval_id)["status"] == "PENDING"


def test_plan_too_large_for_complete_review_is_refused_before_creating_approval(world):
    adapter = world["adapter"]
    too_many_paths = {**PLAN, "allowed_paths": [f"src/dir{index}" for index in range(25)]}
    with pytest.raises(CapabilityError, match="path list"):
        adapter.request_plan_approval(too_many_paths, reason="This must fail before approval.")
    assert world["supervisor"].pending_approvals() == []


def test_orphaned_exact_pending_plan_approval_is_recovered_without_duplicate(world):
    task = world["supervisor"].get_task(world["task_id"])
    assert json.loads(task["recovery_json"])["owner_actor"] == ACTOR
    spec = build_spec(
        plan=PLAN,
        device_id=DEVICE,
        user_id="priya",
        workspace_root=str(world["workspace"]),
        ttl_s=600,
        max_duration_s=900,
    )
    approval_id, _ = world["supervisor"].create_approval(
        world["task_id"],
        "mcp:jarvis_dev:approve_plan",
        plan_approval_target(spec),
        plan_approval_arguments(spec),
        "Simulated orphaned plan card",
        ttl_seconds=600,
    )
    recovered = world["adapter"].request_plan_approval(
        PLAN, reason="Retry after local crash"
    )
    assert recovered.approval_id == approval_id
    assert len(world["supervisor"].pending_approvals()) == 1


def test_bounded_write_fixed_test_and_artifact_manifest(world):
    _approval, capability_id = issue(world)
    write = world["adapter"].authorize_write(
        capability_id,
        op_id="adapter-write-001",
        rel_path="src/new.py",
        content="print('ok')\n",
    )
    expected = b"print('ok')\n"
    assert write == {
        "target": str(world["workspace"] / "src" / "new.py"),
        "bytes": len(expected),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }
    assert not (
        world["workspace"] / "src" / "new.py"
    ).exists()  # authorization itself does not write
    argv = world["adapter"].authorize_test(
        capability_id, op_id="adapter-test-001", command="unittest-tests"
    )
    assert argv == (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q")
    with pytest.raises(CapabilityError) as shell:
        world["adapter"].authorize_test(
            capability_id,
            op_id="adapter-test-002",
            command="unittest-tests; touch /tmp/pwned",
        )
    assert shell.value.code == "COMMAND_NOT_ALLOWED"

    artifact = world["workspace"] / "tests" / "test_sample.py"
    artifact.write_bytes(b"assert True\n")
    manifest = world["adapter"].artifact_manifest(
        capability_id,
        op_id="adapter-artifact-001",
        rel_paths=["tests/test_sample.py"],
    )
    assert manifest["files"] == [
        {
            "path": "tests/test_sample.py",
            "bytes": len(b"assert True\n"),
            "sha256": hashlib.sha256(b"assert True\n").hexdigest(),
        }
    ]
    assert "assert True" not in repr(world["capability"].audit_events(capability_id))


def test_artifact_manifest_accepts_bounded_remote_workspace_reader(world):
    _approval, capability_id = issue(world)
    payload = b"remote guest artifact\n"
    manifest = world["adapter"].artifact_manifest(
        capability_id,
        op_id="adapter-remote-artifact-001",
        rel_paths=["src/main.py"],
        file_reader=lambda path, max_bytes: payload if path == "src/main.py" and len(payload) <= max_bytes else b"x" * (max_bytes + 1),
    )
    assert manifest["files"] == [{
        "path": "src/main.py",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }]
    assert manifest["total_bytes"] == len(payload)


def test_adapter_enforces_write_test_quota_and_expiry(world):
    plan = {**PLAN, "max_writes": 1, "max_test_runs": 1}
    _approval, capability_id = issue(world, plan)
    world["adapter"].authorize_write(
        capability_id, op_id="quota-write-1", rel_path="src/a.py", content="a"
    )
    with pytest.raises(CapabilityError) as too_many_writes:
        world["adapter"].authorize_write(
            capability_id, op_id="quota-write-2", rel_path="src/b.py", content="b"
        )
    assert too_many_writes.value.code in {"QUOTA_WRITES", "QUOTA_TOTAL_BYTES"}
    world["adapter"].authorize_test(
        capability_id, op_id="quota-test-1", command="unittest-tests"
    )
    with pytest.raises(CapabilityError) as too_many_tests:
        world["adapter"].authorize_test(
            capability_id, op_id="quota-test-2", command="unittest-tests"
        )
    assert too_many_tests.value.code == "QUOTA_TEST_RUNS"

    world["capability"]._clock.now += 601
    with pytest.raises(CapabilityError) as expired:
        world["adapter"].artifact_manifest(
            capability_id, op_id="expired-artifact-1", rel_paths=[]
        )
    assert expired.value.code == "EXPIRED"


def test_cancelled_task_denies_operations_and_revokes_capability(world):
    _approval, capability_id = issue(world)
    assert world["supervisor"].cancel_link_task(world["task_id"], actor=ACTOR)[
        "cancelled"
    ]
    assert world["adapter"].revoke_after_cancellation() == 1
    with pytest.raises(CapabilityError) as cancelled:
        world["adapter"].authorize_test(
            capability_id, op_id="cancel-test-01", command="unittest-tests"
        )
    assert cancelled.value.code == "TASK_INACTIVE"


def test_repair_budget_is_single_use_and_bounded_by_plan(world):
    plan = {**PLAN, "max_repairs": 1}
    _approval, capability_id = issue(world, plan)
    assert world["adapter"].consume_repair(capability_id, op_id="repair-attempt-001") == 1
    with pytest.raises(CapabilityError) as exhausted:
        world["adapter"].consume_repair(capability_id, op_id="repair-attempt-002")
    assert exhausted.value.code == "REPAIR_LIMIT"


def test_adapter_refuses_non_development_or_unowned_tasks(tmp_path):
    supervisor = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    capability = CapabilityStore(tmp_path / "capability.db")
    task = supervisor.create_task(
        "ordinary task", metadata={"source": "jarvis_link", "scope": "development"}
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = DevelopmentCapabilityMCPAdapter(
        supervisor,
        capability,
        task_id=task,
        user_id="priya",
        workspace_root=str(workspace),
    )
    try:
        with pytest.raises(CapabilityError) as denied:
            adapter.request_plan_approval(PLAN, reason="Should fail")
        assert denied.value.code == "TASK_SCOPE_MISMATCH"
        assert supervisor.pending_approvals() == []
    finally:
        capability.close()
        supervisor.close()


def test_task_environment_factory_uses_supervisor_injected_task_id(world, monkeypatch):
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_ID", world["task_id"])
    rebound = DevelopmentCapabilityMCPAdapter.from_task_environment(
        world["supervisor"],
        world["capability"],
        user_id="priya",
        workspace_root=str(world["workspace"]),
    )
    assert rebound.task_id == world["task_id"]
    monkeypatch.setenv("JARVIS_SUPERVISOR_TASK_ID", "model-selected-task")
    with pytest.raises(CapabilityError) as denied:
        DevelopmentCapabilityMCPAdapter.from_task_environment(
            world["supervisor"],
            world["capability"],
            user_id="priya",
            workspace_root=str(world["workspace"]),
        )
    assert denied.value.code == "TASK_REQUIRED"
