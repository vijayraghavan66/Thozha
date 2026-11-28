"""SQLite-backed owner-scoped goals, leases, step runs, and append-only events."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from openjarvis.goals.models import (
    GoalActionKind,
    GoalApprovalBinding,
    GoalCreate,
    GoalConflict,
    GoalDisposition,
    GoalError,
    GoalEvent,
    GoalEventType,
    GoalLease,
    GoalNotFound,
    GoalPolicy,
    ProviderPollDecision,
    GoalSnapshot,
    GoalStatus,
    GoalStepPlan,
    GoalStepResult,
    GoalValidationError,
    GoalWakeReason,
    LeaseLost,
    SupervisorApprovalOutcome,
    datetime_text,
    normalize_datetime,
    sanitize_public_summary,
    utc_now,
    validate_evidence,
)


_TERMINAL = {GoalStatus.COMPLETED.value, GoalStatus.FAILED.value, GoalStatus.CANCELLED.value, GoalStatus.EXPIRED.value}
_ACTIVE = {GoalStatus.ACTIVE.value, GoalStatus.WAITING_UNTIL.value}
_WAKE_REASONS = {value.value for value in GoalWakeReason}
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


class _CommitAndRaise(Exception):
    """Commit a terminal state transition before returning a lease error."""

    def __init__(self, error: Exception):
        super().__init__(str(error))
        self.error = error


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _parse_json(value: str | None, *, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _digest_create_goal(goal: GoalCreate) -> str:
    payload = {
        "title": goal.title,
        "objective": goal.objective,
        "deadline_at": datetime_text(goal.deadline_at),
        "next_wake_at": datetime_text(goal.next_wake_at) if goal.next_wake_at else None,
        "preferences": goal.preferences,
        "checkpoint": goal.checkpoint,
        "policy": {
            "max_attempts": goal.policy.max_attempts,
            "retry_base_seconds": goal.policy.retry_base_seconds,
            "retry_max_seconds": goal.policy.retry_max_seconds,
            "max_live_step_seconds": goal.policy.max_live_step_seconds,
        },
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


class GoalStore:
    """Durable goal state with owner checks on every read and mutation.

    A scheduler lease is short-lived and only claims one finite step. Event rows
    are append-only so clients can recover progress from a monotonic sequence.
    """

    def __init__(self, db_path: str | Path):
        requested = Path(db_path).expanduser()
        if not requested.is_absolute():
            raise GoalValidationError("goal database path must be absolute")
        current = Path(requested.anchor)
        for part in requested.parts[1:]:
            current = current / part
            try:
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    raise GoalValidationError("goal database path cannot include symlinks")
            except FileNotFoundError:
                pass
        self.db_path = str(requested)
        Path(self.db_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = os.stat(Path(self.db_path).parent, follow_symlinks=False)
        if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise GoalValidationError("goal database directory must be private and owner-controlled")
        try:
            file_info = os.lstat(self.db_path)
        except FileNotFoundError:
            file_info = None
        if file_info is not None and (
            not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.geteuid() or file_info.st_nlink != 1
        ):
            raise GoalValidationError("goal database must be a private owner-owned regular file")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._create_schema()
        os.chmod(self.db_path, 0o600)
        db_info = os.stat(self.db_path, follow_symlinks=False)
        if db_info.st_uid != os.geteuid() or stat.S_IMODE(db_info.st_mode) & 0o077:
            self._db.close()
            raise GoalValidationError("goal database permissions could not be secured")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _create_schema(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS goals(
                  goal_id TEXT PRIMARY KEY,
                  owner_actor TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  title TEXT NOT NULL,
                  summary TEXT NOT NULL DEFAULT '',
                  objective TEXT NOT NULL,
                  preferences_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  deadline_at TEXT NOT NULL,
                  next_wake_at TEXT,
                  checkpoint_json TEXT NOT NULL,
                  checkpoint_version INTEGER NOT NULL DEFAULT 0,
                  step_index INTEGER NOT NULL DEFAULT 0,
                  attempts_used INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL,
                  retry_base_seconds INTEGER NOT NULL,
                  retry_max_seconds INTEGER NOT NULL,
                  max_live_step_seconds INTEGER NOT NULL,
                  lease_owner TEXT,
                  lease_token TEXT,
                  lease_expires_at TEXT,
                  current_run_key TEXT,
                  last_error_code TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT,
                  UNIQUE(owner_actor,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_goals_due ON goals(status,next_wake_at,deadline_at);
                CREATE INDEX IF NOT EXISTS idx_goals_owner ON goals(owner_actor,updated_at);
                CREATE TABLE IF NOT EXISTS goal_events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  goal_id TEXT NOT NULL REFERENCES goals(goal_id),
                  owner_actor TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  checkpoint_version INTEGER NOT NULL,
                  progress INTEGER,
                  evidence_json TEXT NOT NULL DEFAULT '{}',
                  dedupe_key TEXT,
                  UNIQUE(goal_id,dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS idx_goal_events_owner ON goal_events(owner_actor,goal_id,seq);
                CREATE TRIGGER IF NOT EXISTS goal_events_no_update
                  BEFORE UPDATE ON goal_events BEGIN SELECT RAISE(ABORT,'goal events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS goal_events_no_delete
                  BEFORE DELETE ON goal_events BEGIN SELECT RAISE(ABORT,'goal events are append-only'); END;
                CREATE TABLE IF NOT EXISTS goal_step_runs(
                  run_key TEXT PRIMARY KEY,
                  goal_id TEXT NOT NULL REFERENCES goals(goal_id),
                  step_index INTEGER NOT NULL,
                  checkpoint_version INTEGER NOT NULL,
                  plan_step_key TEXT NOT NULL,
                  action_kind TEXT NOT NULL,
                  action_digest TEXT,
                  supervisor_task_id TEXT,
                  approval_id TEXT,
                  approval_expires_at TEXT,
                  dispatch_started INTEGER NOT NULL DEFAULT 0,
                  dispatch_result TEXT,
                  description TEXT NOT NULL,
                  attempt INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  lease_token TEXT,
                  started_at TEXT,
                  finished_at TEXT,
                  duration_ms INTEGER,
                  error_code TEXT,
                  result_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(goal_id,step_index)
                );
                CREATE INDEX IF NOT EXISTS idx_goal_step_runs_goal ON goal_step_runs(goal_id,step_index);
                CREATE TABLE IF NOT EXISTS goal_approval_bindings(
                  approval_id TEXT PRIMARY KEY,
                  goal_id TEXT NOT NULL REFERENCES goals(goal_id),
                  owner_actor TEXT NOT NULL,
                  run_key TEXT NOT NULL REFERENCES goal_step_runs(run_key),
                  supervisor_task_id TEXT NOT NULL,
                  action_digest TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  resolved_at TEXT,
                  resolution_event_id TEXT,
                  UNIQUE(goal_id,run_key)
                );
                CREATE INDEX IF NOT EXISTS idx_goal_approval_owner ON goal_approval_bindings(owner_actor,goal_id,status);
                CREATE TABLE IF NOT EXISTS goal_provider_poll_state(
                  owner_actor TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  minimum_interval_seconds INTEGER NOT NULL,
                  next_allowed_at TEXT NOT NULL,
                  last_reserved_at TEXT NOT NULL,
                  last_read_at TEXT,
                  last_result TEXT,
                  policy_source TEXT NOT NULL DEFAULT 'local_policy',
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(owner_actor,provider)
                );
                """
            )
            step_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(goal_step_runs)")}
            step_migrations = {
                "action_digest": "TEXT",
                "supervisor_task_id": "TEXT",
                "approval_id": "TEXT",
                "approval_expires_at": "TEXT",
                "dispatch_started": "INTEGER NOT NULL DEFAULT 0",
                "dispatch_result": "TEXT",
            }
            for name, definition in step_migrations.items():
                if name not in step_columns:
                    self._db.execute(f"ALTER TABLE goal_step_runs ADD COLUMN {name} {definition}")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(goals)")}
            if "summary" not in columns:
                self._db.execute("ALTER TABLE goals ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            self._db.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except _CommitAndRaise as signal:
                self._db.commit()
                raise signal.error
            except Exception:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    @staticmethod
    def _snapshot(row: sqlite3.Row | dict[str, Any]) -> GoalSnapshot:
        data = dict(row)
        return GoalSnapshot(
            goal_id=str(data["goal_id"]), owner_actor=str(data["owner_actor"]),
            idempotency_key=str(data["idempotency_key"]), title=str(data["title"]), summary=str(data.get("summary") or ""),
            objective=str(data["objective"]), preferences=_parse_json(data.get("preferences_json"), default={}),
            status=GoalStatus(data["status"]), deadline_at=normalize_datetime(data["deadline_at"], "deadline_at"),
            next_wake_at=normalize_datetime(data["next_wake_at"], "next_wake_at") if data.get("next_wake_at") else None,
            checkpoint=_parse_json(data.get("checkpoint_json"), default={}),
            checkpoint_version=int(data["checkpoint_version"]), step_index=int(data["step_index"]),
            attempts_used=int(data["attempts_used"]), max_attempts=int(data["max_attempts"]),
            retry_base_seconds=int(data["retry_base_seconds"]), retry_max_seconds=int(data["retry_max_seconds"]),
            max_live_step_seconds=int(data["max_live_step_seconds"]),
            lease_expires_at=normalize_datetime(data["lease_expires_at"], "lease_expires_at") if data.get("lease_expires_at") else None,
            last_error_code=data.get("last_error_code"),
            created_at=normalize_datetime(data["created_at"], "created_at"),
            updated_at=normalize_datetime(data["updated_at"], "updated_at"),
        )

    @staticmethod
    def _append_event(
        db: sqlite3.Connection,
        *, goal_id: str, owner_actor: str, event_type: GoalEventType,
        at: datetime, summary: str, checkpoint_version: int,
        progress: int | None = None, evidence: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> int:
        summary = sanitize_public_summary(summary or event_type.value.replace(".", " ").capitalize())
        if progress is not None and not 0 <= progress <= 100:
            raise GoalValidationError("progress must be between 0 and 100")
        safe_evidence = validate_evidence(evidence)
        cursor = db.execute(
            "INSERT OR IGNORE INTO goal_events(goal_id,owner_actor,event_type,created_at,summary,checkpoint_version,progress,evidence_json,dedupe_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (goal_id, owner_actor, event_type.value, datetime_text(at), summary, checkpoint_version, progress, _json(safe_evidence), dedupe_key),
        )
        db.execute("UPDATE goals SET summary=?,updated_at=? WHERE goal_id=?", (summary, datetime_text(at), goal_id))
        return int(cursor.lastrowid or 0)

    def _owned_row(self, db: sqlite3.Connection, goal_id: str, owner_actor: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM goals WHERE goal_id=? AND owner_actor=?", (goal_id, owner_actor)).fetchone()
        if row is None:
            raise GoalNotFound("goal not found")
        return row

    def create_goal(self, request: GoalCreate, *, now: datetime | None = None) -> tuple[GoalSnapshot, bool]:
        request = request.validated()
        now = normalize_datetime(now or utc_now(), "now")
        deadline = normalize_datetime(request.deadline_at, "deadline_at")
        next_wake = normalize_datetime(request.next_wake_at, "next_wake_at") if request.next_wake_at else now
        if deadline <= now:
            raise GoalValidationError("deadline_at must be in the future")
        digest_payload = {
            "title": request.title, "objective": request.objective,
            "deadline_at": datetime_text(deadline), "preferences": request.preferences,
            "next_wake_at": datetime_text(next_wake),
            "checkpoint": request.checkpoint,
            "policy": {
                "max_attempts": request.policy.max_attempts,
                "retry_base_seconds": request.policy.retry_base_seconds,
                "retry_max_seconds": request.policy.retry_max_seconds,
                "max_live_step_seconds": request.policy.max_live_step_seconds,
            },
        }
        digest = hashlib.sha256(_json(digest_payload).encode("utf-8")).hexdigest()
        with self._tx() as db:
            existing = db.execute(
                "SELECT * FROM goals WHERE owner_actor=? AND idempotency_key=?",
                (request.owner_actor, request.idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_digest"] != digest:
                    raise GoalConflict("idempotency key was already used for another goal")
                return self._snapshot(existing), False
            goal_id = uuid.uuid4().hex
            values = (
                goal_id, request.owner_actor, request.idempotency_key, digest,
                request.title, request.objective, _json(request.preferences), GoalStatus.ACTIVE.value,
                datetime_text(deadline), datetime_text(next_wake), _json(request.checkpoint),
                request.policy.max_attempts, request.policy.retry_base_seconds,
                request.policy.retry_max_seconds, request.policy.max_live_step_seconds,
                datetime_text(now), datetime_text(now),
            )
            db.execute(
                "INSERT INTO goals(goal_id,owner_actor,idempotency_key,request_digest,title,objective,preferences_json,status,deadline_at,next_wake_at,checkpoint_json,max_attempts,retry_base_seconds,retry_max_seconds,max_live_step_seconds,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            self._append_event(
                db, goal_id=goal_id, owner_actor=request.owner_actor,
                event_type=GoalEventType.CREATED, at=now,
                summary=f"Goal created: {request.title}", checkpoint_version=0, progress=0,
            )
            row = db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
            return self._snapshot(row), True

    def get_goal(self, goal_id: str, owner_actor: str) -> GoalSnapshot:
        if not re.fullmatch(r"[0-9a-f]{32}", goal_id or ""):
            raise GoalNotFound("goal not found")
        with self._lock:
            row = self._db.execute("SELECT * FROM goals WHERE goal_id=? AND owner_actor=?", (goal_id, owner_actor)).fetchone()
        if row is None:
            raise GoalNotFound("goal not found")
        return self._snapshot(row)

    def list_goals(self, owner_actor: str, *, include_terminal: bool = True, limit: int = 100) -> list[GoalSnapshot]:
        limit = max(1, min(int(limit), 200))
        query = "SELECT * FROM goals WHERE owner_actor=?"
        params: list[Any] = [owner_actor]
        if not include_terminal:
            query += " AND status NOT IN ('completed','failed','cancelled','expired')"
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [self._snapshot(row) for row in rows]

    def events_after(self, goal_id: str, owner_actor: str, after_seq: int = 0, limit: int = 100) -> list[GoalEvent]:
        if after_seq < 0 or not 1 <= limit <= 200:
            raise GoalValidationError("event cursor or limit is invalid")
        with self._lock:
            self._owned_row(self._db, goal_id, owner_actor)
            rows = self._db.execute(
                "SELECT * FROM goal_events WHERE goal_id=? AND owner_actor=? AND seq>? ORDER BY seq LIMIT ?",
                (goal_id, owner_actor, after_seq, limit),
            ).fetchall()
        return [GoalEvent(
            seq=int(row["seq"]), goal_id=str(row["goal_id"]),
            event_type=GoalEventType(row["event_type"]),
            created_at=normalize_datetime(row["created_at"], "created_at"),
            summary=str(row["summary"]), checkpoint_version=int(row["checkpoint_version"]),
            progress=int(row["progress"]) if row["progress"] is not None else None,
            evidence=_parse_json(row["evidence_json"], default={}),
        ) for row in rows]

    def claim_due(self, worker_id: str, *, now: datetime | None = None, lease_seconds: int | None = None, limit: int = 1) -> list[GoalLease]:
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", worker_id):
            raise GoalValidationError("worker_id is invalid")
        if (lease_seconds is not None and not 1 <= lease_seconds <= 600) or not 1 <= limit <= 50:
            raise GoalValidationError("lease_seconds or claim limit is out of range")
        now = normalize_datetime(now or utc_now(), "now")
        now_text = datetime_text(now)
        leases: list[GoalLease] = []
        with self._tx() as db:
            # Recover expired leases before the due scan. A possibly-dispatched
            # supervisor action stops for owner review; safe reads reuse their
            # persisted idempotency key on the next claim.
            stale_rows = db.execute(
                "SELECT * FROM goals WHERE lease_token IS NOT NULL AND lease_expires_at<=?",
                (now_text,),
            ).fetchall()
            for stale in stale_rows:
                run = db.execute("SELECT * FROM goal_step_runs WHERE run_key=?", (stale["current_run_key"],)).fetchone() if stale["current_run_key"] else None
                token = str(stale["lease_token"])
                if (run is not None and run["status"] == "running"
                        and run["action_kind"] == GoalActionKind.SUPERVISOR_ACTION.value
                        and bool(run["dispatch_started"])):
                    db.execute("UPDATE goal_step_runs SET status='needs_review',finished_at=? WHERE run_key=?", (now_text, run["run_key"]))
                    db.execute(
                        "UPDATE goals SET status=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error_code=?,next_wake_at=NULL,updated_at=? WHERE goal_id=? AND lease_token=?",
                        (GoalStatus.NEEDS_REVIEW.value, "UNKNOWN_SUPERVISOR_ACTION_RESULT", now_text, stale["goal_id"], token),
                    )
                    self._append_event(db, goal_id=stale["goal_id"], owner_actor=stale["owner_actor"],
                        event_type=GoalEventType.NEEDS_REVIEW, at=now,
                        summary="A supervisor action may have run before its result was lost. Review the state before continuing.",
                        checkpoint_version=int(stale["checkpoint_version"]))
                else:
                    if run is not None and run["status"] == "running":
                        db.execute("UPDATE goal_step_runs SET status='retryable',finished_at=? WHERE run_key=?", (now_text, run["run_key"]))
                    db.execute(
                        "UPDATE goals SET status=?,next_wake_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE goal_id=? AND lease_token=?",
                        (GoalStatus.ACTIVE.value, now_text, now_text, stale["goal_id"], token),
                    )
                    self._append_event(db, goal_id=stale["goal_id"], owner_actor=stale["owner_actor"],
                        event_type=GoalEventType.LEASE_RECOVERED, at=now,
                        summary=(
                            "A read-only step was interrupted and can resume with the same idempotency key."
                            if run is None or run["action_kind"] == GoalActionKind.READ_ONLY.value
                            else "No supervisor action dispatch began; the bounded step can resume with the same idempotency key."
                        ),
                        checkpoint_version=int(stale["checkpoint_version"]),
                        evidence={"action_id": str(run["run_key"]) if run else "no_step_started"})

            overdue = db.execute(
                "SELECT * FROM goals WHERE status IN ('active','waiting_until') AND lease_token IS NULL AND deadline_at<=?",
                (now_text,),
            ).fetchall()
            for goal in overdue:
                db.execute("UPDATE goals SET status=?,next_wake_at=NULL,updated_at=?,completed_at=? WHERE goal_id=?",
                    (GoalStatus.EXPIRED.value, now_text, now_text, goal["goal_id"]))
                self._append_event(db, goal_id=goal["goal_id"], owner_actor=goal["owner_actor"],
                    event_type=GoalEventType.EXPIRED, at=now, summary="Goal deadline expired before the next step ran.",
                    checkpoint_version=int(goal["checkpoint_version"]))

            due = db.execute(
                "SELECT * FROM goals WHERE status IN ('active','waiting_until') AND lease_token IS NULL AND next_wake_at<=? AND deadline_at>? ORDER BY next_wake_at,created_at LIMIT ?",
                (now_text, now_text, limit),
            ).fetchall()
            for goal in due:
                if int(goal["attempts_used"]) >= int(goal["max_attempts"]):
                    db.execute("UPDATE goals SET status=?,next_wake_at=NULL,updated_at=?,completed_at=?,last_error_code=? WHERE goal_id=?",
                        (GoalStatus.FAILED.value, now_text, now_text, "ATTEMPT_BUDGET_EXHAUSTED", goal["goal_id"]))
                    self._append_event(db, goal_id=goal["goal_id"], owner_actor=goal["owner_actor"],
                        event_type=GoalEventType.FAILED, at=now, summary="Goal stopped after reaching its retry budget.",
                        checkpoint_version=int(goal["checkpoint_version"]))
                    continue
                token = secrets.token_hex(24)
                derived_lease_seconds = int(goal["max_live_step_seconds"]) + 15
                current_lease_seconds = max(derived_lease_seconds, lease_seconds or 0)
                if current_lease_seconds > 600:
                    raise GoalValidationError("step lease duration exceeds the supported limit")
                lease_until = now + timedelta(seconds=current_lease_seconds)
                updated = db.execute(
                    "UPDATE goals SET status=?,lease_owner=?,lease_token=?,lease_expires_at=?,updated_at=? WHERE goal_id=? AND lease_token IS NULL AND status IN ('active','waiting_until')",
                    (GoalStatus.ACTIVE.value, worker_id, token, datetime_text(lease_until), now_text, goal["goal_id"]),
                )
                if updated.rowcount != 1:
                    continue
                refreshed = db.execute("SELECT * FROM goals WHERE goal_id=?", (goal["goal_id"],)).fetchone()
                self._append_event(db, goal_id=goal["goal_id"], owner_actor=goal["owner_actor"],
                    event_type=GoalEventType.CLAIMED, at=now, summary="A finite goal step was claimed.",
                    checkpoint_version=int(goal["checkpoint_version"]))
                leases.append(GoalLease(self._snapshot(refreshed), worker_id, token, lease_until))
        return leases

    def prepare_step(self, lease: GoalLease, plan: GoalStepPlan, *, now: datetime | None = None) -> str:
        now = normalize_datetime(now or utc_now(), "now")
        plan = plan.validated(lease.goal.policy if hasattr(lease.goal, "policy") else GoalPolicy(
            max_attempts=lease.goal.max_attempts, retry_base_seconds=lease.goal.retry_base_seconds,
            retry_max_seconds=lease.goal.retry_max_seconds, max_live_step_seconds=lease.goal.max_live_step_seconds,
        ))
        run_key = hashlib.sha256(
            f"goal:{lease.goal.goal_id}:checkpoint:{lease.goal.checkpoint_version}:step:{lease.goal.step_index}".encode()
        ).hexdigest()
        with self._tx() as db:
            goal = self._owned_row(db, lease.goal.goal_id, lease.goal.owner_actor)
            self._assert_lease(db, goal, lease, now)
            existing = db.execute("SELECT * FROM goal_step_runs WHERE run_key=?", (run_key,)).fetchone()
            if existing is None:
                attempt = 1
                db.execute(
                    "INSERT INTO goal_step_runs(run_key,goal_id,step_index,checkpoint_version,plan_step_key,action_kind,action_digest,description,attempt,status,lease_token,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_key, goal["goal_id"], goal["step_index"], goal["checkpoint_version"], plan.step_key,
                     plan.action_kind.value, plan.action_digest, plan.description, attempt, "running", lease.token, datetime_text(now)),
                )
            else:
                if (existing["plan_step_key"] != plan.step_key
                        or existing["action_kind"] != plan.action_kind.value
                        or existing["action_digest"] != plan.action_digest):
                    raise GoalConflict("recovered step plan does not match its durable idempotency record")
                # A consumed approval makes the original exact action eligible
                # for one dispatch. Keep the same run key and require the same
                # plan/digest so restart recovery cannot turn it into a new action.
                approved_resume = (
                    existing["status"] == "approved"
                    and existing["action_kind"] == GoalActionKind.SUPERVISOR_ACTION.value
                    and bool(existing["approval_id"])
                    and db.execute(
                        "SELECT 1 FROM goal_approval_bindings WHERE approval_id=? AND goal_id=? AND owner_actor=? AND status='consumed' AND action_digest=?",
                        (existing["approval_id"], goal["goal_id"], goal["owner_actor"], plan.action_digest),
                    ).fetchone() is not None
                )
                if existing["status"] != "retryable" and not approved_resume:
                    raise GoalConflict("step is already running or terminal")
                attempt = int(existing["attempt"]) + (0 if approved_resume else 1)
                db.execute(
                    "UPDATE goal_step_runs SET attempt=?,status='running',lease_token=?,started_at=?,finished_at=NULL,duration_ms=NULL WHERE run_key=?",
                    (attempt, lease.token, datetime_text(now), run_key),
                )
            if int(goal["attempts_used"]) >= int(goal["max_attempts"]):
                raise GoalConflict("goal retry budget is exhausted")
            db.execute("UPDATE goals SET attempts_used=attempts_used+1,current_run_key=?,updated_at=? WHERE goal_id=?",
                (run_key, datetime_text(now), goal["goal_id"]))
            self._append_event(db, goal_id=goal["goal_id"], owner_actor=goal["owner_actor"],
                event_type=GoalEventType.STEP_STARTED, at=now, summary=plan.description,
                checkpoint_version=int(goal["checkpoint_version"]),
                evidence={"step_id": plan.step_key, "action_id": run_key, "record_count": attempt})
        return run_key

    @staticmethod
    def _assert_lease(db: sqlite3.Connection, goal: sqlite3.Row, lease: GoalLease, now: datetime) -> None:
        if goal["status"] != GoalStatus.ACTIVE.value or goal["lease_token"] != lease.token or goal["lease_owner"] != lease.worker_id:
            raise LeaseLost("goal lease is no longer current")
        if now >= normalize_datetime(goal["deadline_at"], "deadline_at"):
            run = db.execute("SELECT * FROM goal_step_runs WHERE run_key=?", (goal["current_run_key"],)).fetchone() if goal["current_run_key"] else None
            uncertain = bool(run is not None and run["dispatch_started"] and run["dispatch_result"] not in {"completed", "failed"})
            status = GoalStatus.NEEDS_REVIEW if uncertain else GoalStatus.EXPIRED
            error_code = "SUPERVISOR_ACTION_RESULT_UNKNOWN" if uncertain else "DEADLINE_EXCEEDED"
            if run is not None and run["status"] in {"running", "action_running"}:
                db.execute("UPDATE goal_step_runs SET status=?,finished_at=?,error_code=? WHERE run_key=?",
                    ("needs_review" if uncertain else "expired", datetime_text(now), error_code, run["run_key"]))
            db.execute(
                "UPDATE goals SET status=?,next_wake_at=NULL,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,current_run_key=NULL,last_error_code=?,updated_at=?,completed_at=? WHERE goal_id=?",
                (status.value, error_code, datetime_text(now), None if uncertain else datetime_text(now), goal["goal_id"]),
            )
            GoalStore._append_event(
                db, goal_id=goal["goal_id"], owner_actor=goal["owner_actor"],
                event_type=GoalEventType.NEEDS_REVIEW if uncertain else GoalEventType.EXPIRED,
                at=now,
                summary=("Goal deadline elapsed with an unknown supervisor action result; review before continuing."
                         if uncertain else "Goal deadline expired before the step could finish."),
                checkpoint_version=int(goal["checkpoint_version"]),
            )
            raise _CommitAndRaise(LeaseLost("goal deadline has expired"))
        if not goal["lease_expires_at"] or normalize_datetime(goal["lease_expires_at"], "lease_expires_at") <= now:
            raise LeaseLost("goal lease has expired")

    def finish_step(
        self,
        lease: GoalLease,
        plan: GoalStepPlan,
        run_key: str,
        result: GoalStepResult,
        *,
        now: datetime | None = None,
        duration_ms: int = 0,
        retry_delay_seconds: int | None = None,
    ) -> GoalSnapshot:
        now = normalize_datetime(now or utc_now(), "now")
        plan = plan.validated(GoalPolicy(
            max_attempts=lease.goal.max_attempts, retry_base_seconds=lease.goal.retry_base_seconds,
            retry_max_seconds=lease.goal.retry_max_seconds, max_live_step_seconds=lease.goal.max_live_step_seconds,
        ))
        result = result.validated(plan, now=now)
        if not isinstance(duration_ms, int) or not 0 <= duration_ms <= 600_000:
            raise GoalValidationError("duration_ms is invalid")
        with self._tx() as db:
            goal = self._owned_row(db, lease.goal.goal_id, lease.goal.owner_actor)
            self._assert_lease(db, goal, lease, now)
            run = db.execute("SELECT * FROM goal_step_runs WHERE run_key=? AND goal_id=?", (run_key, goal["goal_id"])).fetchone()
            if run is None or run["status"] not in {"running", "action_running"} or run["lease_token"] != lease.token or goal["current_run_key"] != run_key:
                raise LeaseLost("goal step run is no longer current")
            if result.disposition == GoalDisposition.WAITING_APPROVAL:
                if plan.action_kind != GoalActionKind.SUPERVISOR_ACTION or not plan.action_digest:
                    raise GoalValidationError("approval must bind a supervisor action digest")
                if result.approval_expires_at is None or result.approval_expires_at > normalize_datetime(goal["deadline_at"], "deadline_at"):
                    raise GoalValidationError("approval expiry cannot exceed the goal deadline")
                prior = db.execute("SELECT * FROM goal_approval_bindings WHERE approval_id=?", (result.approval_id,)).fetchone()
                if prior is not None:
                    raise GoalConflict("supervisor approval is already bound to a goal action")
                db.execute(
                    "INSERT INTO goal_approval_bindings(approval_id,goal_id,owner_actor,run_key,supervisor_task_id,action_digest,expires_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (result.approval_id, goal["goal_id"], goal["owner_actor"], run_key, result.supervisor_task_id,
                     plan.action_digest, datetime_text(result.approval_expires_at), "pending", datetime_text(now)),
                )
                db.execute(
                    "UPDATE goal_step_runs SET approval_id=?,supervisor_task_id=?,action_digest=?,approval_expires_at=?,status='waiting_approval' WHERE run_key=?",
                    (result.approval_id, result.supervisor_task_id, plan.action_digest, datetime_text(result.approval_expires_at), run_key),
                )
            checkpoint = result.checkpoint if result.checkpoint is not None else _parse_json(goal["checkpoint_json"], default={})
            checkpoint_changed = _json(checkpoint) != str(goal["checkpoint_json"])
            if result.disposition == GoalDisposition.FAILED and result.retryable and checkpoint_changed:
                raise GoalValidationError("a retryable step cannot advance its checkpoint")
            checkpoint_version = int(goal["checkpoint_version"]) + (1 if checkpoint_changed else 0)
            if plan.action_kind == GoalActionKind.SUPERVISOR_ACTION and result.disposition == GoalDisposition.COMPLETED:
                if not bool(run["dispatch_started"]) or run["dispatch_result"] != "completed":
                    raise GoalConflict("supervisor action cannot complete without a confirmed dispatch result")
            status = GoalStatus.ACTIVE
            next_wake: datetime | None = now
            completed_at: str | None = None
            error_code = result.error_code
            run_status = "finished"
            event_type = GoalEventType.STEP_FINISHED
            event_summary = result.summary or plan.description

            if result.disposition == GoalDisposition.CONTINUE:
                status = GoalStatus.ACTIVE
                next_wake = result.next_wake_at or now
            elif result.disposition == GoalDisposition.WAITING_UNTIL:
                status = GoalStatus.WAITING_UNTIL
                next_wake = result.next_wake_at
            elif result.disposition == GoalDisposition.WAITING_APPROVAL:
                status = GoalStatus.WAITING_APPROVAL
                next_wake = None
                run_status = "waiting_approval"
            elif result.disposition == GoalDisposition.NEEDS_USER:
                status = GoalStatus.NEEDS_USER
                next_wake = None
                run_status = "needs_user"
            elif result.disposition == GoalDisposition.NEEDS_REVIEW:
                status = GoalStatus.NEEDS_REVIEW
                next_wake = None
                run_status = "needs_review"
                event_type = GoalEventType.NEEDS_REVIEW
            elif result.disposition == GoalDisposition.COMPLETED:
                status = GoalStatus.COMPLETED
                next_wake = None
                completed_at = datetime_text(now)
                run_status = "completed"
                event_type = GoalEventType.COMPLETED
            elif result.disposition == GoalDisposition.FAILED:
                error_code = result.error_code or "STEP_FAILED"
                if result.retryable and int(goal["attempts_used"]) < int(goal["max_attempts"]):
                    delay = retry_delay_seconds or int(goal["retry_base_seconds"])
                    if not 1 <= delay <= int(goal["retry_max_seconds"]):
                        raise GoalValidationError("retry delay exceeds the goal policy")
                    status = GoalStatus.ACTIVE
                    next_wake = now + timedelta(seconds=delay)
                    run_status = "retryable"
                    event_type = GoalEventType.STEP_RETRY_SCHEDULED
                    event_summary = "The finite step failed; retry was scheduled within the goal budget."
                else:
                    status = GoalStatus.FAILED
                    next_wake = None
                    completed_at = datetime_text(now)
                    run_status = "failed"
                    event_type = GoalEventType.FAILED

            if next_wake and next_wake >= normalize_datetime(goal["deadline_at"], "deadline_at"):
                status = GoalStatus.EXPIRED
                next_wake = None
                completed_at = datetime_text(now)
                error_code = "DEADLINE_EXCEEDED"
                run_status = "expired"
                event_type = GoalEventType.EXPIRED
                event_summary = "Goal deadline expires before the next scheduled step."

            evidence = dict(result.evidence)
            evidence.update({"step_id": plan.step_key, "action_id": run_key})
            if result.approval_id:
                evidence["approval_id"] = result.approval_id
            db.execute(
                "UPDATE goal_step_runs SET status=?,finished_at=?,duration_ms=?,error_code=?,result_json=? WHERE run_key=?",
                (run_status, datetime_text(now), duration_ms, error_code, _json({"disposition": result.disposition.value}), run_key),
            )
            db.execute(
                "UPDATE goals SET status=?,checkpoint_json=?,checkpoint_version=?,step_index=step_index+?,next_wake_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,current_run_key=NULL,last_error_code=?,updated_at=?,completed_at=? WHERE goal_id=?",
                (status.value, _json(checkpoint), checkpoint_version, 0 if run_status in {"retryable", "waiting_approval"} else 1,
                 datetime_text(next_wake) if next_wake else None, error_code,
                 datetime_text(now), completed_at, goal["goal_id"]),
            )
            self._append_event(
                db, goal_id=goal["goal_id"], owner_actor=goal["owner_actor"],
                event_type=event_type, at=now, summary=event_summary,
                checkpoint_version=checkpoint_version, progress=result.progress, evidence=evidence,
            )
            refreshed = db.execute("SELECT * FROM goals WHERE goal_id=?", (goal["goal_id"],)).fetchone()
            return self._snapshot(refreshed)

    def pending_approval_binding(
        self, goal_id: str, owner_actor: str, approval_id: str, *, now: datetime | None = None,
    ) -> GoalApprovalBinding:
        now = normalize_datetime(now or utc_now(), "now")
        if not re.fullmatch(r"[0-9a-f]{32}", approval_id or ""):
            raise GoalNotFound("goal approval not found")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM goal_approval_bindings WHERE goal_id=? AND owner_actor=? AND approval_id=?",
                (goal_id, owner_actor, approval_id),
            ).fetchone()
        if row is None or row["status"] != "pending":
            raise GoalNotFound("goal approval not found")
        expires_at = normalize_datetime(row["expires_at"], "approval expiry")
        return GoalApprovalBinding(
            goal_id=str(row["goal_id"]), owner_actor=str(row["owner_actor"]),
            run_key=str(row["run_key"]), approval_id=str(row["approval_id"]),
            supervisor_task_id=str(row["supervisor_task_id"]), action_digest=str(row["action_digest"]),
            expires_at=expires_at, status=str(row["status"]),
        )

    def consumed_approval_binding_for_action(
        self, goal_id: str, owner_actor: str, action_digest: str, *, now: datetime | None = None,
    ) -> GoalApprovalBinding | None:
        now = normalize_datetime(now or utc_now(), "now")
        if not re.fullmatch(r"[0-9a-f]{64}", action_digest or ""):
            raise GoalValidationError("action_digest is invalid")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM goal_approval_bindings WHERE goal_id=? AND owner_actor=? AND action_digest=? AND status='consumed' AND expires_at>? ORDER BY resolved_at DESC LIMIT 1",
                (goal_id, owner_actor, action_digest, datetime_text(now)),
            ).fetchone()
        if row is None:
            return None
        return GoalApprovalBinding(
            goal_id=str(row["goal_id"]), owner_actor=str(row["owner_actor"]),
            run_key=str(row["run_key"]), approval_id=str(row["approval_id"]),
            supervisor_task_id=str(row["supervisor_task_id"]), action_digest=str(row["action_digest"]),
            expires_at=normalize_datetime(row["expires_at"], "approval expiry"), status=str(row["status"]),
        )

    def resolve_approval_binding(
        self, goal_id: str, owner_actor: str, approval_id: str, *,
        supervisor_task_id: str, action_digest: str,
        outcome: SupervisorApprovalOutcome | str, event_id: str,
        now: datetime | None = None,
    ) -> tuple[GoalSnapshot, bool]:
        """Resolve only a stored, exact approval after the supervisor verifies it."""
        try:
            outcome = SupervisorApprovalOutcome(outcome)
        except (ValueError, TypeError) as exc:
            raise GoalValidationError("supervisor approval outcome is invalid") from exc
        if outcome == SupervisorApprovalOutcome.PENDING:
            raise GoalConflict("pending approval cannot wake a goal")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", event_id or ""):
            raise GoalValidationError("approval event id is invalid")
        now = normalize_datetime(now or utc_now(), "now")
        with self._tx() as db:
            goal = self._owned_row(db, goal_id, owner_actor)
            binding = db.execute(
                "SELECT * FROM goal_approval_bindings WHERE approval_id=? AND goal_id=? AND owner_actor=?",
                (approval_id, goal_id, owner_actor),
            ).fetchone()
            if binding is None:
                raise GoalNotFound("goal approval not found")
            if binding["resolution_event_id"] == event_id:
                return self._snapshot(goal), False
            if binding["status"] != "pending":
                raise GoalConflict("goal approval was already resolved")
            if binding["supervisor_task_id"] != supervisor_task_id or binding["action_digest"] != action_digest:
                raise GoalConflict("supervisor decision does not match the bound goal action")
            if GoalStatus(goal["status"]) != GoalStatus.WAITING_APPROVAL:
                raise GoalConflict("goal is not waiting for this approval")
            expiry = normalize_datetime(binding["expires_at"], "approval expiry")
            if now >= expiry or now >= normalize_datetime(goal["deadline_at"], "deadline_at"):
                outcome = SupervisorApprovalOutcome.EXPIRED
            if outcome == SupervisorApprovalOutcome.CONSUMED:
                status, next_wake, error, binding_status, event_type, summary, run_status = (
                    GoalStatus.ACTIVE, now, None, "consumed", GoalEventType.APPROVAL_RESOLVED,
                    "The approved action completed through the supervisor; the goal will revalidate before continuing.", "approved",
                )
            elif outcome == SupervisorApprovalOutcome.REJECTED:
                status, next_wake, error, binding_status, event_type, summary, run_status = (
                    GoalStatus.NEEDS_USER, None, "APPROVAL_REJECTED", "rejected", GoalEventType.APPROVAL_REJECTED,
                    "The proposed action was rejected; no provider action was dispatched.", "rejected",
                )
            else:
                status, next_wake, error, binding_status, event_type, summary, run_status = (
                    GoalStatus.ACTIVE, now, "APPROVAL_EXPIRED", "expired", GoalEventType.APPROVAL_EXPIRED,
                    "The approval expired; availability must be revalidated before asking again.", "expired",
                )
            if outcome == SupervisorApprovalOutcome.UNKNOWN:
                status, next_wake, error, binding_status, event_type, summary, run_status = (
                    GoalStatus.NEEDS_REVIEW, None, "SUPERVISOR_ACTION_RESULT_UNKNOWN", "unknown", GoalEventType.NEEDS_REVIEW,
                    "The supervisor action result is unknown; revalidate provider state before continuing.", "needs_review",
                )
            db.execute(
                "UPDATE goal_approval_bindings SET status=?,resolved_at=?,resolution_event_id=? WHERE approval_id=? AND status='pending'",
                (binding_status, datetime_text(now), event_id, approval_id),
            )
            db.execute(
                "UPDATE goal_step_runs SET status=?,finished_at=? WHERE run_key=? AND goal_id=?",
                (run_status, datetime_text(now), binding["run_key"], goal_id),
            )
            db.execute(
                "UPDATE goals SET status=?,next_wake_at=?,last_error_code=?,step_index=step_index+?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE goal_id=?",
                (status.value, datetime_text(next_wake) if next_wake else None, error,
                 0 if binding_status == "consumed" else 1, datetime_text(now), goal_id),
            )
            self._append_event(
                db, goal_id=goal_id, owner_actor=owner_actor, event_type=event_type,
                at=now, summary=summary, checkpoint_version=int(goal["checkpoint_version"]),
                evidence={"approval_id": approval_id, "digest": action_digest},
                dedupe_key=f"approval:{approval_id}:{event_id}",
            )
            refreshed = db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
            return self._snapshot(refreshed), True

    def mark_supervisor_action_dispatched(
        self, lease: GoalLease, plan: GoalStepPlan, run_key: str, *, approval_id: str,
        now: datetime | None = None,
    ) -> bool:
        now = normalize_datetime(now or utc_now(), "now")
        plan = plan.validated(GoalPolicy(
            max_attempts=lease.goal.max_attempts, retry_base_seconds=lease.goal.retry_base_seconds,
            retry_max_seconds=lease.goal.retry_max_seconds, max_live_step_seconds=lease.goal.max_live_step_seconds,
        ))
        if plan.action_kind != GoalActionKind.SUPERVISOR_ACTION or not plan.action_digest:
            raise GoalValidationError("only an approved supervisor action can be dispatched")
        with self._tx() as db:
            goal = self._owned_row(db, lease.goal.goal_id, lease.goal.owner_actor)
            self._assert_lease(db, goal, lease, now)
            run = db.execute("SELECT * FROM goal_step_runs WHERE run_key=? AND goal_id=?", (run_key, goal["goal_id"])).fetchone()
            if (run is None or run["status"] != "running" or run["lease_token"] != lease.token
                    or run["action_digest"] != plan.action_digest or run["approval_id"] != approval_id
                    or not run["supervisor_task_id"]):
                raise LeaseLost("supervisor action step is no longer current")
            binding = db.execute(
                "SELECT * FROM goal_approval_bindings WHERE approval_id=? AND goal_id=? AND owner_actor=? AND run_key=? AND supervisor_task_id=? AND status='consumed' AND action_digest=? AND expires_at>?",
                (approval_id, goal["goal_id"], lease.goal.owner_actor, run_key, run["supervisor_task_id"], plan.action_digest, datetime_text(now)),
            ).fetchone()
            if binding is None:
                raise GoalConflict("no consumed supervisor approval matches this exact goal action")
            if run["dispatch_started"]:
                raise GoalConflict("supervisor action dispatch was already claimed")
            db.execute("UPDATE goal_step_runs SET dispatch_started=1,dispatch_result='started',status='action_running' WHERE run_key=? AND dispatch_started=0", (run_key,))
            db.execute("UPDATE goal_approval_bindings SET status='dispatching' WHERE approval_id=? AND status='consumed'", (approval_id,))
            return True

    def record_supervisor_action_result(
        self, lease: GoalLease, run_key: str, *, result: str, now: datetime | None = None,
    ) -> None:
        if result not in {"completed", "failed", "unknown"}:
            raise GoalValidationError("supervisor action result is invalid")
        now = normalize_datetime(now or utc_now(), "now")
        with self._tx() as db:
            goal = self._owned_row(db, lease.goal.goal_id, lease.goal.owner_actor)
            run = db.execute("SELECT * FROM goal_step_runs WHERE run_key=? AND goal_id=?", (run_key, goal["goal_id"])).fetchone()
            if run is None or not run["dispatch_started"] or run["lease_token"] != lease.token:
                raise LeaseLost("supervisor action dispatch is not current")
            if goal["lease_token"] != lease.token or goal["lease_owner"] != lease.worker_id:
                raise LeaseLost("supervisor action lease is no longer current")
            db.execute("UPDATE goal_step_runs SET dispatch_result=? WHERE run_key=?", (result, run_key))
            if run["approval_id"]:
                binding_status = "action_completed" if result == "completed" else "action_failed" if result == "failed" else "needs_review"
                db.execute("UPDATE goal_approval_bindings SET status=? WHERE approval_id=?", (binding_status, run["approval_id"]))

    def reserve_provider_poll(
        self, owner_actor: str, provider: str, *, minimum_interval_seconds: int,
        now: datetime | None = None, retry_after_seconds: int = 0,
        policy_source: str = "local_policy",
    ) -> ProviderPollDecision:
        if not re.fullmatch(r"device:[0-9a-f]{32}", owner_actor or ""):
            raise GoalValidationError("provider polling requires a paired owner")
        if not _PROVIDER_RE.fullmatch(provider or ""):
            raise GoalValidationError("provider name is invalid")
        if not 1 <= minimum_interval_seconds <= 86_400 or not 0 <= retry_after_seconds <= 86_400:
            raise GoalValidationError("provider polling interval is out of range")
        if policy_source not in {"local_policy", "provider_retry_after"}:
            raise GoalValidationError("provider polling policy source is invalid")
        now = normalize_datetime(now or utc_now(), "now")
        now_text = datetime_text(now)
        with self._tx() as db:
            row = db.execute("SELECT * FROM goal_provider_poll_state WHERE owner_actor=? AND provider=?", (owner_actor, provider)).fetchone()
            if row is not None:
                next_allowed = normalize_datetime(row["next_allowed_at"], "next_allowed_at")
                interval = max(int(row["minimum_interval_seconds"]), minimum_interval_seconds)
                last_read = normalize_datetime(row["last_read_at"], "last_read_at") if row["last_read_at"] else None
                if next_allowed > now:
                    if retry_after_seconds:
                        next_allowed = max(next_allowed, now + timedelta(seconds=retry_after_seconds))
                        db.execute(
                            "UPDATE goal_provider_poll_state SET next_allowed_at=?,policy_source=?,updated_at=? WHERE owner_actor=? AND provider=?",
                            (datetime_text(next_allowed), policy_source, now_text, owner_actor, provider),
                        )
                    return ProviderPollDecision(False, provider, interval, next_allowed, last_read, policy_source if retry_after_seconds else row["policy_source"])
            else:
                interval = minimum_interval_seconds
                last_read = None
            next_allowed = now + timedelta(seconds=max(interval, retry_after_seconds))
            db.execute(
                "INSERT INTO goal_provider_poll_state(owner_actor,provider,minimum_interval_seconds,next_allowed_at,last_reserved_at,last_read_at,last_result,policy_source,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_actor,provider) DO UPDATE SET minimum_interval_seconds=excluded.minimum_interval_seconds,next_allowed_at=excluded.next_allowed_at,last_reserved_at=excluded.last_reserved_at,policy_source=excluded.policy_source,updated_at=excluded.updated_at",
                (owner_actor, provider, interval, datetime_text(next_allowed), now_text,
                 row["last_read_at"] if row is not None else None, row["last_result"] if row is not None else None,
                 policy_source, now_text),
            )
            return ProviderPollDecision(True, provider, interval, next_allowed, last_read, policy_source)

    def record_provider_read_result(
        self, owner_actor: str, provider: str, *, result: str, observed_at: datetime | None = None,
    ) -> None:
        if not re.fullmatch(r"device:[0-9a-f]{32}", owner_actor or "") or not _PROVIDER_RE.fullmatch(provider or ""):
            raise GoalValidationError("provider read owner or provider is invalid")
        if result not in {"success", "unavailable", "error"}:
            raise GoalValidationError("provider read result must be a symbolic outcome")
        observed_at = normalize_datetime(observed_at or utc_now(), "observed_at")
        with self._tx() as db:
            db.execute(
                "UPDATE goal_provider_poll_state SET last_read_at=?,last_result=?,updated_at=? WHERE owner_actor=? AND provider=?",
                (datetime_text(observed_at), result, datetime_text(observed_at), owner_actor, provider),
            )

    def fail_unprepared_step(self, lease: GoalLease, *, error_code: str, now: datetime | None = None) -> GoalSnapshot:
        """Release a lease after a bounded local planner failed before any action ran."""
        if not __import__("re").fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code):
            raise GoalValidationError("error_code must be a symbolic label")
        now = normalize_datetime(now or utc_now(), "now")
        with self._tx() as db:
            goal = self._owned_row(db, lease.goal.goal_id, lease.goal.owner_actor)
            self._assert_lease(db, goal, lease, now)
            attempts_used = int(goal["attempts_used"]) + 1
            if attempts_used < int(goal["max_attempts"]):
                status = GoalStatus.ACTIVE
                wake = now + timedelta(seconds=int(goal["retry_base_seconds"]))
                event_type = GoalEventType.STEP_RETRY_SCHEDULED
                summary = "Step planning failed before action dispatch; retry was scheduled."
                completed = None
            else:
                status = GoalStatus.FAILED
                wake = None
                event_type = GoalEventType.FAILED
                summary = "Goal stopped after bounded step planning failed."
                completed = datetime_text(now)
            db.execute(
                "UPDATE goals SET status=?,attempts_used=?,next_wake_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error_code=?,updated_at=?,completed_at=? WHERE goal_id=?",
                (status.value, attempts_used, datetime_text(wake) if wake else None, error_code, datetime_text(now), completed, goal["goal_id"]),
            )
            self._append_event(db, goal_id=goal["goal_id"], owner_actor=goal["owner_actor"],
                event_type=event_type, at=now, summary=summary,
                checkpoint_version=int(goal["checkpoint_version"]))
            return self._snapshot(db.execute("SELECT * FROM goals WHERE goal_id=?", (goal["goal_id"],)).fetchone())

    def wake_on_event(
        self, goal_id: str, owner_actor: str, reason: GoalWakeReason | str,
        *, event_id: str, checkpoint_update: dict[str, Any] | None = None,
        summary: str | None = None, now: datetime | None = None,
    ) -> tuple[GoalSnapshot, bool]:
        try:
            reason = GoalWakeReason(reason)
        except (ValueError, TypeError) as exc:
            raise GoalValidationError("wake reason is unsupported") from exc
        if not isinstance(event_id, str) or not __import__("re").fullmatch(r"[A-Za-z0-9_.:-]{1,120}", event_id):
            raise GoalValidationError("event_id is invalid")
        now = normalize_datetime(now or utc_now(), "now")
        if reason == GoalWakeReason.APPROVAL_RESOLVED:
            raise GoalConflict("approval resolutions must use a verified supervisor binding")
        if checkpoint_update is not None:
            _validate_json_object(checkpoint_update, label="checkpoint_update", limit=MAX_GOAL_CHECKPOINT_BYTES, reject_secrets=True)
        dedupe_key = f"wake:{reason.value}:{event_id}"
        with self._tx() as db:
            goal = self._owned_row(db, goal_id, owner_actor)
            prior = db.execute("SELECT 1 FROM goal_events WHERE goal_id=? AND dedupe_key=?", (goal_id, dedupe_key)).fetchone()
            if prior:
                return self._snapshot(goal), False
            status = GoalStatus(goal["status"])
            if reason == GoalWakeReason.APPROVAL_RESOLVED:
                raise GoalConflict("approval wakes must use a verified supervisor binding")
            allowed = {
                GoalStatus.WAITING_UNTIL: {GoalWakeReason.TIMER, GoalWakeReason.USER_REQUESTED, GoalWakeReason.CONNECTION_RECOVERED},
                GoalStatus.NEEDS_USER: {GoalWakeReason.USER_RESPONSE, GoalWakeReason.USER_REQUESTED},
                GoalStatus.NEEDS_REVIEW: {GoalWakeReason.USER_CONFIRMED_STATE},
                GoalStatus.ACTIVE: {GoalWakeReason.USER_REQUESTED},
            }
            if reason not in allowed.get(status, set()):
                raise GoalConflict(f"goal cannot wake from {status.value} for {reason.value}")
            checkpoint = _parse_json(goal["checkpoint_json"], default={})
            if checkpoint_update:
                checkpoint = {**checkpoint, **checkpoint_update}
            checkpoint_changed = _json(checkpoint) != str(goal["checkpoint_json"])
            version = int(goal["checkpoint_version"]) + (1 if checkpoint_changed else 0)
            db.execute(
                "UPDATE goals SET status=?,next_wake_at=?,checkpoint_json=?,checkpoint_version=?,updated_at=? WHERE goal_id=?",
                (GoalStatus.ACTIVE.value, datetime_text(now), _json(checkpoint), version, datetime_text(now), goal_id),
            )
            event_summary = "Goal resumed after an external event." if not summary else " ".join(summary.split())[:500]
            self._append_event(db, goal_id=goal_id, owner_actor=owner_actor, event_type=GoalEventType.WOKEN,
                at=now, summary=event_summary, checkpoint_version=version,
                evidence={"source": reason.value, "action_id": event_id}, dedupe_key=dedupe_key)
            return self._snapshot(db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()), True

    def cancel_goal(self, goal_id: str, owner_actor: str, *, now: datetime | None = None) -> GoalSnapshot:
        now = normalize_datetime(now or utc_now(), "now")
        with self._tx() as db:
            goal = self._owned_row(db, goal_id, owner_actor)
            status = GoalStatus(goal["status"])
            if status.value in _TERMINAL:
                return self._snapshot(goal)
            if status == GoalStatus.NEEDS_REVIEW:
                # Cancelling an uncertain external side effect must not erase
                # the mandatory owner-review state.
                return self._snapshot(goal)
            run = db.execute(
                "SELECT * FROM goal_step_runs WHERE run_key=? AND goal_id=?",
                (goal["current_run_key"], goal_id),
            ).fetchone() if goal["current_run_key"] else None
            if run is None:
                run = db.execute(
                    "SELECT r.* FROM goal_step_runs r JOIN goal_approval_bindings b ON b.run_key=r.run_key "
                    "WHERE b.goal_id=? AND b.owner_actor=? AND b.status='pending' ORDER BY b.created_at DESC LIMIT 1",
                    (goal_id, owner_actor),
                ).fetchone()
            uncertain = bool(
                run is not None
                and bool(run["dispatch_started"])
                and run["dispatch_result"] not in {"completed", "failed"}
            )
            next_status = GoalStatus.NEEDS_REVIEW if uncertain else GoalStatus.CANCELLED
            if run is not None and run["status"] in {"running", "action_running", "waiting_approval"}:
                db.execute(
                    "UPDATE goal_step_runs SET status=?,finished_at=?,error_code=? WHERE run_key=?",
                    ("needs_review" if uncertain else "cancelled", datetime_text(now),
                     "SUPERVISOR_ACTION_RESULT_UNKNOWN" if uncertain else None, run["run_key"]),
                )
            db.execute(
                "UPDATE goal_approval_bindings SET status='cancelled',resolved_at=? WHERE goal_id=? AND owner_actor=? AND status='pending'",
                (datetime_text(now), goal_id, owner_actor),
            )
            db.execute(
                "UPDATE goals SET status=?,next_wake_at=NULL,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error_code=?,updated_at=?,completed_at=? WHERE goal_id=?",
                (next_status.value, "SUPERVISOR_ACTION_RESULT_UNKNOWN" if uncertain else None,
                 datetime_text(now), None if uncertain else datetime_text(now), goal_id),
            )
            self._append_event(db, goal_id=goal_id, owner_actor=owner_actor,
                event_type=GoalEventType.NEEDS_REVIEW if uncertain else GoalEventType.CANCELLED,
                at=now,
                summary=("Goal stopped by its owner while a supervisor action result was unknown; review is required."
                         if uncertain else "Goal cancelled by its owner."),
                checkpoint_version=int(goal["checkpoint_version"]))
            return self._snapshot(db.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone())

    def is_lease_current(self, lease: GoalLease, *, now: datetime | None = None) -> bool:
        now = normalize_datetime(now or utc_now(), "now")
        with self._lock:
            row = self._db.execute("SELECT * FROM goals WHERE goal_id=? AND owner_actor=?", (lease.goal.goal_id, lease.goal.owner_actor)).fetchone()
        return bool(
            row is not None
            and row["status"] == GoalStatus.ACTIVE.value
            and row["lease_token"] == lease.token
            and row["lease_owner"] == lease.worker_id
            and row["lease_expires_at"]
            and normalize_datetime(row["lease_expires_at"], "lease_expires_at") > now
            and normalize_datetime(row["deadline_at"], "deadline_at") > now
        )

    def seconds_until_next_due(self, *, now: datetime | None = None, maximum_seconds: int = 60) -> float:
        """Return a bounded idle delay; the caller holds no model/browser worker while waiting."""
        if not 1 <= maximum_seconds <= 3600:
            raise GoalValidationError("scheduler idle bound is invalid")
        current = normalize_datetime(now or utc_now(), "now")
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(CASE WHEN lease_token IS NOT NULL THEN lease_expires_at ELSE next_wake_at END) AS next_at "
                "FROM goals WHERE status IN ('active','waiting_until')"
            ).fetchone()
        if row is None or not row["next_at"]:
            return float(maximum_seconds)
        next_at = normalize_datetime(row["next_at"], "next_at")
        return max(0.05, min(float(maximum_seconds), (next_at - current).total_seconds()))

    def _validate_lease(self, goal_id: str, owner_actor: str, token: str, worker_id: str, *, now: datetime) -> sqlite3.Row:
        row = self._owned_row(self._db, goal_id, owner_actor)
        if (
            row["status"] != GoalStatus.ACTIVE.value
            or row["lease_token"] != token
            or row["lease_owner"] != worker_id
            or not row["lease_expires_at"]
            or normalize_datetime(row["lease_expires_at"], "lease_expires_at") <= now
            or normalize_datetime(row["deadline_at"], "deadline_at") <= now
        ):
            raise LeaseLost("goal lease is no longer current")
        return row
