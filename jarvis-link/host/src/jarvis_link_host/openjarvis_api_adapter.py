"""Loopback-only HTTP adapter to the running OpenJarvis supervisor.

The Mac API key is read on the Mac and is sent only to 127.0.0.1. It is never
included in a Link envelope or sent to the relay. The relay host uses this
adapter instead of opening the supervisor SQLite database in a second process.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import tomllib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .openjarvis_adapter import SupervisorRefused
from .supervisor import ApprovalRecord

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_ALLOWED_SCOPES = {"read_only", "assistant", "development"}


def _result_text(data: Mapping[str, Any]) -> str:
    """The canonical completed answer. Never shortened here; older supervisors fall back to their summary."""
    value = data.get("result_text")
    if isinstance(value, str):
        return value
    return str(data.get("summary") or "")


class SupervisorUnavailable(RuntimeError):
    """Transient local API failure; caller should keep the encrypted relay message queued."""


def _expiry_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _submission_digest(objective: str, capability_scope: list[str]) -> str:
    """Identifies one exact submission without storing the objective text."""
    canonical = json.dumps({"objective": objective, "capability_scope": capability_scope},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _SubmissionLedger:
    """Maps each idempotency key to the digest it was first submitted with and the task it created.

    A replayed key is accepted only when its digest matches, so a key reused for a different request is refused.
    Holds digests and task IDs only. With no path it is kept in memory for the life of the adapter.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SupervisorUnavailable("submission ledger is unreadable") from exc
            if not isinstance(loaded, dict):
                raise SupervisorUnavailable("submission ledger is invalid")
            self._entries = loaded

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._entries.get(key)

    def key_for_task(self, task_id: str) -> str | None:
        with self._lock:
            matches = [key for key, entry in self._entries.items() if entry.get("task_id") == task_id]
            return matches[0] if len(matches) == 1 else None

    def reserve(self, key: str, digest: str) -> dict[str, Any] | None:
        """Bind a key to one digest atomically before any network request."""
        with self._lock:
            known = self._entries.get(key)
            if known is not None and known.get("digest") != digest:
                raise SupervisorRefused("task idempotency key was already used for a different request")
            if known is None:
                self._entries[key] = {"digest": digest, "task_id": None}
                self._save()
            return known

    def put(self, key: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = entry
            self._save()

    def discard(self, key: str) -> None:
        with self._lock:
            if self._entries.pop(key, None) is not None:
                self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self._entries, indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)


