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
from .trusted_apps import resolve_trusted_app

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_ALLOWED_SCOPES = {"read_only", "assistant", "development"}
_PLAN_APPROVAL_OPERATION = "mcp:jarvis_dev:approve_plan"
_PLAN_KEYS = {
    "plan_id", "device_id", "user_id", "workspace_root", "allowed_ops", "allowed_paths",
    "max_file_bytes", "max_total_bytes", "max_writes", "max_test_runs", "max_repairs",
    "max_duration_s", "ttl_s",
}
_MAX_PLAN_DETAIL_BYTES = 16 * 1024


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _verified_development_plan(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete canonical plan against both the plan and supervisor digests."""
    arguments = row.get("canonical_arguments")
    if not isinstance(arguments, dict) or set(arguments) != {"plan", "plan_digest"}:
        raise SupervisorRefused("development approval is missing its complete canonical plan")
    plan = arguments.get("plan")
    plan_digest = arguments.get("plan_digest")
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS or not isinstance(plan_digest, str) or not _DIGEST_RE.fullmatch(plan_digest):
        raise SupervisorRefused("development approval plan fields are incomplete or invalid")
    if (
        not isinstance(plan.get("plan_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{8,80}", plan["plan_id"])
        or not isinstance(plan.get("device_id"), str)
        or not _ID_RE.fullmatch(plan["device_id"])
        or not isinstance(plan.get("user_id"), str)
        or not isinstance(plan.get("workspace_root"), str)
        or not plan["workspace_root"].startswith("/")
        or not isinstance(plan.get("allowed_ops"), list)
        or not all(isinstance(value, str) for value in plan["allowed_ops"])
        or not isinstance(plan.get("allowed_paths"), list)
        or not all(isinstance(value, str) for value in plan["allowed_paths"])
        or len(plan["allowed_paths"]) > 24
        or not plan["allowed_ops"]
        or plan["allowed_ops"] != sorted(set(plan["allowed_ops"]))
        or not set(plan["allowed_ops"]).issubset({"write_file", "run_tests", "artifact_manifest"})
    ):
        raise SupervisorRefused("development approval scope fields are invalid")
    if plan["user_id"] != "device:" + plan["device_id"]:
        raise SupervisorRefused("development approval user is not bound to its paired device")
    for relative in plan["allowed_paths"]:
        if relative == "":
            continue
        if relative.startswith(("/", "~")) or "\\" in relative or "\x00" in relative:
            raise SupervisorRefused("development approval contains a non-relative path")
        parts = relative.split("/")
        if any(part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", part) for part in parts):
            raise SupervisorRefused("development approval contains an invalid path restriction")
    integer_fields = ("max_file_bytes", "max_total_bytes", "max_writes", "max_test_runs", "max_repairs", "max_duration_s", "ttl_s")
    if any(type(plan.get(field)) is not int for field in integer_fields):
        raise SupervisorRefused("development approval quotas are invalid")
    if (
        not 1 <= plan["max_file_bytes"] <= 256 * 1024
        or not 1 <= plan["max_total_bytes"] <= 4 * 1024 * 1024
        or not 0 <= plan["max_writes"] <= 200
        or not 0 <= plan["max_test_runs"] <= 50
        or not 0 <= plan["max_repairs"] <= 5
        or not 1 <= plan["max_duration_s"] <= 4 * 60 * 60
        or not 1 <= plan["ttl_s"] <= 4 * 60 * 60
    ):
        raise SupervisorRefused("development approval quota or lifetime is out of bounds")
    encoded_plan = _canonical_json(plan)
    if len(encoded_plan.encode("utf-8")) > _MAX_PLAN_DETAIL_BYTES:
        raise SupervisorRefused("complete development plan is too large to display safely")
    if hashlib.sha256(encoded_plan.encode("utf-8")).hexdigest() != plan_digest:
        raise SupervisorRefused("development plan details do not match their plan digest")
    operation = row.get("operation")
    target = row.get("target_resource")
    expected_target = f"taskflow:plan:{plan['plan_id']}"
    if operation != _PLAN_APPROVAL_OPERATION or target != expected_target:
        raise SupervisorRefused("development plan operation or target does not match its canonical fields")
    canonical_arguments_digest = row.get("operation_digest")
    operation_payload = {"operation": operation, "target": target, "arguments": arguments}
    if hashlib.sha256(_canonical_json(operation_payload).encode("utf-8")).hexdigest() != canonical_arguments_digest:
        raise SupervisorRefused("development plan details do not match the supervisor approval digest")
    return {**plan, "plan_digest": plan_digest}


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


def _submission_digest(objective: str, capability_scope: list[str], requester_device_id: str) -> str:
    """Identifies one exact submission without storing the objective text."""
    canonical = json.dumps({"objective": objective, "capability_scope": capability_scope,
                            "requester_device_id": requester_device_id},
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
        async_submission: bool = False,
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
        self.async_submission = bool(async_submission)
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

    def submit_task(self, *, objective: str, capability_scope: list[str], idempotency_key: str,
                    requester_device_id: str) -> dict[str, Any]:
        if not isinstance(objective, str) or not objective.strip() or len(objective.encode("utf-8")) > 4096:
            raise SupervisorRefused("task objective is invalid")
        if len(capability_scope) != 1 or capability_scope[0] not in _ALLOWED_SCOPES:
            raise SupervisorRefused("task capability scope is unsupported")
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise SupervisorRefused("task idempotency key is invalid")
        if not _ID_RE.fullmatch(requester_device_id or ""):
            raise SupervisorRefused("task requester device ID is invalid")
        digest = _submission_digest(objective.strip(), capability_scope, requester_device_id)
        # Bind the key before posting, including when multiple workers race.
        known = self._ledger.reserve(idempotency_key, digest)
        submit_path = "/v1/jarvis/link/tasks?mode=async" if self.async_submission else "/v1/jarvis/link/tasks"
        status, data = self._request("POST", submit_path, {
            "objective": objective.strip(),
            "capability_scope": capability_scope,
            "idempotency_key": idempotency_key,
            "requester_device_id": requester_device_id,
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
        # Persist server acceptance before returning to the host receive loop.
        # A process crash after HTTP 202 can be reconciled only by this exact
        # idempotency key and digest; it must never submit a second AI run.
        self._ledger.put(idempotency_key, {"digest": digest, "task_id": task_id})
        if self.async_submission and status == 202 and state.upper() in {"CREATED", "QUEUED", "PLANNING", "RUNNING", "VERIFYING"}:
            return {
                "task_id": task_id,
                "state": state.upper(),
                "summary": str(data.get("summary") or "")[:1000],
                "result_text": "",
                "result_digest": None,
                "http_status": status,
                "simulated": False,
            }
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

    def task_events(self, idempotency_key: str, *, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise SupervisorRefused("task idempotency key is invalid")
        if after_seq < 0 or not 1 <= limit <= 200:
            raise SupervisorRefused("task event cursor is invalid")
        path = f"/v1/jarvis/link/tasks/{idempotency_key}/events?after_seq={after_seq}&limit={limit}"
        _, data = self._request("GET", path)
        events = data.get("events")
        task_id = data.get("task_id")
        next_seq = data.get("next_seq")
        if (not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id)
                or not isinstance(events, list) or not isinstance(next_seq, int)):
            raise SupervisorRefused("OpenJarvis returned an invalid task event page")
        safe_events = []
        for event in events:
            if not isinstance(event, dict):
                raise SupervisorRefused("OpenJarvis returned a malformed task event")
            seq, event_type, payload = event.get("seq"), event.get("event_type"), event.get("payload")
            if not isinstance(seq, int) or not isinstance(event_type, str) or not isinstance(payload, dict):
                raise SupervisorRefused("OpenJarvis returned a malformed task event")
            safe_events.append({"seq": seq, "event_type": event_type, "payload": payload})
        return {"task_id": task_id, "events": safe_events, "next_seq": next_seq}

    def fast_command(self, *, command: str, requester_device_id: str, idempotency_key: str | None = None,
                     task_id: str | None = None, app: str | None = None) -> dict[str, Any]:
        """Dispatch only the versioned, allowlisted deterministic command contract to loopback."""
        if not _ID_RE.fullmatch(requester_device_id or ""):
            raise SupervisorRefused("fast command requester is invalid")
        body: dict[str, Any] = {"command": command, "requester_device_id": requester_device_id}
        if command == "task.status":
            if not re.fullmatch(r"[0-9a-f]{32}", task_id or ""):
                raise SupervisorRefused("fast task ID is invalid")
            body["task_id"] = task_id
        elif command == "mac.battery":
            pass
        elif command == "app.open":
            if not re.fullmatch(r"[0-9a-f]{32}", idempotency_key or ""):
                raise SupervisorRefused("fast command idempotency key is invalid")
            trusted_app = resolve_trusted_app(app)
            if trusted_app is None or app != trusted_app.bundle_id:
                raise SupervisorRefused("fast app activation requires a canonical trusted bundle ID")
            body.update({"app": app, "idempotency_key": idempotency_key})
        elif command != "mac.status":
            raise SupervisorRefused("fast command is not supported")
        _, data = self._request("POST", "/v1/jarvis/link/fast-command", body)
        if data.get("command") != command or not isinstance(data.get("state"), str):
            raise SupervisorRefused("OpenJarvis returned an invalid fast command result")
        if command == "mac.status":
            if (data.get("state") != "COMPLETED" or data.get("executed") is not True
                    or data.get("success") is not True or type(data.get("online")) is not bool
                    or type(data.get("ai_available")) is not bool
                    or not isinstance(data.get("summary"), str) or not data["summary"].strip()):
                raise SupervisorRefused("OpenJarvis returned an invalid Mac status result")
            return {k: data[k] for k in ("state", "executed", "success", "online", "ai_available", "summary")}
        if command == "mac.battery":
            if data.get("state") == "FAILED":
                if data.get("success") is not False or not isinstance(data.get("summary"), str) or not data["summary"].strip():
                    raise SupervisorRefused("OpenJarvis returned an incomplete battery failure")
                return {"state": "FAILED", "success": False, "summary": data["summary"].strip()[:500]}
            if (data.get("state") != "COMPLETED" or data.get("executed") is not True
                    or data.get("success") is not True or type(data.get("battery_percent")) is not int
                    or not 0 <= data["battery_percent"] <= 100
                    or not isinstance(data.get("summary"), str) or not data["summary"].strip()
                    or (data.get("charging") is not None and type(data.get("charging")) is not bool)):
                raise SupervisorRefused("OpenJarvis returned an invalid battery status result")
            result = {"state": "COMPLETED", "executed": True, "success": True,
                      "battery_percent": data["battery_percent"], "summary": data["summary"].strip()[:500]}
            if "charging" in data:
                result["charging"] = data["charging"]
            return result
        if command == "app.open":
            if data.get("bundle_id") != app:
                raise SupervisorRefused("OpenJarvis app result is bound to a different bundle ID")
            state = data["state"]
            if state == "COMPLETED":
                if data.get("executed") is not True or data.get("success") is not True:
                    raise SupervisorRefused("OpenJarvis did not confirm app activation")
                if not isinstance(data.get("summary"), str) or not data["summary"].strip():
                    raise SupervisorRefused("OpenJarvis returned no verified app result")
                return {k: data[k] for k in ("state", "bundle_id", "executed", "success", "summary")}
            if state == "FAILED":
                if data.get("success") is not False or not isinstance(data.get("summary"), str) or not data["summary"].strip():
                    raise SupervisorRefused("OpenJarvis returned an incomplete app failure")
                return {k: data[k] for k in ("state", "bundle_id", "executed", "success", "summary") if k in data}
            if state in {"EXECUTION_PENDING", "RUNNING", "INDETERMINATE"}:
                if (not isinstance(data.get("summary"), str) or not data["summary"].strip()):
                    raise SupervisorRefused("OpenJarvis returned an incomplete in-flight app result")
                return {"state": state, "bundle_id": app, "request_id": idempotency_key,
                        "executed": False, "success": False,
                        "summary": data["summary"].strip()[:500]}
            if state == "WAITING_APPROVAL":
                task = data.get("task_id")
                approval = data.get("approval")
                if not isinstance(task, str) or not _ID_RE.fullmatch(task) or not isinstance(approval, dict):
                    raise SupervisorRefused("OpenJarvis returned an invalid trusted-app approval")
                approval_id = approval.get("approval_id")
                approval_digest = approval.get("canonical_arguments_digest")
                approval_expiry = approval.get("expires_at_unix_ms")
                if (approval.get("task_id") != task
                        or approval.get("operation") != "mcp:jarvis_mac:open_app"
                        or approval.get("target") not in (app, trusted_app.display_name)
                        or not isinstance(approval_id, str) or not _ID_RE.fullmatch(approval_id)
                        or not isinstance(approval_digest, str) or not _DIGEST_RE.fullmatch(approval_digest)
                        or type(approval_expiry) is not int or approval_expiry <= 0):
                    raise SupervisorRefused("OpenJarvis returned an approval with invalid scope or expiry")
                summary = approval.get("redacted_summary")
                if not isinstance(summary, str) or not summary.strip():
                    summary = "Review the exact app activation before approving."
                safe_approval = {
                    "approval_id": approval_id,
                    "task_id": task,
                    "operation": "mcp:jarvis_mac:open_app",
                    "target": approval["target"],
                    "canonical_arguments_digest": approval_digest,
                    "expires_at_unix_ms": approval_expiry,
                    "redacted_summary": summary.strip()[:500],
                }
                result = {"state": state, "bundle_id": app, "task_id": task,
                          "approval": safe_approval}
                if isinstance(data.get("summary"), str):
                    result["summary"] = data["summary"].strip()[:500]
                return result
            raise SupervisorRefused("OpenJarvis returned an unsupported app activation state")
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
            pending_item = {
                "approval_id": approval_id,
                "task_id": task_id,
                "operation": operation,
                "target": target,
                "canonical_arguments_digest": digest,
                "expires_at_unix_ms": _expiry_ms(expiry),
            }
            if operation == _PLAN_APPROVAL_OPERATION:
                pending_item["redacted_summary"] = "Review the complete bounded development plan below before approving."
                pending_item["development_plan"] = _verified_development_plan(row)
            else:
                pending_item["redacted_summary"] = (
                    str(row.get("reason") or "Exact operation requires your approval.")
                    + " Arguments: " + json.dumps(args, sort_keys=True, ensure_ascii=False)
                )[:500]
            pending.append(pending_item)
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
        requester_device_id: str,
        continuation_objective: str | None = None,
    ) -> dict[str, Any]:
        if not _ID_RE.fullmatch(task_id) or not _ID_RE.fullmatch(approval_id) or decision not in {"approve", "reject"} or not _DIGEST_RE.fullmatch(expected_arguments_digest):
            raise SupervisorRefused("approval decision fields are invalid")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128 or "\n" in idempotency_key or "\r" in idempotency_key:
            raise SupervisorRefused("approval decision idempotency key is invalid")
        if not _ID_RE.fullmatch(requester_device_id or ""):
            raise SupervisorRefused("approval requester device ID is invalid")
        body: dict[str, Any] = {
            "task_id": task_id,
            "requester_device_id": requester_device_id,
            "decision": decision,
            "expected_operation_digest": expected_arguments_digest,
            "idempotency_key": idempotency_key,
        }
        if continuation_objective is not None:
            if not continuation_objective.strip() or len(continuation_objective.encode("utf-8")) > 4096:
                raise SupervisorRefused("approval continuation objective is invalid")
            body["continuation_objective"] = continuation_objective.strip()
        _, data = self._request("POST", f"/v1/jarvis/approvals/{approval_id}/decision", body)
        execution_state = data.get("execution_state") or data.get("task_state") or data.get("state") or data.get("status", "UNKNOWN")
        if not isinstance(execution_state, str):
            raise SupervisorRefused("OpenJarvis returned an invalid approval execution state")
        summary = data.get("result_summary") or data.get("summary") or ""
        if not isinstance(summary, str):
            raise SupervisorRefused("OpenJarvis returned an invalid approval result summary")
        executed = data.get("executed")
        success = data.get("success")
        continued = data.get("continued")
        if any(value is not None and type(value) is not bool for value in (executed, success, continued)):
            raise SupervisorRefused("OpenJarvis returned invalid approval execution flags")
        return {
            "task_id": task_id,
            "approval_id": approval_id,
            "decision": decision,
            "status": data.get("status", "UNKNOWN"),
            "state": execution_state,
            "execution_state": execution_state,
            "summary": summary[:1000],
            "executed": executed is True,
            "continued": continued is True,
            "success": success,
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
