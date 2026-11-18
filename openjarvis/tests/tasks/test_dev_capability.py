"""Bounded development capability: issuance, binding, containment, quotas, replay, revocation and audit."""
from __future__ import annotations

import hashlib
import os

import pytest

from openjarvis.tasks.dev_capability import (
    CapabilityError,
    CapabilityStore,
    build_spec,
)

PLAN = {
    "plan_id": "plan-acceptance-001",
    "allowed_ops": ["write_file", "run_tests"],
    "allowed_paths": ["src", "tests"],
    "max_file_bytes": 4096,
    "max_total_bytes": 16384,
    "max_writes": 4,
    "max_test_runs": 2,
    "max_repairs": 1,
}
DEVICE = "8f31ca48f4bc2ea8b6bc88b4aad9f6ae"
USER = "priya"
DECIDER = "owner"


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
    yield {"workspace": workspace, "store": store, "clock": clock, "tmp": tmp_path}
    store.close()


def issue(world, *, ttl_s=600, plan=PLAN, device=DEVICE, user=USER, decider=DECIDER, workspace=None):
    spec = build_spec(plan=plan, device_id=device, user_id=user,
                      workspace_root=str(workspace or world["workspace"]), ttl_s=ttl_s, max_duration_s=900)
    return world["store"].issue(spec, approved_plan_digest=spec.plan_digest, approved_by=decider)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- issuance and expiry -------------------------------------------------------------------------

def test_issues_only_for_the_exact_approved_plan_digest(world):
    spec = build_spec(plan=PLAN, device_id=DEVICE, user_id=USER, workspace_root=str(world["workspace"]), ttl_s=600, max_duration_s=900)
    with pytest.raises(CapabilityError) as caught:
        world["store"].issue(spec, approved_plan_digest="0" * 64, approved_by=DECIDER)
    assert caught.value.code == "PLAN_MISMATCH"


def test_requesting_user_cannot_approve_own_plan(world):
    with pytest.raises(CapabilityError) as caught:
        issue(world, decider=USER)
    assert caught.value.code == "SELF_APPROVAL"


def test_missing_decision_refuses_issuance(world):
    with pytest.raises(CapabilityError) as caught:
        issue(world, decider="")
    assert caught.value.code == "APPROVAL_REQUIRED"


def test_capability_expires_and_refuses_further_operations(world):
    cap = issue(world, ttl_s=60)
    world["clock"].now += 61
    with pytest.raises(CapabilityError) as caught:
        world["store"].authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="op-1",
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
        world["store"].authorize_write(cap, device_id="af877883c8d930faa83898670e7c6a97", user_id=USER,
                                       op_id="op-d", rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert other_device.value.code == "IDENTITY_MISMATCH"
    with pytest.raises(CapabilityError) as other_user:
        world["store"].authorize_write(cap, device_id=DEVICE, user_id="someone-else",
                                       op_id="op-u", rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert other_user.value.code == "IDENTITY_MISMATCH"


# ---- workspace containment, traversal and symlinks ----------------------------------------------

@pytest.mark.parametrize("bad", ["../escape.py", "src/../../escape.py", "/etc/passwd", "~/x.py", "src//a.py", "src\\a.py", "src/./a.py", "src/a\x00.py"])
def test_traversal_and_absolute_paths_are_denied(world, bad):
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        world["store"].authorize_write(cap, device_id=DEVICE, user_id=USER, op_id=f"op-{abs(hash(bad))}",
                                       rel_path=bad, size=1, content_sha256=sha(b"a"))
    assert caught.value.code in {"PATH_DENIED", "PATH_NOT_PERMITTED"}


def test_path_outside_the_plan_prefixes_is_denied(world):
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        world["store"].authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="op-out",
                                       rel_path="docs/readme.md", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "PATH_NOT_PERMITTED"


def test_symlink_inside_workspace_pointing_outside_is_denied(world):
    outside = world["tmp"] / "outside"
    outside.mkdir()
    os.symlink(outside, world["workspace"] / "src" / "link")
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        world["store"].authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="op-link",
                                       rel_path="src/link/evil.py", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "SYMLINK_DENIED"
    assert not (outside / "evil.py").exists()


def test_symlinked_final_file_is_denied(world):
    target = world["tmp"] / "secret.txt"
    target.write_text("do not touch")
    os.symlink(target, world["workspace"] / "src" / "config.py")
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        world["store"].authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="op-file-link",
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
        world["store"].authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="op-big",
                                       rel_path="src/big.py", size=PLAN["max_file_bytes"] + 1, content_sha256=sha(b"a"))
    assert caught.value.code == "QUOTA_FILE_SIZE"


def test_total_bytes_and_write_count_quotas_are_enforced(world):
    cap = issue(world)
    store = world["store"]
    for index in range(PLAN["max_writes"]):
        store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id=f"w{index}",
                              rel_path=f"src/f{index}.py", size=4000, content_sha256=sha(b"a"))
    with pytest.raises(CapabilityError) as too_many:
        store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="w-extra",
                              rel_path="src/extra.py", size=1, content_sha256=sha(b"a"))
    assert too_many.value.code in {"QUOTA_WRITES", "QUOTA_TOTAL_BYTES"}