class OpenJarvisApiSupervisorAdapter:
    """SupervisorPort client restricted to an authenticated loopback API."""

    def __init__(
        self,
        *,
        key_file: str | Path | None = None,
        config_file: str | Path | None = None,
        base_url: str = "http://127.0.0.1:8000",
        http_client: httpx.Client | None = None,
        submission_ledger: str | Path | None = None,
        strict_recovery: bool = False,
    ) -> None:
        parsed = urlsplit(base_url)
        try:
            loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            loopback = False
        if parsed.scheme != "http" or not loopback or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("OpenJarvis supervisor URL must be a plain HTTP loopback origin")
        self.base_url = base_url.rstrip("/")
        self.key_file = Path(key_file or (Path.home() / ".openjarvis" / "supervisor_api_key")).expanduser()
        self.config_file = Path(config_file or (Path.home() / ".openjarvis" / "config.toml")).expanduser()
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(180.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._owned_client = http_client is None
        self._strict_recovery = strict_recovery
        self._ledger = _SubmissionLedger(Path(submission_ledger).expanduser() if submission_ledger else None)

    def _api_key(self) -> str:
        if self.key_file.exists():
            if self.key_file.is_symlink() or not self.key_file.is_file():
                raise SupervisorUnavailable("OpenJarvis supervisor credential is invalid")
            try:
                metadata = self.key_file.stat()
                mode = stat.S_IMODE(metadata.st_mode)
                key = self.key_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SupervisorUnavailable("OpenJarvis supervisor credential is unavailable") from exc
            if metadata.st_uid != os.getuid() or mode & 0o077 or not key or "\n" in key or "\r" in key:
                raise SupervisorUnavailable("OpenJarvis supervisor credential permissions are invalid")
            return key
        environment_key = os.environ.get("OPENJARVIS_API_KEY", "").strip()
        if environment_key:
            return environment_key
        if self.config_file.is_file() and not self.config_file.is_symlink():
            try:
                metadata = self.config_file.stat()
                if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                    raise SupervisorUnavailable("OpenJarvis credential configuration permissions are invalid")
                config = tomllib.loads(self.config_file.read_text(encoding="utf-8"))
                key = str(config.get("server", {}).get("auth", {}).get("api_key", "")).strip()
            except SupervisorUnavailable:
                raise
            except (OSError, tomllib.TOMLDecodeError, AttributeError) as exc:
                raise SupervisorUnavailable("OpenJarvis supervisor credential configuration is unavailable") from exc
            if key and "\n" not in key and "\r" not in key:
                return key
        raise SupervisorUnavailable("OpenJarvis supervisor credential is unavailable")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 *, accept_statuses: frozenset[int] = frozenset()) -> tuple[int, dict[str, Any]]:
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("invalid supervisor API path")
        headers = {"Authorization": f"Bearer {self._api_key()}", "Accept": "application/json"}
        try:
            response = self._client.request(method, self.base_url + path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise SupervisorUnavailable("OpenJarvis supervisor is unavailable") from exc
        try:
            data = response.json()
        except ValueError as exc:
            if response.status_code >= 500 or response.status_code in {408, 425, 429}:
                raise SupervisorUnavailable("OpenJarvis supervisor returned a transient error") from exc
            raise SupervisorRefused("OpenJarvis supervisor returned an invalid response") from exc
        if not isinstance(data, dict):
            raise SupervisorRefused("OpenJarvis supervisor returned an invalid response")
        if response.status_code >= 500 or response.status_code in {408, 425, 429}:
            raise SupervisorUnavailable(f"OpenJarvis supervisor returned HTTP {response.status_code}")
        if response.status_code in accept_statuses:
            return response.status_code, data
        if response.status_code >= 400:
            # Never relay upstream detail, which could contain a private objective.
            raise SupervisorRefused(f"OpenJarvis supervisor refused the request (HTTP {response.status_code})")
        return response.status_code, data

    def submit_task(self, *, objective: str, capability_scope: list[str], idempotency_key: str) -> dict[str, Any]:
        if not isinstance(objective, str) or not objective.strip() or len(objective.encode("utf-8")) > 4096:
            raise SupervisorRefused("task objective is invalid")
        if len(capability_scope) != 1 or capability_scope[0] not in _ALLOWED_SCOPES:
            raise SupervisorRefused("task capability scope is unsupported")
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise SupervisorRefused("task idempotency key is invalid")
        digest = _submission_digest(objective.strip(), capability_scope)
        # Bind the key before posting, including when multiple workers race.
        known = self._ledger.reserve(idempotency_key, digest)
        status, data = self._request("POST", "/v1/jarvis/link/tasks", {
            "objective": objective.strip(),
            "capability_scope": capability_scope,
            "idempotency_key": idempotency_key,
        }, accept_statuses=frozenset({409}))
        if status == 409:
            # The supervisor already holds this key. Accept it only when this ledger recorded the same digest before
            # posting, so the existing task is the one this request created. Otherwise the key is not ours: drop the
            # record made above and refuse.
            if self._strict_recovery and (known is None or not known.get("task_id")):
                # The local digest was recorded before POST, but a crash or
                # conflicting request can occur before we learn the task ID.
                # GET status does not prove the server bound that ID to these
                # exact arguments, so leave the relay request unacknowledged.
                raise SupervisorUnavailable("task submission outcome is uncertain; manual reconciliation is required")
            if known is None:
                self._ledger.discard(idempotency_key)
                raise SupervisorRefused("task idempotency key replay could not be verified")
            current = self.task_status(idempotency_key)
            if known.get("task_id") and current["task_id"] != known["task_id"]:
                raise SupervisorRefused("task idempotency key replay does not match the recorded task")
            self._ledger.put(idempotency_key, {"digest": digest, "task_id": current["task_id"]})
            return {**current, "simulated": False, "replayed": True}
        task_id = data.get("task_id")
        state = data.get("state")
        if not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id) or not isinstance(state, str):
            raise SupervisorRefused("OpenJarvis supervisor returned an invalid task record")
        self._ledger.put(idempotency_key, {"digest": digest, "task_id": task_id})
        if status == 202 and state.upper() in {"CREATED", "PLANNING", "RUNNING", "VERIFYING"}:
            deadline = time.monotonic() + 240.0
            delay = 1.0
            while time.monotonic() < deadline:
                time.sleep(delay)
                current = self.task_status(idempotency_key)
                if current["state"] not in {"CREATED", "PLANNING", "RUNNING", "VERIFYING"}:
                    return {**current, "simulated": False}
                delay = min(delay * 2.0, 12.0)
            raise SupervisorUnavailable("OpenJarvis task is still running; relay delivery will retry idempotently")
        return {
            "task_id": task_id,
            "state": state.upper(),
            "summary": str(data.get("summary") or "")[:1000],
            "result_text": _result_text(data),
            "result_digest": data.get("result_digest"),
            "http_status": status,
            "simulated": False,
        }

    def task_status(self, idempotency_key: str) -> dict[str, Any]:
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise SupervisorRefused("task idempotency key is invalid")
        status, data = self._request("GET", "/v1/jarvis/link/tasks/" + idempotency_key)
        task_id = data.get("task_id")
        state = data.get("state")
        if not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id) or not isinstance(state, str):
            raise SupervisorRefused("OpenJarvis supervisor returned an invalid task status")
        return {"task_id": task_id, "state": state.upper(), "summary": str(data.get("summary") or "")[:1000], "result_text": _result_text(data), "result_digest": data.get("result_digest"), "http_status": status}

    def fast_command(self, *, command: str, requester: str, idempotency_key: str | None = None,
                     task_id: str | None = None, app: str | None = None) -> dict[str, Any]:
        """Use the authenticated loopback supervisor for fixed, non-LLM operations only."""
        if not re.fullmatch(r"[0-9a-f]{32}", requester or ""):
            raise SupervisorRefused("fast command requester is invalid")
        body: dict[str, Any] = {"command": command, "requester": requester}
        if command == "task.status":
            if not re.fullmatch(r"[0-9a-f]{32}", task_id or ""):
                raise SupervisorRefused("fast task ID is invalid")
            body["task_id"] = task_id
        elif command == "app.open" and app == "Calculator":
            if not re.fullmatch(r"[0-9a-f]{32}", idempotency_key or ""):
                raise SupervisorRefused("fast command idempotency key is invalid")
            body.update({"app": app, "idempotency_key": idempotency_key})
        elif command != "mac.status":
            raise SupervisorRefused("fast command is not supported")
        _, data = self._request("POST", "/v1/jarvis/link/fast-command", body)
        if data.get("command") != command or not isinstance(data.get("state"), str):
            raise SupervisorRefused("OpenJarvis returned an invalid fast command result")
        if command == "app.open":
            approval = data.get("approval")
            task = data.get("task_id")
            if (not isinstance(task, str) or not _ID_RE.fullmatch(task) or data["state"] != "WAITING_APPROVAL"
                    or not isinstance(approval, dict) or approval.get("task_id") != task
                    or approval.get("operation") != "mcp:jarvis_mac:open_app"
                    or approval.get("target") != "Calculator"):
                raise SupervisorRefused("OpenJarvis returned an invalid Calculator approval")
        return {k: data[k] for k in ("state", "online", "ai_available", "task_id", "summary", "approval") if k in data}

    def fast_command(self, *, command: str, requester: str, idempotency_key: str | None = None,
                     task_id: str | None = None, app: str | None = None) -> dict[str, Any]:
        """Dispatch only the versioned, allowlisted deterministic command contract to loopback."""
        if not re.fullmatch(r"[0-9a-f]{32}", requester or ""):
            raise SupervisorRefused("fast command requester is invalid")
        body: dict[str, Any] = {"command": command, "requester": requester}
        if command == "task.status":
            if not re.fullmatch(r"[0-9a-f]{32}", task_id or ""):
                raise SupervisorRefused("fast task ID is invalid")
            body["task_id"] = task_id
        elif command == "app.open" and app == "Calculator":
            if not re.fullmatch(r"[0-9a-f]{32}", idempotency_key or ""):
                raise SupervisorRefused("fast command idempotency key is invalid")
            body.update({"app": app, "idempotency_key": idempotency_key})
        elif command != "mac.status":
            raise SupervisorRefused("fast command is not supported")
        _, data = self._request("POST", "/v1/jarvis/link/fast-command", body)
        if data.get("command") != command or not isinstance(data.get("state"), str):
            raise SupervisorRefused("OpenJarvis returned an invalid fast command result")
        if command == "app.open":
            task = data.get("task_id")
            approval = data.get("approval")
            if (not isinstance(task, str) or not _ID_RE.fullmatch(task) or data["state"] != "WAITING_APPROVAL"
                    or not isinstance(approval, dict) or approval.get("task_id") != task
                    or approval.get("operation") != "mcp:jarvis_mac:open_app"
                    or approval.get("target") != "Calculator"):
                raise SupervisorRefused("OpenJarvis returned an invalid Calculator approval")
        return {k: data[k] for k in ("state", "online", "ai_available", "task_id", "summary", "approval") if k in data}

    def list_pending_approvals(self, task_id: str) -> list[dict[str, Any]]:
        if not _ID_RE.fullmatch(task_id):
            raise SupervisorRefused("task ID is invalid")
        _, data = self._request("GET", "/v1/jarvis/approvals/pending?limit=100")
        rows = data.get("approvals")
        if not isinstance(rows, list):
            raise SupervisorRefused("OpenJarvis supervisor returned an invalid approval list")
        pending = []
        for row in rows:
            if not isinstance(row, dict) or row.get("task_id") != task_id:
                continue
            approval_id = row.get("id")
            operation = row.get("operation")
            target = row.get("target_resource")
            digest = row.get("operation_digest")
            expiry = row.get("expires_at")
            if not isinstance(approval_id, str) or not _ID_RE.fullmatch(approval_id):
                raise SupervisorRefused("OpenJarvis supervisor returned an invalid approval ID")
            if not isinstance(operation, str) or not isinstance(target, str) or not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest) or not isinstance(expiry, str):
                raise SupervisorRefused("OpenJarvis supervisor returned an incomplete approval record")
            args = row.get("canonical_arguments", {})
            pending.append({
                "approval_id": approval_id,
                "task_id": task_id,
                "operation": operation,
                "target": target,
                "canonical_arguments_digest": digest,
                "expires_at_unix_ms": _expiry_ms(expiry),
                "redacted_summary": (str(row.get("reason") or "Exact operation requires your approval.") + " Arguments: " + json.dumps(args, sort_keys=True, ensure_ascii=False))[:500],
            })
        return pending

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        if not _ID_RE.fullmatch(approval_id):
            return None
        try:
            _, row = self._request("GET", f"/v1/jarvis/approvals/{approval_id}")
        except SupervisorRefused as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return ApprovalRecord(
            approval_id=row["id"],
            task_id=row["task_id"],
            operation=row["operation"],
            target=row["target_resource"],
            arguments_digest=row["operation_digest"],
            expires_at_unix_ms=_expiry_ms(row["expires_at"]),
            status=row.get("status", "PENDING"),
        )

    def decide_approval(
        self,
        *,
        task_id: str,
        approval_id: str,
        decision: str,
        expected_arguments_digest: str,
        idempotency_key: str,
        continuation_objective: str | None = None,
    ) -> dict[str, Any]:
        if not _ID_RE.fullmatch(task_id) or not _ID_RE.fullmatch(approval_id) or decision not in {"approve", "reject"} or not _DIGEST_RE.fullmatch(expected_arguments_digest):
            raise SupervisorRefused("approval decision fields are invalid")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128 or "\n" in idempotency_key or "\r" in idempotency_key:
            raise SupervisorRefused("approval decision idempotency key is invalid")
        body: dict[str, Any] = {
            "task_id": task_id,
            "decision": decision,
            "expected_operation_digest": expected_arguments_digest,
            "idempotency_key": idempotency_key,
        }
        if continuation_objective is not None:
            if not continuation_objective.strip() or len(continuation_objective.encode("utf-8")) > 4096:
                raise SupervisorRefused("approval continuation objective is invalid")
            body["continuation_objective"] = continuation_objective.strip()
        _, data = self._request("POST", f"/v1/jarvis/approvals/{approval_id}/decision", body)
        return {
            "task_id": task_id,
            "approval_id": approval_id,
            "status": data.get("status", "UNKNOWN"),
            "state": data.get("task_state") or data.get("state") or data.get("status", "UNKNOWN"),
            "summary": str(data.get("result_summary") or "")[:1000],
            "executed": bool(data.get("executed")),
            "continued": bool(data.get("continued")),
            "success": data.get("success"),
            "simulated": False,
        }

    def cancel_task(self, task_id: str, idempotency_key: str) -> dict[str, Any]:
        if not self._strict_recovery or not _ID_RE.fullmatch(task_id):
            raise SupervisorRefused("remote task cancellation is not enabled")
        submission_key = self._ledger.key_for_task(task_id)
        if submission_key is None or not _IDEMPOTENCY_RE.fullmatch(submission_key):
            raise SupervisorRefused("task submission mapping is unavailable")
        _, data = self._request("POST", f"/v1/jarvis/link/tasks/{submission_key}/cancel")
        if data.get("task_id") != task_id or not isinstance(data.get("state"), str):
            raise SupervisorRefused("OpenJarvis supervisor returned an invalid cancellation record")
        return {"task_id": task_id, "state": data["state"].upper(),
                "summary": str(data.get("summary") or "")[:1000],
                "action_may_have_started": data.get("action_may_have_started") is True}

    def report_progress(self, task_id: str, stage: str, result_digest: str = "") -> dict[str, Any]:
        if not _ID_RE.fullmatch(task_id):
            raise SupervisorRefused("task ID is invalid")
        return {"task_id": task_id, "stage": stage[:64], "result_digest": result_digest[:64], "status": "local progress is maintained by the supervisor"}

    def close(self) -> None:
        if self._owned_client:
            self._client.close()


__all__ = ["OpenJarvisApiSupervisorAdapter", "SupervisorUnavailable"]
