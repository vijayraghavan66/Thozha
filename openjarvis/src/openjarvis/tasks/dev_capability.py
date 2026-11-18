"""Bounded development capability: the enforcement core for plan-level development approvals.

A capability is issued only after the user approves a plan through the normal supervisor approval flow.
It binds one device, one user, one canonical workspace root, explicit operation types, file restrictions,
size and count quotas, a time limit and a bounded repair budget. Every operation is validated against it
independently, and usage is recorded atomically. Anything outside the bounds is refused; nothing is
expanded automatically.

This module performs no approvals itself. The caller must pass in an approval decision that the supervisor
recorded for the exact plan digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OP_WRITE = "write_file"
OP_TEST = "run_tests"
ALLOWED_OPS = frozenset({OP_WRITE, OP_TEST})
# Test commands are fixed identifiers. Arbitrary command strings are never accepted.
TEST_COMMANDS = {"unittest-tests": ("python3", "-m", "unittest", "discover", "-s", "tests", "-q")}
MAX_FILE_BYTES_HARD = 256 * 1024
MAX_TOTAL_BYTES_HARD = 4 * 1024 * 1024
MAX_WRITES_HARD = 200
MAX_TEST_RUNS_HARD = 50
MAX_REPAIRS_HARD = 5
MAX_DURATION_HARD_S = 4 * 60 * 60
_PLAN_ID = re.compile(r"[A-Za-z0-9_.-]{8,80}")
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,120}")


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
        for prefix in self.allowed_paths:
            _check_relative(prefix, allow_empty=True)


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
    root = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(root):
        raise CapabilityError("WORKSPACE_INVALID", "workspace root must be an existing directory")
    return root


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


def _under_allowed(rel: str, allowed: tuple[str, ...]) -> bool:
    if not allowed or "" in allowed:
        return True
    return any(rel == prefix or rel.startswith(prefix + "/") for prefix in allowed)


def digest_plan(spec_fields: dict[str, Any]) -> str:
    """Canonical digest of the exact plan the user approved; any change alters the digest."""
    canonical = json.dumps(spec_fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CapabilityStore:
    """SQLite-backed, durable capability state. All counter updates are transactional."""

    def __init__(self, path: str | os.PathLike[str], *, clock=time.time) -> None:
        self.path = str(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
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
    def issue(self, spec: CapabilitySpec, *, approved_plan_digest: str, approved_by: str) -> str:
        """Issue only when the supervisor-recorded approval matches this exact plan digest.

        The caller obtains ``approved_plan_digest`` from a decided supervisor approval. A mismatch refuses issuance.
        """
        spec.validate_bounds()
        if approved_plan_digest != spec.plan_digest:
            raise CapabilityError("PLAN_MISMATCH", "approval does not match this exact plan")
        if not approved_by:
            raise CapabilityError("APPROVAL_REQUIRED", "a decided approval is required")
        if approved_by == spec.user_id:
            raise CapabilityError("SELF_APPROVAL", "the requesting user cannot approve their own plan")
        root = canonical_workspace(spec.workspace_root)
        if root != spec.workspace_root:
            spec = CapabilitySpec(**{**spec.__dict__, "workspace_root": root})
        capability_id = uuid.uuid4().hex
        now = self._clock()
        payload = {**spec.__dict__, "allowed_ops": sorted(spec.allowed_ops), "allowed_paths": list(spec.allowed_paths)}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO dev_capabilities(capability_id, spec_json, state, issued_at, expires_at) VALUES (?,?,?,?,?)",
                    (capability_id, json.dumps(payload, sort_keys=True), "ACTIVE", now, now + spec.ttl_s),
                )
                self._audit(capability_id, "issued", plan_id=spec.plan_id, plan_digest=spec.plan_digest,
                            approved_by=approved_by, device_id=spec.device_id, user_id=spec.user_id,
                            workspace_root=root, ttl_s=spec.ttl_s)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return capability_id

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

    def _check_live(self, capability_id: str, device_id: str, user_id: str) -> tuple[dict[str, Any], int, int, int, int]:
        spec, state, _issued, expires, writes, tests, repairs, total, revoked = self._load(capability_id)
        if state != "ACTIVE" or revoked is not None:
            raise CapabilityError("REVOKED", "capability is revoked or cancelled")
        if self._clock() >= expires:
            self._db.execute("UPDATE dev_capabilities SET state='EXPIRED' WHERE capability_id=? AND state='ACTIVE'", (capability_id,))
            raise CapabilityError("EXPIRED", "capability has expired")
        if spec["device_id"] != device_id or spec["user_id"] != user_id:
            raise CapabilityError("IDENTITY_MISMATCH", "request identity is not bound to this capability")
        return spec, writes, tests, repairs, total

    # ---- operations --------------------------------------------------------------------------
    def authorize_write(self, capability_id: str, *, device_id: str, user_id: str, op_id: str,
                        rel_path: str, size: int, content_sha256: str) -> str:
        """Validate one write against every bound, then record it. Returns the canonical target path."""
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha256 or ""):
            raise CapabilityError("INVALID_HASH", "content digest must be SHA-256 hex")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                spec, writes, _tests, _repairs, total = self._check_live(capability_id, device_id, user_id)
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

    def authorize_test(self, capability_id: str, *, device_id: str, user_id: str, op_id: str, command: str) -> tuple[str, ...]:
        if command not in TEST_COMMANDS:
            self._record_denial(capability_id, "test_denied", op_id, "COMMAND_NOT_ALLOWED", command)
            raise CapabilityError("COMMAND_NOT_ALLOWED", "test command is not on the allowlist")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                spec, _writes, tests, _repairs, _total = self._check_live(capability_id, device_id, user_id)
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

    def consume_repair(self, capability_id: str, *, device_id: str, user_id: str, op_id: str) -> int:
        """Spend one bounded repair attempt. Exhaustion stops the loop; it never grants more budget."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                spec, _w, _t, repairs, _total = self._check_live(capability_id, device_id, user_id)
                if repairs + 1 > spec["max_repairs"]:
                    raise CapabilityError("REPAIR_LIMIT", "repair budget is exhausted; request a new approval")
                self._db.execute("UPDATE dev_capabilities SET repairs=repairs+1 WHERE capability_id=?", (capability_id,))
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
    return CapabilitySpec(
        plan_id=plan["plan_id"], device_id=device_id, user_id=user_id, workspace_root=fields["workspace_root"],
        allowed_ops=frozenset(plan["allowed_ops"]), allowed_paths=tuple(plan["allowed_paths"]),
        max_file_bytes=plan["max_file_bytes"], max_total_bytes=plan["max_total_bytes"],
        max_writes=plan["max_writes"], max_test_runs=plan["max_test_runs"], max_repairs=plan["max_repairs"],
        max_duration_s=max_duration_s, ttl_s=ttl_s, plan_digest=digest_plan(fields),
    )
