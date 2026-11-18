"""Bounded development capability: issuance, binding, containment, quotas, replay, revocation and audit."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from openjarvis.tasks.dev_capability import (
    CapabilityError,
    CapabilityStore,
    build_spec,
    plan_approval_arguments,
    plan_approval_target,
)
from openjarvis.tasks.supervisor import SupervisorStore

PLAN = {
    "plan_id": "plan-acceptance-001",
    "allowed_ops": ["write_file", "run_tests", "artifact_manifest"],
    "allowed_paths": ["src", "tests"],
    "max_file_bytes": 4096,
    "max_total_bytes": 16384,
    "max_writes": 4,
    "max_test_runs": 2,
    "max_repairs": 1,
}
DEVICE = "8f31ca48f4bc2ea8b6bc88b4aad9f6ae"
USER = "priya"
DECIDER = f"device:{DEVICE}"
PLAN_APPROVAL_OPERATION = "mcp:jarvis_dev:approve_plan"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def world(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    clock = Clock()
    store = CapabilityStore(tmp_path / "capabilities.db", clock=clock)
    supervisor = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    yield {"workspace": workspace, "store": store, "clock": clock, "supervisor": supervisor, "tmp": tmp_path}
    store.close()
    supervisor.close()


def approved_plan(world, *, ttl_s=600, plan=PLAN, device=DEVICE, user=USER, decider=DECIDER, workspace=None, decide=True):
    spec = build_spec(plan=plan, device_id=device, user_id=user,
                      workspace_root=str(workspace or world["workspace"]), ttl_s=ttl_s, max_duration_s=900)
    actor = f"device:{device}"
    task = world["supervisor"].create_link_task(
        "Create and test the approved Jarvis project",
        capability_scope=["development"],
        idempotency_key="dev-plan-" + os.urandom(12).hex(),
        owner_actor=actor,
    )
    task_id = task["task_id"]
    approval_id, digest = world["supervisor"].create_approval(
        task_id, PLAN_APPROVAL_OPERATION, plan_approval_target(spec), plan_approval_arguments(spec),
        "Approve this exact bounded development plan.", ttl_seconds=ttl_s,
    )
    if decide:
        assert world["supervisor"].decide_approval(
            approval_id, decision="approve", expected_digest=digest, task_id=task_id,
            actor=decider, nonce="test-approval-" + approval_id,
        )
    return spec, task_id, approval_id, actor


def issue(world, *, ttl_s=600, plan=PLAN, device=DEVICE, user=USER, decider=DECIDER, workspace=None):
    spec, task_id, approval_id, actor = approved_plan(
        world, ttl_s=ttl_s, plan=plan, device=device, user=user, decider=decider, workspace=workspace,
    )
    capability_id = world["store"].issue(
        spec, supervisor_store=world["supervisor"], approval_id=approval_id,
        task_id=task_id, requester_actor=actor,
    )
    world.setdefault("bindings", {})[capability_id] = {"task_id": task_id, "requester_actor": actor}
    return capability_id


def capability_binding(world, capability_id):
    binding = world["bindings"][capability_id]
    return {
        "supervisor_store": world["supervisor"],
        "task_id": binding["task_id"],
        "requester_actor": binding["requester_actor"],
    }


def authorize_write(world, capability_id, **kwargs):
    return world["store"].authorize_write(capability_id, **kwargs, **capability_binding(world, capability_id))


def authorize_test(world, capability_id, **kwargs):
    return world["store"].authorize_test(capability_id, **kwargs, **capability_binding(world, capability_id))


def consume_repair(world, capability_id, **kwargs):
    return world["store"].consume_repair(capability_id, **kwargs, **capability_binding(world, capability_id))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- issuance and expiry -------------------------------------------------------------------------

def test_issues_only_for_the_exact_approved_plan_digest(world):
    spec, task_id, approval_id, actor = approved_plan(world)
    forged_spec = type(spec)(**{**spec.__dict__, "plan_digest": "0" * 64})
    with pytest.raises(CapabilityError) as caught:
        world["store"].issue(
            forged_spec, supervisor_store=world["supervisor"], approval_id=approval_id,
            task_id=task_id, requester_actor=actor,
        )
    assert caught.value.code == "PLAN_MISMATCH"


def test_actual_supervisor_device_decision_issues_once_and_idempotently(world):
    spec, task_id, approval_id, actor = approved_plan(world)
    store = world["store"]
    cap = store.issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                      task_id=task_id, requester_actor=actor)
    repeated = store.issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                           task_id=task_id, requester_actor=actor)
    assert repeated == cap
    assert world["supervisor"].approval(approval_id)["status"] == "CONSUMED"
    assert store.usage(cap)["state"] == "ACTIVE"
    assert store._issuance(approval_id)["approved_by"] == actor


def test_capability_issuance_retry_survives_store_restart(world):
    spec, task_id, approval_id, actor = approved_plan(world)
    first = world["store"].issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                                  task_id=task_id, requester_actor=actor)
    world["store"].close()
    reopened = CapabilityStore(world["tmp"] / "capabilities.db", clock=world["clock"])
    try:
        assert reopened.issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                              task_id=task_id, requester_actor=actor) == first
        assert reopened.usage(first)["state"] == "ACTIVE"
        assert world["supervisor"].approval(approval_id)["status"] == "CONSUMED"
    finally:
        reopened.close()
    world["store"] = CapabilityStore(world["tmp"] / "capabilities.db", clock=world["clock"])


def test_consumed_approval_crash_gap_recovers_from_durable_intent(world, monkeypatch):
    spec, task_id, approval_id, actor = approved_plan(world)
    original_consume = world["supervisor"].consume_approval

    def consume_then_crash(*args, **kwargs):
        assert original_consume(*args, **kwargs) is True
        raise RuntimeError("injected crash after supervisor commit")

    monkeypatch.setattr(world["supervisor"], "consume_approval", consume_then_crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        world["store"].issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                             task_id=task_id, requester_actor=actor)

    approval = world["supervisor"].approval(approval_id)
    assert approval["status"] == "CONSUMED"
    assert approval["consumed_at"] < approval["expires_at"]
    assert world["store"]._issuance(approval_id) is None
    intent = world["store"]._issuance_intent(approval_id)
    assert intent is not None
    expected_capability_id = intent["capability_id"]
    assert world["store"]._db.execute("SELECT COUNT(*) FROM dev_capabilities").fetchone()[0] == 0

    # Simulate a process restart; recovery uses the persisted intent and consumed
    # supervisor record, without replaying or re-consuming the approval.
    world["store"].close()
    reopened = CapabilityStore(world["tmp"] / "capabilities.db", clock=world["clock"])
    try:
        capability_id = reopened.issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                                       task_id=task_id, requester_actor=actor)
        assert capability_id == expected_capability_id
        assert reopened.usage(capability_id)["state"] == "ACTIVE"
        assert world["supervisor"].approval(approval_id)["status"] == "CONSUMED"
        assert reopened._issuance_intent(approval_id) is None
        assert reopened.issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                              task_id=task_id, requester_actor=actor) == capability_id
    finally:
        reopened.close()
    world["store"] = CapabilityStore(world["tmp"] / "capabilities.db", clock=world["clock"])


def test_consumed_approval_without_matching_durable_intent_is_not_recovered(world):
    spec, task_id, approval_id, actor = approved_plan(world)
    assert world["supervisor"].consume_approval(
        approval_id, task_id=task_id, operation=PLAN_APPROVAL_OPERATION,
        target=plan_approval_target(spec), arguments=plan_approval_arguments(spec),
    )
    with pytest.raises(CapabilityError) as caught:
        world["store"].issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                             task_id=task_id, requester_actor=actor)
    assert caught.value.code == "APPROVAL_ALREADY_USED"
    assert world["store"]._issuance(approval_id) is None
    assert world["store"]._db.execute("SELECT COUNT(*) FROM dev_capabilities").fetchone()[0] == 0


def test_forged_approval_labels_are_not_accepted(world):
    spec = build_spec(plan=PLAN, device_id=DEVICE, user_id=USER,
                      workspace_root=str(world["workspace"]), ttl_s=600, max_duration_s=900)
    with pytest.raises(CapabilityError) as caught:
        world["store"].issue(spec, approved_plan_digest=spec.plan_digest, approved_by="model:approved")
    assert caught.value.code == "CALLER_CLAIMS_REJECTED"


def test_model_actor_cannot_issue_from_a_forged_supervisor_decision(world):
    spec, task_id, approval_id, actor = approved_plan(world, decider="model:codex")
    with pytest.raises(CapabilityError) as caught:
        world["store"].issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                              task_id=task_id, requester_actor=actor)
    assert caught.value.code == "APPROVER_IDENTITY_MISMATCH"
    assert world["supervisor"].approval(approval_id)["status"] == "APPROVED"


def test_missing_decision_refuses_issuance(world):
    with pytest.raises(CapabilityError) as caught:
        spec, task_id, approval_id, actor = approved_plan(world, decide=False)
        world["store"].issue(spec, supervisor_store=world["supervisor"], approval_id=approval_id,
                              task_id=task_id, requester_actor=actor)
    assert caught.value.code == "SUPERVISOR_APPROVAL_REQUIRED"


def test_expired_supervisor_plan_approval_cannot_issue_a_capability(world):
    spec, task_id, approval_id, actor = approved_plan(world, decide=False)
    world["supervisor"].expire_approval(approval_id)
    with pytest.raises(CapabilityError) as caught:
        world["store"].issue(
            spec, supervisor_store=world["supervisor"], approval_id=approval_id,
            task_id=task_id, requester_actor=actor,
        )
    assert caught.value.code == "APPROVAL_EXPIRED"
    assert world["store"]._issuance(approval_id) is None


def test_capability_expires_and_refuses_further_operations(world):
    cap = issue(world, ttl_s=60)
    world["clock"].now += 61
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="op-1",
                                       rel_path="src/a.py", size=10, content_sha256=sha(b"x"))
    assert caught.value.code == "EXPIRED"


def test_invalid_bounds_are_refused_before_issuance(world):
    bad = {**PLAN, "max_file_bytes": 10**9}
    with pytest.raises(CapabilityError) as caught:
        issue(world, plan=bad)
    assert caught.value.code == "INVALID_SPEC"


# ---- device and user binding ---------------------------------------------------------------------

def test_other_device_or_user_cannot_use_the_capability(world):
    cap = issue(world)
    with pytest.raises(CapabilityError) as other_device:
        authorize_write(world, cap, device_id="af877883c8d930faa83898670e7c6a97", user_id=USER,
                                       op_id="op-d", rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert other_device.value.code == "IDENTITY_MISMATCH"
    with pytest.raises(CapabilityError) as other_user:
        authorize_write(world, cap, device_id=DEVICE, user_id="someone-else",
                                       op_id="op-u", rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert other_user.value.code == "IDENTITY_MISMATCH"


# ---- workspace containment, traversal and symlinks ----------------------------------------------

@pytest.mark.parametrize("bad", ["../escape.py", "src/../../escape.py", "/etc/passwd", "~/x.py", "src//a.py", "src\\a.py", "src/./a.py", "src/a\x00.py"])
def test_traversal_and_absolute_paths_are_denied(world, bad):
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id=f"op-{abs(hash(bad))}",
                                       rel_path=bad, size=1, content_sha256=sha(b"a"))
    assert caught.value.code in {"PATH_DENIED", "PATH_NOT_PERMITTED"}


def test_path_outside_the_plan_prefixes_is_denied(world):
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="op-out",
                                       rel_path="docs/readme.md", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "PATH_NOT_PERMITTED"


def test_capability_cannot_be_replayed_from_another_task_or_device(world):
    cap = issue(world)
    binding = world["bindings"][cap]
    other_task = world["supervisor"].create_task("other task", metadata={"source": "jarvis_link"})
    with pytest.raises(CapabilityError) as wrong_task:
        world["store"].authorize_write(
            cap, device_id=DEVICE, user_id=USER, op_id="wrong-task", rel_path="src/a.py", size=1,
            content_sha256=sha(b"a"), supervisor_store=world["supervisor"],
            task_id=other_task, requester_actor=binding["requester_actor"],
        )
    assert wrong_task.value.code == "TASK_BINDING_MISMATCH"
    with pytest.raises(CapabilityError) as wrong_actor:
        world["store"].authorize_write(
            cap, device_id=DEVICE, user_id=USER, op_id="wrong-actor", rel_path="src/a.py", size=1,
            content_sha256=sha(b"a"), supervisor_store=world["supervisor"],
            task_id=binding["task_id"], requester_actor="device:" + "a" * 32,
        )
    assert wrong_actor.value.code == "TASK_BINDING_MISMATCH"


def test_symlink_inside_workspace_pointing_outside_is_denied(world):
    outside = world["tmp"] / "outside"
    outside.mkdir()
    os.symlink(outside, world["workspace"] / "src" / "link")
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="op-link",
                                       rel_path="src/link/evil.py", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "SYMLINK_DENIED"
    assert not (outside / "evil.py").exists()


def test_symlinked_final_file_is_denied(world):
    target = world["tmp"] / "secret.txt"
    target.write_text("do not touch")
    os.symlink(target, world["workspace"] / "src" / "config.py")
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="op-file-link",
                                       rel_path="src/config.py", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "SYMLINK_DENIED"


def test_workspace_root_cannot_be_a_symlink_to_elsewhere(world):
    real = world["tmp"] / "real-project"
    real.mkdir()
    (real / "src").mkdir()
    link_root = world["tmp"] / "linked-workspace"
    os.symlink(real, link_root)
    cap = issue(world, workspace=link_root)
    # Canonicalisation happens at issuance: the capability is bound to the real directory, not the link.
    spec_row = world["store"]._db.execute("SELECT spec_json FROM dev_capabilities WHERE capability_id=?", (cap,)).fetchone()
    assert str(real.resolve()) in spec_row[0]


# ---- quotas --------------------------------------------------------------------------------------

def test_per_file_size_quota_is_enforced(world):
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="op-big",
                                       rel_path="src/big.py", size=PLAN["max_file_bytes"] + 1, content_sha256=sha(b"a"))
    assert caught.value.code == "QUOTA_FILE_SIZE"


def test_total_bytes_and_write_count_quotas_are_enforced(world):
    cap = issue(world)
    store = world["store"]
    for index in range(PLAN["max_writes"]):
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id=f"w{index}",
                              rel_path=f"src/f{index}.py", size=4000, content_sha256=sha(b"a"))
    with pytest.raises(CapabilityError) as too_many:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="w-extra",
                              rel_path="src/extra.py", size=1, content_sha256=sha(b"a"))
    assert too_many.value.code in {"QUOTA_WRITES", "QUOTA_TOTAL_BYTES"}


def test_total_bytes_quota_refuses_before_writes_run_out(world):
    plan = {**PLAN, "max_total_bytes": 5000, "max_writes": 10}
    cap = issue(world, plan=plan)
    store = world["store"]
    authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="t1", rel_path="src/a.py", size=4000, content_sha256=sha(b"a"))
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="t2", rel_path="src/b.py", size=2000, content_sha256=sha(b"b"))
    assert caught.value.code == "QUOTA_TOTAL_BYTES"


def test_test_run_quota_is_enforced(world):
    cap = issue(world)
    store = world["store"]
    for index in range(PLAN["max_test_runs"]):
        authorize_test(world, cap, device_id=DEVICE, user_id=USER, op_id=f"t{index}", command="unittest-tests")
    with pytest.raises(CapabilityError) as caught:
        authorize_test(world, cap, device_id=DEVICE, user_id=USER, op_id="t-extra", command="unittest-tests")
    assert caught.value.code == "QUOTA_TEST_RUNS"


# ---- test command restrictions -------------------------------------------------------------------

@pytest.mark.parametrize("command", ["sh -c 'rm -rf /'", "python3 -c 'import os'", "unittest-tests; curl evil", ""])
def test_arbitrary_or_shell_test_commands_are_refused(world, command):
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        authorize_test(world, cap, device_id=DEVICE, user_id=USER, op_id="cmd", command=command)
    assert caught.value.code == "COMMAND_NOT_ALLOWED"


def test_only_the_fixed_allowlisted_command_is_returned_as_argv(world):
    cap = issue(world)
    argv = authorize_test(world, cap, device_id=DEVICE, user_id=USER, op_id="ok", command="unittest-tests")
    assert argv[:3] == (sys.executable, "-m", "unittest")


def test_plan_without_test_operation_refuses_tests(world):
    plan = {**PLAN, "allowed_ops": ["write_file"]}
    cap = issue(world, plan=plan)
    with pytest.raises(CapabilityError) as caught:
        authorize_test(world, cap, device_id=DEVICE, user_id=USER, op_id="nt", command="unittest-tests")
    assert caught.value.code == "OP_NOT_PERMITTED"


# ---- revocation, replay and duplicates -----------------------------------------------------------

def test_revocation_is_immediate_and_final(world):
    cap = issue(world)
    world["store"].revoke(cap, reason="user cancelled")
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="after",
                                       rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "REVOKED"


def test_duplicate_operation_id_is_rejected_as_replay(world):
    cap = issue(world)
    store = world["store"]
    authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="same", rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="same", rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "REPLAY"
    assert store.usage(cap)["writes"] == 1  # the duplicate did not consume quota


# ---- repair budget ---------------------------------------------------------------------------------

def test_repair_loop_stops_at_the_approved_budget(world):
    cap = issue(world)
    store = world["store"]
    assert consume_repair(world, cap, device_id=DEVICE, user_id=USER, op_id="r1") == 1
    with pytest.raises(CapabilityError) as caught:
        consume_repair(world, cap, device_id=DEVICE, user_id=USER, op_id="r2")
    assert caught.value.code == "REPAIR_LIMIT"


def test_repair_operation_id_cannot_be_replayed(world):
    cap = issue(world)
    binding = capability_binding(world, cap)
    kwargs = {"device_id": DEVICE, "user_id": USER, **binding}
    assert world["store"].consume_repair(cap, op_id="repair-replay-0001", **kwargs) == 1
    with pytest.raises(CapabilityError) as caught:
        world["store"].consume_repair(cap, op_id="repair-replay-0001", **kwargs)
    assert caught.value.code == "REPLAY"
    assert world["store"].usage(cap)["repairs"] == 1


def test_supervisor_cancellation_revokes_linked_capability(world):
    cap = issue(world)
    binding = capability_binding(world, cap)
    task_id = binding["task_id"]
    actor = binding["requester_actor"]
    cancelled = world["supervisor"].cancel_link_task(task_id, actor=actor)
    assert cancelled["state"] == "CANCELLED"
    assert world["store"].revoke_for_supervisor_task(
        world["supervisor"], task_id=task_id, requester_actor=actor,
    ) == 1
    assert world["store"].usage(cap)["state"] == "REVOKED"
    with pytest.raises(CapabilityError) as caught:
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="after-cancel",
                        rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "REVOKED"


# ---- crash recovery and audit ----------------------------------------------------------------------

def test_counters_and_revocation_survive_a_process_restart(world):
    cap = issue(world)
    store = world["store"]
    authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="pre", rel_path="src/a.py", size=100, content_sha256=sha(b"a"))
    store.close()
    reopened = CapabilityStore(world["tmp"] / "capabilities.db", clock=world["clock"])
    try:
        assert reopened.usage(cap)["writes"] == 1
        with pytest.raises(CapabilityError) as caught:
            reopened.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="pre", rel_path="src/a.py", size=100, content_sha256=sha(b"a"), **capability_binding(world, cap))
        assert caught.value.code == "REPLAY"
    finally:
        reopened.close()
    world["store"] = CapabilityStore(world["tmp"] / "capabilities.db", clock=world["clock"])


def test_every_decision_is_audited_including_denials(world):
    cap = issue(world)
    store = world["store"]
    authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="ok1", rel_path="src/a.py", size=10, content_sha256=sha(b"a"))
    with pytest.raises(CapabilityError):
        authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="bad1", rel_path="../x.py", size=10, content_sha256=sha(b"a"))
    events = [event for event, _ in store.audit_events(cap)]
    assert events[0] == "issued"
    assert "write_authorized" in events
    assert "write_denied" in events


def test_audit_never_records_file_contents(world):
    cap = issue(world)
    secret_text = "API_KEY=do-not-log-this"
    store = world["store"]
    authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="content", rel_path="src/a.py",
                          size=len(secret_text), content_sha256=sha(secret_text.encode()))
    rendered = repr(store.audit_events(cap))
    assert "do-not-log-this" not in rendered


# ---- artifact integrity ----------------------------------------------------------------------------

def test_recorded_digest_matches_the_bytes_written_for_the_approved_path(world):
    cap = issue(world)
    store = world["store"]
    content = b"print('hello')\n"
    target = authorize_write(world, cap, device_id=DEVICE, user_id=USER, op_id="art", rel_path="src/hello.py",
                                   size=len(content), content_sha256=sha(content))
    assert target.startswith(os.path.realpath(world["workspace"]) + os.sep)
    with open(target, "wb") as handle:
        handle.write(content)
    assert sha(open(target, "rb").read()) == sha(content)


def test_artifact_manifest_is_scoped_and_contains_hashes_only(world):
    cap = issue(world)
    binding = capability_binding(world, cap)
    first = world["workspace"] / "src" / "main.py"
    second = world["workspace"] / "tests" / "test_main.py"
    first_bytes, second_bytes = b"print('ok')\n", b"assert 1 + 1 == 2\n"
    first.write_bytes(first_bytes)
    second.write_bytes(second_bytes)
    manifest = world["store"].artifact_manifest(
        cap, device_id=DEVICE, user_id=USER, op_id="artifact-manifest-001",
        rel_paths=["tests/test_main.py", "src/main.py"], **binding,
    )
    expected_files = [
        {"path": "src/main.py", "bytes": len(first_bytes), "sha256": sha(first_bytes)},
        {"path": "tests/test_main.py", "bytes": len(second_bytes), "sha256": sha(second_bytes)},
    ]
    expected_json = json.dumps(expected_files, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert manifest["files"] == expected_files
    assert manifest["total_bytes"] == len(first_bytes) + len(second_bytes)
    assert manifest["manifest_sha256"] == sha(expected_json.encode())
    assert "print('ok')" not in repr(manifest)
    assert "assert 1 + 1" not in repr(world["store"].audit_events(cap))
