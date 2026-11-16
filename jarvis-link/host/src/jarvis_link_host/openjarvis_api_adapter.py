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
import tomllib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .openjarvis_adapter import SupervisorRefused
from .supervisor import ApprovalRecord

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_ALLOWED_SCOPES = {"read_only", "assistant"}


class SupervisorUnavailable(RuntimeError):
    """Transient local API failure; caller should keep the encrypted relay message queued."""


def _expiry_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class OpenJarvisApiSupervisorAdapter:
    """SupervisorPort client restricted to an authenticated loopback API."""

    def __init__(
        self,
        *,
        key_file: str | Path | None = None,
        config_file: str | Path | None = None,
        base_url: str = "http://127.0.0.1:8000",
        http_client: httpx.Client | None = None,
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

    def _api_key(self) -> str:
        if self.key_file.exists():
            if self.key_file.is_symlink() or not self.key_file.is_file():
                raise SupervisorUnavailable("OpenJarvis supervisor credential is invalid")
            try:
                mode = stat.S_IMODE(self.key_file.stat().st_mode)
                key = self.key_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SupervisorUnavailable("OpenJarvis supervisor credential is unavailable") from exc
            if mode & 0o077 or not key or "\n" in key or "\r" in key:
                raise SupervisorUnavailable("OpenJarvis supervisor credential permissions are invalid")
            return key
        environment_key = os.environ.get("OPENJARVIS_API_KEY", "").strip()
        if environment_key:
            return environment_key
        if self.config_file.is_file() and not self.config_file.is_symlink():
            try:
                config = tomllib.loads(self.config_file.read_text(encoding="utf-8"))
                key = str(config.get("server", {}).get("auth", {}).get("api_key", "")).strip()
            except (OSError, tomllib.TOMLDecodeError, AttributeError) as exc:
                raise SupervisorUnavailable("OpenJarvis supervisor credential configuration is unavailable") from exc
            if key and "\n" not in key and "\r" not in key:
                return key
        raise SupervisorUnavailable("OpenJarvis supervisor credential is unavailable")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
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
        status, data = self._request("POST", "/v1/jarvis/link/tasks", {
            "objective": objective.strip(),
            "capability_scope": capability_scope,
            "idempotency_key": idempotency_key,
        })
        task_id = data.get("task_id")
        state = data.get("state")
        if not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id) or not isinstance(state, str):
            raise SupervisorRefused("OpenJarvis supervisor returned an invalid task record")
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
        return {"task_id": task_id, "state": state.upper(), "summary": str(data.get("summary") or "")[:1000], "result_digest": data.get("result_digest"), "http_status": status}

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
        raise SupervisorRefused("remote task cancellation is not exposed by the OpenJarvis supervisor")

    def report_progress(self, task_id: str, stage: str, result_digest: str = "") -> dict[str, Any]:
        if not _ID_RE.fullmatch(task_id):
            raise SupervisorRefused("task ID is invalid")
        return {"task_id": task_id, "stage": stage[:64], "result_digest": result_digest[:64], "status": "local progress is maintained by the supervisor"}

    def close(self) -> None:
        if self._owned_client:
            self._client.close()


__all__ = ["OpenJarvisApiSupervisorAdapter", "SupervisorUnavailable"]