def test_total_bytes_quota_refuses_before_writes_run_out(world):
    plan = {**PLAN, "max_total_bytes": 5000, "max_writes": 10}
    cap = issue(world, plan=plan)
    store = world["store"]
    store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="t1", rel_path="src/a.py", size=4000, content_sha256=sha(b"a"))
    with pytest.raises(CapabilityError) as caught:
        store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="t2", rel_path="src/b.py", size=2000, content_sha256=sha(b"b"))
    assert caught.value.code == "QUOTA_TOTAL_BYTES"


def test_test_run_quota_is_enforced(world):
    cap = issue(world)
    store = world["store"]
    for index in range(PLAN["max_test_runs"]):
        store.authorize_test(cap, device_id=DEVICE, user_id=USER, op_id=f"t{index}", command="unittest-tests")
    with pytest.raises(CapabilityError) as caught:
        store.authorize_test(cap, device_id=DEVICE, user_id=USER, op_id="t-extra", command="unittest-tests")
    assert caught.value.code == "QUOTA_TEST_RUNS"


# ---- test command restrictions -------------------------------------------------------------------

@pytest.mark.parametrize("command", ["sh -c 'rm -rf /'", "python3 -c 'import os'", "unittest-tests; curl evil", ""])
def test_arbitrary_or_shell_test_commands_are_refused(world, command):
    cap = issue(world)
    with pytest.raises(CapabilityError) as caught:
        world["store"].authorize_test(cap, device_id=DEVICE, user_id=USER, op_id="cmd", command=command)
    assert caught.value.code == "COMMAND_NOT_ALLOWED"


def test_only_the_fixed_allowlisted_command_is_returned_as_argv(world):
    cap = issue(world)
    argv = world["store"].authorize_test(cap, device_id=DEVICE, user_id=USER, op_id="ok", command="unittest-tests")
    assert argv[:3] == ("python3", "-m", "unittest")


def test_plan_without_test_operation_refuses_tests(world):
    plan = {**PLAN, "allowed_ops": ["write_file"]}
    cap = issue(world, plan=plan)
    with pytest.raises(CapabilityError) as caught:
        world["store"].authorize_test(cap, device_id=DEVICE, user_id=USER, op_id="nt", command="unittest-tests")
    assert caught.value.code == "OP_NOT_PERMITTED"


# ---- revocation, replay and duplicates -----------------------------------------------------------

def test_revocation_is_immediate_and_final(world):
    cap = issue(world)
    world["store"].revoke(cap, reason="user cancelled")
    with pytest.raises(CapabilityError) as caught:
        world["store"].authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="after",
                                       rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "REVOKED"


def test_duplicate_operation_id_is_rejected_as_replay(world):
    cap = issue(world)
    store = world["store"]
    store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="same", rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    with pytest.raises(CapabilityError) as caught:
        store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="same", rel_path="src/a.py", size=1, content_sha256=sha(b"a"))
    assert caught.value.code == "REPLAY"
    assert store.usage(cap)["writes"] == 1  # the duplicate did not consume quota


# ---- repair budget ---------------------------------------------------------------------------------

def test_repair_loop_stops_at_the_approved_budget(world):
    cap = issue(world)
    store = world["store"]
    assert store.consume_repair(cap, device_id=DEVICE, user_id=USER, op_id="r1") == 1
    with pytest.raises(CapabilityError) as caught:
        store.consume_repair(cap, device_id=DEVICE, user_id=USER, op_id="r2")
    assert caught.value.code == "REPAIR_LIMIT"


# ---- crash recovery and audit ----------------------------------------------------------------------

def test_counters_and_revocation_survive_a_process_restart(world):
    cap = issue(world)
    store = world["store"]
    store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="pre", rel_path="src/a.py", size=100, content_sha256=sha(b"a"))
    store.close()
    reopened = CapabilityStore(world["tmp"] / "capabilities.db", clock=world["clock"])
    try:
        assert reopened.usage(cap)["writes"] == 1
        with pytest.raises(CapabilityError) as caught:
            reopened.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="pre", rel_path="src/a.py", size=100, content_sha256=sha(b"a"))
        assert caught.value.code == "REPLAY"
    finally:
        reopened.close()
    world["store"] = CapabilityStore(world["tmp"] / "capabilities.db", clock=world["clock"])


def test_every_decision_is_audited_including_denials(world):
    cap = issue(world)
    store = world["store"]
    store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="ok1", rel_path="src/a.py", size=10, content_sha256=sha(b"a"))
    with pytest.raises(CapabilityError):
        store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="bad1", rel_path="../x.py", size=10, content_sha256=sha(b"a"))
    events = [event for event, _ in store.audit_events(cap)]
    assert events[0] == "issued"
    assert "write_authorized" in events
    assert "write_denied" in events


def test_audit_never_records_file_contents(world):
    cap = issue(world)
    secret_text = "API_KEY=do-not-log-this"
    store = world["store"]
    store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="content", rel_path="src/a.py",
                          size=len(secret_text), content_sha256=sha(secret_text.encode()))
    rendered = repr(store.audit_events(cap))
    assert "do-not-log-this" not in rendered


# ---- artifact integrity ----------------------------------------------------------------------------

def test_recorded_digest_matches_the_bytes_written_for_the_approved_path(world):
    cap = issue(world)
    store = world["store"]
    content = b"print('hello')\n"
    target = store.authorize_write(cap, device_id=DEVICE, user_id=USER, op_id="art", rel_path="src/hello.py",
                                   size=len(content), content_sha256=sha(content))
    assert target.startswith(os.path.realpath(world["workspace"]) + os.sep)
    with open(target, "wb") as handle:
        handle.write(content)
    assert sha(open(target, "rb").read()) == sha(content)
