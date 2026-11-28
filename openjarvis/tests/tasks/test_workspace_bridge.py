from __future__ import annotations

import hashlib
import json
import stat
import threading
import time

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


def _approve_patch_once(store, registry, task_id, owner, patch, nonce):
    approval_id, digest = registry.request_patch_approval(task_id, owner, patch)
    store.mark_link_task_waiting_approval(task_id, approval_id)
    assert store.decide_approval(
        approval_id,
        decision="approve",
        expected_digest=digest,
        task_id=task_id,
        actor=owner,
        nonce=nonce,
    )
    approval = store.approval(approval_id)
    assert store.consume_approval(
        approval_id,
        task_id=task_id,
        operation=approval["operation"],
        target=approval["target"],
        arguments={
            "patch_id": patch["patch_id"],
            "workspace_id": patch["workspace_id"],
            "patch_sha256": patch["patch_sha256"],
        },
    )
    store.resume_link_task_after_approval(task_id, approval_id)
    return approval_id


def test_multifile_import_rolls_back_prior_replacements_on_failure(
    tmp_path, monkeypatch
):
    from openjarvis.tasks import workspace_bridge

    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    first, second = root / "first.py", root / "second.py"
    first.write_text("first = 1\n")
    second.write_text("second = 1\n")
    owner = "device:" + "2" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(
        task_id, owner, str(root), ["first.py", "second.py"]
    )
    registry.mark_staged(task_id, registered["workspace_id"])
    updates = {"first.py": b"first = 2\n", "second.py": b"second = 2\n"}
    patch = registry.build_patch(task_id, owner, lambda path, **_kw: updates[path])
    approval_id = _approve_patch_once(
        store, registry, task_id, owner, patch, "multi-import-failure"
    )

    real_replace = workspace_bridge._atomic_replace
    calls = 0

    def fail_second_replace(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Emulate a directory fsync error after the second os.replace has
            # already committed its rename.
            real_replace(*args)
            raise OSError("synthetic post-rename failure")
        return real_replace(*args)

    monkeypatch.setattr(workspace_bridge, "_atomic_replace", fail_second_replace)
    with pytest.raises(CapabilityError, match="PATCH_IMPORT_FAILED"):
        registry.apply_approved_patch(
            task_id,
            owner,
            {
                "patch_id": patch["patch_id"],
                "workspace_id": patch["workspace_id"],
                "patch_sha256": patch["patch_sha256"],
            },
            approval_id=approval_id,
        )

    assert first.read_text() == "first = 1\n"
    assert second.read_text() == "second = 1\n"
    saved = store._db.execute(
        "SELECT state FROM workspace_patches WHERE patch_id=?", (patch["patch_id"],)
    ).fetchone()
    assert saved["state"] == "EXPORTED"
    assert not any(
        event["event_type"] == "workspace.patch_applied"
        for event in store.events(task_id)
    )
    store.close()


def test_cancellation_committed_before_import_prevents_any_patch_write(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("value = 1\n")
    owner = "device:" + "3" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(task_id, owner, str(root), ["main.py"])
    registry.mark_staged(task_id, registered["workspace_id"])
    patch = registry.build_patch(task_id, owner, lambda _path, **_kw: b"value = 2\n")
    approval_id = _approve_patch_once(
        store, registry, task_id, owner, patch, "cancel-before-import"
    )

    assert store.cancel_link_task(task_id, actor=owner)["cancelled"] is True
    with pytest.raises(CapabilityError, match="TASK_INACTIVE"):
        registry.apply_approved_patch(
            task_id,
            owner,
            {
                "patch_id": patch["patch_id"],
                "workspace_id": patch["workspace_id"],
                "patch_sha256": patch["patch_sha256"],
            },
            approval_id=approval_id,
        )
    assert source.read_text() == "value = 1\n"
    store.close()


def test_import_transaction_serializes_cancellation_after_patch_commit(
    tmp_path, monkeypatch
):
    from openjarvis.tasks import workspace_bridge

    database = tmp_path / "supervisor.db"
    store = SupervisorStore(str(database), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("value = 1\n")
    owner = "device:" + "4" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(task_id, owner, str(root), ["main.py"])
    registry.mark_staged(task_id, registered["workspace_id"])
    patch = registry.build_patch(task_id, owner, lambda _path, **_kw: b"value = 2\n")
    approval_id = _approve_patch_once(
        store, registry, task_id, owner, patch, "cancel-import-race"
    )
    cancel_store = SupervisorStore(str(database), run_recovery=False)

    replacement_started = threading.Event()
    allow_replace = threading.Event()
    cancel_started = threading.Event()
    cancel_finished = threading.Event()
    real_replace = workspace_bridge._atomic_replace

    def blocked_replace(*args):
        replacement_started.set()
        assert allow_replace.wait(timeout=5)
        return real_replace(*args)

    monkeypatch.setattr(workspace_bridge, "_atomic_replace", blocked_replace)
    outcome = {}

    def import_patch():
        outcome["import"] = registry.apply_approved_patch(
            task_id,
            owner,
            {
                "patch_id": patch["patch_id"],
                "workspace_id": patch["workspace_id"],
                "patch_sha256": patch["patch_sha256"],
            },
            approval_id=approval_id,
        )

    def cancel_task():
        cancel_started.set()
        outcome["cancel"] = cancel_store.cancel_link_task(task_id, actor=owner)
        cancel_finished.set()

    importer = threading.Thread(target=import_patch)
    importer.start()
    assert replacement_started.wait(timeout=5)
    canceller = threading.Thread(target=cancel_task)
    canceller.start()
    assert cancel_started.wait(timeout=5)
    time.sleep(0.1)
    assert not cancel_finished.is_set()
    allow_replace.set()
    importer.join(timeout=5)
    canceller.join(timeout=5)

    assert not importer.is_alive() and not canceller.is_alive()
    assert outcome["import"]["files"] == ["main.py"]
    assert source.read_text() == "value = 2\n"
    assert outcome["cancel"]["cancelled"] is True
    event_types = [event["event_type"] for event in store.events(task_id)]
    assert event_types.index("workspace.patch_applied") < event_types.index("task.cancelled")
    cancel_store.close()
    store.close()


def test_cleanup_pending_workspaces_are_listed_for_startup_recovery(tmp_path):
    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("pass\n")
    owner = "device:" + "5" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(task_id, owner, str(root), ["main.py"])
    assert registry.has_task_workspace(task_id, owner) is True
    assert registry.is_staged(task_id, owner) is False

    registry.mark_cancelled(task_id, cleanup_succeeded=False, error_type="RemoteWorkerError")
    assert registry.has_task_workspace(task_id, owner) is True

    assert registry.pending_cleanup_workspaces() == [
        {
            "task_id": task_id,
            "workspace_id": registered["workspace_id"],
            "owner_actor": owner,
        }
    ]
    assert registry.pending_cleanup_workspaces(limit=0) == [
        {
            "task_id": task_id,
            "workspace_id": registered["workspace_id"],
            "owner_actor": owner,
        }
    ]
    registry.mark_cancelled(task_id, cleanup_succeeded=True)
    assert registry.has_task_workspace(task_id, owner) is False
    store.close()


def test_interrupted_multifile_import_recovers_from_durable_journal(tmp_path, monkeypatch):
    from openjarvis.tasks import workspace_bridge

    database = tmp_path / "supervisor.db"
    store = SupervisorStore(str(database), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    first, second = root / "first.py", root / "second.py"
    first.write_text("first = 1\n")
    second.write_text("second = 1\n")
    owner = "device:" + "6" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(
        task_id, owner, str(root), ["first.py", "second.py"]
    )
    registry.mark_staged(task_id, registered["workspace_id"])
    updates = {"first.py": b"first = 2\n", "second.py": b"second = 2\n"}
    patch = registry.build_patch(task_id, owner, lambda path, **_kw: updates[path])
    approval_id = _approve_patch_once(
        store, registry, task_id, owner, patch, "crash-journal-import"
    )

    real_replace = workspace_bridge._atomic_replace
    calls = 0

    def crash_after_second_rename(*args):
        nonlocal calls
        calls += 1
        real_replace(*args)
        if calls == 2:
            # BaseException models process death: SQLite rolls back transaction
            # two but the prior committed IMPORTING journal survives.
            raise SystemExit("simulated worker process crash")

    monkeypatch.setattr(workspace_bridge, "_atomic_replace", crash_after_second_rename)
    with pytest.raises(SystemExit, match="simulated worker process crash"):
        registry.apply_approved_patch(
            task_id,
            owner,
            {
                "patch_id": patch["patch_id"],
                "workspace_id": patch["workspace_id"],
                "patch_sha256": patch["patch_sha256"],
            },
            approval_id=approval_id,
        )

    saved = store._db.execute(
        "SELECT state,import_journal_json FROM workspace_patches WHERE patch_id=?",
        (patch["patch_id"],),
    ).fetchone()
    assert saved["state"] == "IMPORTING"
    assert json.loads(saved["import_journal_json"])["patch_sha256"] == patch["patch_sha256"]
    assert first.read_text() == "first = 2\n"
    assert second.read_text() == "second = 2\n"

    monkeypatch.setattr(workspace_bridge, "_atomic_replace", real_replace)
    reopened = SupervisorStore(str(database), run_recovery=False)
    recovered_registry = WorkspaceRegistry(reopened)
    outcome = recovered_registry.recover_incomplete_imports()

    assert outcome == {"recovered": [patch["patch_id"]], "pending": [], "conflicts": []}
    assert first.read_text() == "first = 1\n"
    assert second.read_text() == "second = 1\n"
    saved = reopened._db.execute(
        "SELECT state,import_journal_json FROM workspace_patches WHERE patch_id=?",
        (patch["patch_id"],),
    ).fetchone()
    assert saved["state"] == "EXPORTED"
    assert saved["import_journal_json"] is None
    assert any(
        event["event_type"] == "workspace.patch_import_recovered"
        for event in reopened.events(task_id)
    )
    reopened.close()
    store.close()


def test_import_recovery_preserves_external_edit_as_conflict(tmp_path, monkeypatch):
    from openjarvis.tasks import workspace_bridge

    database = tmp_path / "supervisor.db"
    store = SupervisorStore(str(database), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("value = 1\n")
    owner = "device:" + "7" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(task_id, owner, str(root), ["main.py"])
    registry.mark_staged(task_id, registered["workspace_id"])
    patch = registry.build_patch(task_id, owner, lambda _path, **_kw: b"value = 2\n")
    approval_id = _approve_patch_once(
        store, registry, task_id, owner, patch, "crash-conflict-import"
    )
    real_replace = workspace_bridge._atomic_replace

    def crash_after_rename(*args):
        real_replace(*args)
        raise SystemExit("simulated crash")

    monkeypatch.setattr(workspace_bridge, "_atomic_replace", crash_after_rename)
    with pytest.raises(SystemExit):
        registry.apply_approved_patch(
            task_id,
            owner,
            {
                "patch_id": patch["patch_id"],
                "workspace_id": patch["workspace_id"],
                "patch_sha256": patch["patch_sha256"],
            },
            approval_id=approval_id,
        )

    # An owner edit after interruption is neither the baseline nor the exact
    # approved result; recovery must not overwrite it.
    source.write_text("owner edit after interruption\n")
    monkeypatch.setattr(workspace_bridge, "_atomic_replace", real_replace)
    outcome = registry.recover_incomplete_imports()
    assert outcome == {"recovered": [], "pending": [], "conflicts": [patch["patch_id"]]}
    assert source.read_text() == "owner edit after interruption\n"
    saved = store._db.execute(
        "SELECT state FROM workspace_patches WHERE patch_id=?", (patch["patch_id"],)
    ).fetchone()
    assert saved["state"] == "IMPORT_RECOVERY_CONFLICT"
    store.close()


def test_failed_compensation_keeps_journal_and_recovery_reconciles(tmp_path, monkeypatch):
    from openjarvis.tasks import workspace_bridge

    store = SupervisorStore(str(tmp_path / "supervisor.db"), run_recovery=False)
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("value = 1\n")
    owner = "device:" + "8" * 32
    task_id = _task(store, owner)
    registry = WorkspaceRegistry(store)
    registered = registry.register_for_task(task_id, owner, str(root), ["main.py"])
    registry.mark_staged(task_id, registered["workspace_id"])
    patch = registry.build_patch(task_id, owner, lambda _path, **_kw: b"value = 2\n")
    approval_id = _approve_patch_once(
        store, registry, task_id, owner, patch, "recovery-pending-import"
    )

    real_replace = workspace_bridge._atomic_replace
    calls = 0

    def fail_apply_and_first_compensation(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            real_replace(*args)
            raise OSError("rename succeeded but fsync reported failure")
        if calls == 2:
            raise OSError("temporary compensation failure")
        return real_replace(*args)

    monkeypatch.setattr(
        workspace_bridge, "_atomic_replace", fail_apply_and_first_compensation
    )
    with pytest.raises(CapabilityError, match="PATCH_ROLLBACK_PENDING"):
        registry.apply_approved_patch(
            task_id,
            owner,
            {
                "patch_id": patch["patch_id"],
                "workspace_id": patch["workspace_id"],
                "patch_sha256": patch["patch_sha256"],
            },
            approval_id=approval_id,
        )

    # The public importer performs one immediate recovery retry after the
    # injected compensation failure; this succeeds on call three.
    assert calls == 3
    assert source.read_text() == "value = 1\n"
    saved = store._db.execute(
        "SELECT state,import_journal_json FROM workspace_patches WHERE patch_id=?",
        (patch["patch_id"],),
    ).fetchone()
    assert saved["state"] == "EXPORTED"
    assert saved["import_journal_json"] is None
    event_types = [event["event_type"] for event in store.events(task_id)]
    assert "workspace.patch_import_recovery_pending" in event_types
    assert "workspace.patch_import_recovered" in event_types
    store.close()
