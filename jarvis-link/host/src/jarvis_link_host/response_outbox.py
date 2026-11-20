"""Durable encrypted-response outbox and approval replay ledger.

Signed/encrypted relay envelopes are stored for delivery. The replay ledger
contains only approval/task/device IDs, digests, decision, idempotency key,
message ID and expiry; it never stores decrypted task objectives, approval
arguments, API credentials, or supervisor results.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Mapping


class ResponseReplayConflict(RuntimeError):
    """A message ID was observed with bytes different from the stored envelope."""


class HostResponseOutbox:
    def __init__(self, path: str | Path = ":memory:") -> None:
        requested_path = str(path)
        self.path = requested_path
        if requested_path != ":memory:":
            database_path = Path(requested_path).expanduser().absolute()
            if any(component.is_symlink() for component in (database_path, *database_path.parents)):
                raise ValueError("response outbox path must not traverse symbolic links")
            parent = database_path.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if parent.is_symlink() or not parent.is_dir():
                raise ValueError("response outbox directory must be a private real directory")
            parent_metadata = parent.stat()
            if parent_metadata.st_uid != os.getuid() or stat.S_IMODE(parent_metadata.st_mode) & 0o077:
                raise PermissionError("response outbox directory must be owner-only; refusing to change its permissions")
            if database_path.is_symlink():
                raise ValueError("response outbox database must not be a symbolic link")
            if database_path.exists() and not database_path.is_file():
                raise ValueError("response outbox database path must be a regular file")
            if database_path.exists() and database_path.stat().st_uid != os.getuid():
                raise PermissionError("response outbox database must be owned by the current user")
            self.path = str(database_path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=15000")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS relay_response_outbox(
                 tenant_id TEXT NOT NULL,
                 sender_device_id TEXT NOT NULL,
                 message_id TEXT NOT NULL,
                 request_digest TEXT NOT NULL,
                 responses_json TEXT NOT NULL,
                 task_id TEXT,
                 approvals_checked INTEGER NOT NULL DEFAULT 0,
                 expires_at_ms INTEGER NOT NULL,
                 created_at_ms INTEGER NOT NULL,
                 delivered_at_ms INTEGER,
                 PRIMARY KEY(tenant_id,sender_device_id,message_id)
               )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS relay_progress_outbox(
                 task_id TEXT NOT NULL,
                 event_seq INTEGER NOT NULL,
                 message_id TEXT NOT NULL UNIQUE,
                 envelope_json TEXT NOT NULL,
                 expires_at_ms INTEGER NOT NULL,
                 created_at_ms INTEGER NOT NULL,
                 delivered_at_ms INTEGER,
                 PRIMARY KEY(task_id,event_seq)
               )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS relay_approval_decision_claims(
                 tenant_id TEXT NOT NULL,
                 approval_id TEXT NOT NULL,
                 task_id TEXT NOT NULL,
                 sender_device_id TEXT NOT NULL,
                 message_id TEXT NOT NULL,
                 envelope_digest TEXT NOT NULL,
                 decision TEXT NOT NULL,
                 operation_digest TEXT NOT NULL,
                 idempotency_key TEXT NOT NULL,
                 envelope_expires_at_ms INTEGER NOT NULL,
                 created_at_ms INTEGER NOT NULL,
                 PRIMARY KEY(tenant_id,approval_id),
                 UNIQUE(tenant_id,sender_device_id,message_id)
               )"""
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(relay_response_outbox)")}
        if "task_id" not in columns:
            self._db.execute("ALTER TABLE relay_response_outbox ADD COLUMN task_id TEXT")
        if "approvals_checked" not in columns:
            self._db.execute("ALTER TABLE relay_response_outbox ADD COLUMN approvals_checked INTEGER NOT NULL DEFAULT 0")
        self._db.commit()
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)

    @staticmethod
    def _canonical_request(envelope: Mapping[str, Any]) -> tuple[str, str, str, int, str]:
        tenant = envelope.get("tenant_id")
        sender = envelope.get("from_device_id")
        message_id = envelope.get("message_id")
        expiry = envelope.get("expires_at_unix_ms")
        if not all(isinstance(value, str) for value in (tenant, sender, message_id)) or not isinstance(expiry, int):
            raise ValueError("relay request identity fields are invalid")
        raw = json.dumps(dict(envelope), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        return tenant, sender, message_id, expiry, hashlib.sha256(raw).hexdigest()

    def get(self, envelope: Mapping[str, Any], *, now_ms: int | None = None) -> list[dict[str, Any]] | None:
        tenant, sender, message_id, expiry, request_digest = self._canonical_request(envelope)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            row = self._db.execute(
                "SELECT request_digest,responses_json,expires_at_ms FROM relay_response_outbox WHERE tenant_id=? AND sender_device_id=? AND message_id=?",
                (tenant, sender, message_id),
            ).fetchone()
            if row is None:
                return None
            if row[2] <= now or expiry <= now:
                self._db.execute(
                    "DELETE FROM relay_response_outbox WHERE tenant_id=? AND sender_device_id=? AND message_id=?",
                    (tenant, sender, message_id),
                )
                self._db.commit()
                return None
            if row[0] != request_digest:
                raise ResponseReplayConflict("message ID was reused with different envelope bytes")
            value = json.loads(row[1])
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise RuntimeError("stored relay response batch is invalid")
            return value

    def put(
        self,
        envelope: Mapping[str, Any],
        responses: list[Mapping[str, Any]],
        *,
        task_id: str | None = None,
        approvals_checked: bool | None = None,
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        tenant, sender, message_id, expiry, request_digest = self._canonical_request(envelope)
        if expiry <= (int(time.time() * 1000) if now_ms is None else now_ms):
            raise ValueError("cannot cache a reply for an expired request")
        raw_responses = json.dumps([dict(item) for item in responses], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM relay_response_outbox WHERE expires_at_ms<=?", (now,))
                row = self._db.execute(
                    "SELECT request_digest,responses_json FROM relay_response_outbox WHERE tenant_id=? AND sender_device_id=? AND message_id=?",
                    (tenant, sender, message_id),
                ).fetchone()
                if row is not None:
                    if row[0] != request_digest:
                        raise ResponseReplayConflict("message ID was reused with different envelope bytes")
                    self._db.commit()
                    return json.loads(row[1])
                self._db.execute(
                    "INSERT INTO relay_response_outbox(tenant_id,sender_device_id,message_id,request_digest,responses_json,task_id,approvals_checked,expires_at_ms,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                    (tenant, sender, message_id, request_digest, raw_responses, task_id,
                     int(task_id is None if approvals_checked is None else approvals_checked), expiry, now),
                )
                self._db.commit()
                return json.loads(raw_responses)
            except BaseException:
                self._db.rollback()
                raise

    def task_id(self, envelope: Mapping[str, Any]) -> str | None:
        tenant, sender, message_id, _expiry, request_digest = self._canonical_request(envelope)
        with self._lock:
            row = self._db.execute(
                "SELECT task_id FROM relay_response_outbox WHERE tenant_id=? AND sender_device_id=? AND message_id=? AND request_digest=?",
                (tenant, sender, message_id, request_digest),
            ).fetchone()
        return row[0] if row else None

    def owns_task(self, *, task_id: str, companion_id: str) -> bool:
        """Only an unambiguous persisted task-to-sender binding grants control."""
        with self._lock:
            owners = self._db.execute(
                "SELECT DISTINCT sender_device_id FROM relay_response_outbox WHERE task_id=?",
                (task_id,),
            ).fetchall()
        return len(owners) == 1 and owners[0][0] == companion_id

    def claim_approval_decision(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        task_id: str,
        sender_device_id: str,
        message_id: str,
        envelope_digest: str,
        decision: str,
        operation_digest: str,
        idempotency_key: str,
        envelope_expires_at_ms: int,
        allow_new: bool,
        now_ms: int | None = None,
    ) -> str:
        """Bind one approval to one signed decision envelope before dispatch.

        Returns ``claimed`` for the first pending decision, ``replay`` only for
        the exact same authenticated envelope, and fails closed for a changed
        decision, sender, digest, key, or envelope. No plaintext arguments are
        stored in this ledger.
        """
        id_fields = (tenant_id, approval_id, task_id, sender_device_id, message_id)
        if any(not isinstance(value, str) or not value for value in id_fields):
            raise ValueError("approval decision identity is invalid")
        if (
            decision not in {"approve", "reject"}
            or not isinstance(envelope_digest, str) or len(envelope_digest) != 64
            or not isinstance(operation_digest, str) or len(operation_digest) != 64
            or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
            or type(envelope_expires_at_ms) is not int or envelope_expires_at_ms <= 0
            or type(allow_new) is not bool
        ):
            raise ValueError("approval decision binding is invalid")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        identity = (
            task_id, sender_device_id, message_id, envelope_digest, decision,
            operation_digest, idempotency_key, envelope_expires_at_ms,
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "DELETE FROM relay_approval_decision_claims WHERE envelope_expires_at_ms<=?",
                    (now,),
                )
                row = self._db.execute(
                    """SELECT task_id,sender_device_id,message_id,envelope_digest,decision,
                              operation_digest,idempotency_key,envelope_expires_at_ms
                       FROM relay_approval_decision_claims WHERE tenant_id=? AND approval_id=?""",
                    (tenant_id, approval_id),
                ).fetchone()
                if row is not None:
                    self._db.commit()
                    return "replay" if tuple(row) == identity else "conflict"
                message_claim = self._db.execute(
                    """SELECT approval_id FROM relay_approval_decision_claims
                       WHERE tenant_id=? AND sender_device_id=? AND message_id=?""",
                    (tenant_id, sender_device_id, message_id),
                ).fetchone()
                if message_claim is not None:
                    self._db.commit()
                    return "conflict"
                if not allow_new or envelope_expires_at_ms <= now:
                    self._db.commit()
                    return "not_found"
                self._db.execute(
                    """INSERT INTO relay_approval_decision_claims(
                           tenant_id,approval_id,task_id,sender_device_id,message_id,
                           envelope_digest,decision,operation_digest,idempotency_key,
                           envelope_expires_at_ms,created_at_ms)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (tenant_id, approval_id, *identity, now),
                )
                self._db.commit()
                return "claimed"
            except BaseException:
                self._db.rollback()
                raise

    def approvals_checked(self, envelope: Mapping[str, Any]) -> bool:
        tenant, sender, message_id, _expiry, request_digest = self._canonical_request(envelope)
        with self._lock:
            row = self._db.execute(
                "SELECT approvals_checked FROM relay_response_outbox WHERE tenant_id=? AND sender_device_id=? AND message_id=? AND request_digest=?",
                (tenant, sender, message_id, request_digest),
            ).fetchone()
        return bool(row[0]) if row else False

    def append_responses(
        self,
        envelope: Mapping[str, Any],
        responses: list[Mapping[str, Any]],
        *,
        approvals_checked: bool = True,
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        tenant, sender, message_id, expiry, request_digest = self._canonical_request(envelope)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if expiry <= now:
            raise ValueError("cannot append a response to an expired request")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT request_digest,responses_json FROM relay_response_outbox WHERE tenant_id=? AND sender_device_id=? AND message_id=?",
                    (tenant, sender, message_id),
                ).fetchone()
                if row is None:
                    raise KeyError("response outbox entry does not exist")
                if row[0] != request_digest:
                    raise ResponseReplayConflict("message ID was reused with different envelope bytes")
                stored = json.loads(row[1])
                known = {item.get("message_id") for item in stored if isinstance(item, dict)}
                for response in responses:
                    item = dict(response)
                    if item.get("message_id") not in known:
                        stored.append(item)
                        known.add(item.get("message_id"))
                updated = self._db.execute(
                    "UPDATE relay_response_outbox SET responses_json=?,approvals_checked=? WHERE tenant_id=? AND sender_device_id=? AND message_id=? AND expires_at_ms>?",
                    (json.dumps(stored, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
                     int(approvals_checked), tenant, sender, message_id, now),
                )
                if updated.rowcount != 1:
                    raise ValueError("response outbox entry has expired")
                self._db.commit()
                return stored
            except BaseException:
                self._db.rollback()
                raise

    def mark_delivered(self, envelope: Mapping[str, Any], *, now_ms: int | None = None) -> None:
        tenant, sender, message_id, _expiry, digest = self._canonical_request(envelope)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            self._db.execute(
                "UPDATE relay_response_outbox SET delivered_at_ms=COALESCE(delivered_at_ms,?) WHERE tenant_id=? AND sender_device_id=? AND message_id=? AND request_digest=?",
                (now, tenant, sender, message_id, digest),
            )
            self._db.commit()

    def get_progress(self, task_id: str, event_seq: int, *, now_ms: int | None = None) -> dict[str, Any] | None:
        self._validate_progress_identity(task_id, event_seq)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            row = self._db.execute(
                "SELECT envelope_json,expires_at_ms,delivered_at_ms FROM relay_progress_outbox WHERE task_id=? AND event_seq=?",
                (task_id, event_seq),
            ).fetchone()
            if row is None:
                return None
            if row[1] <= now:
                self._db.execute("DELETE FROM relay_progress_outbox WHERE task_id=? AND event_seq=?", (task_id, event_seq))
                self._db.commit()
                return None
            envelope = json.loads(row[0])
            if not isinstance(envelope, dict) or envelope.get("message_id") is None:
                raise RuntimeError("stored progress envelope is invalid")
            return {"envelope": envelope, "delivered": row[2] is not None}

    def put_progress(
        self,
        task_id: str,
        event_seq: int,
        envelope: Mapping[str, Any],
        *,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Persist exact encrypted bytes before relay send, keyed by supervisor sequence."""
        self._validate_progress_identity(task_id, event_seq)
        message_id = envelope.get("message_id")
        expiry = envelope.get("expires_at_unix_ms")
        if not isinstance(message_id, str) or not isinstance(expiry, int):
            raise ValueError("progress envelope identity is invalid")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if expiry <= now:
            raise ValueError("cannot cache an expired progress envelope")
        raw = json.dumps(dict(envelope), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM relay_progress_outbox WHERE expires_at_ms<=?", (now,))
                row = self._db.execute(
                    "SELECT message_id,envelope_json,delivered_at_ms FROM relay_progress_outbox WHERE task_id=? AND event_seq=?",
                    (task_id, event_seq),
                ).fetchone()
                if row is not None:
                    existing = json.loads(row[1])
                    if row[0] != message_id or existing != dict(envelope):
                        raise ResponseReplayConflict("task progress sequence was reused with different envelope bytes")
                    self._db.commit()
                    return {"envelope": existing, "delivered": row[2] is not None}
                self._db.execute(
                    "INSERT INTO relay_progress_outbox(task_id,event_seq,message_id,envelope_json,expires_at_ms,created_at_ms) VALUES(?,?,?,?,?,?)",
                    (task_id, event_seq, message_id, raw, expiry, now),
                )
                self._db.commit()
                return {"envelope": json.loads(raw), "delivered": False}
            except BaseException:
                self._db.rollback()
                raise

    def mark_progress_delivered(self, task_id: str, event_seq: int, message_id: str, *, now_ms: int | None = None) -> None:
        self._validate_progress_identity(task_id, event_seq)
        now = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            cursor = self._db.execute(
                "UPDATE relay_progress_outbox SET delivered_at_ms=COALESCE(delivered_at_ms,?) WHERE task_id=? AND event_seq=? AND message_id=?",
                (now, task_id, event_seq, message_id),
            )
            self._db.commit()
            if cursor.rowcount != 1:
                raise KeyError("progress envelope was not persisted")

    @staticmethod
    def _validate_progress_identity(task_id: str, event_seq: int) -> None:
        if not isinstance(task_id, str) or len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            raise ValueError("task progress ID is invalid")
        if not isinstance(event_seq, int) or event_seq < 1:
            raise ValueError("task progress event sequence is invalid")

    def close(self) -> None:
        with self._lock:
            self._db.close()


__all__ = ["HostResponseOutbox", "ResponseReplayConflict"]
