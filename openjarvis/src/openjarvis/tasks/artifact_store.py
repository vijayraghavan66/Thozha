"""Private, task/device-bound ZIP storage for capability-authorized VM artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import zipfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

MAX_BUNDLE_BYTES = 5 * 1024 * 1024
MAX_ARTIFACTS_PER_TASK = 5
RETENTION_SECONDS = 7 * 24 * 60 * 60
_TASK_ID = re.compile(r"^[0-9a-f]{32}$")
_DEVICE_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}$")


class ArtifactStoreError(RuntimeError):
    """A sanitized artifact-store validation or persistence error."""


class ArtifactNotFoundError(ArtifactStoreError):
    """No archive exists for this exact task and paired device."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Stored archive bytes no longer match their durable metadata."""


def _validate_relative(path: Any) -> str:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise ArtifactStoreError("artifact path is empty or too long")
    if "\x00" in path or "\\" in path or path.startswith(("/", "~")):
        raise ArtifactStoreError("artifact path must be relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or not _SEGMENT.fullmatch(part) for part in parts):
        raise ArtifactStoreError("artifact path contains a disallowed segment")
    return "/".join(parts)


def _no_symlink_components(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return False
        except FileNotFoundError:
            continue
        except OSError:
            return False
    return True


def _utc_after(seconds: int, now: float) -> str:
    return datetime.fromtimestamp(now + seconds, timezone.utc).isoformat()


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    task_id: str
    device_id: str
    file_name: str
    size_bytes: int
    archive_sha256: str
    manifest_sha256: str
    manifest: tuple[dict[str, Any], ...]
    created_at: str
    expires_at: str

    def to_wire(self, *, download_path: str = "") -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "archive_sha256": self.archive_sha256,
            "manifest_sha256": self.manifest_sha256,
            "files": list(self.manifest),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "download_path": download_path,
        }


class ArtifactStore:
    """Stores bounded archives below a private root and verifies every read."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        db_path: str | os.PathLike[str] | None = None,
        *,
        clock=time.time,
    ) -> None:
        home = Path.home()
        self.root = Path(root or (home / ".openjarvis" / "artifacts")).expanduser()
        self.db_path = Path(db_path or (home / ".openjarvis" / "artifact_store.sqlite3")).expanduser()
        self.clock = clock
        self._lock = threading.RLock()
        if not self.root.is_absolute() or not self.db_path.is_absolute():
            raise ArtifactStoreError("artifact store paths must be absolute")
        if not _no_symlink_components(self.root) or not _no_symlink_components(self.db_path):
            raise ArtifactStoreError("artifact store paths must not contain symlinks")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        os.chmod(self.db_path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS task_artifacts(
                artifact_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                archive_sha256 TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE(task_id, manifest_sha256)
            )"""
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ArtifactStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def create_bundle(
        self,
        *,
        task_id: str,
        device_id: str,
        export: dict[str, Any],
    ) -> StoredArtifact:
        if not _TASK_ID.fullmatch(task_id) or not _DEVICE_ID.fullmatch(device_id):
            raise ArtifactStoreError("task or device identity is invalid")
        files = export.get("file_manifest")
        payloads = export.get("payloads")
        total = export.get("bytes")
        manifest_digest = export.get("manifest_sha256")
        if (
            not isinstance(files, list)
            or not isinstance(payloads, dict)
            or not isinstance(total, int)
            or total < 0
            or total > MAX_BUNDLE_BYTES
            or not isinstance(manifest_digest, str)
            or not _SHA256.fullmatch(manifest_digest)
        ):
            raise ArtifactStoreError("authorized artifact export receipt is malformed")
        normalized: list[dict[str, Any]] = []
        normalized_payloads: dict[str, bytes] = {}
        if len(files) > 2000:
            raise ArtifactStoreError("artifact count exceeds the store limit")
        for item in sorted(files, key=lambda entry: str(entry.get("path", ""))):
            if not isinstance(item, dict):
                raise ArtifactStoreError("artifact manifest entry is malformed")
            path = _validate_relative(item.get("path"))
            if path.casefold() == "manifest.json":
                raise ArtifactStoreError("project file collides with the generated manifest")
            data = payloads.get(path)
            if not isinstance(data, bytes):
                raise ArtifactStoreError("artifact payload is missing")
            digest = hashlib.sha256(data).hexdigest()
            if (
                item.get("bytes") != len(data)
                or item.get("sha256") != digest
                or len(data) > MAX_BUNDLE_BYTES
            ):
                raise ArtifactStoreError("artifact payload did not match its authorized manifest")
            normalized.append({"path": path, "bytes": len(data), "sha256": digest})
            normalized_payloads[path] = data
        if set(payloads) != set(normalized_payloads) or sum(len(x) for x in normalized_payloads.values()) != total:
            raise ArtifactStoreError("artifact export total or path set does not match its manifest")
        expected_manifest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if expected_manifest != manifest_digest:
            raise ArtifactStoreError("artifact manifest digest does not match its files")

        artifact_id = hashlib.sha256(f"{task_id}:{device_id}:{manifest_digest}".encode()).hexdigest()
        manifest_doc = {
            "format": "jarvis-artifact-manifest-v1",
            "task_id": task_id,
            "artifact_id": artifact_id,
            "manifest_sha256": manifest_digest,
            "files": normalized,
        }
        manifest_bytes = json.dumps(manifest_doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            self._write_zip_entry(bundle, "MANIFEST.json", manifest_bytes)
            for path in sorted(normalized_payloads):
                self._write_zip_entry(bundle, path, normalized_payloads[path])
        archive_bytes = archive.getvalue()
        if len(archive_bytes) > MAX_BUNDLE_BYTES:
            raise ArtifactStoreError("compressed archive exceeds the delivery size limit")
        archive_sha = hashlib.sha256(archive_bytes).hexdigest()
        file_name = f"jarvis-project-{artifact_id[:16]}.zip"
        created = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        expires = _utc_after(RETENTION_SECONDS, self.clock())
        task_dir = self.root / task_id
        try:
            task_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            task_dir_mode = os.lstat(task_dir).st_mode
        except OSError as exc:
            raise ArtifactStoreError("artifact task directory could not be inspected") from exc
        if stat.S_ISLNK(task_dir_mode) or not stat.S_ISDIR(task_dir_mode) or not _no_symlink_components(task_dir):
            raise ArtifactStoreError("artifact task directory contains a symlink")
        os.chmod(task_dir, 0o700, follow_symlinks=False)
        path = task_dir / file_name

        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT * FROM task_artifacts WHERE task_id=? AND manifest_sha256=?",
                (task_id, manifest_digest),
            ).fetchone()
            if existing:
                prior = self._row_to_artifact(existing)
                self.download(task_id, device_id, prior.artifact_id)
                return prior
            count = self._db.execute("SELECT COUNT(*) FROM task_artifacts WHERE task_id=?", (task_id,)).fetchone()[0]
            if count >= MAX_ARTIFACTS_PER_TASK:
                raise ArtifactStoreError("task artifact retention limit is exhausted")
            temp_fd, temp_name = tempfile.mkstemp(prefix=".jarvis-artifact-", suffix=".tmp", dir=task_dir)
            created_file = False
            try:
                os.fchmod(temp_fd, 0o600)
                with os.fdopen(temp_fd, "wb") as stream:
                    stream.write(archive_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists() or path.is_symlink():
                    raise ArtifactStoreError("artifact output path already exists")
                os.replace(temp_name, path)
                created_file = True
                dir_fd = os.open(task_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                self._db.execute(
                    "INSERT INTO task_artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (artifact_id, task_id, device_id, file_name, len(archive_bytes), archive_sha,
                     manifest_digest, json.dumps(normalized, sort_keys=True, separators=(",", ":")), created, expires),
                )
            except Exception:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
                if created_file:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                raise
        row = self._db.execute("SELECT * FROM task_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        assert row is not None
        return self._row_to_artifact(row)

    @staticmethod
    def _write_zip_entry(bundle: zipfile.ZipFile, name: str, data: bytes) -> None:
        safe = _validate_relative(name)
        entry = zipfile.ZipInfo(safe, date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.external_attr = 0o600 << 16
        bundle.writestr(entry, data)

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> StoredArtifact:
        try:
            manifest = json.loads(row["manifest_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArtifactStoreError("stored artifact manifest is invalid") from exc
        return StoredArtifact(
            artifact_id=row["artifact_id"], task_id=row["task_id"], device_id=row["device_id"],
            file_name=row["file_name"], size_bytes=row["size_bytes"], archive_sha256=row["archive_sha256"],
            manifest_sha256=row["manifest_sha256"], manifest=tuple(manifest),
            created_at=row["created_at"], expires_at=row["expires_at"],
        )

    def list_for_task(self, task_id: str, device_id: str) -> list[StoredArtifact]:
        self._validate_identity(task_id, device_id)
        with self._lock:
            self._expire_task(task_id)
            rows = self._db.execute(
            "SELECT * FROM task_artifacts WHERE task_id=? AND device_id=? ORDER BY created_at",
            (task_id, device_id),
            ).fetchall()
            return [self._row_to_artifact(row) for row in rows]

    def download(self, task_id: str, device_id: str, artifact_id: str) -> tuple[StoredArtifact, bytes]:
        self._validate_identity(task_id, device_id)
        with self._lock:
            self._expire_task(task_id)
            row = self._db.execute(
            "SELECT * FROM task_artifacts WHERE artifact_id=? AND task_id=? AND device_id=?",
            (artifact_id, task_id, device_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFoundError("artifact was not found for this task and device")
            artifact = self._row_to_artifact(row)
            task_dir = self.root / task_id
            file_path = task_dir / artifact.file_name
            if not _no_symlink_components(file_path):
                raise ArtifactStoreError("artifact path contains a symlink")
            try:
                fd = os.open(file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError as exc:
                raise ArtifactNotFoundError("stored artifact file is missing") from exc
            except OSError as exc:
                raise ArtifactIntegrityError("stored artifact file could not be opened safely") from exc
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != artifact.size_bytes or metadata.st_size > MAX_BUNDLE_BYTES:
                    raise ArtifactIntegrityError("stored artifact size is invalid")
                chunks = []
                remaining = MAX_BUNDLE_BYTES + 1
                while remaining:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
            finally:
                os.close(fd)
            if hashlib.sha256(data).hexdigest() != artifact.archive_sha256:
                raise ArtifactIntegrityError("stored artifact checksum does not match")
            return artifact, data

    def _expire_task(self, task_id: str) -> None:
        now = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        rows = self._db.execute(
            "SELECT file_name FROM task_artifacts WHERE task_id=? AND expires_at<=?",
            (task_id, now),
        ).fetchall()
        for row in rows:
            path = self.root / task_id / row["file_name"]
            try:
                if _no_symlink_components(path):
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        self._db.execute("DELETE FROM task_artifacts WHERE task_id=? AND expires_at<=?", (task_id, now))
        self._db.commit()

    @staticmethod
    def _validate_identity(task_id: str, device_id: str) -> None:
        if not _TASK_ID.fullmatch(task_id) or not _DEVICE_ID.fullmatch(device_id):
            raise ArtifactStoreError("task or device identity is invalid")
