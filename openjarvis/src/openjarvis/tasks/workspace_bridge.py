"""Owner-bound project snapshots and verified patch application.

Host paths enter this module only through an owner-authorized registration call.
Model-facing development tools receive opaque workspace IDs and relative paths.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import stat
import uuid
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
            state TEXT NOT NULL, created_at TEXT NOT NULL)"""
        )


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
        approval = self.supervisor.approval(approval_id)
        expected_args = {
            "patch_id": patch_id,
            "workspace_id": workspace_id,
            "patch_sha256": digest,
        }
        expected_target = f"workspace:{workspace_id}:patch:{digest}"
        expected_digest = operation_digest(
            "mcp:jarvis_dev:import_project_patch", expected_target, expected_args
        )
        if (
            not approval
            or approval.get("task_id") != task_id
            or approval.get("operation") != "mcp:jarvis_dev:import_project_patch"
            or approval.get("target") != expected_target
            or approval.get("args_digest") != expected_digest
            or approval.get("status") != "CONSUMED"
            or approval.get("decided_by") != owner_actor
        ):
            raise CapabilityError(
                "PATCH_APPROVAL_REQUIRED",
                "a consumed owner approval for this exact patch is required",
            )
        root_row = self.supervisor._db.execute(
            "SELECT canonical_root FROM authorized_workspaces "
            "WHERE workspace_id=? AND owner_actor=? AND revoked_at IS NULL",
            (workspace_id, owner_actor),
        ).fetchone()
        if not root_row:
            raise CapabilityError(
                "WORKSPACE_NOT_AUTHORIZED", "workspace authorization has been revoked"
            )
        root = canonical_workspace(root_row["canonical_root"])
        if set(contents) - set(original):
            raise CapabilityError(
                "PATCH_SCOPE", "patch adds files outside the registered snapshot"
            )
        if set(patch.get("files", {})) != set(contents):
            raise CapabilityError(
                "PATCH_INVALID", "patch file manifest does not match content"
            )
        # Validate every baseline before any write, so stale projects are untouched.
        for rel in contents:
            _verify_current(root, rel, original[rel])
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
            _atomic_replace(root, rel, original[rel], new_content)
        self.supervisor.record_event(
            task_id,
            "workspace.patch_applied",
            {
                "workspace_id": workspace_id,
                "patch_sha256": digest,
                "files": sorted(contents),
            },
        )
        with self.supervisor._tx() as db:
            db.execute(
                "UPDATE workspace_patches SET state='IMPORTED' "
                "WHERE patch_id=? AND state='EXPORTED'",
                (patch_id,),
            )
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
