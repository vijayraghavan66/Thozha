"""Persistent host-side replay cache for received relay envelopes."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class ReplayCache:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS seen_messages(message_id TEXT PRIMARY KEY, expires_at_ms INTEGER NOT NULL, received_at_ms INTEGER NOT NULL)")
        self._db.commit()

    def claim(self, message_id: str, expires_at_ms: int, *, now_ms: int | None = None) -> bool:
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if expires_at_ms <= now:
            return False
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("DELETE FROM seen_messages WHERE expires_at_ms<=?", (now,))
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO seen_messages(message_id,expires_at_ms,received_at_ms) VALUES(?,?,?)",
                    (message_id, expires_at_ms, now),
                )
                self._db.commit()
                return cursor.rowcount == 1
            except BaseException:
                self._db.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()
