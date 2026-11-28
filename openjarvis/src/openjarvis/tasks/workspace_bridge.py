"""Owner-bound project snapshots and verified patch application.

Host paths enter this module only through an owner-authorized registration call.
Model-facing development tools receive opaque workspace IDs and relative paths.
"""

from __future__ import annotations

import base64
import difflib
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from typing import Any

from openjarvis.tasks.dev_capability import (
    CapabilityError,
    _check_relative,
    canonical_workspace,
)
from openjarvis.tasks.supervisor import SupervisorStore, operation_digest
from openjarvis.tasks.workspace_snapshot import snapshot_files, snapshot_manifest

MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
_ACTOR = re.compile(r"device:([0-9a-f]{32})$")


def _ensure_schema(store: SupervisorStore) -> None:
    with store._tx() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS authorized_workspaces(
            workspace_id TEXT PRIMARY KEY, owner_actor TEXT NOT NULL,
            canonical_root TEXT NOT NULL, scope TEXT NOT NULL, created_at TEXT NOT NULL,
            revoked_at TEXT, UNIQUE(owner_actor,canonical_root))"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS workspace_task_snapshots(
            task_id TEXT PRIMARY KEY REFERENCES tasks(id),
            workspace_id TEXT NOT NULL REFERENCES authorized_workspaces(workspace_id),
            manifest_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS workspace_patches(
            patch_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
            workspace_id TEXT NOT NULL, patch_json TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL,
            import_journal_json TEXT)"""
        )
        patch_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(workspace_patches)")
        }
        if "import_journal_json" not in patch_columns:
            db.execute("ALTER TABLE workspace_patches ADD COLUMN import_journal_json TEXT")


@contextmanager
def _exclusive_patch_import(db_path: str, patch_id: str):
    """Serialize import and recovery across threads and server processes."""
    if not re.fullmatch(r"[0-9a-f]{32}", patch_id):
        raise CapabilityError("PATCH_INVALID", "patch identity is invalid")
    lock_path = os.path.join(
        os.path.dirname(os.path.abspath(db_path)), f".workspace-patch-{patch_id}.lock"
    )
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class WorkspaceRegistry:
    """Persistent owner and task binding for explicitly selected source files."""

    def __init__(self, supervisor: SupervisorStore):
        if not isinstance(supervisor, SupervisorStore):
            raise TypeError("workspace registry requires the supervisor store")
        self.supervisor = supervisor
        _ensure_schema(supervisor)

    def register_for_task(
        self, task_id: str, owner_actor: str, root: str, paths: list[str]
    ) -> dict[str, Any]:
        """Snapshot selected files only for the persisted paired owner."""
        match = _ACTOR.fullmatch(owner_actor or "")
        if not match:
            raise CapabilityError(
                "OWNER_REQUIRED", "workspace owner must be a paired device"
            )
        task = self.supervisor.get_task(task_id)
        run = self.supervisor.link_task_run(task_id)
        if task is None or run is None:
            raise CapabilityError("TASK_UNKNOWN", "workspace task does not exist")
        try:
            metadata = json.loads(task.get("recovery_json") or "{}")
            scope = json.loads(task.get("capabilities_json") or "[]")
        except (TypeError, json.JSONDecodeError) as exc:
            raise CapabilityError(
                "TASK_INVALID", "workspace task metadata is invalid"
            ) from exc
        if (
            metadata.get("source") != "jarvis_link"
            or metadata.get("scope") != "development"
            or metadata.get("owner_actor") != owner_actor
            or run.get("owner_actor") != owner_actor
            or scope
            != sorted({"file:read", "file:write", "code:execute", "tool:invoke"})
        ):
            raise CapabilityError(
                "OWNER_SCOPE_MISMATCH",
                "task is not authorized for this owner and development scope",
            )
        if task.get("state") not in {
            "CREATED",
            "PLANNING",
            "RUNNING",
            "WAITING_APPROVAL",
        }:
            raise CapabilityError("TASK_INACTIVE", "task is no longer active")
        root = canonical_workspace(root)
        files = snapshot_files(root, paths, max_bytes=MAX_SNAPSHOT_BYTES)
        manifest = snapshot_manifest(files)
        if any(len(name.encode("utf-8")) > 240 for name in files):
            raise CapabilityError(
                "PATH_DENIED", "workspace path exceeds the approved limit"
            )
        encoded = {
            name: base64.b64encode(data).decode("ascii") for name, data in files.items()
        }
        now = __import__("openjarvis.tasks.supervisor", fromlist=["utc_now"]).utc_now()
        with self.supervisor._tx() as db:
            existing = db.execute(
                "SELECT workspace_id,revoked_at FROM authorized_workspaces "
                "WHERE owner_actor=? AND canonical_root=?",
                (owner_actor, root),
            ).fetchone()
            if existing and existing["revoked_at"] is None:
                workspace_id = existing["workspace_id"]
            else:
                workspace_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO authorized_workspaces("
                    "workspace_id,owner_actor,canonical_root,scope,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (workspace_id, owner_actor, root, "development", now),
                )
            db.execute(
                "INSERT INTO workspace_task_snapshots("
                "task_id,workspace_id,manifest_json,snapshot_json,state,"
                "created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "workspace_id=excluded.workspace_id, "
                "manifest_json=excluded.manifest_json, "
                "snapshot_json=excluded.snapshot_json, state='SNAPSHOTTED', "
                "updated_at=excluded.updated_at",
                (
                    task_id,
                    workspace_id,
                    json.dumps(manifest, sort_keys=True),
                    json.dumps(encoded, sort_keys=True),
                    "SNAPSHOTTED",
                    now,
                    now,
                ),
            )
            self.supervisor._event(
                db,
                task_id,
                "workspace.snapshot_registered",
                {
                    "workspace_id": workspace_id,
                    "files": len(files),
                    "total_bytes": sum(map(len, files.values())),
                    "manifest_sha256": hashlib.sha256(
                        json.dumps(
                            manifest, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                },
            )
        return {
            "workspace_id": workspace_id,
            "manifest": manifest,
            "file_count": len(files),
            "total_bytes": sum(map(len, files.values())),
        }

    def snapshot_for_task(
        self, task_id: str, owner_actor: str, *, require_staged: bool = False
    ) -> tuple[str, dict[str, bytes], dict[str, str]]:
        row = self.supervisor._db.execute(
            "SELECT s.workspace_id,s.manifest_json,s.snapshot_json,s.state, "
            "w.owner_actor,w.scope,w.revoked_at,t.state AS task_state "
            "FROM workspace_task_snapshots s "
            "JOIN authorized_workspaces w USING(workspace_id) "
            "JOIN tasks t ON t.id=s.task_id WHERE s.task_id=?",
            (task_id,),
        ).fetchone()
        if (
            row is None
            or row["owner_actor"] != owner_actor
            or row["scope"] != "development"
            or row["revoked_at"] is not None
        ):
            raise CapabilityError(
                "WORKSPACE_NOT_AUTHORIZED",
                "task has no active owner-authorized workspace",
            )
        if require_staged and row["state"] != "STAGED":
            raise CapabilityError(
                "WORKSPACE_NOT_STAGED",
                "workspace snapshot has not been staged into the guest",
            )
        if row["task_state"] not in {
            "CREATED",
            "PLANNING",
            "RUNNING",
            "VERIFYING",
            "WAITING_APPROVAL",
        }:
            raise CapabilityError(
                "TASK_INACTIVE", "workspace is not staged for an active supervisor task"
            )
        encoded = json.loads(row["snapshot_json"])
        files = {
            name: base64.b64decode(content, validate=True)
            for name, content in encoded.items()
        }
        manifest = json.loads(row["manifest_json"])
        if snapshot_manifest(files) != manifest:
            raise CapabilityError(
                "SNAPSHOT_CORRUPT",
                "persistent workspace snapshot failed integrity validation",
            )
        return row["workspace_id"], files, manifest

    def root_for_task(self, task_id: str, owner_actor: str) -> str:
        row = self.supervisor._db.execute(
            "SELECT w.canonical_root FROM workspace_task_snapshots s "
            "JOIN authorized_workspaces w USING(workspace_id) "
            "WHERE s.task_id=? AND w.owner_actor=? "
            "AND w.scope='development' AND w.revoked_at IS NULL",
            (task_id, owner_actor),
        ).fetchone()
        if not row:
            raise CapabilityError(
                "WORKSPACE_NOT_AUTHORIZED",
                "task has no active owner-authorized workspace",
            )
        return str(row["canonical_root"])

    def is_staged(self, task_id: str, owner_actor: str) -> bool:
        row = self.supervisor._db.execute(
            "SELECT s.state FROM workspace_task_snapshots s "
            "JOIN authorized_workspaces w USING(workspace_id) "
            "WHERE s.task_id=? AND w.owner_actor=? "
            "AND w.scope='development' AND w.revoked_at IS NULL",
            (task_id, owner_actor),
        ).fetchone()
        if row is None:
            raise CapabilityError(
                "WORKSPACE_NOT_AUTHORIZED",
                "task has no active owner-authorized workspace",
            )
        return row["state"] == "STAGED"

    def has_task_workspace(self, task_id: str, owner_actor: str) -> bool:
        """Whether this owner has a registered guest workspace for the task.

        A snapshot is marked STAGED only after every file transfer completes. The
        guest unit can already be active while that transfer is in progress, so
        cancellation must also clean SNAPSHOTTED registrations. CLEANUP_PENDING
        is included so recovery can retry after a previous cancellation failure.
        """
        row = self.supervisor._db.execute(
            "SELECT s.state,w.owner_actor,w.scope FROM workspace_task_snapshots s "
            "JOIN authorized_workspaces w USING(workspace_id) WHERE s.task_id=?",
            (task_id,),
        ).fetchone()
        return bool(
            row
            and row["owner_actor"] == owner_actor
            and row["scope"] == "development"
            and row["state"] in {"SNAPSHOTTED", "STAGED", "CLEANUP_PENDING"}
        )

    def mark_staged(self, task_id: str, workspace_id: str) -> None:
        with self.supervisor._tx() as db:
            updated = db.execute(
                "UPDATE workspace_task_snapshots SET state='STAGED',updated_at=? "
                "WHERE task_id=? AND workspace_id=? "
                "AND state IN ('SNAPSHOTTED','STAGED')",
                (
                    __import__(
                        "openjarvis.tasks.supervisor", fromlist=["utc_now"]
                    ).utc_now(),
                    task_id,
                    workspace_id,
                ),
            ).rowcount
            if not updated:
                raise CapabilityError(
                    "WORKSPACE_BINDING_MISMATCH",
                    "staged workspace identity does not match the task",
                )
            self.supervisor._event(
                db, task_id, "workspace.snapshot_staged", {"workspace_id": workspace_id}
            )

    def mark_cancelled(
        self, task_id: str, *, cleanup_succeeded: bool, error_type: str | None = None
    ) -> None:
        state = "CANCELLED" if cleanup_succeeded else "CLEANUP_PENDING"
        with self.supervisor._tx() as db:
            row = db.execute(
                "SELECT workspace_id FROM workspace_task_snapshots WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return
            db.execute(
                "UPDATE workspace_task_snapshots SET state=?,updated_at=? "
                "WHERE task_id=?",
                (
                    state,
                    __import__(
                        "openjarvis.tasks.supervisor", fromlist=["utc_now"]
                    ).utc_now(),
                    task_id,
                ),
            )
            self.supervisor._event(
                db,
                task_id,
                "workspace.guest_cleanup_completed"
                if cleanup_succeeded
                else "workspace.guest_cleanup_pending",
                {"workspace_id": row["workspace_id"], "error_type": error_type},
            )

    def pending_cleanup_workspaces(self, *, limit: int = 100) -> list[dict[str, str]]:
        """Return bounded task-owned guest workspaces whose cancellation cleanup failed.

        Startup recovery can use these stable IDs to retry stopping each fixed
        systemd unit and removing only its UUID-derived guest directory.
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("cleanup recovery limit must be an integer")
        limit = min(max(limit, 1), 1000)
        with self.supervisor._lock:
            rows = self.supervisor._db.execute(
                "SELECT s.task_id,s.workspace_id,w.owner_actor "
                "FROM workspace_task_snapshots s "
                "JOIN authorized_workspaces w USING(workspace_id) "
                "WHERE s.state='CLEANUP_PENDING' "
                "ORDER BY s.updated_at,s.task_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "task_id": str(row["task_id"]),
                "workspace_id": str(row["workspace_id"]),
                "owner_actor": str(row["owner_actor"]),
            }
            for row in rows
        ]

    def recover_incomplete_imports(self, *, limit: int = 100) -> dict[str, list[str]]:
        """Recover imports interrupted after their durable journal was committed.

        Files are restored only if every target still matches either its exact
        baseline bytes or the exact approved patch bytes. Unknown host edits are
        never overwritten and leave an explicit IMPORT_RECOVERY_CONFLICT state.
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("patch recovery limit must be an integer")
        limit = min(max(limit, 1), 1000)
        with self.supervisor._lock:
            rows = self.supervisor._db.execute(
                "SELECT patch_id FROM workspace_patches "
                "WHERE state IN ('IMPORTING','IMPORT_RECOVERY_PENDING') "
                "ORDER BY created_at,patch_id LIMIT ?",
                (limit,),
            ).fetchall()
        outcomes = {"recovered": [], "pending": [], "conflicts": []}
        for row in rows:
            patch_id = str(row["patch_id"])
            try:
                with _exclusive_patch_import(self.supervisor.db_path, patch_id):
                    status = self._recover_incomplete_patch_import(patch_id)
            except Exception:
                status = "pending"
            outcomes[status].append(patch_id)
        return outcomes

    def _recover_incomplete_patch_import(self, patch_id: str) -> str:
        """Restore known partial imports; called with this patch's flock held."""
        status = "recovered"
        with self.supervisor._tx() as db:
            row = db.execute(
                "SELECT p.task_id,p.workspace_id,p.patch_json,p.state,p.import_journal_json,"
                "s.snapshot_json,s.manifest_json,w.owner_actor,w.canonical_root,w.scope "
                "FROM workspace_patches p "
                "JOIN workspace_task_snapshots s ON s.task_id=p.task_id "
                "JOIN authorized_workspaces w ON w.workspace_id=p.workspace_id "
                "WHERE p.patch_id=?",
                (patch_id,),
            ).fetchone()
            if row is None or row["state"] not in {
                "IMPORTING", "IMPORT_RECOVERY_PENDING"
            }:
                return "recovered"
            task_id = str(row["task_id"])
            workspace_id = str(row["workspace_id"])
            try:
                persisted = json.loads(row["patch_json"])
                journal = json.loads(row["import_journal_json"] or "{}")
                encoded_original = json.loads(row["snapshot_json"])
                original = {
                    name: base64.b64decode(content, validate=True)
                    for name, content in encoded_original.items()
                }
                manifest = json.loads(row["manifest_json"])
                encoded_contents = persisted["contents"]
                contents = {
                    name: base64.b64decode(content, validate=True)
                    for name, content in encoded_contents.items()
                }
                patch_text = persisted["patch"]
                digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
                valid = (
                    row["scope"] == "development"
                    and row["owner_actor"]
                    and persisted.get("patch_id") == patch_id
                    and persisted.get("workspace_id") == workspace_id
                    and persisted.get("patch_sha256") == digest
                    and journal.get("version") == 1
                    and journal.get("patch_sha256") == digest
                    and journal.get("files") == sorted(contents)
                    and journal.get("base_manifest") == manifest
                    and persisted.get("base_manifest") == manifest
                    and snapshot_manifest(original) == manifest
                    and set(contents) <= set(original)
                    and set(persisted.get("files", {})) == set(contents)
                )
                for rel, new_content in contents.items():
                    if (
                        not isinstance(new_content, bytes)
                        or persisted["files"][rel]
                        != {
                            "bytes": len(new_content),
                            "sha256": hashlib.sha256(new_content).hexdigest(),
                        }
                    ):
                        valid = False
                        break
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                valid = False
                persisted = {}
                original = {}
                contents = {}
                digest = ""
            if not valid:
                db.execute(
                    "UPDATE workspace_patches SET state='IMPORT_RECOVERY_CONFLICT' "
                    "WHERE patch_id=?",
                    (patch_id,),
                )
                self.supervisor._event(
                    db,
                    task_id,
                    "workspace.patch_import_recovery_conflict",
                    {"patch_id": patch_id, "patch_sha256": digest or None},
                )
                return "conflicts"

            root = canonical_workspace(row["canonical_root"])
            plans: list[tuple[str, bytes, bytes]] = []
            try:
                # Preflight all files before the first rollback write. A file
                # edited after the interrupted rename is a conflict, not a
                # reason to overwrite the owner's newer content.
                for rel in sorted(contents):
                    parts = _check_relative(rel)
                    if any(part.startswith(".") or part == ".git" for part in parts):
                        raise CapabilityError("PATCH_SCOPE", "recovery path is not permitted")
                    current = snapshot_files(
                        root, [rel], max_bytes=MAX_SNAPSHOT_BYTES
                    )[rel]
                    before = original[rel]
                    after = contents[rel]
                    if current == after:
                        plans.append((rel, after, before))
                    elif current != before:
                        raise CapabilityError(
                            "PATCH_RECOVERY_CONFLICT",
                            "workspace file differs from both import journal states",
                        )
            except Exception:
                db.execute(
                    "UPDATE workspace_patches SET state='IMPORT_RECOVERY_CONFLICT' "
                    "WHERE patch_id=?",
                    (patch_id,),
                )
                self.supervisor._event(
                    db,
                    task_id,
                    "workspace.patch_import_recovery_conflict",
                    {"patch_id": patch_id, "patch_sha256": digest},
                )
                return "conflicts"

            try:
                for rel, after, before in plans:
                    _atomic_replace(root, rel, after, before)
            except Exception as exc:
                db.execute(
                    "UPDATE workspace_patches SET state='IMPORT_RECOVERY_PENDING' "
                    "WHERE patch_id=?",
                    (patch_id,),
                )
                self.supervisor._event(
                    db,
                    task_id,
                    "workspace.patch_import_recovery_pending",
                    {"patch_id": patch_id, "error_type": type(exc).__name__},
                )
                return "pending"

            db.execute(
                "UPDATE workspace_patches SET state='EXPORTED',import_journal_json=NULL "
                "WHERE patch_id=?",
                (patch_id,),
            )
            self.supervisor._event(
                db,
                task_id,
                "workspace.patch_import_recovered",
                {
                    "workspace_id": workspace_id,
                    "patch_id": patch_id,
                    "patch_sha256": digest,
                    "restored_files": [rel for rel, _after, _before in plans],
                },
            )
        return status

    def build_patch(
        self, task_id: str, owner_actor: str, read_guest_file
    ) -> dict[str, Any]:
        workspace_id, original, manifest = self.snapshot_for_task(
            task_id, owner_actor, require_staged=True
        )
        changes: dict[str, bytes] = {}
        patches: list[str] = []
        for name, before in original.items():
            try:
                after = read_guest_file(name, max_bytes=MAX_SNAPSHOT_BYTES)
            except Exception:
                continue
            if not isinstance(after, bytes):
                raise CapabilityError("PATCH_INVALID", "guest returned an invalid file")
            if hashlib.sha256(before).hexdigest() != hashlib.sha256(after).hexdigest():
                changes[name] = after
                try:
                    old_text, new_text = (
                        before.decode("utf-8").splitlines(keepends=True),
                        after.decode("utf-8").splitlines(keepends=True),
                    )
                except UnicodeDecodeError as exc:
                    raise CapabilityError(
                        "PATCH_INVALID", "binary file changes cannot be imported"
                    ) from exc
                patches.extend(
                    difflib.unified_diff(
                        old_text, new_text, fromfile=f"a/{name}", tofile=f"b/{name}"
                    )
                )
        patch_text = "".join(patches)
        digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
        patch_id = uuid.uuid4().hex
        value = {
            "patch_id": patch_id,
            "workspace_id": workspace_id,
            "base_manifest": manifest,
            "patch": patch_text,
            "patch_sha256": digest,
            "files": {
                name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in changes.items()
            },
            "_contents": changes,
        }
        encoded = {
            name: base64.b64encode(data).decode("ascii")
            for name, data in changes.items()
        }
        persisted = {key: item for key, item in value.items() if key != "_contents"}
        persisted["contents"] = encoded
        with self.supervisor._tx() as db:
            db.execute(
                "INSERT INTO workspace_patches("
                "patch_id,task_id,workspace_id,patch_json,state,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    patch_id,
                    task_id,
                    workspace_id,
                    json.dumps(persisted, sort_keys=True),
                    "EXPORTED",
                    __import__(
                        "openjarvis.tasks.supervisor", fromlist=["utc_now"]
                    ).utc_now(),
                ),
            )
            self.supervisor._event(
                db,
                task_id,
                "workspace.patch_exported",
                {
                    "workspace_id": workspace_id,
                    "patch_id": patch_id,
                    "patch_sha256": digest,
                    "files": sorted(changes),
                },
            )
        return value

    def request_patch_approval(
        self, task_id: str, owner_actor: str, patch: dict[str, Any]
    ) -> tuple[str, str]:
        workspace_id, _original, _manifest = self.snapshot_for_task(
            task_id, owner_actor, require_staged=True
        )
        contents = patch.get("_contents")
        patch_text = patch.get("patch")
        if (
            patch.get("workspace_id") != workspace_id
            or not isinstance(contents, dict)
            or not isinstance(patch_text, str)
        ):
            raise CapabilityError(
                "PATCH_INVALID", "patch is not bound to this owner workspace"
            )
        actual_digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
        if actual_digest != patch.get("patch_sha256"):
            raise CapabilityError(
                "PATCH_INVALID", "patch digest does not match its content"
            )
        patch_id = patch.get("patch_id")
        if not isinstance(patch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", patch_id):
            raise CapabilityError("PATCH_INVALID", "patch identity is invalid")
        row = self.supervisor._db.execute(
            "SELECT patch_json,state FROM workspace_patches "
            "WHERE patch_id=? AND task_id=? AND workspace_id=?",
            (patch_id, task_id, workspace_id),
        ).fetchone()
        if (
            not row
            or row["state"] != "EXPORTED"
            or json.loads(row["patch_json"]).get("patch_sha256") != actual_digest
        ):
            raise CapabilityError(
                "PATCH_INVALID", "patch export record does not match this patch"
            )
        args = {
            "patch_id": patch_id,
            "workspace_id": workspace_id,
            "patch_sha256": actual_digest,
        }
        target = f"workspace:{workspace_id}:patch:{actual_digest}"
        approval_id, digest = self.supervisor.create_approval(
            task_id,
            "mcp:jarvis_dev:import_project_patch",
            target,
            args,
            "Apply this exact verified patch to the registered project workspace.",
        )
        return approval_id, digest

    def apply_approved_patch(
        self, task_id: str, owner_actor: str, patch: dict[str, Any], *, approval_id: str
    ) -> dict[str, Any]:
        patch_id = patch.get("patch_id") if isinstance(patch, dict) else None
        if not isinstance(patch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", patch_id):
            raise CapabilityError("PATCH_INVALID", "patch identity is invalid")
        with _exclusive_patch_import(self.supervisor.db_path, patch_id):
            self._recover_incomplete_patch_import(patch_id)
            try:
                return self._apply_approved_patch_locked(
                    task_id, owner_actor, patch, approval_id=approval_id
                )
            except Exception:
                # If the database commit failed after filesystem changes, the
                # committed IMPORTING journal lets this process compensate now.
                # A process crash releases the flock and startup recovery does
                # the same work later.
                try:
                    self._recover_incomplete_patch_import(patch_id)
                except Exception:
                    # Keep the original operation error visible. The durable
                    # journal remains pending for the startup retry loop.
                    pass
                raise

    def _apply_approved_patch_locked(
        self, task_id: str, owner_actor: str, patch: dict[str, Any], *, approval_id: str
    ) -> dict[str, Any]:
        """Apply VM changes only after owner approval binds the patch digest."""
        workspace_id, original, _manifest = self.snapshot_for_task(
            task_id, owner_actor, require_staged=True
        )
        if patch.get("workspace_id") != workspace_id or not isinstance(
            patch.get("patch_sha256"), str
        ):
            raise CapabilityError(
                "PATCH_INVALID", "patch is not bound to this workspace"
            )
        patch_id = patch.get("patch_id")
        if not isinstance(patch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", patch_id):
            raise CapabilityError("PATCH_INVALID", "patch identity is invalid")
        record = self.supervisor._db.execute(
            "SELECT patch_json,state FROM workspace_patches "
            "WHERE patch_id=? AND task_id=? AND workspace_id=?",
            (patch_id, task_id, workspace_id),
        ).fetchone()
        if not record or record["state"] != "EXPORTED":
            raise CapabilityError(
                "PATCH_INVALID", "patch is unavailable or already imported"
            )
        persisted = json.loads(record["patch_json"])
        if patch.get("patch_sha256") != persisted.get("patch_sha256"):
            raise CapabilityError(
                "PATCH_INVALID", "patch digest differs from its exported record"
            )
        contents = {
            name: base64.b64decode(data, validate=True)
            for name, data in persisted["contents"].items()
        }
        patch_text = persisted.get("patch")
        patch = {**persisted, "_contents": contents}
        if not isinstance(contents, dict) or not isinstance(patch_text, str):
            raise CapabilityError("PATCH_INVALID", "patch payload is malformed")
        digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
        if digest != patch["patch_sha256"]:
            raise CapabilityError(
                "PATCH_INVALID", "patch digest does not match its content"
            )
        expected_diff: list[str] = []
        for rel in sorted(contents):
            if rel not in original or not isinstance(contents[rel], bytes):
                raise CapabilityError(
                    "PATCH_SCOPE",
                    "patch changes a path outside the registered snapshot",
                )
            try:
                expected_diff.extend(
                    difflib.unified_diff(
                        original[rel].decode("utf-8").splitlines(keepends=True),
                        contents[rel].decode("utf-8").splitlines(keepends=True),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
            except UnicodeDecodeError as exc:
                raise CapabilityError(
                    "PATCH_INVALID", "binary file changes cannot be imported"
                ) from exc
        if "".join(expected_diff) != patch_text:
            raise CapabilityError(
                "PATCH_INVALID",
                "unified diff does not match the proposed file contents",
            )
        expected_args = {
            "patch_id": patch_id,
            "workspace_id": workspace_id,
            "patch_sha256": digest,
        }
        expected_target = f"workspace:{workspace_id}:patch:{digest}"
        expected_digest = operation_digest(
            "mcp:jarvis_dev:import_project_patch", expected_target, expected_args
        )
        if set(contents) - set(original):
            raise CapabilityError(
                "PATCH_SCOPE", "patch adds files outside the registered snapshot"
            )
        if set(patch.get("files", {})) != set(contents):
            raise CapabilityError(
                "PATCH_INVALID", "patch file manifest does not match content"
            )

        def validate_import_context(db, expected_patch_state: str):
            task_row = db.execute(
                "SELECT state,recovery_json,capabilities_json FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise CapabilityError("TASK_UNKNOWN", "supervisor task does not exist")
            try:
                task_metadata = json.loads(task_row["recovery_json"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise CapabilityError("TASK_INVALID", "supervisor task metadata is invalid") from exc
            if (
                task_row["state"] not in {
                    "CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL"
                }
                or task_metadata.get("owner_actor") != owner_actor
                or task_metadata.get("scope") != "development"
            ):
                raise CapabilityError("TASK_INACTIVE", "task is no longer authorized to import a patch")

            # Revalidate all database bindings inside the cancellation lock.
            staged_workspace_id, current_original, _manifest = self.snapshot_for_task(
                task_id, owner_actor, require_staged=True
            )
            if staged_workspace_id != workspace_id or current_original != original:
                raise CapabilityError("PATCH_STALE", "workspace snapshot changed before import")
            current_patch = db.execute(
                "SELECT patch_json,state,import_journal_json FROM workspace_patches "
                "WHERE patch_id=? AND task_id=? AND workspace_id=?",
                (patch_id, task_id, workspace_id),
            ).fetchone()
            if (
                not current_patch
                or current_patch["state"] != expected_patch_state
                or json.loads(current_patch["patch_json"]).get("patch_sha256") != digest
            ):
                raise CapabilityError("PATCH_INVALID", "patch import state is unavailable")
            approval_row = db.execute(
                "SELECT task_id,operation,target,args_digest,status,decided_by "
                "FROM operation_approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
            if (
                not approval_row
                or approval_row["task_id"] != task_id
                or approval_row["operation"] != "mcp:jarvis_dev:import_project_patch"
                or approval_row["target"] != expected_target
                or approval_row["args_digest"] != expected_digest
                or approval_row["status"] != "CONSUMED"
                or approval_row["decided_by"] != owner_actor
            ):
                raise CapabilityError(
                    "PATCH_APPROVAL_REQUIRED",
                    "a consumed owner approval for this exact patch is required",
                )
            root_row = db.execute(
                "SELECT canonical_root FROM authorized_workspaces "
                "WHERE workspace_id=? AND owner_actor=? AND revoked_at IS NULL",
                (workspace_id, owner_actor),
            ).fetchone()
            if not root_row:
                raise CapabilityError(
                    "WORKSPACE_NOT_AUTHORIZED", "workspace authorization has been revoked"
                )
            return (
                canonical_workspace(root_row["canonical_root"]),
                current_original,
                current_patch,
            )

        # Commit a recovery journal before touching host files. The exclusive
        # patch flock is held by the public wrapper across both transactions, so
        # recovery cannot mistake this live two-phase import for a crash.
        with self.supervisor._tx() as db:
            root, current_original, _current_patch = validate_import_context(
                db, "EXPORTED"
            )
            if current_original != original:
                raise CapabilityError("PATCH_STALE", "workspace snapshot changed before import")
            for rel in contents:
                _verify_current(root, rel, original[rel])
            journal = json.dumps(
                {
                    "version": 1,
                    "patch_sha256": digest,
                    "files": sorted(contents),
                    "base_manifest": persisted.get("base_manifest", {}),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            updated = db.execute(
                "UPDATE workspace_patches SET state='IMPORTING',import_journal_json=? "
                "WHERE patch_id=? AND task_id=? AND state='EXPORTED'",
                (journal, patch_id, task_id),
            ).rowcount
            if updated != 1:
                raise CapabilityError("PATCH_INVALID", "patch import could not be reserved")
            self.supervisor._event(
                db,
                task_id,
                "workspace.patch_import_started",
                {"workspace_id": workspace_id, "patch_id": patch_id, "patch_sha256": digest},
            )

        failure: CapabilityError | None = None
        with self.supervisor._tx() as db:
            try:
                root, current_original, current_patch = validate_import_context(
                    db, "IMPORTING"
                )
                if current_original != original:
                    raise CapabilityError(
                        "PATCH_STALE", "workspace snapshot changed during import"
                    )
                journal = json.loads(current_patch["import_journal_json"] or "{}")
                if (
                    journal.get("version") != 1
                    or journal.get("patch_sha256") != digest
                    or journal.get("files") != sorted(contents)
                ):
                    raise CapabilityError("PATCH_INVALID", "patch import journal is invalid")
                for rel in contents:
                    _verify_current(root, rel, original[rel])
            except CapabilityError as exc:
                # No host file has been touched in this transaction. Clear the
                # reservation while preserving cancellation/stale-baseline
                # semantics and a durable audit reason.
                db.execute(
                    "UPDATE workspace_patches SET state='EXPORTED',import_journal_json=NULL "
                    "WHERE patch_id=? AND task_id=? AND state='IMPORTING'",
                    (patch_id, task_id),
                )
                self.supervisor._event(
                    db,
                    task_id,
                    "workspace.patch_import_aborted",
                    {"patch_id": patch_id, "reason": exc.code},
                )
                failure = exc
            else:
                attempted: list[str] = []

                def rollback_attempted() -> list[str]:
                    failures: list[str] = []
                    for changed_rel in reversed(attempted):
                        try:
                            _verify_current(root, changed_rel, contents[changed_rel])
                        except CapabilityError:
                            # The atomic rename may not have happened, or an
                            # external edit may have replaced the target. Leave an
                            # already-original file alone; never overwrite a new
                            # owner edit while attempting compensation.
                            try:
                                _verify_current(root, changed_rel, original[changed_rel])
                                continue
                            except CapabilityError:
                                failures.append("PATCH_STALE")
                                continue
                        try:
                            _atomic_replace(
                                root,
                                changed_rel,
                                contents[changed_rel],
                                original[changed_rel],
                            )
                        except Exception as rollback_exc:
                            failures.append(type(rollback_exc).__name__)
                    return failures

                try:
                    for rel, new_content in contents.items():
                        parts = _check_relative(rel)
                        if any(
                            part.startswith(".") or part == ".git" for part in parts
                        ) or not isinstance(new_content, bytes):
                            raise CapabilityError(
                                "PATCH_SCOPE", "patch path or content is not permitted"
                            )
                        if patch["files"][rel] != {
                            "bytes": len(new_content),
                            "sha256": hashlib.sha256(new_content).hexdigest(),
                        }:
                            raise CapabilityError(
                                "PATCH_INVALID", "patch manifest failed validation"
                            )
                        # Track before the call because a parent-directory fsync
                        # error can be reported after os.replace already succeeded.
                        attempted.append(rel)
                        _atomic_replace(root, rel, original[rel], new_content)
                except Exception as exc:
                    rollback_errors = rollback_attempted()
                    if rollback_errors:
                        db.execute(
                            "UPDATE workspace_patches SET state='IMPORT_RECOVERY_PENDING' "
                            "WHERE patch_id=? AND task_id=? AND state='IMPORTING'",
                            (patch_id, task_id),
                        )
                        self.supervisor._event(
                            db,
                            task_id,
                            "workspace.patch_import_recovery_pending",
                            {
                                "patch_id": patch_id,
                                "patch_sha256": digest,
                                "rollback_errors": rollback_errors,
                            },
                        )
                        failure = CapabilityError(
                            "PATCH_ROLLBACK_PENDING",
                            "patch import failed; durable recovery will reconcile changed files",
                        )
                    else:
                        db.execute(
                            "UPDATE workspace_patches SET state='EXPORTED',import_journal_json=NULL "
                            "WHERE patch_id=? AND task_id=? AND state='IMPORTING'",
                            (patch_id, task_id),
                        )
                        self.supervisor._event(
                            db,
                            task_id,
                            "workspace.patch_import_failed",
                            {"patch_id": patch_id, "error_type": type(exc).__name__},
                        )
                        failure = (
                            exc
                            if isinstance(exc, CapabilityError)
                            else CapabilityError(
                                "PATCH_IMPORT_FAILED",
                                "patch import failed; changed files were restored",
                            )
                        )
                else:
                    updated = db.execute(
                        "UPDATE workspace_patches SET state='IMPORTED',import_journal_json=NULL "
                        "WHERE patch_id=? AND task_id=? AND state='IMPORTING'",
                        (patch_id, task_id),
                    ).rowcount
                    if updated != 1:
                        rollback_errors = rollback_attempted()
                        state = "IMPORT_RECOVERY_PENDING" if rollback_errors else "EXPORTED"
                        journal_sql = ",import_journal_json=NULL" if not rollback_errors else ""
                        db.execute(
                            f"UPDATE workspace_patches SET state=?{journal_sql} "
                            "WHERE patch_id=? AND task_id=?",
                            (state, patch_id, task_id),
                        )
                        failure = CapabilityError(
                            "PATCH_ROLLBACK_PENDING" if rollback_errors else "PATCH_INVALID",
                            "patch import state changed before commit",
                        )
                    else:
                        self.supervisor._event(
                            db,
                            task_id,
                            "workspace.patch_applied",
                            {
                                "workspace_id": workspace_id,
                                "patch_sha256": digest,
                                "files": sorted(contents),
                            },
                        )
        if failure is not None:
            raise failure
        return {
            "workspace_id": workspace_id,
            "patch_sha256": digest,
            "files": sorted(contents),
        }


def _atomic_replace(root: str, relative: str, expected_old: bytes, new: bytes) -> None:
    """Replace one registered file via dirfds after rechecking its baseline identity."""
    parts = _check_relative(relative)
    if len(new) > MAX_SNAPSHOT_BYTES:
        raise CapabilityError("PATCH_LIMIT", "patched file exceeds the size limit")
    fds: list[int] = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            fds.append(fd)
        file_mode = _verify_current_at(fd, parts[-1], expected_old)
        temp = f".thozha-patch-{uuid.uuid4().hex}.tmp"
        newfd = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        try:
            os.fchmod(newfd, file_mode)
            with os.fdopen(newfd, "wb") as output:
                output.write(new)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise CapabilityError(
            "PATCH_PATH_DENIED", "workspace patch path cannot be safely updated"
        ) from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _verify_current(root: str, relative: str, expected: bytes) -> None:
    parts = _check_relative(relative)
    fds: list[int] = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            fds.append(fd)
        _verify_current_at(fd, parts[-1], expected)
    except OSError as exc:
        raise CapabilityError(
            "PATCH_PATH_DENIED", "workspace patch path cannot be safely verified"
        ) from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _verify_current_at(parent_fd: int, name: str, expected: bytes) -> int:
    try:
        oldfd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
        )
    except OSError as exc:
        raise CapabilityError(
            "PATCH_STALE", "workspace file changed since snapshot"
        ) from exc
    try:
        info = os.fstat(oldfd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CapabilityError(
                "PATCH_SCOPE", "workspace target is no longer a regular file"
            )
        chunks = []
        while chunk := os.read(oldfd, 65536):
            chunks.append(chunk)
        if b"".join(chunks) != expected:
            raise CapabilityError(
                "PATCH_STALE", "workspace file changed since snapshot"
            )
        return stat.S_IMODE(info.st_mode) & 0o777
    finally:
        os.close(oldfd)
