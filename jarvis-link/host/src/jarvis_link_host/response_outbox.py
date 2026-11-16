"""Durable encrypted-response outbox for relay delivery retries.

Only signed/encrypted relay envelopes are stored. Decrypted task objectives,
approval arguments, API credentials, and supervisor results are never written
to this table.
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

    def close(self) -> None:
        with self._lock:
            self._db.close()


__all__ = ["HostResponseOutbox", "ResponseReplayConflict"]
