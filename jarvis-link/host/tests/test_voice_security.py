from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3

from jarvis_link_host.voice_security import authenticate_paired_device, valid_loopback_peer


def _db(path, *, token, expires, revoked=None):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE paired_devices (id TEXT, token_hash TEXT, revoked_at TEXT, expires_at TEXT)")
        db.execute(
            "INSERT INTO paired_devices VALUES(?,?,?,?)",
            ("a" * 32, hashlib.sha256(token.encode()).hexdigest(), revoked, expires),
        )


def test_paired_device_auth_checks_expiry_and_revocation_read_only(tmp_path):
    token = "paired-test-token"
    path = tmp_path / "supervisor.db"
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    _db(path, token=token, expires=future)
    assert authenticate_paired_device(path, token) == "a" * 32
    assert authenticate_paired_device(path, "wrong-token") is None
    with sqlite3.connect(path) as db:
        db.execute("UPDATE paired_devices SET revoked_at=?", (datetime.now(timezone.utc).isoformat(),))
    assert authenticate_paired_device(path, token) is None
    expired = tmp_path / "expired.db"
    _db(expired, token=token, expires=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert authenticate_paired_device(expired, token) is None


def test_voice_peer_is_strictly_loopback():
    assert valid_loopback_peer(("127.0.0.1", 12345))
    assert valid_loopback_peer(("::1", 12345, 0, 0))
    assert not valid_loopback_peer(("192.168.1.8", 12345))
    assert not valid_loopback_peer(("example.com", 12345))
