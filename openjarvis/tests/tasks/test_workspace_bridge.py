from __future__ import annotations

import hashlib
import stat

import pytest

from openjarvis.tasks.dev_capability import CapabilityError
from openjarvis.tasks.supervisor import SupervisorStore
from openjarvis.tasks.workspace_bridge import WorkspaceRegistry


def _task(store: SupervisorStore, owner: str) -> str:
    objective = "Repair the selected project and return a patch"
    value = store.create_link_task(
        objective,
        capability_scope=["development"],
        idempotency_key="workspace-bridge-"
        + hashlib.sha256(owner.encode()).hexdigest()[:24],
        owner_actor=owner,
    )
    digest = hashlib.sha256(objective.encode()).hexdigest()
    store.start_link_task(
        value["task_id"],
        objective_digest=digest,
        objective_text_digest=digest,
        capability_scope=["development"],
        idempotency_key=value["idempotency_key"],
    )
    return value["task_id"]


def test_registry_persists_owner_bound_snapshot_and_scope(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_bytes(b"def add(a, b): return -1\n")
    (root / ".secret").write_text("excluded")
    owner = "device:" + "a" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)

    registered = registry.register_for_task(task_id, owner, str(root), ["main.py"])
    identity, files, manifest = WorkspaceRegistry(store).snapshot_for_task(
        task_id, owner
    )

    assert identity == registered["workspace_id"]
    assert files == {"main.py": b"def add(a, b): return -1\n"}
    assert manifest == registered["manifest"]
    assert store.events(task_id)[-1]["event_type"] == "workspace.snapshot_registered"
    (root / "main.py").write_text("changed after snapshot")
    assert (
        WorkspaceRegistry(store).snapshot_for_task(task_id, owner)[1]["main.py"]
        != (root / "main.py").read_bytes()
    )
    store.close()


def test_registry_refuses_wrong_owner_scope_and_hidden_paths(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("pass")
    owner = "device:" + "b" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)

    with pytest.raises(CapabilityError, match="OWNER_SCOPE_MISMATCH"):
        registry.register_for_task(
            task_id, "device:" + "c" * 32, str(root), ["main.py"]
        )
    with pytest.raises(CapabilityError, match="PATH_DENIED"):
        registry.register_for_task(task_id, owner, str(root), [".env"])
    store.close()


def test_workspace_snapshot_is_durable_and_cancellation_revokes_access(tmp_path):
    db = tmp_path / "supervisor.db"
    store = SupervisorStore(str(db), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("before\n")
    owner = "device:" + "9" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(task_id, owner, str(root), ["main.py"])
    store.close()

    restarted = SupervisorStore(str(db), run_recovery=False)
    reloaded = WorkspaceRegistry(restarted)
    assert reloaded.snapshot_for_task(task_id, owner)[0] == registered["workspace_id"]
    restarted.cancel_link_task(task_id, actor=owner)
    with pytest.raises(CapabilityError, match="TASK_INACTIVE"):
        reloaded.snapshot_for_task(task_id, owner)
    restarted.close()


def test_patch_import_requires_exact_consumed_approval_and_unchanged_baseline(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("def add(a, b):\n    return -1\n")
    source.chmod(0o640)
    owner = "device:" + "d" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registry.register_for_task(task_id, owner, str(root), ["main.py"])
    registry.mark_staged(task_id, registry.snapshot_for_task(task_id, owner)[0])
    after = b"def add(a, b):\n    return a + b\n"
    patch = registry.build_patch(task_id, owner, lambda _path, **_kw: after)
    approval_id, digest = registry.request_patch_approval(task_id, owner, patch)
    store.mark_link_task_waiting_approval(task_id, approval_id)

    with pytest.raises(CapabilityError, match="PATCH_APPROVAL_REQUIRED"):
        registry.apply_approved_patch(task_id, owner, patch, approval_id=approval_id)
    assert store.decide_approval(
        approval_id,
        decision="approve",
        expected_digest=digest,
        task_id=task_id,
        actor=owner,
        nonce="approved-patch-once",
    )
    assert store.consume_approval(
        approval_id,
        task_id=task_id,
        operation="mcp:jarvis_dev:import_project_patch",
        target=store.approval(approval_id)["target"],
        arguments={
            "patch_id": patch["patch_id"],
            "workspace_id": patch["workspace_id"],
            "patch_sha256": patch["patch_sha256"],
        },
    )
    store.resume_link_task_after_approval(task_id, approval_id)
    result = registry.apply_approved_patch(
        task_id, owner, patch, approval_id=approval_id
    )
    assert result["patch_sha256"] == patch["patch_sha256"]
    assert source.read_bytes() == after
    assert stat.S_IMODE(source.stat().st_mode) == 0o640
    assert store.events(task_id)[-1]["event_type"] == "workspace.patch_applied"
    store.close()


def test_patch_stale_baseline_never_overwrites_external_host_change(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("before\n")
    owner = "device:" + "e" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registry.register_for_task(task_id, owner, str(root), ["main.py"])
    registry.mark_staged(task_id, registry.snapshot_for_task(task_id, owner)[0])
    patch = registry.build_patch(task_id, owner, lambda _path, **_kw: b"after\n")
    approval_id, digest = registry.request_patch_approval(task_id, owner, patch)
    store.mark_link_task_waiting_approval(task_id, approval_id)
    store.decide_approval(
        approval_id,
        decision="approve",
        expected_digest=digest,
        task_id=task_id,
        actor=owner,
        nonce="stale-baseline",
    )
    args = store.approval(approval_id)
    store.consume_approval(
        approval_id,
        task_id=task_id,
        operation=args["operation"],
        target=args["target"],
        arguments={
            "patch_id": patch["patch_id"],
            "workspace_id": patch["workspace_id"],
            "patch_sha256": patch["patch_sha256"],
        },
    )
    store.resume_link_task_after_approval(task_id, approval_id)
    source.write_text("owner edit\n")
    with pytest.raises(CapabilityError, match="PATCH_STALE"):
        registry.apply_approved_patch(task_id, owner, patch, approval_id=approval_id)
    assert source.read_text() == "owner edit\n"
    store.close()


def test_patch_import_refuses_symlink_replacement_after_approval(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("before\n")
    victim = tmp_path / "outside.txt"
    victim.write_text("protected\n")
    owner = "device:" + "1" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(task_id, owner, str(root), ["main.py"])
    registry.mark_staged(task_id, registered["workspace_id"])
    patch = registry.build_patch(task_id, owner, lambda _path, **_kw: b"after\n")
    approval_id, digest = registry.request_patch_approval(task_id, owner, patch)
    store.mark_link_task_waiting_approval(task_id, approval_id)
    assert store.decide_approval(
        approval_id,
        decision="approve",
        expected_digest=digest,
        task_id=task_id,
        actor=owner,
        nonce="symlink-import-approval",
    )
    approval = store.approval(approval_id)
    args = {
        "patch_id": patch["patch_id"],
        "workspace_id": patch["workspace_id"],
        "patch_sha256": patch["patch_sha256"],
    }
    assert store.consume_approval(
        approval_id,
        task_id=task_id,
        operation=approval["operation"],
        target=approval["target"],
        arguments=args,
    )
    store.resume_link_task_after_approval(task_id, approval_id)
    source.unlink()
    source.symlink_to(victim)

    with pytest.raises(CapabilityError):
        registry.apply_approved_patch(task_id, owner, args, approval_id=approval_id)
    assert victim.read_text() == "protected\n"
    assert source.is_symlink()
    store.close()
