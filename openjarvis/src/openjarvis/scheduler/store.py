"""SQLite-backed persistence for scheduled tasks and run logs."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_CREATE_TASKS_TABLE = """\
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              TEXT PRIMARY KEY,
    prompt          TEXT    NOT NULL,
    schedule_type   TEXT    NOT NULL,
    schedule_value  TEXT    NOT NULL,
    context_mode    TEXT    NOT NULL DEFAULT 'isolated',
    status          TEXT    NOT NULL DEFAULT 'active',
    next_run        TEXT,
    last_run        TEXT,
    agent           TEXT    NOT NULL DEFAULT 'simple',
    tools           TEXT    NOT NULL DEFAULT '',
    metadata        TEXT    NOT NULL DEFAULT '{}',
    web_owner_id    TEXT,
    web_idempotency_key TEXT
);
"""

_CREATE_LOGS_TABLE = """\
CREATE TABLE IF NOT EXISTS task_run_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    success     INTEGER NOT NULL DEFAULT 0,
    result      TEXT    NOT NULL DEFAULT '',
    error       TEXT    NOT NULL DEFAULT '',
    state       TEXT    NOT NULL DEFAULT 'completed',
    occurrence_key TEXT,
    idempotency_key TEXT,
    supervisor_task_id TEXT
);
"""

_INSERT_TASK = """\
INSERT OR REPLACE INTO scheduled_tasks
    (id, prompt, schedule_type, schedule_value, context_mode,
     status, next_run, last_run, agent, tools, metadata,
     web_owner_id, web_idempotency_key)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_WEB_TASK = """\
INSERT OR IGNORE INTO scheduled_tasks
    (id, prompt, schedule_type, schedule_value, context_mode,
     status, next_run, last_run, agent, tools, metadata,
     web_owner_id, web_idempotency_key)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_LOG = """\
