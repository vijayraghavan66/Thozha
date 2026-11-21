"""Bounded development capability: the enforcement core for plan-level development approvals.

A capability is issued only after the user approves a plan through the normal supervisor approval flow.
It binds one device, one user, one canonical workspace root, explicit operation types, file restrictions,
size and count quotas, a time limit and a bounded repair budget. Every operation is validated against it
independently, and usage is recorded atomically. Anything outside the bounds is refused; nothing is
expanded automatically.

This module performs no approvals itself. The caller supplies an approval ID, but issuance succeeds only
when the canonical supervisor database contains the live decision for the exact plan digest and paired
device actor. Caller-provided approval labels are never treated as evidence.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

OP_WRITE = "write_file"
OP_TEST = "run_tests"
OP_ARTIFACT = "artifact_manifest"
OP_ARTIFACT_EXPORT = "artifact_export"
OP_REPAIR = "repair_attempt"
ALLOWED_OPS = frozenset({OP_WRITE, OP_TEST, OP_ARTIFACT, OP_ARTIFACT_EXPORT})
PLAN_APPROVAL_OPERATION = "mcp:jarvis_dev:approve_plan"
PLAN_APPROVAL_TARGET_PREFIX = "taskflow:plan:"
# Test commands are fixed identifiers. Arbitrary command strings are never accepted.
TEST_COMMANDS = {"unittest-tests": (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q")}
MAX_FILE_BYTES_HARD = 256 * 1024
MAX_TOTAL_BYTES_HARD = 4 * 1024 * 1024
MAX_WRITES_HARD = 200
MAX_TEST_RUNS_HARD = 50
MAX_REPAIRS_HARD = 5
MAX_ARTIFACTS_HARD = 2000
MAX_ARTIFACT_EXPORTS_PER_CAPABILITY = 5
MAX_DURATION_HARD_S = 4 * 60 * 60
MAX_ALLOWED_PATHS_HARD = 24
MAX_CANONICAL_PLAN_BYTES = 16 * 1024
_PLAN_ID = re.compile(r"[A-Za-z0-9_.-]{8,80}")
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,120}")
_DEVICE_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def capability_db_path() -> str:
    """Return the private, persistent capability database path shared by MCP and cancellation routes."""
    configured = os.environ.get("JARVIS_DEV_CAPABILITY_DB")
    path = Path(configured).expanduser() if configured else Path.home() / ".openjarvis" / "data" / "development-capabilities.sqlite3"
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    return str(path)


class CapabilityError(Exception):
    """A denial with a stable, non-sensitive code. Messages never include file contents or secrets."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CapabilitySpec:
    plan_id: str
    device_id: str
    user_id: str
    workspace_root: str                 # canonicalised on issuance
    allowed_ops: frozenset[str]
    allowed_paths: tuple[str, ...]      # relative prefixes; "" means the whole workspace
    max_file_bytes: int
    max_total_bytes: int
    max_writes: int
    max_test_runs: int
    max_repairs: int
    max_duration_s: int
    ttl_s: int
    plan_digest: str                    # digest of the exact plan the user approved

    def validate_bounds(self) -> None:
        if not _PLAN_ID.fullmatch(self.plan_id):
            raise CapabilityError("INVALID_SPEC", "plan_id must be a bounded identifier")
        if not _DEVICE_ID.fullmatch(self.device_id):
            raise CapabilityError("INVALID_SPEC", "device_id must be a canonical companion identifier")
        if not isinstance(self.user_id, str) or not 1 <= len(self.user_id) <= 128 or any(ord(c) < 32 for c in self.user_id):
            raise CapabilityError("INVALID_SPEC", "user_id must be a bounded printable identifier")
        if not _DIGEST.fullmatch(self.plan_digest or ""):
            raise CapabilityError("INVALID_SPEC", "plan_digest must be SHA-256 hex")
        if not self.allowed_ops or not self.allowed_ops <= ALLOWED_OPS:
            raise CapabilityError("INVALID_SPEC", "operation types must be a non-empty subset of the allowlist")
        if not (1 <= self.max_file_bytes <= MAX_FILE_BYTES_HARD):
            raise CapabilityError("INVALID_SPEC", "max_file_bytes out of range")
        if not (1 <= self.max_total_bytes <= MAX_TOTAL_BYTES_HARD):
            raise CapabilityError("INVALID_SPEC", "max_total_bytes out of range")
        if not (0 <= self.max_writes <= MAX_WRITES_HARD and 0 <= self.max_test_runs <= MAX_TEST_RUNS_HARD):
            raise CapabilityError("INVALID_SPEC", "operation counts out of range")
        if not (0 <= self.max_repairs <= MAX_REPAIRS_HARD):
            raise CapabilityError("INVALID_SPEC", "repair budget out of range")
        if not (1 <= self.max_duration_s <= MAX_DURATION_HARD_S and 1 <= self.ttl_s <= MAX_DURATION_HARD_S):
            raise CapabilityError("INVALID_SPEC", "time bounds out of range")
        if len(self.allowed_paths) > MAX_ALLOWED_PATHS_HARD:
            raise CapabilityError("INVALID_SPEC", "approved path list exceeds the displayable plan limit")
        for prefix in self.allowed_paths:
            _check_relative(prefix, allow_empty=True)
        canonical_plan = json.dumps(_spec_fields(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(canonical_plan.encode("utf-8")) > MAX_CANONICAL_PLAN_BYTES:
            raise CapabilityError("INVALID_SPEC", "canonical plan is too large to display and verify")


def _check_relative(rel: str, allow_empty: bool = False) -> list[str]:
    if rel == "" and allow_empty:
        return []
    if not isinstance(rel, str) or "\x00" in rel or "\\" in rel or rel.startswith("/") or rel.startswith("~"):
        raise CapabilityError("PATH_DENIED", "absolute, home-relative, backslash or NUL paths are refused")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise CapabilityError("PATH_DENIED", "empty, '.' and '..' segments are refused")
        if not _SAFE_SEGMENT.fullmatch(part):
            raise CapabilityError("PATH_DENIED", "path segment uses characters outside the allowlist")
    return parts


def canonical_workspace(path: str) -> str:
    candidate = Path(os.path.abspath(os.path.expanduser(path)))
    _reject_workspace_symlinks(candidate)
    root = os.path.realpath(candidate)
    if not os.path.isdir(root):
        raise CapabilityError("WORKSPACE_INVALID", "workspace root must be an existing directory")
    return root


def _reject_workspace_symlinks(candidate: Path) -> None:
    """Reject symlinks in every existing path component, including root ancestors."""
    current = Path(candidate.anchor)
    for segment in candidate.parts[1:]:
        current = current / segment
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise CapabilityError(
                "WORKSPACE_INVALID", "workspace path cannot be inspected safely"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CapabilityError(
                "WORKSPACE_INVALID", "workspace path components must not be symlinks"
            )


def _resolve_inside(root: str, rel: str, *, for_write: bool) -> str:
    """Containment with symlink safety: every existing component must be a real directory or the final file."""
    parts = _check_relative(rel)
    current = root
    for index, part in enumerate(parts):
        candidate = os.path.join(current, part)
        is_last = index == len(parts) - 1
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            if not for_write:
                raise CapabilityError("NOT_FOUND", "path does not exist")
            # Remaining components will be created by the write; they cannot be symlinks because they do not exist.
            current = candidate
            continue
        if stat.S_ISLNK(st.st_mode):
            raise CapabilityError("SYMLINK_DENIED", "symlinks are never followed inside a capability")
        if not is_last and not stat.S_ISDIR(st.st_mode):
            raise CapabilityError("PATH_DENIED", "a non-final component is not a directory")
        current = candidate
    real = os.path.realpath(current)
    root_real = os.path.realpath(root)
    if real != root_real and not real.startswith(root_real + os.sep):
        raise CapabilityError("WORKSPACE_ESCAPE", "resolved path leaves the workspace")
    return real


def _read_regular_file_nofollow(root: str, rel: str, max_bytes: int) -> bytes:
    """Read one bounded workspace file by walking directory FDs without following symlinks."""
    parts = _check_relative(rel)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, os.O_RDONLY | directory_flag | nofollow_flag)
    current_fd = root_fd
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | directory_flag | nofollow_flag, dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow_flag, dir_fd=current_fd)
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise CapabilityError("PATH_DENIED", "artifact is not a regular file")
            if metadata.st_size > max_bytes:
                raise CapabilityError("QUOTA_FILE_SIZE", "artifact exceeds the approved per-file size")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise CapabilityError("QUOTA_FILE_SIZE", "artifact exceeds the approved per-file size")
            return data
        finally:
            os.close(file_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CapabilityError("SYMLINK_DENIED", "symlinks are never followed inside a capability") from exc
        if exc.errno == errno.ENOENT:
            raise CapabilityError("NOT_FOUND", "artifact does not exist") from exc
        raise CapabilityError("PATH_DENIED", "artifact path could not be read safely") from exc
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _under_allowed(rel: str, allowed: tuple[str, ...]) -> bool:
    if not allowed or "" in allowed:
        return True
    return any(rel == prefix or rel.startswith(prefix + "/") for prefix in allowed)


def digest_plan(spec_fields: dict[str, Any]) -> str:
    """Canonical digest of the exact plan the user approved; any change alters the digest."""
    canonical = json.dumps(spec_fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _spec_fields(spec: CapabilitySpec) -> dict[str, Any]:
    """Canonical public capability bounds covered by the supervisor's plan approval."""
    return {
        "plan_id": spec.plan_id,
        "device_id": spec.device_id,
        "user_id": spec.user_id,
        "workspace_root": spec.workspace_root,
        "allowed_ops": sorted(spec.allowed_ops),
        "allowed_paths": list(spec.allowed_paths),
        "max_file_bytes": spec.max_file_bytes,
        "max_total_bytes": spec.max_total_bytes,
        "max_writes": spec.max_writes,
        "max_test_runs": spec.max_test_runs,
        "max_repairs": spec.max_repairs,
        "max_duration_s": spec.max_duration_s,
        "ttl_s": spec.ttl_s,
    }


def plan_approval_target(spec: CapabilitySpec) -> str:
    return PLAN_APPROVAL_TARGET_PREFIX + spec.plan_id


def plan_approval_arguments(spec: CapabilitySpec) -> dict[str, Any]:
    """Exact arguments the lead-owned MCP adapter must put in the supervisor approval record."""
    return {"plan": _spec_fields(spec), "plan_digest": spec.plan_digest}


class CapabilityStore:
    """SQLite-backed, durable capability state. All counter updates are transactional."""

    def __init__(self, path: str | os.PathLike[str], *, clock=time.time) -> None:
        self.path = str(path)
        self._clock = clock
        # Operation methods call issuance/task helpers while holding the store lock.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if self.path != ":memory:":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS dev_capabilities (
                capability_id TEXT PRIMARY KEY, spec_json TEXT NOT NULL, state TEXT NOT NULL,
                issued_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL,
                writes INTEGER NOT NULL DEFAULT 0, test_runs INTEGER NOT NULL DEFAULT 0,
                repairs INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER NOT NULL DEFAULT 0)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS dev_operations (
                op_id TEXT PRIMARY KEY, capability_id TEXT NOT NULL, kind TEXT NOT NULL,
                detail_json TEXT NOT NULL, created_at REAL NOT NULL)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS dev_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT NOT NULL, event TEXT NOT NULL,
                detail_json TEXT NOT NULL, at REAL NOT NULL)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS dev_capability_issuances (
                approval_id TEXT PRIMARY KEY, capability_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL, requester_actor TEXT NOT NULL, plan_digest TEXT NOT NULL,
                approved_by TEXT NOT NULL, created_at REAL NOT NULL)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS dev_capability_issuance_intents (
                approval_id TEXT PRIMARY KEY, capability_id TEXT NOT NULL UNIQUE,
                spec_json TEXT NOT NULL, task_id TEXT NOT NULL, requester_actor TEXT NOT NULL,
                plan_digest TEXT NOT NULL, approved_by TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL)"""
        )

    def close(self) -> None:
        self._db.close()

    # ---- audit -------------------------------------------------------------------------------
    def _audit(self, capability_id: str, event: str, **detail: Any) -> None:
        self._db.execute(
            "INSERT INTO dev_audit(capability_id, event, detail_json, at) VALUES (?,?,?,?)",
            (capability_id, event, json.dumps(detail, sort_keys=True, ensure_ascii=False), self._clock()),
        )

    def audit_events(self, capability_id: str) -> list[tuple[str, dict[str, Any]]]:
        rows = self._db.execute(
            "SELECT event, detail_json FROM dev_audit WHERE capability_id=? ORDER BY seq", (capability_id,)
        ).fetchall()
        return [(event, json.loads(detail)) for event, detail in rows]

    # ---- issuance ----------------------------------------------------------------------------
    def issue(
        self,
        spec: CapabilitySpec,
        *,
        supervisor_store: Any | None = None,
        approval_id: str | None = None,
        task_id: str | None = None,
        requester_actor: str | None = None,
        **caller_claims: Any,
    ) -> str:
        """Issue from a real supervisor record; caller-supplied approval labels are rejected."""
        if caller_claims:
            raise CapabilityError("CALLER_CLAIMS_REJECTED", "approval identity must come from the supervisor record")
        if supervisor_store is None or not approval_id or not task_id or not requester_actor:
            raise CapabilityError("SUPERVISOR_APPROVAL_REQUIRED", "a supervisor-recorded plan approval is required")
        return self.issue_from_supervisor_approval(
            spec,
            supervisor_store=supervisor_store,
            approval_id=approval_id,
            task_id=task_id,
            requester_actor=requester_actor,
        )

    def issue_from_supervisor_approval(
        self,
        spec: CapabilitySpec,
        *,
        supervisor_store: Any,
        approval_id: str,
        task_id: str,
        requester_actor: str,
    ) -> str:
        """Consume one exact, live user approval before issuing a durable project capability.

        The approved MCP operation is ``mcp:jarvis_dev:approve_plan`` with target
        ``taskflow:plan:<plan_id>`` and :func:`plan_approval_arguments`. The approval's
        actual ``decided_by`` value must be the same paired device actor that owns the
        development task. There is no ``approved_by`` or digest assertion parameter.
        """
        spec.validate_bounds()
        if canonical_workspace(spec.workspace_root) != spec.workspace_root:
            raise CapabilityError("WORKSPACE_NOT_CANONICAL", "approved workspace root must already be canonical")
        if digest_plan(_spec_fields(spec)) != spec.plan_digest:
            raise CapabilityError("PLAN_MISMATCH", "capability bounds do not match the approved plan digest")
        if not re.fullmatch(r"[0-9a-f]{32}", task_id or ""):
            raise CapabilityError("TASK_INVALID", "task ID is invalid")
        if requester_actor != f"device:{spec.device_id}":
            raise CapabilityError("IDENTITY_MISMATCH", "requester actor is not bound to this device")

        # Resolve only the canonical supervisor implementation; a model-provided object
        # or dictionary cannot assert that an approval exists.
        from openjarvis.tasks.supervisor import SupervisorStore, operation_digest, utc_now

        if not isinstance(supervisor_store, SupervisorStore):
            raise CapabilityError("SUPERVISOR_REQUIRED", "capabilities can only be issued from SupervisorStore")
        task = supervisor_store.get_task(task_id)
        if task is None:
            raise CapabilityError("TASK_UNKNOWN", "approved development task does not exist")
        try:
            metadata = json.loads(task.get("recovery_json") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise CapabilityError("TASK_INVALID", "development task metadata is invalid") from exc
        if (
            metadata.get("source") != "jarvis_link"
            or metadata.get("scope") != "development"
            or metadata.get("owner_actor") != requester_actor
        ):
            raise CapabilityError("TASK_SCOPE_MISMATCH", "task source, scope, or owning device does not match")

        expected_operation = PLAN_APPROVAL_OPERATION
        expected_target = plan_approval_target(spec)
        expected_arguments = plan_approval_arguments(spec)
        expected_digest = operation_digest(expected_operation, expected_target, expected_arguments)
        approval = supervisor_store.approval(approval_id)
        if approval is None:
            raise CapabilityError("APPROVAL_UNKNOWN", "supervisor approval does not exist")
        if approval.get("task_id") != task_id:
            raise CapabilityError("TASK_MISMATCH", "approval belongs to another task")
        if approval.get("operation") != expected_operation or approval.get("target") != expected_target:
            raise CapabilityError("OPERATION_MISMATCH", "approval is not for this exact development plan")
        if approval.get("args_digest") != expected_digest:
            raise CapabilityError("PLAN_MISMATCH", "supervisor approval does not match the canonical plan")
        if approval.get("status") == "EXPIRED":
            raise CapabilityError("APPROVAL_EXPIRED", "supervisor approval has expired")
        # The supervisor clears arguments_json when a decision is consumed. A retry
        # must therefore use the immutable digest and our durable issuance row, rather
        # than expecting the now-cleared arguments to remain available.
        approved_by = approval.get("decided_by")
        if approval.get("status") == "PENDING":
            raise CapabilityError("SUPERVISOR_APPROVAL_REQUIRED", "supervisor has not recorded a live approval decision")
        if not isinstance(approved_by, str) or approved_by != requester_actor or not approved_by.startswith("device:"):
            raise CapabilityError("APPROVER_IDENTITY_MISMATCH", "approval must come from the paired user device")

        existing = self._issuance(approval_id)
        if existing is not None:
            if (
                existing["task_id"] != task_id
                or existing["requester_actor"] != requester_actor
                or existing["plan_digest"] != spec.plan_digest
                or existing["approved_by"] != approved_by
            ):
                raise CapabilityError("REPLAY", "approval ID was already used for a different capability")
            _payload, state, _issued, expires, _w, _t, _r, _b, revoked = self._load(existing["capability_id"])
            if state != "ACTIVE" or revoked is not None or self._clock() >= expires:
                raise CapabilityError("CAPABILITY_INACTIVE", "previously issued capability is no longer active")
            if task.get("state") not in {"RUNNING", "PLANNING", "VERIFYING"}:
                raise CapabilityError("TASK_INACTIVE", "development task is no longer active")
            return existing["capability_id"]

        intent = self._issuance_intent(approval_id)
        if approval.get("status") == "CONSUMED":
            # Supervisor and capability state live in separate SQLite databases.
            # A durable intent must exist before the approval is consumed; missing
            # intents fail closed even when a consumed supervisor row looks plausible.
            self._validate_issuance_intent(
                intent, spec=spec, task_id=task_id, requester_actor=requester_actor, approved_by=approved_by,
            )
            self._validate_consumed_approval(
                approval, intent=intent, task_id=task_id, operation=expected_operation,
                target=expected_target, args_digest=expected_digest, requester_actor=requester_actor,
                task_state=task.get("state"),
            )
            return self._finalize_issuance_intent(intent)

        if approval.get("status") != "APPROVED":
            raise CapabilityError("SUPERVISOR_APPROVAL_REQUIRED", "supervisor has not recorded a live approval decision")
        try:
            recorded_arguments = json.loads(approval.get("arguments_json") or "null")
        except (TypeError, json.JSONDecodeError) as exc:
            raise CapabilityError("PLAN_MISMATCH", "supervisor approval arguments are invalid") from exc
        if recorded_arguments != expected_arguments:
            raise CapabilityError("PLAN_MISMATCH", "supervisor approval does not match the canonical plan")
        if approval.get("expires_at", "") <= utc_now():
            supervisor_store.expire_approval(approval_id, reason="development_capability_issue_expired")
            raise CapabilityError("APPROVAL_EXPIRED", "supervisor approval has expired")
        if task.get("state") != "WAITING_APPROVAL":
            raise CapabilityError("TASK_STATE_MISMATCH", "task is not waiting for this approval")

        # Persist the exact capability payload and stable ID before the supervisor's
        # one-shot consume commits. This intent is inert until consume is recorded.
        intent = self._reserve_issuance_intent(
            spec, approval_id=approval_id, task_id=task_id,
            requester_actor=requester_actor, approved_by=approved_by,
        )
        consumed = supervisor_store.consume_approval(
            approval_id,
            task_id=task_id,
            operation=expected_operation,
            target=expected_target,
            arguments=expected_arguments,
        )
        if not consumed:
            # The competing issuer may have consumed the decision. Recover only if
            # the durable intent and supervisor's consumed row prove the exact binding.
            approval = supervisor_store.approval(approval_id)
            if approval is None or approval.get("status") != "CONSUMED":
                raise CapabilityError("APPROVAL_ALREADY_USED", "supervisor approval is stale, expired, or already consumed")
            self._validate_consumed_approval(
                approval, intent=intent, task_id=task_id, operation=expected_operation,
                target=expected_target, args_digest=expected_digest, requester_actor=requester_actor,
                task_state=supervisor_store.get_task(task_id).get("state"),
            )
        else:
            approval = supervisor_store.approval(approval_id)
            self._validate_consumed_approval(
                approval, intent=intent, task_id=task_id, operation=expected_operation,
                target=expected_target, args_digest=expected_digest, requester_actor=requester_actor,
                task_state=supervisor_store.get_task(task_id).get("state"),
            )
        return self._finalize_issuance_intent(intent)

    def _issuance_intent(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT approval_id,capability_id,spec_json,task_id,requester_actor,plan_digest,approved_by,created_at,expires_at "
                "FROM dev_capability_issuance_intents WHERE approval_id=?", (approval_id,),
            ).fetchone()
        return dict(row) if row else None

    def _validate_issuance_intent(
        self, intent: dict[str, Any] | None, *, spec: CapabilitySpec, task_id: str,
        requester_actor: str, approved_by: str,
    ) -> None:
        if intent is None:
            raise CapabilityError("APPROVAL_ALREADY_USED", "consumed approval has no durable issuance intent")
        expected_payload = {**spec.__dict__, "allowed_ops": sorted(spec.allowed_ops), "allowed_paths": list(spec.allowed_paths)}
        try:
            payload = json.loads(intent["spec_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise CapabilityError("ISSUANCE_INTENT_INVALID", "durable issuance intent is malformed") from exc
        if (
            payload != expected_payload or intent["task_id"] != task_id
            or intent["requester_actor"] != requester_actor or intent["approved_by"] != approved_by
            or intent["plan_digest"] != spec.plan_digest
        ):
            raise CapabilityError("REPLAY", "approval ID is reserved for a different plan or identity")
        if self._clock() >= intent["expires_at"]:
            raise CapabilityError("CAPABILITY_INACTIVE", "reserved capability expired before recovery")

    @staticmethod
    def _validate_consumed_approval(
        approval: dict[str, Any] | None, *, intent: dict[str, Any], task_id: str,
        operation: str, target: str, args_digest: str, requester_actor: str,
        task_state: str | None,
    ) -> None:
        if approval is None or (
            approval.get("status") != "CONSUMED" or approval.get("task_id") != task_id
            or approval.get("operation") != operation or approval.get("target") != target
            or approval.get("args_digest") != args_digest
            or approval.get("decided_by") != requester_actor
            or not approval.get("consumed_at") or not approval.get("expires_at")
            or approval["consumed_at"] >= approval["expires_at"]
            or task_state not in {"RUNNING", "PLANNING", "VERIFYING"}
            or intent["task_id"] != task_id or intent["requester_actor"] != requester_actor
        ):
            raise CapabilityError("APPROVAL_ALREADY_USED", "consumed supervisor approval does not match the durable issuance intent")

    def _reserve_issuance_intent(
        self, spec: CapabilitySpec, *, approval_id: str, task_id: str,
        requester_actor: str, approved_by: str,
    ) -> dict[str, Any]:
        now = self._clock()
        payload = {**spec.__dict__, "allowed_ops": sorted(spec.allowed_ops), "allowed_paths": list(spec.allowed_paths)}
        values = {
            "approval_id": approval_id, "capability_id": uuid.uuid4().hex,
            "spec_json": json.dumps(payload, sort_keys=True), "task_id": task_id,
            "requester_actor": requester_actor, "plan_digest": spec.plan_digest,
            "approved_by": approved_by, "created_at": now, "expires_at": now + spec.ttl_s,
        }
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT OR IGNORE INTO dev_capability_issuance_intents "
                    "(approval_id,capability_id,spec_json,task_id,requester_actor,plan_digest,approved_by,created_at,expires_at) "
                    "VALUES (:approval_id,:capability_id,:spec_json,:task_id,:requester_actor,:plan_digest,:approved_by,:created_at,:expires_at)",
                    values,
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        intent = self._issuance_intent(approval_id)
        self._validate_issuance_intent(intent, spec=spec, task_id=task_id,
                                       requester_actor=requester_actor, approved_by=approved_by)
        return intent

    def _finalize_issuance_intent(self, intent: dict[str, Any]) -> str:
        now = self._clock()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                existing = self._db.execute(
                    "SELECT capability_id,task_id,requester_actor,plan_digest,approved_by FROM dev_capability_issuances WHERE approval_id=?",
                    (intent["approval_id"],),
                ).fetchone()
                if existing:
                    if dict(existing) != {key: intent[key] for key in ("capability_id", "task_id", "requester_actor", "plan_digest", "approved_by")}:
                        raise CapabilityError("REPLAY", "approval issuance record conflicts with its durable intent")
                    self._db.execute("COMMIT")
                    return intent["capability_id"]
                payload = json.loads(intent["spec_json"])
                self._db.execute(
                    "INSERT INTO dev_capabilities(capability_id,spec_json,state,issued_at,expires_at) VALUES (?,?,?,?,?)",
                    (intent["capability_id"], intent["spec_json"], "ACTIVE", intent["created_at"], intent["expires_at"]),
                )
                self._db.execute(
                    "INSERT INTO dev_capability_issuances(approval_id,capability_id,task_id,requester_actor,plan_digest,approved_by,created_at) VALUES (?,?,?,?,?,?,?)",
                    (intent["approval_id"], intent["capability_id"], intent["task_id"], intent["requester_actor"], intent["plan_digest"], intent["approved_by"], now),
                )
                self._audit(intent["capability_id"], "issued", plan_id=payload["plan_id"], plan_digest=intent["plan_digest"],
                            approval_id=intent["approval_id"], task_id=intent["task_id"], approved_by=intent["approved_by"],
                            device_id=payload["device_id"], user_id=payload["user_id"], workspace_root=payload["workspace_root"],
                            ttl_s=payload["ttl_s"], recovered=now != intent["created_at"])
                self._db.execute("DELETE FROM dev_capability_issuance_intents WHERE approval_id=?", (intent["approval_id"],))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return intent["capability_id"]

    def _issuance(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT capability_id,task_id,requester_actor,plan_digest,approved_by FROM dev_capability_issuances WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        return dict(row) if row else None

    def _issuance_for_capability(self, capability_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT approval_id,capability_id,task_id,requester_actor,plan_digest,approved_by FROM dev_capability_issuances WHERE capability_id=?",
                (capability_id,),
            ).fetchone()
        return dict(row) if row else None

    def revoke_for_supervisor_task(self, supervisor_store: Any, *, task_id: str, requester_actor: str) -> int:
        """Revoke task-bound capabilities only after supervisor cancellation is durable."""
        from openjarvis.tasks.supervisor import SupervisorStore

        if not isinstance(supervisor_store, SupervisorStore):
            raise CapabilityError("SUPERVISOR_REQUIRED", "revocation requires the canonical supervisor store")
        task = supervisor_store.get_task(task_id)
        if task is None:
            raise CapabilityError("TASK_UNKNOWN", "development task does not exist")
        try:
            metadata = json.loads(task.get("recovery_json") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise CapabilityError("TASK_INVALID", "development task metadata is invalid") from exc
        if metadata.get("source") != "jarvis_link" or metadata.get("scope") != "development" or metadata.get("owner_actor") != requester_actor:
            raise CapabilityError("TASK_SCOPE_MISMATCH", "task source, scope, or owner does not match")
        if task.get("state") != "CANCELLED":
            raise CapabilityError("TASK_NOT_CANCELLED", "supervisor must cancel the task before revocation")
        now = self._clock()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT capability_id FROM dev_capability_issuances WHERE task_id=?", (task_id,)
                ).fetchall()
                count = 0
                for (capability_id,) in rows:
                    updated = self._db.execute(
                        "UPDATE dev_capabilities SET state='REVOKED',revoked_at=? WHERE capability_id=? AND state='ACTIVE'",
                        (now, capability_id),
                    )
                    if updated.rowcount:
                        count += 1
                        self._audit(capability_id, "revoked", reason="supervisor_task_cancelled", task_id=task_id)
                self._db.execute("COMMIT")
                return count
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _load(self, capability_id: str) -> tuple[dict[str, Any], str, float, float, int, int, int, int, float | None]:
        row = self._db.execute(
            "SELECT spec_json, state, issued_at, expires_at, writes, test_runs, repairs, total_bytes, revoked_at "
            "FROM dev_capabilities WHERE capability_id=?", (capability_id,)
        ).fetchone()
        if row is None:
            raise CapabilityError("UNKNOWN_CAPABILITY", "no such capability")
        spec_json, state, issued, expires, writes, tests, repairs, total, revoked = row
        spec = json.loads(spec_json)
        return spec, state, issued, expires, writes, tests, repairs, total, revoked

    def _check_live(
        self,
        capability_id: str,
        device_id: str,
        user_id: str,
        *,
        supervisor_store: Any,
        task_id: str,
        requester_actor: str,
    ) -> tuple[dict[str, Any], int, int, int, int]:
        spec, state, _issued, expires, writes, tests, repairs, total, revoked = self._load(capability_id)
        if state != "ACTIVE" or revoked is not None:
            raise CapabilityError("REVOKED", "capability is revoked or cancelled")
        if self._clock() >= expires:
            self._db.execute("UPDATE dev_capabilities SET state='EXPIRED' WHERE capability_id=? AND state='ACTIVE'", (capability_id,))
            raise CapabilityError("EXPIRED", "capability has expired")
        if spec["device_id"] != device_id or spec["user_id"] != user_id:
            raise CapabilityError("IDENTITY_MISMATCH", "request identity is not bound to this capability")
        from openjarvis.tasks.supervisor import SupervisorStore

        if not isinstance(supervisor_store, SupervisorStore):
            raise CapabilityError("SUPERVISOR_REQUIRED", "operation requires the canonical supervisor store")
        issuance = self._issuance_for_capability(capability_id)
        if issuance is None:
            raise CapabilityError("ISSUANCE_MISSING", "capability is not bound to a supervisor approval")
        if issuance["task_id"] != task_id or issuance["requester_actor"] != requester_actor or requester_actor != f"device:{device_id}":
            raise CapabilityError("TASK_BINDING_MISMATCH", "operation task or device does not match the approved capability")
        task = supervisor_store.get_task(task_id)
        if task is None:
            raise CapabilityError("TASK_UNKNOWN", "capability task no longer exists")
        try:
            metadata = json.loads(task.get("recovery_json") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise CapabilityError("TASK_INVALID", "capability task metadata is invalid") from exc
        if metadata.get("source") != "jarvis_link" or metadata.get("scope") != "development" or metadata.get("owner_actor") != requester_actor:
            raise CapabilityError("TASK_BINDING_MISMATCH", "task source, scope, or device does not match the capability")
        if task.get("state") not in {"RUNNING", "PLANNING", "VERIFYING"}:
            raise CapabilityError("TASK_INACTIVE", "development capability is unusable outside an active task")
        return spec, writes, tests, repairs, total

    # ---- operations --------------------------------------------------------------------------
    def authorize_write(self, capability_id: str, *, device_id: str, user_id: str, op_id: str,
                        rel_path: str, size: int, content_sha256: str,
                        supervisor_store: Any, task_id: str, requester_actor: str) -> str:
        """Validate one write against every bound, then record it. Returns the canonical target path."""
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha256 or ""):
            raise CapabilityError("INVALID_HASH", "content digest must be SHA-256 hex")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                spec, writes, _tests, _repairs, total = self._check_live(
                    capability_id, device_id, user_id, supervisor_store=supervisor_store,
                    task_id=task_id, requester_actor=requester_actor,
                )
                if OP_WRITE not in spec["allowed_ops"]:
                    raise CapabilityError("OP_NOT_PERMITTED", "writes are not part of this plan")
                _check_relative(rel_path)
                if not _under_allowed(rel_path, tuple(spec["allowed_paths"])):
                    raise CapabilityError("PATH_NOT_PERMITTED", "path is outside the plan's allowed paths")
                if not (0 <= size <= spec["max_file_bytes"]):
                    raise CapabilityError("QUOTA_FILE_SIZE", "file exceeds the approved per-file size")
                if total + size > spec["max_total_bytes"]:
                    raise CapabilityError("QUOTA_TOTAL_BYTES", "write would exceed the approved total bytes")
                if writes + 1 > spec["max_writes"]:
                    raise CapabilityError("QUOTA_WRITES", "approved write count is exhausted")
                if self._db.execute("SELECT 1 FROM dev_operations WHERE op_id=?", (op_id,)).fetchone():
                    raise CapabilityError("REPLAY", "operation id was already used")
                target = _resolve_inside(spec["workspace_root"], rel_path, for_write=True)
                self._db.execute(
                    "INSERT INTO dev_operations(op_id, capability_id, kind, detail_json, created_at) VALUES (?,?,?,?,?)",
                    (op_id, capability_id, OP_WRITE, json.dumps({"path": rel_path, "size": size, "sha256": content_sha256}), self._clock()),
                )
                self._db.execute(
                    "UPDATE dev_capabilities SET writes=writes+1, total_bytes=total_bytes+? WHERE capability_id=?",
                    (size, capability_id),
                )
                self._audit(capability_id, "write_authorized", op_id=op_id, path=rel_path, size=size, sha256=content_sha256)
                self._db.execute("COMMIT")
            except Exception as failure:
                self._db.execute("ROLLBACK")
                if isinstance(failure, CapabilityError):
                    self._record_denial(capability_id, "write_denied", op_id, failure.code, rel_path)
                raise
        return target

    def authorize_test(self, capability_id: str, *, device_id: str, user_id: str, op_id: str, command: str,
                       supervisor_store: Any, task_id: str, requester_actor: str) -> tuple[str, ...]:
        if command not in TEST_COMMANDS:
            self._record_denial(capability_id, "test_denied", op_id, "COMMAND_NOT_ALLOWED", command)
            raise CapabilityError("COMMAND_NOT_ALLOWED", "test command is not on the allowlist")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                spec, _writes, tests, _repairs, _total = self._check_live(
                    capability_id, device_id, user_id, supervisor_store=supervisor_store,
                    task_id=task_id, requester_actor=requester_actor,
                )
                if OP_TEST not in spec["allowed_ops"]:
                    raise CapabilityError("OP_NOT_PERMITTED", "test runs are not part of this plan")
                if tests + 1 > spec["max_test_runs"]:
                    raise CapabilityError("QUOTA_TEST_RUNS", "approved test-run count is exhausted")
                if self._db.execute("SELECT 1 FROM dev_operations WHERE op_id=?", (op_id,)).fetchone():
                    raise CapabilityError("REPLAY", "operation id was already used")
                self._db.execute(
                    "INSERT INTO dev_operations(op_id, capability_id, kind, detail_json, created_at) VALUES (?,?,?,?,?)",
                    (op_id, capability_id, OP_TEST, json.dumps({"command": command}), self._clock()),
                )
                self._db.execute("UPDATE dev_capabilities SET test_runs=test_runs+1 WHERE capability_id=?", (capability_id,))
                self._audit(capability_id, "test_authorized", op_id=op_id, command=command)
                self._db.execute("COMMIT")
            except Exception as failure:
                self._db.execute("ROLLBACK")
                if isinstance(failure, CapabilityError):
                    self._record_denial(capability_id, "test_denied", op_id, failure.code, command)
                raise
        return TEST_COMMANDS[command]

    def artifact_manifest(
        self,
        capability_id: str,
        *,
        device_id: str,
        user_id: str,
        task_id: str,
        requester_actor: str,
        supervisor_store: Any,
        op_id: str,
        rel_paths: list[str],
        file_reader: Callable[[str, int], bytes] | None = None,
    ) -> dict[str, Any]:
        """Return hashes/sizes only for explicitly listed, in-scope regular files."""
        if not isinstance(rel_paths, list) or len(rel_paths) > MAX_ARTIFACTS_HARD or any(not isinstance(path, str) for path in rel_paths):
            raise CapabilityError("ARTIFACT_LIMIT", "artifact file list exceeds the bounded manifest limit")
        if len(set(rel_paths)) != len(rel_paths):
            raise CapabilityError("REPLAY", "artifact manifest contains duplicate paths")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                spec, _writes, _tests, _repairs, _total = self._check_live(
                    capability_id, device_id, user_id, supervisor_store=supervisor_store,
                    task_id=task_id, requester_actor=requester_actor,
                )
                if OP_ARTIFACT not in spec["allowed_ops"]:
                    raise CapabilityError("OP_NOT_PERMITTED", "artifact delivery is not part of this plan")
                if self._db.execute("SELECT 1 FROM dev_operations WHERE op_id=?", (op_id,)).fetchone():
                    raise CapabilityError("REPLAY", "operation id was already used")
                files: list[dict[str, Any]] = []
                total_bytes = 0
                for rel_path in sorted(rel_paths):
                    _check_relative(rel_path)
                    if not _under_allowed(rel_path, tuple(spec["allowed_paths"])):
                        raise CapabilityError("PATH_NOT_PERMITTED", "artifact is outside the plan's allowed paths")
                    if file_reader is None:
                        data = _read_regular_file_nofollow(spec["workspace_root"], rel_path, spec["max_file_bytes"])
                    else:
                        data = file_reader(rel_path, spec["max_file_bytes"])
                        if not isinstance(data, bytes) or len(data) > spec["max_file_bytes"]:
                            raise CapabilityError("ARTIFACT_LIMIT", "artifact reader exceeded the approved file limit")
                    total_bytes += len(data)
                    if total_bytes > spec["max_total_bytes"]:
                        raise CapabilityError("QUOTA_TOTAL_BYTES", "artifact manifest exceeds the approved total bytes")
                    files.append({"path": rel_path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                manifest_json = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                manifest_digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
                self._db.execute(
                    "INSERT INTO dev_operations(op_id,capability_id,kind,detail_json,created_at) VALUES (?,?,?,?,?)",
                    (op_id, capability_id, OP_ARTIFACT, json.dumps({"files": len(files), "bytes": total_bytes, "manifest_sha256": manifest_digest}), self._clock()),
                )
                self._audit(capability_id, "artifact_manifest_created", files=len(files), bytes=total_bytes, manifest_sha256=manifest_digest)
                self._db.execute("COMMIT")
                return {"files": files, "total_bytes": total_bytes, "manifest_sha256": manifest_digest}
            except Exception as failure:
                self._db.execute("ROLLBACK")
                if isinstance(failure, CapabilityError):
                    self._record_denial(capability_id, "artifact_manifest_denied", op_id, failure.code, "manifest")
                raise

    def artifact_export(
        self,
        capability_id: str,
        *,
        device_id: str,
        user_id: str,
        task_id: str,
        requester_actor: str,
        supervisor_store: Any,
        op_id: str,
        rel_paths: list[str],
        file_reader: Callable[[str, int], bytes],
    ) -> dict[str, Any]:
        """Authorize and fetch only explicitly listed, in-scope guest artifacts for delivery."""
        if not callable(file_reader):
            raise CapabilityError("ARTIFACT_READER_INVALID", "artifact reader is unavailable")
        if not isinstance(rel_paths, list) or not rel_paths or len(rel_paths) > MAX_ARTIFACTS_HARD or any(not isinstance(path, str) for path in rel_paths):
            raise CapabilityError("ARTIFACT_LIMIT", "artifact file list exceeds the bounded export limit")
        if len(set(rel_paths)) != len(rel_paths):
            raise CapabilityError("REPLAY", "artifact export contains duplicate paths")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                spec, _writes, _tests, _repairs, _total = self._check_live(
                    capability_id, device_id, user_id, supervisor_store=supervisor_store,
                    task_id=task_id, requester_actor=requester_actor,
                )
                if OP_ARTIFACT_EXPORT not in spec["allowed_ops"]:
                    raise CapabilityError("OP_NOT_PERMITTED", "artifact delivery is not part of this plan")
                if self._db.execute("SELECT 1 FROM dev_operations WHERE op_id=?", (op_id,)).fetchone():
                    raise CapabilityError("REPLAY", "artifact export operation ID was already used")
                prior_exports = self._db.execute(
                    "SELECT detail_json FROM dev_operations WHERE capability_id=? AND kind=?",
                    (capability_id, OP_ARTIFACT_EXPORT),
                ).fetchall()
                if len(prior_exports) >= MAX_ARTIFACT_EXPORTS_PER_CAPABILITY:
                    raise CapabilityError("ARTIFACT_LIMIT", "artifact export operation quota is exhausted")
                prior_export_bytes = sum(int(json.loads(row["detail_json"]).get("bytes", 0)) for row in prior_exports)
                files: list[dict[str, Any]] = []
                payloads: dict[str, bytes] = {}
                total_bytes = 0
                for rel_path in sorted(rel_paths):
                    _check_relative(rel_path)
                    if not _under_allowed(rel_path, tuple(spec["allowed_paths"])):
                        raise CapabilityError("PATH_NOT_PERMITTED", "artifact is outside the approved export paths")
                    data = file_reader(rel_path, spec["max_file_bytes"])
                    if not isinstance(data, bytes) or len(data) > spec["max_file_bytes"]:
                        raise CapabilityError("ARTIFACT_LIMIT", "artifact reader exceeded the approved file limit")
                    total_bytes += len(data)
                    if total_bytes + prior_export_bytes > spec["max_total_bytes"]:
                        raise CapabilityError("QUOTA_TOTAL_BYTES", "artifact export exceeds the approved total bytes")
                    digest = hashlib.sha256(data).hexdigest()
                    files.append({"path": rel_path, "bytes": len(data), "sha256": digest})
                    payloads[rel_path] = data
                manifest_json = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                manifest_digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
                detail = {"files": len(files), "bytes": total_bytes, "manifest_sha256": manifest_digest}
                self._db.execute(
                    "INSERT INTO dev_operations(op_id,capability_id,kind,detail_json,created_at) VALUES (?,?,?,?,?)",
                    (op_id, capability_id, OP_ARTIFACT_EXPORT, json.dumps(detail, sort_keys=True), self._clock()),
                )
                self._audit(capability_id, "artifact_export_authorized", op_id=op_id, **detail)
                self._db.execute("COMMIT")
                return {**detail, "file_manifest": files, "payloads": payloads}
            except Exception as failure:
                self._db.execute("ROLLBACK")
                if isinstance(failure, CapabilityError):
                    self._record_denial(capability_id, "artifact_export_denied", op_id, failure.code, "manifest")
                raise

    def consume_repair(self, capability_id: str, *, device_id: str, user_id: str, op_id: str,
                       supervisor_store: Any, task_id: str, requester_actor: str) -> int:
        """Spend one bounded repair attempt. Exhaustion stops the loop; it never grants more budget."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                spec, _w, _t, repairs, _total = self._check_live(
                    capability_id, device_id, user_id, supervisor_store=supervisor_store,
                    task_id=task_id, requester_actor=requester_actor,
                )
                if self._db.execute("SELECT 1 FROM dev_operations WHERE op_id=?", (op_id,)).fetchone():
                    raise CapabilityError("REPLAY", "operation id was already used")
                if repairs + 1 > spec["max_repairs"]:
                    raise CapabilityError("REPAIR_LIMIT", "repair budget is exhausted; request a new approval")
                self._db.execute("UPDATE dev_capabilities SET repairs=repairs+1 WHERE capability_id=?", (capability_id,))
                self._db.execute(
                    "INSERT INTO dev_operations(op_id,capability_id,kind,detail_json,created_at) VALUES (?,?,?,?,?)",
                    (op_id, capability_id, OP_REPAIR, json.dumps({"attempt": repairs + 1}), self._clock()),
                )
                self._audit(capability_id, "repair_consumed", op_id=op_id, attempt=repairs + 1)
                self._db.execute("COMMIT")
                return repairs + 1
            except Exception as failure:
                self._db.execute("ROLLBACK")
                if isinstance(failure, CapabilityError):
                    self._record_denial(capability_id, "repair_denied", op_id, failure.code, "")
                raise

    def revoke(self, capability_id: str, *, reason: str) -> None:
        """Cancellation and revocation are immediate and final."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE dev_capabilities SET state='REVOKED', revoked_at=? WHERE capability_id=? AND state='ACTIVE'",
                    (self._clock(), capability_id),
                )
                self._audit(capability_id, "revoked", reason=reason)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _record_denial(self, capability_id: str, event: str, op_id: str, code: str, subject: str) -> None:
        # Denials are audited outside the failed transaction so they survive the rollback.
        with self._lock:
            self._db.execute(
                "INSERT INTO dev_audit(capability_id, event, detail_json, at) VALUES (?,?,?,?)",
                (capability_id, event, json.dumps({"op_id": op_id, "code": code, "subject": subject[:200]}, sort_keys=True), self._clock()),
            )

    def usage(self, capability_id: str) -> dict[str, Any]:
        _spec, state, _i, _e, writes, tests, repairs, total, _r = self._load(capability_id)
        return {"state": state, "writes": writes, "test_runs": tests, "repairs": repairs, "total_bytes": total}


def build_spec(*, plan: dict[str, Any], device_id: str, user_id: str, workspace_root: str,
               ttl_s: int, max_duration_s: int) -> CapabilitySpec:
    """Build a spec whose digest covers every bound, so the approved plan cannot drift from what runs."""
    fields = {
        "plan_id": plan["plan_id"], "device_id": device_id, "user_id": user_id,
        "workspace_root": canonical_workspace(workspace_root),
        "allowed_ops": sorted(plan["allowed_ops"]), "allowed_paths": list(plan["allowed_paths"]),
        "max_file_bytes": plan["max_file_bytes"], "max_total_bytes": plan["max_total_bytes"],
        "max_writes": plan["max_writes"], "max_test_runs": plan["max_test_runs"],
        "max_repairs": plan["max_repairs"], "max_duration_s": max_duration_s, "ttl_s": ttl_s,
    }
    spec = CapabilitySpec(
        plan_id=plan["plan_id"], device_id=device_id, user_id=user_id, workspace_root=fields["workspace_root"],
        allowed_ops=frozenset(plan["allowed_ops"]), allowed_paths=tuple(plan["allowed_paths"]),
        max_file_bytes=plan["max_file_bytes"], max_total_bytes=plan["max_total_bytes"],
        max_writes=plan["max_writes"], max_test_runs=plan["max_test_runs"], max_repairs=plan["max_repairs"],
        max_duration_s=max_duration_s, ttl_s=ttl_s, plan_digest=digest_plan(fields),
    )
    spec.validate_bounds()
    return spec
