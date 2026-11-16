"""SupervisorPort implementation over OpenJarvis's real SupervisorStore (public methods only).

What it does:
- records remote task objectives as supervisor tasks, with the allowed capability names;
- forwards approval decisions to the store, which enforces exact digest, expiry, and single use;
- records cancellation and progress events.

What it never does: execute tools, build shell commands, read approval arguments to act on them, or
create approvals. The supervisor's own authorization boundary stays authoritative. OpenJarvis code and its
database schema are not modified; the adapter keeps its idempotency records in its own SQLite file.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .supervisor import ApprovalRecord


class SupervisorRefused(RuntimeError):
    """The supervisor refused a request. The message is a fixed, non-secret reason."""


def _to_unix_ms(iso_text: str) -> int:
    parsed = datetime.fromisoformat(iso_text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class OpenJarvisSupervisorAdapter:
    def __init__(self, store: Any, mapping_db: str | Path, actor: str = "jarvis-link:companion") -> None:
        self.store = store
        self.actor = actor
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(mapping_db), check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS submit_keys(idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_keys(idempotency_key TEXT PRIMARY KEY, result_json TEXT NOT NULL);
            """
        )
        self._db.commit()

    # --- SupervisorPort -------------------------------------------------------------------------------
    def submit_task(self, *, objective: str, capability_scope: list[str], idempotency_key: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT task_id FROM submit_keys WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row is None:
                task_id = self.store.create_task(objective, capabilities=list(capability_scope), metadata={"source": "jarvis_link"})
                self._db.execute("INSERT INTO submit_keys(idempotency_key, task_id) VALUES(?, ?)", (idempotency_key, task_id))
                self._db.commit()
            else:
                task_id = row[0]
        task = self.store.get_task(task_id)
        if task is None:
            raise SupervisorRefused("supervisor did not record the task")
        return {"task_id": task_id, "state": task["state"], "simulated": False}

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        row = self.store.approval(approval_id)
        if row is None:
            return None
        return ApprovalRecord(
            approval_id=row["id"],
            task_id=row["task_id"],
            operation=row["operation"],
            target=row["target"],
            arguments_digest=row["args_digest"],
            expires_at_unix_ms=_to_unix_ms(row["expires_at"]),
            status=row["status"],
        )

    def list_pending_approvals(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.store.pending_approvals(100)
        return [
            {
                "approval_id": row["id"],
                "task_id": row["task_id"],
                "operation": row["operation"],
                "target": row["target"],
                "canonical_arguments_digest": row["args_digest"],
                "expires_at_unix_ms": _to_unix_ms(row["expires_at"]),
                "redacted_summary": (str(row.get("reason") or "Exact operation requires approval.") + " Arguments: " + str(row.get("safe_args") or {}))[:500],
            }
            for row in rows
            if row["task_id"] == task_id and row["status"] == "PENDING"
        ]

    def decide_approval(self, *, task_id: str, approval_id: str, decision: str,
                        expected_arguments_digest: str, idempotency_key: str,
                        continuation_objective: str | None = None) -> dict[str, Any]:
        with self._lock:
            cached = self._db.execute("SELECT result_json FROM decision_keys WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if cached is not None:
                import json
                return json.loads(cached[0])
            accepted = self.store.decide_approval(
                approval_id, decision=decision, expected_digest=expected_arguments_digest,
                task_id=task_id, actor=self.actor, nonce=idempotency_key,
            )
            if not accepted:
                raise SupervisorRefused("supervisor refused the approval decision (digest, expiry, status, or replay)")
            status = self.store.approval(approval_id)["status"]
            result = {"task_id": task_id, "approval_id": approval_id, "status": status, "simulated": False}
            import json
            self._db.execute("INSERT INTO decision_keys(idempotency_key, result_json) VALUES(?, ?)", (idempotency_key, json.dumps(result, sort_keys=True)))
            self._db.commit()
            return result

    def cancel_task(self, task_id: str, idempotency_key: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise SupervisorRefused("unknown task")
        if task["state"] != "CANCELLED":
            self.store.transition(task_id, "CANCELLED", expected=task["state"], step="remote_cancel")
        return {"task_id": task_id, "state": "CANCELLED"}

    def report_progress(self, task_id: str, stage: str, result_digest: str = "") -> dict[str, Any]:
        if self.store.get_task(task_id) is None:
            raise SupervisorRefused("unknown task")
        self.store.record_event(task_id, "link.progress", {"stage": stage[:64], "result_digest": result_digest[:64]})
        return {"task_id": task_id, "stage": stage[:64]}