INSERT INTO task_run_logs
    (task_id, started_at, finished_at, success, result, error, state,
     occurrence_key, idempotency_key, supervisor_task_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class SchedulerStore:
    """SQLite CRUD store for scheduled tasks and their run logs."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        if self._db_path != ":memory:":
            self._secure_database_path(Path(self._db_path).expanduser())
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TASKS_TABLE)
        self._conn.execute(_CREATE_LOGS_TABLE)
        self._ensure_column("scheduled_tasks", "web_owner_id", "TEXT")
        self._ensure_column("scheduled_tasks", "web_idempotency_key", "TEXT")
        self._ensure_column(
            "task_run_logs", "state", "TEXT NOT NULL DEFAULT 'completed'"
        )
        self._ensure_column("task_run_logs", "occurrence_key", "TEXT")
        self._ensure_column("task_run_logs", "idempotency_key", "TEXT")
        self._ensure_column("task_run_logs", "supervisor_task_id", "TEXT")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_web_idempotency "
            "ON scheduled_tasks(web_owner_id, web_idempotency_key) "
            "WHERE web_owner_id IS NOT NULL AND web_idempotency_key IS NOT NULL"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_run_occurrence "
            "ON task_run_logs(task_id, occurrence_key) WHERE occurrence_key IS NOT NULL"
        )
        self._conn.commit()

    @staticmethod
    def _secure_database_path(path: Path) -> None:
        """Require owner-only storage for scheduled prompts and run history."""
        path = path.absolute()
        if path.is_symlink():
            raise PermissionError("scheduler database must not be a symlink")
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise PermissionError("scheduler database directory must not be a symlink")
        parent_stat = path.parent.stat()
        if (
            parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) & 0o077
        ):
            raise PermissionError("scheduler database directory must be owner-only")
        if path.exists():
            file_stat = path.stat()
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.geteuid():
                raise PermissionError(
                    "scheduler database must be an owner-owned regular file"
                )
            os.chmod(path, 0o600)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise PermissionError(
                    "scheduler database permissions could not be restricted"
                )
            return
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            file_stat = os.fstat(fd)
            if (
                file_stat.st_uid != os.geteuid()
                or stat.S_IMODE(file_stat.st_mode) != 0o600
            ):
                raise PermissionError(
                    "scheduler database could not be created owner-only"
                )
        finally:
            os.close(fd)

    def _ensure_column(self, table: str, name: str, declaration: str) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if name not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    # -- Task CRUD -----------------------------------------------------------

    def save_task(self, task: Dict[str, Any]) -> None:
        """Insert or replace a scheduled task record."""
        metadata = task.get("metadata") or {}
        with self._lock:
            self._conn.execute(
                _INSERT_TASK,
                (
                    task["id"],
                    task["prompt"],
                    task["schedule_type"],
                    task["schedule_value"],
                    task.get("context_mode", "isolated"),
                    task.get("status", "active"),
                    task.get("next_run"),
                    task.get("last_run"),
                    task.get("agent", "simple"),
                    task.get("tools", ""),
                    json.dumps(metadata),
                    metadata.get("thozha_web_owner_id"),
                    metadata.get("thozha_web_idempotency_key"),
                ),
            )
            self._conn.commit()

    def create_web_task(self, task: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
        """Atomically create an owner/idempotency-scoped Web schedule.

        Unlike the generic save/upsert path, this never replaces an existing
        schedule on duplicate owner/idempotency keys.
        """
        metadata = task.get("metadata") or {}
        owner_id = metadata.get("thozha_web_owner_id")
        idempotency_key = metadata.get("thozha_web_idempotency_key")
        if (
            not isinstance(owner_id, str)
            or not owner_id
            or not isinstance(idempotency_key, str)
            or not idempotency_key
        ):
            raise ValueError("Web schedule owner and idempotency key are required")
        values = (
            task["id"],
            task["prompt"],
            task["schedule_type"],
            task["schedule_value"],
            task.get("context_mode", "isolated"),
            task.get("status", "active"),
            task.get("next_run"),
            task.get("last_run"),
            task.get("agent", "simple"),
            task.get("tools", ""),
            json.dumps(metadata),
            owner_id,
            idempotency_key,
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(_INSERT_WEB_TASK, values)
                row = self._conn.execute(
                    "SELECT * FROM scheduled_tasks "
                    "WHERE web_owner_id=? AND web_idempotency_key=?",
                    (owner_id, idempotency_key),
                ).fetchone()
                if row is None:
                    raise sqlite3.IntegrityError(
                        "Web schedule row could not be inserted or recovered"
                    )
                self._conn.commit()
                return self._row_to_dict(row), cursor.rowcount == 1
            except BaseException:
                self._conn.rollback()
                raise

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single task by ID, or ``None`` if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_tasks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return all tasks, optionally filtered by *status*."""
        if status is not None:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE status = ?", (status,)
                ).fetchall()
        else:
            with self._lock:
                rows = self._conn.execute("SELECT * FROM scheduled_tasks").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_due_tasks(self, now_iso: str) -> List[Dict[str, Any]]:
        """Return active tasks whose ``next_run`` is at or before *now_iso*."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE status = 'active' "
                "AND next_run IS NOT NULL AND next_run <= ?",
                (now_iso,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_task(self, task: Dict[str, Any]) -> None:
        """Update an existing task (same as save_task — uses INSERT OR REPLACE)."""
        self.save_task(task)

    def find_web_task(
        self, owner_id: str, idempotency_key: str
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scheduled_tasks "
                "WHERE web_owner_id=? AND web_idempotency_key=?",
                (owner_id, idempotency_key),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def update_web_task(self, task: Dict[str, Any]) -> bool:
        """Atomically update a Web schedule without replacing the row or history."""
        metadata = task.get("metadata") or {}
        owner_id = metadata.get("thozha_web_owner_id")
        idempotency_key = metadata.get("thozha_web_idempotency_key")
        if not isinstance(owner_id, str) or not isinstance(idempotency_key, str):
            raise ValueError("Web schedule owner and idempotency key are required")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "UPDATE scheduled_tasks SET prompt=?,schedule_type=?,"
                    "schedule_value=?,context_mode=?,status=?,next_run=?,last_run=?,"
                    "agent=?,tools=?,metadata=? "
                    "WHERE id=? AND web_owner_id=? AND web_idempotency_key=?",
                    (
                        task["prompt"],
                        task["schedule_type"],
                        task["schedule_value"],
                        task.get("context_mode", "isolated"),
                        task.get("status", "active"),
                        task.get("next_run"),
                        task.get("last_run"),
                        task.get("agent", "simple"),
                        task.get("tools", ""),
                        json.dumps(metadata),
                        task["id"],
                        owner_id,
                        idempotency_key,
                    ),
                )
                self._conn.commit()
                return cursor.rowcount == 1
            except BaseException:
                self._conn.rollback()
                raise

    def delete_task(self, task_id: str) -> None:
        """Delete a task by ID."""
        with self._lock:
            self._conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            self._conn.commit()

    # -- Run logs ------------------------------------------------------------

    def log_run(
        self,
        task_id: str,
        started_at: str,
        finished_at: str,
        success: bool,
        result: str = "",
        error: str = "",
        *,
        state: str = "completed",
        occurrence_key: str | None = None,
        idempotency_key: str | None = None,
        supervisor_task_id: str | None = None,
    ) -> None:
        """Record a single execution of a task."""
        with self._lock:
            self._conn.execute(
                _INSERT_LOG,
                (
                    task_id,
                    started_at,
                    finished_at,
                    int(success),
                    result,
                    error,
                    state,
                    occurrence_key,
                    idempotency_key,
                    supervisor_task_id,
                ),
            )
            self._conn.commit()

    def claim_run(
        self,
        task_id: str,
        *,
        occurrence_key: str,
        idempotency_key: str,
        started_at: str,
        expected_next_run: str | None = None,
        allowed_statuses: tuple[str, ...] | None = None,
    ) -> tuple[Dict[str, Any], bool]:
        """Persist an occurrence claim and recover the same claim on retries."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM task_run_logs WHERE task_id=? AND occurrence_key=?",
                    (task_id, occurrence_key),
                ).fetchone()
                if row is not None:
                    record = dict(row)
                    self._conn.commit()
                    return record, False
                if expected_next_run is not None or allowed_statuses is not None:
                    current = self._conn.execute(
                        "SELECT status,next_run FROM scheduled_tasks WHERE id=?",
                        (task_id,),
                    ).fetchone()
                    if (
                        current is None
                        or (
                            expected_next_run is not None
                            and (
                                current["status"] != "active"
                                or current["next_run"] != expected_next_run
                            )
                        )
                        or (
                            allowed_statuses is not None
                            and current["status"] not in allowed_statuses
                        )
                    ):
                        self._conn.commit()
                        raise RuntimeError("scheduled occurrence is no longer active")
                cursor = self._conn.execute(
                    "INSERT INTO task_run_logs("
                    "task_id,started_at,finished_at,success,result,error,state,"
                    "occurrence_key,idempotency_key,supervisor_task_id) "
                    "VALUES(?,?,NULL,0,'','',?,?,?,NULL)",
                    (
                        task_id,
                        started_at,
                        "dispatching",
                        occurrence_key,
                        idempotency_key,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM task_run_logs WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                self._conn.commit()
                return dict(row), True
            except BaseException:
                self._conn.rollback()
                raise

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        state: str,
        success: bool,
        result: str = "",
        error: str = "",
        supervisor_task_id: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE task_run_logs SET finished_at=?,state=?,success=?,result=?,"
                "error=?,supervisor_task_id=? WHERE id=?",
                (
                    finished_at,
                    state,
                    int(success),
                    result,
                    error,
                    supervisor_task_id,
                    run_id,
                ),
            )
            self._conn.commit()

    def get_run_logs(self, task_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the most recent run logs for *task_id*."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_run_logs WHERE task_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- Lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        if "metadata" in d and isinstance(d["metadata"], str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = {}
        return d


__all__ = ["SchedulerStore"]
