"""Read-only paired-device authentication and loopback policy for voice sessions."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
from pathlib import Path
import sqlite3
from typing import Any


def valid_loopback_peer(peer: Any) -> bool:
    if not isinstance(peer, tuple) or not peer:
        return False
    try:
        return ipaddress.ip_address(str(peer[0]).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def authenticate_paired_device(db_path: str | Path, token: str) -> str | None:
    """Validate an existing paired token without opening a writable DB connection."""
    if not token or len(token) > 2048:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    uri = f"{path.resolve().as_uri()}?mode=ro"
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        with sqlite3.connect(uri, uri=True, timeout=1) as db:
            row = db.execute(
                "SELECT id FROM paired_devices WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",
                (digest, now),
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None
