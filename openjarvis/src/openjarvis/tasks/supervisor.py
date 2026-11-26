"""Durable task lifecycle and exact-operation approvals.

Approval decisions are bound to the operation name, target, and canonical
argument digest. The tool executor consumes the decision atomically exactly
once; model-authored labels and approval IDs in tool arguments are ignored.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from openjarvis.core.paths import get_config_dir

TASK_STATES = {"CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"}
ACTIVE_STATES = {"PLANNING", "RUNNING", "VERIFYING"}
LINK_SCOPE_CAPABILITIES = {
    # Restricted development scope: bounded project files and the fixed test runner only, inside one
    # allowlisted workspace. Granted only to allowlisted companion devices by the Link host.
    "development": frozenset({"file:read", "file:write", "code:execute", "tool:invoke"}),
    "read_only": frozenset({"file:read"}),
    # Narrow, read-only Mac diagnostics. This remains distinct from assistant
    # control scope so version/uptime probes cannot discover screen or control tools.
    "mac_read": frozenset({"file:read", "tool:read"}),
    "web_research": frozenset({"network:fetch", "file:read"}),
    "desktop_observation": frozenset({"file:read"}),
    # The assistant may request a bounded Mac MCP action, but the Codex bridge
    # turns every state-changing call into an exact supervisor approval.
    "assistant": frozenset({"file:read", "tool:invoke"}),
}
LINK_TASK_SCOPES = frozenset({"read_only", "mac_read", "assistant", "development"})


def is_link_scope(scope) -> bool:
    """A Link task scope is exactly one known scope name in a list (the server-bound form)."""
    return isinstance(scope, list) and len(scope) == 1 and scope[0] in LINK_TASK_SCOPES
_TRANSITIONS = {
    "CREATED": {"PLANNING", "RUNNING", "CANCELLED", "FAILED"},
    "PLANNING": {"RUNNING", "WAITING_APPROVAL", "FAILED", "CANCELLED", "PAUSED"},
    "RUNNING": {"VERIFYING", "WAITING_APPROVAL", "FAILED", "CANCELLED", "PAUSED"},
    "VERIFYING": {"RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED", "PAUSED"},
    "WAITING_APPROVAL": {"RUNNING", "PAUSED", "FAILED", "CANCELLED"},
    "PAUSED": {"PLANNING", "RUNNING", "CANCELLED", "FAILED"},
    "COMPLETED": set(), "FAILED": {"PLANNING"}, "CANCELLED": set(),
}
APPROVAL_PENDING = "PENDING"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"
APPROVAL_EXPIRED = "EXPIRED"
APPROVAL_CONSUMED = "CONSUMED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Full assistant answers are stored unmodified up to this many UTF-8 bytes. Beyond it the text is cut at a
# character boundary and the cut is stated in the text itself, so a long answer is never silently shortened.
MAX_RESULT_TEXT_BYTES = 256 * 1024
RESULT_TEXT_CUT_NOTICE = "\n\n[Jarvis stopped this answer at 256 KiB on the Mac. The rest was not stored.]"
MAX_LINK_CONVERSATION_MESSAGE_CHARS = 12_000
MAX_LINK_CONVERSATION_MESSAGES = 100


def bounded_result_text(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_RESULT_TEXT_BYTES:
        return text
    budget = MAX_RESULT_TEXT_BYTES - len(RESULT_TEXT_CUT_NOTICE.encode("utf-8"))
    kept = encoded[:budget].decode("utf-8", errors="ignore")
    return kept + RESULT_TEXT_CUT_NOTICE


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def operation_digest(operation: str, target: str, arguments: dict[str, Any]) -> str:
    body = canonical_json({"operation": operation, "target": target, "arguments": arguments})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _display_args(args: Any) -> Any:
    secret_key = re.compile(r"(password|secret|token|api.?key|credential|authorization|cookie)", re.I)
    if isinstance(args, dict):
        return {k: ("[redacted]" if secret_key.search(str(k)) else _display_args(v)) for k, v in args.items()}
    if isinstance(args, list):
        return [_display_args(v) for v in args]
    return args


def _target(tool: str, args: dict[str, Any]) -> str:
    for key in ("target", "path", "file_path", "url", "recipient", "app", "name", "working_dir"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    if tool == "shell_exec":
        return str(args.get("working_dir") or "current working directory")[:500]
    return tool


_SAFE_READONLY_BINARIES = {
    # Require PATH resolution to one of the expected system binaries; basename
    # matching alone would let ./git or /tmp/diskutil inherit a safe verdict.
    "df": ("/bin/df", "/usr/bin/df"),
    "pwd": ("/bin/pwd", "/usr/bin/pwd"),
    "ls": ("/bin/ls", "/usr/bin/ls"),
    "stat": ("/usr/bin/stat", "/bin/stat"),
    "du": ("/usr/bin/du", "/bin/du"),
    "whoami": ("/usr/bin/whoami", "/bin/whoami"),
    "uname": ("/usr/bin/uname", "/bin/uname"),
    "system_profiler": ("/usr/sbin/system_profiler",),
    "diskutil": ("/usr/sbin/diskutil",),
    "date": ("/bin/date", "/usr/bin/date"),
}


def _trusted_readonly_executable(token: str) -> bool:
    expected = _SAFE_READONLY_BINARIES.get(token)
    if not expected or "/" in token or "\\" in token:
        return False
    resolved = shutil.which(token)
    if not resolved:
        return False
    try:
        actual = Path(resolved).resolve(strict=True)
        expected_paths = {
            Path(path).resolve(strict=True) for path in expected if Path(path).exists()
        }
    except (OSError, RuntimeError):
        return False
    return actual in expected_paths


def requires_approval(tool: str, args: dict[str, Any], declared: bool = False) -> bool:
    """Classify from the registered tool and actual parameters, never its label."""
    if not isinstance(tool, str) or not isinstance(args, dict):
        return True
    name = tool.lower()
    # Read-only shell operations are safe by command semantics even though the
    # generic shell tool is marked confirmation-required. Never trust a
    # model-authored "safe" flag; inspect the actual parsed command instead.
    if name in {"file_write", "apply_patch", "git_commit", "git_push", "browser_click", "browser_type", "open_app", "shell_exec"}:
        if name == "shell_exec":
            command = args.get("command", "")
            # The shell tool is invoked with shell=True, so recognizing only
            # the first parsed word is unsafe: `df -h; rm ...` and
            # `df -h $(...)` both begin with an apparently read-only binary.
            # Conservatively require a single simple command before applying
            # the read-only executable allowlist. False positives merely ask
            # for approval.
            if not isinstance(command, str) or any(
                marker in command for marker in (";", "&", "|", ">", "<", "`", "$")
            ) or "\n" in command or "\r" in command:
                return True
            try:
                parts = shlex.split(command) if isinstance(command, str) else []
            except ValueError:
                return True
            if not parts:
                return True
            executable = parts[0]
            # Generic Git invokes repository-controlled pagers, fsmonitor and
            # external diff hooks. Use typed, separately reviewed Git adapters.
            if executable == "git":
                return True
            if not _trusted_readonly_executable(executable):
                return True
            if executable == "diskutil":
                if len(parts) == 2 and parts[1] == "list":
                    return False
                if (
                    len(parts) == 3
                    and parts[1] == "info"
                    and re.fullmatch(r"disk[0-9]+(?:s[0-9]+)?", parts[2])
                ):
                    return False
                return True
            if executable == "date":
                return len(parts) != 1
            if executable in _SAFE_READONLY_BINARIES:
                return False
            return True
        return True
    if declared:
        return True
    if name in {"http_request", "webhook_send"}:
        return str(args.get("method", "GET")).upper() not in {"GET", "HEAD", "OPTIONS"}
    if any(word in name for word in ("send_email", "send_message", "delete", "purchase", "payment", "transfer", "publish", "submit", "post_message")):
        return True
    return False


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    approval_id: str | None = None
    source: str = ""
    scope: str = ""
    allowed_capabilities: frozenset[str] | None = None

_CURRENT: contextvars.ContextVar[TaskContext | None] = contextvars.ContextVar("jarvis_task_context", default=None)

@contextmanager
def bind_task(
    task_id: str,
    approval_id: str | None = None,
    *,
    source: str = "",
    scope: str = "",
    allowed_capabilities: frozenset[str] | None = None,
) -> Iterator[None]:
    token = _CURRENT.set(TaskContext(task_id, approval_id, source, scope, allowed_capabilities))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_task_context() -> TaskContext | None:
    return _CURRENT.get()


def operation_target(tool: str, args: dict[str, Any]) -> str:
    """Return the canonical resource identifier used by the approval digest."""
    return _target(tool, args)


_STORE_LOCK = threading.Lock()
_STORE_INSTANCE: "SupervisorStore | None" = None


def get_supervisor_store() -> "SupervisorStore":
    """Return one process-wide store so constructing executors is not recovery."""
    global _STORE_INSTANCE
    with _STORE_LOCK:
        if _STORE_INSTANCE is None:
            _STORE_INSTANCE = SupervisorStore()
        return _STORE_INSTANCE


class SupervisorStore:
    def __init__(self, db_path: str | None = None, *, run_recovery: bool = True) -> None:
        self.db_path = str(db_path or (get_config_dir() / "supervisor.db"))
        Path(self.db_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._create_schema()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        if run_recovery:
            self.recover_after_restart()

    def _create_schema(self) -> None:
        with self._lock:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(
              id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, state TEXT NOT NULL,
              current_step TEXT NOT NULL DEFAULT '', capabilities_json TEXT NOT NULL DEFAULT '[]',
              recovery_json TEXT NOT NULL DEFAULT '{}', verification_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS task_events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
              event_type TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operation_approvals(
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
              operation TEXT NOT NULL, target TEXT NOT NULL, args_digest TEXT NOT NULL,
              safe_args_json TEXT NOT NULL, arguments_json TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL, status TEXT NOT NULL,
              created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT,
              decided_by TEXT, decision_nonce TEXT, consumed_at TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS fast_command_requests(
              request_id TEXT PRIMARY KEY, requester TEXT NOT NULL,
              command TEXT NOT NULL, target TEXT NOT NULL, state TEXT NOT NULL,
              executed INTEGER NOT NULL DEFAULT 0, success INTEGER NOT NULL DEFAULT 0,
              summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS link_task_runs(
              task_id TEXT PRIMARY KEY REFERENCES tasks(id), idempotency_key TEXT NOT NULL UNIQUE,
              objective_digest TEXT NOT NULL, scope TEXT NOT NULL, scope_digest TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
              objective_text TEXT, result_digest TEXT, result_summary TEXT, created_at TEXT NOT NULL,
              conversation_id TEXT NOT NULL DEFAULT '', owner_actor TEXT NOT NULL DEFAULT '',
              started_at TEXT, completed_at TEXT, last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS link_conversation_messages(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              owner_actor TEXT NOT NULL, conversation_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
              content TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(owner_actor,conversation_id,idempotency_key,role)
            );
            CREATE TABLE IF NOT EXISTS mcp_approval_dispatches(
              approval_id TEXT PRIMARY KEY REFERENCES operation_approvals(id),
              task_id TEXT NOT NULL REFERENCES tasks(id), operation TEXT NOT NULL,
              target TEXT NOT NULL, args_digest TEXT NOT NULL, claimed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device_pairings(
              id TEXT PRIMARY KEY, challenge TEXT NOT NULL, created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL, used_at TEXT,
              status TEXT NOT NULL DEFAULT 'INVITED', device_name TEXT NOT NULL DEFAULT '',
              public_key_b64 TEXT, device_fingerprint TEXT, requested_at TEXT,
              confirmed_at TEXT, claim_challenge TEXT, claim_expires_at TEXT,
              claim_used_at TEXT, device_id TEXT
            );
            CREATE TABLE IF NOT EXISTS paired_devices(
              id TEXT PRIMARY KEY, public_key_b64 TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
              device_name TEXT NOT NULL DEFAULT 'Android companion'
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, updated_at);
            CREATE INDEX IF NOT EXISTS idx_approvals_task ON operation_approvals(task_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_link_conversation_messages ON link_conversation_messages(owner_actor,conversation_id,seq);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_decision_nonce ON operation_approvals(decision_nonce) WHERE decision_nonce IS NOT NULL;
            """)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(operation_approvals)")}
            if "arguments_json" not in columns:
                self._db.execute("ALTER TABLE operation_approvals ADD COLUMN arguments_json TEXT NOT NULL DEFAULT '{}'")
            link_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(link_task_runs)")}
            if link_columns and "scope_digest" not in link_columns:
                self._db.execute("ALTER TABLE link_task_runs ADD COLUMN scope_digest TEXT NOT NULL DEFAULT ''")
            if link_columns and "objective_text" not in link_columns:
                self._db.execute("ALTER TABLE link_task_runs ADD COLUMN objective_text TEXT")
            if link_columns and "result_text" not in link_columns:
                # Canonical full answer. result_summary stays a 1000-character preview for Activity cards.
                self._db.execute("ALTER TABLE link_task_runs ADD COLUMN result_text TEXT")
            if link_columns and "conversation_id" not in link_columns:
                self._db.execute("ALTER TABLE link_task_runs ADD COLUMN conversation_id TEXT NOT NULL DEFAULT ''")
            if link_columns and "owner_actor" not in link_columns:
                self._db.execute("ALTER TABLE link_task_runs ADD COLUMN owner_actor TEXT NOT NULL DEFAULT ''")
            pairing_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(device_pairings)")}
            pairing_migrations = {
                "status": "TEXT NOT NULL DEFAULT 'INVITED'",
                "device_name": "TEXT NOT NULL DEFAULT ''",
                "public_key_b64": "TEXT",
                "device_fingerprint": "TEXT",
                "requested_at": "TEXT",
                "confirmed_at": "TEXT",
                "claim_challenge": "TEXT",
                "claim_expires_at": "TEXT",
                "claim_used_at": "TEXT",
                "device_id": "TEXT",
            }
            for name, definition in pairing_migrations.items():
                if name not in pairing_columns:
                    self._db.execute(f"ALTER TABLE device_pairings ADD COLUMN {name} {definition}")
            device_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(paired_devices)")}
            if "device_name" not in device_columns:
                self._db.execute("ALTER TABLE paired_devices ADD COLUMN device_name TEXT NOT NULL DEFAULT 'Android companion'")
            self._db.commit()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _event(self, db: sqlite3.Connection, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        db.execute("INSERT INTO task_events(task_id,event_type,created_at,payload_json) VALUES(?,?,?,?)", (task_id, event_type, utc_now(), canonical_json(_display_args(payload))))

    def create_task(self, request: str, *, capabilities: list[str] | None = None, metadata: dict[str, Any] | None = None) -> str:
        task_id = uuid.uuid4().hex
        now = utc_now()
        req_digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
        with self._tx() as db:
            db.execute("INSERT INTO tasks(id,request_digest,state,capabilities_json,recovery_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (task_id, req_digest, "CREATED", canonical_json(capabilities or []), canonical_json(metadata or {}), now, now))
            self._event(db, task_id, "task.created", {"request_digest": req_digest})
        return task_id

    def transition(self, task_id: str, new_state: str, *, expected: str | None = None, step: str | None = None, error: str | None = None, evidence: dict[str, Any] | None = None) -> None:
        if new_state not in TASK_STATES:
            raise ValueError(f"Unknown task state: {new_state}")
        with self._tx() as db:
            row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            old = row["state"]
            if expected is not None and old != expected:
                raise RuntimeError(f"Task state changed: expected {expected}, found {old}")
            if new_state != old and new_state not in _TRANSITIONS[old]:
                raise RuntimeError(f"Invalid task transition {old} -> {new_state}")
            db.execute("UPDATE tasks SET state=?,current_step=COALESCE(?,current_step),updated_at=?,last_error=? WHERE id=?", (new_state, step, utc_now(), error, task_id))
            self._event(db, task_id, "task.state", {"from": old, "to": new_state, "step": step, "error": error, "verification": evidence or {}})

    def record_event(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        with self._tx() as db:
            self._event(db, task_id, event_type, payload or {})
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (utc_now(), task_id))

    def create_link_task(
        self,
        objective: str,
        *,
        capability_scope: list[str],
        idempotency_key: str,
        owner_actor: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a one-shot Link task or return the prior run for a retry."""
        objective = objective.strip()
        if not objective:
            raise ValueError("objective is required")
        if not is_link_scope(capability_scope):
            raise ValueError("Only read_only, supervisor-gated assistant, or allowlisted development Link scope is enabled")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{16,128}", idempotency_key or ""):
            raise ValueError("idempotency_key must be URL-safe and 16–128 characters")
        if conversation_id is None:
            conversation_id = ""
        if not isinstance(conversation_id, str):
            raise ValueError("conversation_id must be a string")
        if conversation_id and not re.fullmatch(r"[0-9a-f]{32}", conversation_id):
            raise ValueError("conversation_id must be a 32-character lowercase conversation ID")
        scope = capability_scope[0]
        scope_json = canonical_json(capability_scope)
        scope_digest = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()
        objective_digest = hashlib.sha256(objective.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._tx() as db:
            existing = db.execute(
                "SELECT * FROM link_task_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["objective_digest"] != objective_digest or existing["scope"] != scope_json or existing["scope_digest"] != scope_digest:
                    raise ValueError("idempotency key was already used for a different Link request")
                if str(existing["conversation_id"] or "") != conversation_id:
                    raise ValueError("idempotency key was already used in a different conversation")
                task_row = db.execute("SELECT recovery_json FROM tasks WHERE id=?", (existing["task_id"],)).fetchone()
                prior_metadata = json.loads(task_row["recovery_json"] or "{}") if task_row else {}
                prior_owner = prior_metadata.get("owner_actor")
                if prior_owner and owner_actor and prior_owner != owner_actor:
                    raise PermissionError("idempotency key belongs to another paired device")
                return {**dict(existing), "created": False}

            task_id = uuid.uuid4().hex
            metadata = {
                "source": "jarvis_link",
                "scope": scope,
                "capability_scope": capability_scope,
                "scope_digest": scope_digest,
                "idempotency_key": idempotency_key,
                "conversation_id": conversation_id,
            }
            if owner_actor:
                metadata["owner_actor"] = owner_actor
            db.execute(
                "INSERT INTO tasks(id,request_digest,state,capabilities_json,recovery_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, objective_digest, "CREATED", canonical_json(sorted(LINK_SCOPE_CAPABILITIES[scope])), canonical_json(metadata), now, now),
            )
            db.execute(
                "INSERT INTO link_task_runs(task_id,idempotency_key,objective_digest,scope,scope_digest,status,objective_text,created_at,conversation_id,owner_actor) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, idempotency_key, objective_digest, scope_json, scope_digest, "CREATED", objective, now, conversation_id, owner_actor or ""),
            )
            self._event(db, task_id, "task.created", {"request_digest": objective_digest, "source": "jarvis_link", "scope": scope})
            self._event(db, task_id, "link_task.created", {"idempotency_key": idempotency_key, "scope": scope})
            self._event(db, task_id, "link_task.queued", {"state": "CREATED", "scope": scope})
        return {"task_id": task_id, "idempotency_key": idempotency_key, "objective_digest": objective_digest, "scope": scope, "status": "CREATED", "created": True}

    def start_link_task(self, task_id: str, *, objective_digest: str, objective_text_digest: str, capability_scope: list[str], idempotency_key: str) -> dict[str, Any]:
        """Atomically claim one existing CREATED jarvis_link task for execution."""
        now = utc_now()
        with self._tx() as db:
            task = db.execute("SELECT request_digest,state,recovery_json,capabilities_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            metadata = json.loads(task["recovery_json"] or "{}")
            scope = metadata.get("scope", "")
            stored_scope = metadata.get("capability_scope", [])
            scope_json = canonical_json(capability_scope)
            scope_digest = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()
            if metadata.get("source") != "jarvis_link" or scope not in LINK_TASK_SCOPES:
                raise PermissionError("Task is not an approved jarvis_link task")
            if not is_link_scope(capability_scope) or stored_scope != capability_scope or metadata.get("scope_digest") != scope_digest:
                raise PermissionError("Link task capability scope does not match its server-bound scope")
            if task["request_digest"] != objective_digest or task["request_digest"] != objective_text_digest:
                raise PermissionError("Link task objective digest does not match")
            if metadata.get("idempotency_key") != idempotency_key:
                raise PermissionError("Link task idempotency key does not match")
            if json.loads(task["capabilities_json"] or "[]") != sorted(LINK_SCOPE_CAPABILITIES[scope]):
                raise PermissionError("Link task capability scope was modified")
            run = db.execute("SELECT * FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
            if run is None:
                db.execute(
                    "INSERT INTO link_task_runs(task_id,idempotency_key,objective_digest,scope,scope_digest,status,objective_text,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (task_id, idempotency_key, objective_digest, scope_json, scope_digest, "CREATED", "", now),
                )
                status = "CREATED"
            else:
                if run["idempotency_key"] != idempotency_key or run["objective_digest"] != objective_digest or run["scope"] != scope_json or run["scope_digest"] != scope_digest:
                    raise PermissionError("Link task run record does not match")
                status = run["status"]
                if status == "COMPLETED":
                    return {**dict(run), "replay": True}
                if status != "CREATED":
                    raise RuntimeError("Link task has already been claimed")
            if task["state"] != "CREATED":
                raise RuntimeError("Link task is not in CREATED state")
            db.execute("UPDATE tasks SET state='RUNNING',updated_at=?,current_step='codex_link' WHERE id=? AND state='CREATED'", (now, task_id))
            # Clear the queued prompt in the same transaction that claims execution.
            # If the process dies after this point, restart recovery pauses the task
            # and does not replay an operation whose execution may have started.
            db.execute("UPDATE link_task_runs SET status='RUNNING',started_at=?,objective_text=NULL WHERE task_id=? AND status='CREATED'", (now, task_id))
            self._event(db, task_id, "task.state", {"from": "CREATED", "to": "RUNNING", "step": "codex_link"})
            self._event(db, task_id, "link_task.started", {"scope": scope, "objective_digest": objective_digest})
        return {"task_id": task_id, "idempotency_key": idempotency_key, "objective_digest": objective_digest, "scope": scope, "capability_scope": capability_scope, "scope_digest": scope_digest, "status": "RUNNING", "replay": False}

    def queued_link_tasks(self, limit: int = 32) -> list[dict[str, Any]]:
        """Return persisted, not-yet-claimed Link submissions in FIFO order.

        Only tasks still in CREATED are returned. On restart, in-flight tasks
        are paused by recover_after_restart and are deliberately not replayed.
        """
        if not 1 <= limit <= 128:
            raise ValueError("queued Link task limit must be between 1 and 128")
        with self._lock:
            rows = self._db.execute(
                """SELECT r.task_id,r.idempotency_key,r.objective_digest,r.scope,
                          r.scope_digest,r.objective_text,r.created_at
                   FROM link_task_runs AS r JOIN tasks AS t ON t.id=r.task_id
                   WHERE r.status='CREATED' AND t.state='CREATED' AND r.objective_text IS NOT NULL
                   ORDER BY r.created_at,r.task_id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fail_queued_link_task(self, task_id: str, *, error_code: str) -> bool:
        """Fail a corrupt/unrunnable queued task without claiming or replaying it."""
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", error_code):
            raise ValueError("invalid queued task error code")
        now = utc_now()
        with self._tx() as db:
            task = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            run = db.execute("SELECT status FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
            if task is None or run is None or task["state"] != "CREATED" or run["status"] != "CREATED":
                return False
            db.execute(
                "UPDATE tasks SET state='FAILED',current_step='dispatch_failed',updated_at=?,last_error=? WHERE id=? AND state='CREATED'",
                (now, error_code, task_id),
            )
            db.execute(
                "UPDATE link_task_runs SET status='FAILED',objective_text=NULL,completed_at=?,last_error=? WHERE task_id=? AND status='CREATED'",
                (now, error_code, task_id),
            )
            self._event(db, task_id, "link_task.failed", {"error": error_code})
            self._event(db, task_id, "task.state", {"from": "CREATED", "to": "FAILED", "step": "dispatch_failed", "error": error_code})
        return True

    def finish_link_task(
        self,
        task_id: str,
        *,
        result_text: str | None = None,
        error: str | None = None,
        conversation_user_text: str | None = None,
    ) -> None:
        now = utc_now()
        success = error is None
        digest = hashlib.sha256((result_text or "").encode("utf-8")).hexdigest() if success else None
        summary = (result_text or "")[:1000] if success else ""
        full_text = bounded_result_text(result_text or "") if success else None
        with self._tx() as db:
            run = db.execute(
                "SELECT status,idempotency_key,conversation_id,owner_actor FROM link_task_runs WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if run is None or run["status"] != "RUNNING":
                raise RuntimeError("Link task is not running")
            db.execute(
                "UPDATE link_task_runs SET status=?,result_digest=?,result_summary=?,result_text=?,completed_at=?,last_error=? WHERE task_id=?",
                ("COMPLETED" if success else "FAILED", digest, summary, full_text, now, (error or "")[:200] or None, task_id),
            )
            owner = str(run["owner_actor"] or "")
            conversation_id = str(run["conversation_id"] or "")
            if success and owner and conversation_id and conversation_user_text and full_text:
                conversation_user_text = conversation_user_text.strip()
                if conversation_user_text:
                    db.execute(
                        """INSERT OR IGNORE INTO link_conversation_messages
                           (owner_actor,conversation_id,idempotency_key,role,content,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (
                            owner, conversation_id, run["idempotency_key"], "user",
                            conversation_user_text[:MAX_LINK_CONVERSATION_MESSAGE_CHARS], now,
                        ),
                    )
                    db.execute(
                        """INSERT OR IGNORE INTO link_conversation_messages
                           (owner_actor,conversation_id,idempotency_key,role,content,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (
                            owner, conversation_id, run["idempotency_key"], "assistant",
                            (full_text or "")[:MAX_LINK_CONVERSATION_MESSAGE_CHARS], now,
                        ),
                    )
                    db.execute(
                        """DELETE FROM link_conversation_messages WHERE seq IN (
                             SELECT seq FROM link_conversation_messages
                             WHERE owner_actor=? AND conversation_id=?
                             ORDER BY seq DESC LIMIT -1 OFFSET ?
                           )""",
                        (owner, conversation_id, MAX_LINK_CONVERSATION_MESSAGES),
                    )
            self._event(db, task_id, "link_task.completed" if success else "link_task.failed", {"result_digest": digest, "error": (error or "")[:200] or None})

    def mark_link_task_waiting_approval(self, task_id: str, approval_id: str) -> None:
        now = utc_now()
        with self._tx() as db:
            task = db.execute("SELECT state,recovery_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            run = db.execute("SELECT status FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
            approval = db.execute("SELECT task_id,status,operation,target FROM operation_approvals WHERE id=?", (approval_id,)).fetchone()
            if task is None or run is None or approval is None:
                raise KeyError("Link approval task record is missing")
            metadata = json.loads(task["recovery_json"] or "{}")
            allowed_approval = (
                metadata.get("scope") == "assistant"
                or (
                    metadata.get("scope") == "development"
                    and approval["operation"] == "mcp:jarvis_dev:approve_plan"
                    and approval["target"].startswith("taskflow:plan:")
                )
            )
            if metadata.get("source") != "jarvis_link" or not allowed_approval:
                raise PermissionError("This Link task and approval type cannot enter the approval wait state")
            if task["state"] != "WAITING_APPROVAL" or approval["task_id"] != task_id or approval["status"] != "PENDING":
                raise RuntimeError("Link task approval is not pending for this task")
            if run["status"] not in {"RUNNING", "WAITING_APPROVAL"}:
                raise RuntimeError("Link task is not in a resumable state")
            db.execute("UPDATE link_task_runs SET status='WAITING_APPROVAL',last_error=NULL WHERE task_id=?", (task_id,))
            self._event(db, task_id, "link_task.waiting_approval", {"approval_id": approval_id})

    def resume_link_task_after_approval(self, task_id: str, approval_id: str) -> None:
        now = utc_now()
        with self._tx() as db:
            task = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            run = db.execute("SELECT status FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
            approval = db.execute("SELECT task_id,status,operation,target FROM operation_approvals WHERE id=?", (approval_id,)).fetchone()
            if task is None or run is None or approval is None:
                raise KeyError("Link approval task record is missing")
            if task["state"] != "RUNNING" or run["status"] != "WAITING_APPROVAL":
                raise RuntimeError("Link task did not resume through the approval boundary")
            if approval["task_id"] != task_id or approval["status"] != "CONSUMED":
                raise PermissionError("Link approval was not consumed for this task")
            db.execute("UPDATE link_task_runs SET status='RUNNING',last_error=NULL WHERE task_id=?", (task_id,))
            self._event(db, task_id, "link_task.approval_consumed", {"approval_id": approval_id, "resumed_at": now})

    def reject_link_task_after_approval(self, task_id: str, approval_id: str) -> None:
        with self._tx() as db:
            task = db.execute("SELECT state,recovery_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            run = db.execute("SELECT status FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
            approval = db.execute("SELECT task_id,status,operation,target FROM operation_approvals WHERE id=?", (approval_id,)).fetchone()
            if task is None or run is None or approval is None:
                raise KeyError("Link approval task record is missing")
            metadata = json.loads(task["recovery_json"] or "{}")
            allowed_approval = (
                metadata.get("scope") == "assistant"
                or (
                    metadata.get("scope") == "development"
                    and approval["operation"] == "mcp:jarvis_dev:approve_plan"
                    and approval["target"].startswith("taskflow:plan:")
                )
            )
            if metadata.get("source") != "jarvis_link" or not allowed_approval:
                raise PermissionError("This Link task and approval type cannot record plan rejection")
            if task["state"] not in {"PAUSED", "WAITING_APPROVAL"} or run["status"] != "WAITING_APPROVAL":
                raise RuntimeError("Link task is not waiting for this approval")
            if approval["task_id"] != task_id or approval["status"] != "REJECTED":
                raise PermissionError("Supervisor did not record this task approval as rejected")
            db.execute("UPDATE link_task_runs SET status='REJECTED',objective_text=NULL,last_error='approval_rejected' WHERE task_id=?", (task_id,))
            self._event(db, task_id, "link_task.approval_rejected", {"approval_id": approval_id})

    def link_task_run(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def link_task_run_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        """Return a safe, persisted Link task status lookup without exposing its objective."""
        with self._lock:
            row = self._db.execute(
                """SELECT r.*, t.state AS task_state
                   FROM link_task_runs AS r JOIN tasks AS t ON t.id=r.task_id
                   WHERE r.idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
        return dict(row) if row else None

    def link_conversation_context(
        self,
        task_id: str,
        current_objective: str,
        *,
        max_prior_turns: int = 6,
        max_context_chars: int = 32_000,
    ) -> list[dict[str, str]]:
        """Return persisted owner-bound turns and the current prompt for a Link task.

        Older API clients without a conversation ID remain isolated one-turn prompts.
        Only completed turns for the exact owner and conversation are included.
        """
        current = current_objective.strip() if isinstance(current_objective, str) else ""
        if not current:
            raise ValueError("current conversation turn is required")
        max_prior_turns = max(0, min(int(max_prior_turns), 8))
        max_context_chars = max(1_024, min(int(max_context_chars), 64_000))
        with self._lock:
            task = self._db.execute(
                "SELECT recovery_json FROM tasks WHERE id=?", (task_id,),
            ).fetchone()
            run = self._db.execute(
                "SELECT conversation_id,owner_actor,idempotency_key FROM link_task_runs WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None or run is None:
                raise KeyError("Link conversation task is missing")
            metadata = json.loads(task["recovery_json"] or "{}")
            owner = str(run["owner_actor"] or "")
            conversation_id = str(run["conversation_id"] or "")
            if (
                metadata.get("source") != "jarvis_link"
                or not owner
                or metadata.get("owner_actor") != owner
                or metadata.get("conversation_id") != conversation_id
                or not conversation_id
                or max_prior_turns == 0
            ):
                return [{"role": "user", "content": current}]
            rows = self._db.execute(
                """SELECT role,content FROM link_conversation_messages
                   WHERE owner_actor=? AND conversation_id=? AND idempotency_key<>?
                   ORDER BY seq DESC LIMIT ?""",
                (owner, conversation_id, run["idempotency_key"], max_prior_turns * 2),
            ).fetchall()
        prior = [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in reversed(rows)
        ]
        # The table commits user/assistant pairs atomically. If an old or
        # partially migrated record violates that invariant, keep only complete pairs.
        if len(prior) % 2:
            prior = prior[1:]
        pairs = [prior[index:index + 2] for index in range(0, len(prior), 2)]
        while pairs and sum(len(message["content"]) for pair in pairs for message in pair) + len(current) > max_context_chars:
            pairs.pop(0)
        history = [message for pair in pairs for message in pair]
        history.append({"role": "user", "content": current})
        return history

    def cancel_link_task(self, task_id: str, *, actor: str) -> dict[str, Any]:
        """Cancel a Link task and revoke any not-yet-consumed approval atomically.

        Cancellation cannot reverse a Mac operation whose one-time dispatch was
        already claimed. In that case it stops the remaining task continuation
        and reports that an operation may already have started.
        """
        now = utc_now()
        with self._tx() as db:
            task = db.execute("SELECT state,recovery_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            run = db.execute("SELECT status FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
            if task is None or run is None:
                raise KeyError(task_id)
            metadata = json.loads(task["recovery_json"] or "{}")
            if metadata.get("source") != "jarvis_link":
                raise PermissionError("Task is not a Jarvis Link task")
            owner = metadata.get("owner_actor")
            if owner and actor != owner and not actor.startswith("api:"):
                raise PermissionError("Task belongs to another paired device")
            state = task["state"]
            if state in {"COMPLETED", "FAILED", "CANCELLED"} or run["status"] in {"COMPLETED", "FAILED", "REJECTED", "CANCELLED"}:
                return {"state": state, "cancelled": state == "CANCELLED", "action_may_have_started": False}

            claimed = db.execute(
                "SELECT 1 FROM mcp_approval_dispatches WHERE task_id=? LIMIT 1", (task_id,)
            ).fetchone() is not None
            if state not in {"CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL", "PAUSED"}:
                raise RuntimeError(f"Task cannot be cancelled from {state}")
            db.execute(
                "UPDATE tasks SET state='CANCELLED',current_step='cancelled',updated_at=?,last_error='cancelled_by_user' WHERE id=?",
                (now, task_id),
            )
            pending = db.execute(
                "SELECT id FROM operation_approvals WHERE task_id=? AND status IN ('PENDING','APPROVED')",
                (task_id,),
            ).fetchall()
            db.execute(
                "UPDATE operation_approvals SET status='EXPIRED',last_error='task_cancelled' WHERE task_id=? AND status IN ('PENDING','APPROVED')",
                (task_id,),
            )
            db.execute(
                "UPDATE link_task_runs SET status='CANCELLED',objective_text=NULL,completed_at=?,last_error='cancelled_by_user' WHERE task_id=? AND status NOT IN ('COMPLETED','FAILED','REJECTED','CANCELLED')",
                (now, task_id),
            )
            self._event(db, task_id, "task.cancelled", {
                "actor": actor,
                "revoked_approval_count": len(pending),
                "action_may_have_started": claimed,
            })
            self._event(db, task_id, "task.state", {"from": state, "to": "CANCELLED", "step": "cancelled"})
            for approval in pending:
                self._event(db, task_id, "approval.expired", {"approval_id": approval["id"], "reason": "task_cancelled"})
            return {"state": "CANCELLED", "cancelled": True, "action_may_have_started": claimed}

    def create_approval(self, task_id: str, operation: str, target: str, arguments: dict[str, Any], reason: str, ttl_seconds: int = 600) -> tuple[str, str]:
        if not self.task_exists(task_id):
            raise KeyError(f"Unknown task: {task_id}")
        digest = operation_digest(operation, target, arguments)
        approval_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=max(1, ttl_seconds))).isoformat()
        safe_args = canonical_json(_display_args(arguments))
        with self._tx() as db:
            task = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(f"Unknown task: {task_id}")
            if task["state"] not in {"CREATED", "PLANNING", "RUNNING", "VERIFYING"}:
                raise RuntimeError(f"Cannot request approval while task is {task['state']}")
            db.execute("INSERT INTO operation_approvals(id,task_id,operation,target,args_digest,safe_args_json,arguments_json,reason,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (approval_id, task_id, operation, target, digest, safe_args, canonical_json(arguments), reason[:1000], "PENDING", now.isoformat(), expires))
            old_state = task["state"]
            db.execute("UPDATE tasks SET state='WAITING_APPROVAL',updated_at=? WHERE id=?", (now.isoformat(), task_id))
            self._event(db, task_id, "approval.created", {"approval_id": approval_id, "operation": operation, "target": target, "args_digest": digest, "reason": reason[:1000]})
            self._event(db, task_id, "task.state", {"from": old_state, "to": "WAITING_APPROVAL", "step": "approval"})
        self.schedule_approval_notification(approval_id)
        return approval_id, digest

    def schedule_approval_notification(self, approval_id: str) -> None:
        """Best-effort push for a new pending approval; disabled without credentials."""
        try:
            from openjarvis.server.fcm_notifications import dispatch_pending_approval, service_account_path

            if service_account_path() is None:
                return

            def dispatch() -> None:
                try:
                    dispatch_pending_approval(self.db_path, approval_id)
                except Exception:
                    # Never expose credentials, registration tokens, or args.
                    return

            threading.Thread(target=dispatch, name="jarvis-fcm-notification", daemon=True).start()
        except Exception:
            return

    def expire_approval(self, approval_id: str, *, reason: str = "expired") -> None:
        now = utc_now()
        with self._tx() as db:
            row = db.execute("SELECT task_id,status FROM operation_approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None or row["status"] not in {"PENDING", "APPROVED"}:
                return
            db.execute("UPDATE operation_approvals SET status='EXPIRED',last_error=? WHERE id=? AND status IN ('PENDING','APPROVED')", (reason[:500], approval_id))
            self._event(db, row["task_id"], "approval.expired", {"approval_id": approval_id, "reason": reason[:500]})
            self._terminalize_expired_link_approval(db, row["task_id"], approval_id, now=now)

    def _terminalize_expired_link_approval(
        self, db: sqlite3.Connection, task_id: str, approval_id: str, *, now: str,
    ) -> bool:
        """Make an expired Link approval's wait terminal without resuming or dispatching it."""
        task = db.execute("SELECT state,recovery_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        run = db.execute("SELECT status FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
        if task is None or run is None or task["state"] not in {"WAITING_APPROVAL", "PAUSED"} or run["status"] != "WAITING_APPROVAL":
            return False
        try:
            metadata = json.loads(task["recovery_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        if metadata.get("source") != "jarvis_link":
            return False
        other = db.execute(
            "SELECT 1 FROM operation_approvals WHERE task_id=? AND id<>? AND status IN ('PENDING','APPROVED') AND expires_at>? LIMIT 1",
            (task_id, approval_id, now),
        ).fetchone()
        if other:
            return False
        previous = task["state"]
        db.execute(
            "UPDATE tasks SET state='FAILED',current_step='approval_expired',updated_at=?,last_error='approval_expired' WHERE id=? AND state IN ('WAITING_APPROVAL','PAUSED')",
            (now, task_id),
        )
        db.execute(
            "UPDATE link_task_runs SET status='FAILED',objective_text=NULL,result_summary='',result_text=NULL,completed_at=?,last_error='approval_expired' WHERE task_id=? AND status='WAITING_APPROVAL'",
            (now, task_id),
        )
        self._event(db, task_id, "link_task.failed", {"error": "approval_expired", "approval_id": approval_id})
        self._event(db, task_id, "task.state", {
            "from": previous, "to": "FAILED", "step": "approval_expired", "error": "approval_expired",
        })
        return True

    def decide_approval(self, approval_id: str, *, decision: str, expected_digest: str, task_id: str, actor: str, nonce: str) -> bool:
        status = "APPROVED" if decision == "approve" else "REJECTED" if decision == "reject" else None
        if status is None: raise ValueError("decision must be approve or reject")
        now = datetime.now(timezone.utc).isoformat()
        with self._tx() as db:
            if db.execute("SELECT 1 FROM operation_approvals WHERE decision_nonce=?", (nonce,)).fetchone():
                return False
            row = db.execute("SELECT task_id,operation,target,args_digest,status,expires_at FROM operation_approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None or row["task_id"] != task_id or row["args_digest"] != expected_digest or row["status"] != "PENDING":
                return False
            if row["expires_at"] <= now:
                db.execute("UPDATE operation_approvals SET status='EXPIRED',last_error='approval expired before decision' WHERE id=? AND status='PENDING'", (approval_id,))
                self._event(db, task_id, "approval.expired", {"approval_id": approval_id, "reason": "decision_after_expiry"})
                self._terminalize_expired_link_approval(db, task_id, approval_id, now=now)
                return False
            cur = db.execute("UPDATE operation_approvals SET status=?,decided_at=?,decided_by=?,decision_nonce=? WHERE id=? AND task_id=? AND args_digest=? AND status='PENDING' AND expires_at>?", (status, now, actor, nonce, approval_id, task_id, expected_digest, now))
            if cur.rowcount != 1: return False
            self._event(db, task_id, "approval.decision", {"approval_id": approval_id, "decision": decision, "actor": actor, "args_digest": expected_digest})
            if row["operation"] == "mcp:jarvis_mac:open_app":
                self._event(db, task_id, "mac.open_app.decision", {
                    "task_id": task_id,
                    "approval_id": approval_id,
                    "operation": row["operation"],
                    "requested_application": row["target"][:120],
                    "decision": decision,
                    "dispatch_claim_outcome": "not_attempted",
                    "execution_attempted": False,
                    "result": status.lower(),
                    "timestamp_utc": now,
                    "process_context": {"pid": os.getpid(), "ppid": os.getppid()},
                })
            if status == "REJECTED":
                db.execute("UPDATE tasks SET state='PAUSED',updated_at=? WHERE id=? AND state='WAITING_APPROVAL'", (now, task_id))
                self._event(db, task_id, "task.state", {"from": "WAITING_APPROVAL", "to": "PAUSED", "step": "approval_rejected"})
            return True

    def consume_approval(self, approval_id: str, *, task_id: str, operation: str, target: str, arguments: dict[str, Any]) -> bool:
        digest = operation_digest(operation, target, arguments)
        now = datetime.now(timezone.utc).isoformat()
        with self._tx() as db:
            cur = db.execute("UPDATE operation_approvals SET status='CONSUMED',consumed_at=?,arguments_json='{}' WHERE id=? AND task_id=? AND operation=? AND target=? AND args_digest=? AND status='APPROVED' AND expires_at>? AND consumed_at IS NULL", (now, approval_id, task_id, operation, target, digest, now))
            if cur.rowcount != 1: return False
            self._event(db, task_id, "approval.consumed", {"approval_id": approval_id, "operation": operation, "target": target, "args_digest": digest})
            row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] != "WAITING_APPROVAL":
                # Do not let a valid but detached approval execute outside the
                # task's expected lifecycle.
                db.execute("UPDATE operation_approvals SET status='EXPIRED',last_error='task state mismatch' WHERE id=?", (approval_id,))
                self._event(db, task_id, "tool.authorization_denied", {"approval_id": approval_id, "reason": "task_state_mismatch"})
                return False
            db.execute("UPDATE tasks SET state='RUNNING',updated_at=? WHERE id=?", (now, task_id))
            self._event(db, task_id, "task.state", {"from": "WAITING_APPROVAL", "to": "RUNNING", "step": "approved_operation"})
            return True

    def claim_mcp_dispatch(
        self,
        approval_id: str,
        *,
        task_id: str,
        operation: str,
        target: str,
        arguments: dict[str, Any],
    ) -> bool:
        """Atomically claim one post-consumption MCP dispatch in the child process."""
        digest = operation_digest(operation, target, arguments)
        now = datetime.now(timezone.utc).isoformat()
        with self._tx() as db:
            approval = db.execute(
                "SELECT task_id,operation,target,args_digest,status,consumed_at,expires_at FROM operation_approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
            task = db.execute("SELECT state,request_digest,recovery_json,capabilities_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            if (
                approval is None or task is None or approval["task_id"] != task_id
                or approval["operation"] != operation or approval["target"] != target
                or approval["args_digest"] != digest or approval["status"] != "CONSUMED"
                or approval["consumed_at"] is None or approval["expires_at"] <= now
                or task["state"] != "RUNNING"
            ):
                return False
            try:
                metadata = json.loads(task["recovery_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                return False
            source = metadata.get("source")
            if source not in {"agent_chat", "jarvis_link"}:
                return False
            if source == "jarvis_link":
                scope = metadata.get("capability_scope")
                try:
                    scope_json = canonical_json(scope)
                    scope_digest = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()
                    capabilities = json.loads(task["capabilities_json"] or "[]")
                    run = db.execute("SELECT objective_digest,scope,scope_digest,idempotency_key,status FROM link_task_runs WHERE task_id=?", (task_id,)).fetchone()
                except (TypeError, ValueError, json.JSONDecodeError):
                    return False
                if (
                    not is_link_scope(scope)
                    or metadata.get("scope") != scope[0]
                    or metadata.get("scope_digest") != scope_digest
                    or capabilities != sorted(LINK_SCOPE_CAPABILITIES[scope[0]])
                    or run is None
                    or run["objective_digest"] != task["request_digest"]
                    or run["scope"] != scope_json
                    or run["scope_digest"] != scope_digest
                    or run["idempotency_key"] != metadata.get("idempotency_key")
                    or run["status"] != "RUNNING"
                ):
                    return False
            cur = db.execute(
                "INSERT OR IGNORE INTO mcp_approval_dispatches(approval_id,task_id,operation,target,args_digest,claimed_at) VALUES(?,?,?,?,?,?)",
                (approval_id, task_id, operation, target, digest, now),
            )
            if cur.rowcount != 1:
                return False
            self._event(db, task_id, "tool.mcp_approval_dispatch_claimed", {
                "approval_id": approval_id,
                "operation": operation,
                "target": target,
                "args_digest": digest,
            })
            return True

    def task_exists(self, task_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is not None

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def events(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT seq,event_type,created_at,payload_json FROM task_events WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
        return [{"seq":r["seq"],"event_type":r["event_type"],"created_at":r["created_at"],"payload":json.loads(r["payload_json"])} for r in rows]

    def events_after(self, task_id: str, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if after_seq < 0 or not 1 <= limit <= 200:
            raise ValueError("event cursor or limit is invalid")
        with self._lock:
            rows = self._db.execute(
                "SELECT seq,event_type,created_at,payload_json FROM task_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                (task_id, after_seq, limit),
            ).fetchall()
        return [{"seq": r["seq"], "event_type": r["event_type"], "created_at": r["created_at"],
                 "payload": json.loads(r["payload_json"])} for r in rows]

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM operation_approvals WHERE id=?", (approval_id,)).fetchone()
        if not row: return None
        data=dict(row); data["safe_args"] = json.loads(data.pop("safe_args_json")); return data

    def claim_fast_command(
        self, *, request_id: str, requester: str, command: str, target: str,
    ) -> tuple[dict[str, Any], bool]:
        """Durably claim one typed fast command; replays never dispatch it twice."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM fast_command_requests WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is not None:
                existing = dict(row)
                if any(existing[key] != value for key, value in {
                    "requester": requester, "command": command, "target": target,
                }.items()):
                    raise ValueError("fast-command idempotency key conflicts with the original request")
                if existing["state"] == "EXECUTING":
                    try:
                        updated = datetime.fromisoformat(existing["updated_at"])
                    except (TypeError, ValueError):
                        updated = now_dt - timedelta(days=1)
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    if (now_dt - updated).total_seconds() > 30:
                        db.execute(
                            "UPDATE fast_command_requests SET state='INDETERMINATE',executed=0,success=0,"
                            "summary=?,updated_at=? WHERE request_id=? AND state='EXECUTING'",
                            (
                                "The earlier activation did not record a terminal result. "
                                "Check the Mac before retrying.",
                                now,
                                request_id,
                            ),
                        )
                        row = db.execute(
                            "SELECT * FROM fast_command_requests WHERE request_id=?", (request_id,),
                        ).fetchone()
                        existing = dict(row)
                return existing, False
            db.execute(
                "INSERT INTO fast_command_requests(request_id,requester,command,target,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (request_id, requester, command, target, "EXECUTING", now, now),
            )
            row = db.execute(
                "SELECT * FROM fast_command_requests WHERE request_id=?", (request_id,),
            ).fetchone()
        return dict(row), True

    def finish_fast_command(
        self, request_id: str, *, state: str, executed: bool, success: bool, summary: str,
    ) -> dict[str, Any] | None:
        """Persist the real outcome of a claimed typed command exactly once."""
        if state not in {"COMPLETED", "FAILED", "INDETERMINATE"}:
            raise ValueError("invalid terminal fast-command state")
        if success and (state != "COMPLETED" or not executed):
            raise ValueError("successful command requires confirmed completion")
        now = utc_now()
        with self._tx() as db:
            cur = db.execute(
                "UPDATE fast_command_requests SET state=?,executed=?,success=?,summary=?,updated_at=? "
                "WHERE request_id=? AND state='EXECUTING'",
                (state, int(executed), int(success), summary[:500], now, request_id),
            )
            row = db.execute(
                "SELECT * FROM fast_command_requests WHERE request_id=?", (request_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def pending_approvals(self, limit: int = 100) -> list[dict[str, Any]]:
        now = utc_now()
        with self._tx() as db:
            stale = db.execute("SELECT id,task_id FROM operation_approvals WHERE status IN ('PENDING','APPROVED') AND expires_at<=?", (now,)).fetchall()
            for row in stale:
                db.execute("UPDATE operation_approvals SET status='EXPIRED',last_error='approval expired' WHERE id=?", (row["id"],))
                self._event(db, row["task_id"], "approval.expired", {"approval_id": row["id"], "reason": "expired_during_listing"})
                self._terminalize_expired_link_approval(db, row["task_id"], row["id"], now=now)
            # Older versions expired the approval row but left its Link task waiting forever.
            # Reconcile that durable state without replaying or consuming the approval.
            expired_waits = db.execute(
                "SELECT a.id,a.task_id FROM operation_approvals a JOIN tasks t ON t.id=a.task_id "
                "JOIN link_task_runs r ON r.task_id=t.id "
                "WHERE a.status='EXPIRED' AND t.state IN ('WAITING_APPROVAL','PAUSED') AND r.status='WAITING_APPROVAL'"
            ).fetchall()
            for row in expired_waits:
                self._terminalize_expired_link_approval(db, row["task_id"], row["id"], now=now)
            rows = db.execute("SELECT * FROM operation_approvals WHERE status='PENDING' ORDER BY created_at LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["safe_args"] = json.loads(item.pop("safe_args_json"))
            result.append(item)
        return result

    def recent_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [dict(row) for row in rows]

    def begin_device_pairing(self, ttl_seconds: int = 300) -> tuple[str, str, str]:
        pairing_id = uuid.uuid4().hex
        challenge = uuid.uuid4().hex + uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=max(30, min(ttl_seconds, 600)))).isoformat()
        with self._tx() as db:
            db.execute(
                "INSERT INTO device_pairings(id,challenge,created_at,expires_at,status) VALUES(?,?,?,?, 'INVITED')",
                (pairing_id, challenge, now.isoformat(), expires),
            )
        return pairing_id, challenge, expires

    def complete_device_pairing(
        self,
        pairing_id: str,
        public_key_b64: str,
        signature_b64: str,
        *,
        device_name: str = "Android companion",
    ) -> dict[str, str]:
        """Validate the phone's one-time signature; never issue a token here."""
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        try:
            public_key_data = base64.b64decode(public_key_b64, validate=True)
            signature = base64.b64decode(signature_b64, validate=True)
            public_key = serialization.load_der_public_key(public_key_data)
            if not isinstance(public_key, ec.EllipticCurvePublicKey) or public_key.curve.name != "secp256r1":
                raise ValueError("expected a P-256 ECDSA public key")
        except Exception as exc:
            raise ValueError("invalid public key or signature encoding") from exc

        now = datetime.now(timezone.utc)
        safe_name = " ".join(
            "".join(char for char in str(device_name) if char >= " " and char != "\x7f").split()
        )[:80] or "Android companion"
        fingerprint = "SHA256:" + hashlib.sha256(public_key_data).hexdigest()[:24]
        with self._tx() as db:
            pairing = db.execute("SELECT challenge,expires_at,used_at,status FROM device_pairings WHERE id=?", (pairing_id,)).fetchone()
            if pairing is None or pairing["used_at"] is not None or pairing["status"] != "INVITED" or pairing["expires_at"] <= now.isoformat():
                raise ValueError("pairing challenge is missing, expired, or already used")
            try:
                public_key.verify(signature, pairing["challenge"].encode("utf-8"), ec.ECDSA(hashes.SHA256()))
            except Exception as exc:
                raise ValueError("pairing signature verification failed") from exc
            cur = db.execute(
                "UPDATE device_pairings SET used_at=?,status='PENDING_CONFIRMATION',device_name=?,public_key_b64=?,device_fingerprint=?,requested_at=? WHERE id=? AND status='INVITED' AND used_at IS NULL AND expires_at>?",
                (now.isoformat(), safe_name, public_key_b64, fingerprint, now.isoformat(), pairing_id, now.isoformat()),
            )
            if cur.rowcount != 1:
                raise ValueError("pairing invitation was already used or expired")
        return {"status": "PENDING_CONFIRMATION", "invitation_id": pairing_id, "expires_at": pairing["expires_at"]}

    def pending_device_pairings(self, limit: int = 50) -> list[dict[str, str]]:
        """Return only safe review fields for the local Mac confirmation UI."""
        now = utc_now()
        with self._tx() as db:
            expired = db.execute(
                "UPDATE device_pairings SET status='EXPIRED' WHERE status IN ('INVITED','PENDING_CONFIRMATION') AND expires_at<=?",
                (now,),
            )
            rows = db.execute(
                "SELECT id,status,device_name,device_fingerprint,requested_at,expires_at FROM device_pairings WHERE status='PENDING_CONFIRMATION' ORDER BY requested_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_device_pairing(self, pairing_id: str) -> str:
        """Cancel an unclaimed invitation or pairing request without touching paired devices."""
        now = datetime.now(timezone.utc)
        with self._tx() as db:
            row = db.execute(
                "SELECT status,expires_at,claim_expires_at,claim_used_at FROM device_pairings WHERE id=?",
                (pairing_id,),
            ).fetchone()
            if row is None:
                raise KeyError(pairing_id)
            status = row["status"]
            if status == "CANCELLED":
                return status
            if status in {"INVITED", "PENDING_CONFIRMATION"} and row["expires_at"] <= now.isoformat():
                db.execute(
                    "UPDATE device_pairings SET status='EXPIRED' WHERE id=? AND status=?",
                    (pairing_id, status),
                )
                return "EXPIRED"
            if status == "APPROVED" and (
                row["claim_used_at"] is not None
                or not row["claim_expires_at"]
                or row["claim_expires_at"] <= now.isoformat()
            ):
                db.execute(
                    "UPDATE device_pairings SET status='EXPIRED' WHERE id=? AND status='APPROVED' AND claim_used_at IS NULL",
                    (pairing_id,),
                )
                return "EXPIRED"
            if status == "TOKEN_ISSUED" or row["claim_used_at"] is not None:
                raise RuntimeError("pairing was already claimed; revoke the device instead")
            if status not in {"INVITED", "PENDING_CONFIRMATION", "APPROVED"}:
                return status
            cur = db.execute(
                "UPDATE device_pairings SET status='CANCELLED',claim_challenge=NULL,claim_expires_at=NULL "
                "WHERE id=? AND status=? AND claim_used_at IS NULL",
                (pairing_id, status),
            )
            if cur.rowcount != 1:
                raise RuntimeError("pairing changed before cancellation was recorded")
        return "CANCELLED"

    def decide_device_pairing(self, pairing_id: str, decision: str) -> str:
        """Record the local Mac user's decision; approval only creates a claim challenge."""
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        now = datetime.now(timezone.utc)
        status = "APPROVED" if decision == "approve" else "REJECTED"
        claim_challenge = None
        claim_expires = None
        if decision == "approve":
            claim_challenge = f"openjarvis-device-claim:v1:{pairing_id}:{secrets.token_urlsafe(32)}"
            claim_expires = (now + timedelta(minutes=5)).isoformat()
        with self._tx() as db:
            row = db.execute("SELECT status,expires_at FROM device_pairings WHERE id=?", (pairing_id,)).fetchone()
            if row is None:
                raise KeyError(pairing_id)
            if row["status"] != "PENDING_CONFIRMATION":
                raise RuntimeError("device request is not awaiting local confirmation")
            if row["expires_at"] <= now.isoformat():
                db.execute("UPDATE device_pairings SET status='EXPIRED' WHERE id=? AND status='PENDING_CONFIRMATION'", (pairing_id,))
                return "EXPIRED"
            cur = db.execute(
                "UPDATE device_pairings SET status=?,confirmed_at=?,claim_challenge=?,claim_expires_at=? WHERE id=? AND status='PENDING_CONFIRMATION' AND expires_at>?",
                (status, now.isoformat(), claim_challenge, claim_expires, pairing_id, now.isoformat()),
            )
            if cur.rowcount != 1:
                raise RuntimeError("device request changed before the decision was recorded")
        return status

    def device_pairing_status(self, pairing_id: str) -> dict[str, str]:
        """Expose non-secret pairing state; an approved state includes a claim challenge, never a token."""
        now = datetime.now(timezone.utc)
        with self._tx() as db:
            row = db.execute(
                "SELECT status,expires_at,claim_challenge,claim_expires_at FROM device_pairings WHERE id=?",
                (pairing_id,),
            ).fetchone()
            if row is None:
                raise KeyError(pairing_id)
            status = row["status"]
            if status in {"INVITED", "PENDING_CONFIRMATION"} and row["expires_at"] <= now.isoformat():
                db.execute("UPDATE device_pairings SET status='EXPIRED' WHERE id=? AND status=?", (pairing_id, status))
                status = "EXPIRED"
            if status == "APPROVED" and row["claim_expires_at"] <= now.isoformat():
                db.execute("UPDATE device_pairings SET status='EXPIRED' WHERE id=? AND status='APPROVED'", (pairing_id,))
                status = "EXPIRED"
        result = {"invitation_id": pairing_id, "status": status}
        if status == "APPROVED":
            result["claim_challenge"] = row["claim_challenge"]
            result["claim_expires_at"] = row["claim_expires_at"]
        return result

    def claim_device_token(self, pairing_id: str, signature_b64: str, *, ttl_days: int = 90) -> dict[str, Any]:
        """Verify the phone's post-confirmation proof and issue one revocable token."""
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except Exception as exc:
            raise ValueError("invalid claim signature encoding") from exc
        now = datetime.now(timezone.utc)
        device_id = uuid.uuid4().hex
        token = "ojd_" + secrets.token_urlsafe(40)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires = (now + timedelta(days=max(1, min(ttl_days, 365)))).isoformat()
        with self._tx() as db:
            row = db.execute(
                "SELECT status,public_key_b64,device_name,claim_challenge,claim_expires_at,claim_used_at FROM device_pairings WHERE id=?",
                (pairing_id,),
            ).fetchone()
            if row is None or row["status"] != "APPROVED" or row["claim_used_at"] is not None or not row["claim_challenge"] or row["claim_expires_at"] <= now.isoformat():
                raise ValueError("pairing is not approved, claim is expired, or token was already issued")
            try:
                public_key_data = base64.b64decode(row["public_key_b64"], validate=True)
                public_key = serialization.load_der_public_key(public_key_data)
                if not isinstance(public_key, ec.EllipticCurvePublicKey) or public_key.curve.name != "secp256r1":
                    raise ValueError("expected a P-256 ECDSA public key")
                public_key.verify(signature, row["claim_challenge"].encode("utf-8"), ec.ECDSA(hashes.SHA256()))
            except Exception as exc:
                raise ValueError("claim signature verification failed") from exc
            cur = db.execute(
                "UPDATE device_pairings SET status='TOKEN_ISSUED',claim_used_at=?,device_id=? WHERE id=? AND status='APPROVED' AND claim_used_at IS NULL AND claim_expires_at>?",
                (now.isoformat(), device_id, pairing_id, now.isoformat()),
            )
            if cur.rowcount != 1:
                raise ValueError("pairing claim was already consumed")
            db.execute(
                "INSERT INTO paired_devices(id,public_key_b64,token_hash,created_at,expires_at,device_name) VALUES(?,?,?,?,?,?)",
                (device_id, row["public_key_b64"], token_hash, now.isoformat(), expires, row["device_name"] or "Android companion"),
            )
        return {"device_id": device_id, "access_token": token, "expires_at": expires, "expires_in_days": max(1, min(ttl_days, 365))}

    def revoke_device_by_pairing(self, pairing_id: str) -> bool:
        """Revoke an issued token associated with a pairing; useful for local UI recovery."""
        now = utc_now()
        with self._tx() as db:
            row = db.execute("SELECT device_id FROM device_pairings WHERE id=?", (pairing_id,)).fetchone()
            if row is None or not row["device_id"]:
                return False
            cur = db.execute("UPDATE paired_devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now, row["device_id"]))
            return cur.rowcount == 1

    def authenticate_device_token(self, token: str) -> str | None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = utc_now()
        with self._lock:
            row = self._db.execute("SELECT id FROM paired_devices WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?", (token_hash, now)).fetchone()
        return row["id"] if row else None

    def revoke_device(self, device_id: str) -> bool:
        now = utc_now()
        with self._tx() as db:
            cur = db.execute("UPDATE paired_devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now, device_id))
            return cur.rowcount == 1

    def resume_approved_task(self, task_id: str, approval_id: str) -> bool:
        """Explicitly resume a task after restart without replaying the old call."""
        now = utc_now()
        with self._tx() as db:
            task = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            approval = db.execute("SELECT task_id,status,expires_at FROM operation_approvals WHERE id=?", (approval_id,)).fetchone()
            if task is None or approval is None or task["state"] != "PAUSED" or approval["task_id"] != task_id:
                return False
            if approval["status"] != "APPROVED" or approval["expires_at"] <= now:
                return False
            db.execute("UPDATE tasks SET state='WAITING_APPROVAL',updated_at=?,last_error=NULL WHERE id=? AND state='PAUSED'", (now, task_id))
            self._event(db, task_id, "task.explicit_resume", {"approval_id": approval_id, "note": "new user turn required; no action replayed"})
            self._event(db, task_id, "task.state", {"from": "PAUSED", "to": "WAITING_APPROVAL", "step": "explicit_resume"})
            return True

    def recover_after_restart(self) -> None:
        """Pause in-flight tasks; leave approvals pending and never execute them."""
        now=utc_now()
        with self._tx() as db:
            rows=db.execute("SELECT id,state FROM tasks WHERE state IN ('PLANNING','RUNNING','VERIFYING','WAITING_APPROVAL')").fetchall()
            for row in rows:
                db.execute("UPDATE tasks SET state='PAUSED',updated_at=?,last_error='service restart; explicit resume required' WHERE id=?",(now,row["id"]))
                self._event(db,row["id"],"task.recovered_paused",{"previous_state":row["state"]})
            db.execute("UPDATE operation_approvals SET status='EXPIRED' WHERE status='PENDING' AND expires_at<=?",(now,))

    def close(self) -> None:
        with self._lock: self._db.close()


__all__ = ["SupervisorStore", "TaskContext", "bind_task", "current_task_context", "get_supervisor_store", "operation_target", "requires_approval", "operation_digest", "canonical_json", "TASK_STATES"]
