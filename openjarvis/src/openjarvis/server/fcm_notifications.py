"""Firebase Cloud Messaging support for authenticated Companion approval hints.

Payloads contain only an opaque approval ID. Device registration tokens are
AES-GCM encrypted at rest and never returned by APIs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from openjarvis.core.paths import get_config_dir

_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_APPROVAL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_REGISTRATION_TOKEN = 4096
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


class FCMConfigurationError(RuntimeError):
    """The optional FCM sender is not configured or cannot authenticate."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FCMRegistrationStore:
    """Encrypted per-device registration tokens and notification dedup state."""

    def __init__(self, db_path: str | None = None, key_path: str | None = None) -> None:
        self.db_path = str(db_path or (get_config_dir() / "supervisor.db"))
        self.key_path = Path(key_path) if key_path else Path(self.db_path).parent / "fcm_token_storage.key"
        Path(self.db_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS companion_push_tokens(
              device_id TEXT PRIMARY KEY,
              token_ciphertext BLOB NOT NULL,
              nonce BLOB NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS companion_notification_dedup(
              device_id TEXT NOT NULL,
              approval_id TEXT NOT NULL,
              notification_type TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              sent_at TEXT,
              PRIMARY KEY(device_id, approval_id, notification_type)
            );
            """
        )
        self._db.commit()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _key(self, *, create: bool) -> bytes | None:
        self.key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.key_path.is_symlink():
            raise FCMConfigurationError("FCM token key must not be a symbolic link")
        if self.key_path.exists():
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise FCMConfigurationError("FCM token key has an invalid length")
            os.chmod(self.key_path, 0o600)
            return key
        if not create:
            return None
        key = secrets.token_bytes(32)
        temporary = self.key_path.with_name(f".{self.key_path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.key_path)
            except FileExistsError:
                key = self.key_path.read_bytes()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        os.chmod(self.key_path, 0o600)
        if len(key) != 32:
            raise FCMConfigurationError("FCM token key has an invalid length")
        return key

    @staticmethod
    def _validate_registration_token(token: str) -> str:
        if not isinstance(token, str) or not 20 <= len(token) <= _MAX_REGISTRATION_TOKEN:
            raise ValueError("registration token length is invalid")
        if any(ord(char) < 0x20 or ord(char) == 0x7f for char in token):
            raise ValueError("registration token contains control characters")
        return token

    def register(self, device_id: str, token: str) -> None:
        if not _DEVICE_ID_RE.fullmatch(device_id):
            raise ValueError("device_id is invalid")
        token = self._validate_registration_token(token)
        key = self._key(create=True)
        assert key is not None
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, token.encode("utf-8"), device_id.encode("utf-8"))
        now = _now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                device = self._db.execute(
                    "SELECT 1 FROM paired_devices WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                    (device_id, now),
                ).fetchone()
                if device is None:
                    raise ValueError("device is not active")
                self._db.execute(
                    "INSERT INTO companion_push_tokens(device_id,token_ciphertext,nonce,created_at,updated_at,revoked_at) VALUES(?,?,?,?,?,NULL) "
                    "ON CONFLICT(device_id) DO UPDATE SET token_ciphertext=excluded.token_ciphertext,nonce=excluded.nonce,updated_at=excluded.updated_at,revoked_at=NULL",
                    (device_id, ciphertext, nonce, now, now),
                )
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def revoke(self, device_id: str) -> bool:
        now = _now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute(
                    "UPDATE companion_push_tokens SET token_ciphertext=?,nonce=?,revoked_at=?,updated_at=? WHERE device_id=? AND revoked_at IS NULL",
                    (b"", b"", now, now, device_id),
                )
                self._db.commit()
                return cur.rowcount == 1
            except BaseException:
                self._db.rollback()
                raise

    def registration_token(self, device_id: str) -> str | None:
        now = _now()
        with self._lock:
            row = self._db.execute(
                "SELECT p.token_ciphertext,p.nonce FROM companion_push_tokens p JOIN paired_devices d ON d.id=p.device_id "
                "WHERE p.device_id=? AND p.revoked_at IS NULL AND d.revoked_at IS NULL AND d.expires_at>?",
                (device_id, now),
            ).fetchone()
        if row is None:
            return None
        key = self._key(create=False)
        if key is None:
            return None
        try:
            return AESGCM(key).decrypt(row["nonce"], row["token_ciphertext"], device_id.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise FCMConfigurationError("FCM token storage could not be decrypted") from exc

    def active_device_ids(self) -> list[str]:
        now = _now()
        with self._lock:
            rows = self._db.execute(
                "SELECT p.device_id FROM companion_push_tokens p JOIN paired_devices d ON d.id=p.device_id "
                "WHERE p.revoked_at IS NULL AND d.revoked_at IS NULL AND d.expires_at>? AND length(p.token_ciphertext)>0 ORDER BY p.device_id",
                (now,),
            ).fetchall()
        return [row[0] for row in rows]

    def reserve_notification(self, device_id: str, approval_id: str, notification_type: str) -> bool:
        now = _now()
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO companion_notification_dedup(device_id,approval_id,notification_type,status,created_at) VALUES(?,?,?,?,?)",
                    (device_id, approval_id, notification_type, "SENDING", now),
                )
                reserved = cur.rowcount == 1
                if not reserved:
                    cur = self._db.execute(
                        "UPDATE companion_notification_dedup SET status='SENDING',created_at=?,sent_at=NULL "
                        "WHERE device_id=? AND approval_id=? AND notification_type=? "
                        "AND (status='FAILED' OR (status='SENDING' AND created_at<?))",
                        (now, device_id, approval_id, notification_type, stale_before),
                    )
                    reserved = cur.rowcount == 1
                self._db.commit()
                return reserved
            except BaseException:
                self._db.rollback()
                raise

    def finish_notification(self, device_id: str, approval_id: str, notification_type: str, *, sent: bool) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE companion_notification_dedup SET status=?,sent_at=? WHERE device_id=? AND approval_id=? AND notification_type=? AND status='SENDING'",
                ("SENT" if sent else "FAILED", _now() if sent else None, device_id, approval_id, notification_type),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def service_account_path(config_dir: Path | None = None) -> Path | None:
    """Resolve configured service-account or local ADC files without logging their contents."""
    configured = os.environ.get("OPENJARVIS_FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append((config_dir or get_config_dir()) / "firebase-service-account.json")
    application_credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if application_credentials:
        candidates.append(Path(application_credentials).expanduser())
    candidates.append(Path.home() / ".config" / "gcloud" / "application_default_credentials.json")
    return next((path for path in candidates if path.is_file()), None)


@dataclass(frozen=True)
class FCMDeliveryResult:
    status: str


class FCMNotificationSender:
    """Optional FCM HTTP v1 sender. No caller is wired by default."""

    def __init__(
        self,
        token_store: FCMRegistrationStore,
        *,
        credentials_file: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
        access_token_provider: Callable[[Path], str] | None = None,
    ) -> None:
        self.token_store = token_store
        self.credentials_file = Path(credentials_file).expanduser() if credentials_file else service_account_path()
        self.transport = transport
        self.access_token_provider = access_token_provider

    def _service_account(self) -> dict[str, Any]:
        if self.credentials_file is None or not self.credentials_file.is_file():
            raise FCMConfigurationError("Firebase credentials are not configured")
        try:
            data = json.loads(self.credentials_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FCMConfigurationError("Firebase credentials are unavailable") from exc
        if not isinstance(data, dict):
            raise FCMConfigurationError("Firebase credentials are invalid")
        project_id = (
            data.get("project_id")
            or data.get("quota_project_id")
            or os.environ.get("OPENJARVIS_FIREBASE_PROJECT_ID", "")
            or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        )
        if not isinstance(project_id, str) or not project_id:
            try:
                import google.auth

                _, project_id = google.auth.default(scopes=[_FCM_SCOPE])
            except Exception as exc:
                raise FCMConfigurationError("Firebase project ID is unavailable") from exc
        if not isinstance(project_id, str) or not project_id:
            raise FCMConfigurationError("Firebase project ID is unavailable")
        data["project_id"] = project_id
        return data

    def _access_token(self, service_account: dict[str, Any]) -> str:
        assert self.credentials_file is not None
        if self.access_token_provider is not None:
            token = self.access_token_provider(self.credentials_file)
            if not isinstance(token, str) or not token:
                raise FCMConfigurationError("Firebase access token could not be obtained")
            return token
        try:
            from google.auth import default as default_credentials
            from google.auth.transport.requests import Request

            if service_account.get("type") == "service_account":
                from google.oauth2 import service_account as google_service_account

                credentials = google_service_account.Credentials.from_service_account_file(
                    str(self.credentials_file), scopes=[_FCM_SCOPE],
                )
            else:
                credentials, _ = default_credentials(scopes=[_FCM_SCOPE])
            credentials.refresh(Request())
            if not credentials.token:
                raise FCMConfigurationError("Firebase access token could not be obtained")
            return credentials.token
        except FCMConfigurationError:
            raise
        except Exception as exc:
            raise FCMConfigurationError("Firebase credentials could not be loaded") from exc

    def send_approval_pending(self, device_id: str, approval_id: str) -> FCMDeliveryResult:
        """Send one opaque approval hint, never command, arguments, or task content."""
        if not _DEVICE_ID_RE.fullmatch(device_id) or not _APPROVAL_ID_RE.fullmatch(approval_id):
            raise ValueError("device or approval ID is invalid")
        try:
            service_account = self._service_account()
            registration_token = self.token_store.registration_token(device_id)
            if not registration_token:
                return FCMDeliveryResult("not_registered")
            access_token = self._access_token(service_account)
        except FCMConfigurationError:
            return FCMDeliveryResult("disabled")
        notification_type = "approval_pending"
        if not self.token_store.reserve_notification(device_id, approval_id, notification_type):
            return FCMDeliveryResult("duplicate")

        payload = {"message": {"token": registration_token, "data": {"approval_id": approval_id, "type": notification_type}}}
        url = f"https://fcm.googleapis.com/v1/projects/{service_account['project_id']}/messages:send"
        try:
            with httpx.Client(transport=self.transport, timeout=10.0, trust_env=False, follow_redirects=False) as client:
                response = client.post(
                    url,
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                    json=payload,
                )
            sent = 200 <= response.status_code < 300
        except httpx.HTTPError:
            sent = False
        self.token_store.finish_notification(device_id, approval_id, notification_type, sent=sent)
        return FCMDeliveryResult("sent" if sent else "failed")


def dispatch_pending_approval(db_path: str, approval_id: str) -> None:
    """Best-effort dispatcher used by task/approval creation; no config means no-op."""
    credentials_file = service_account_path()
    if credentials_file is None:
        return
    token_store = FCMRegistrationStore(db_path)
    try:
        sender = FCMNotificationSender(token_store, credentials_file=credentials_file)
        for device_id in token_store.active_device_ids():
            sender.send_approval_pending(device_id, approval_id)
    finally:
        token_store.close()


__all__ = [
    "FCMConfigurationError",
    "FCMDeliveryResult",
    "FCMNotificationSender",
    "FCMRegistrationStore",
    "dispatch_pending_approval",
    "service_account_path",
]
