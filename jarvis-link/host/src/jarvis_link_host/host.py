"""Outbound Jarvis Link host processor and safe supervisor adapter boundary."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .crypto import (
    DeviceCertificate,
    DeviceIdentity,
    EnvelopeError,
    LinkEnvelope,
    decrypt_envelope,
    device_id_for_signing_key,
    encrypt_envelope,
    issue_device_certificate,
    verify_device_certificate,
)
from .replay import ReplayCache
from .supervisor import SupervisorPort
from .trusted_apps import is_safe_untrusted_app_label, resolve_trusted_app

_CAPABILITIES = {"read_only", "web_research", "desktop_observation", "assistant", "development"}
# The development scope is never granted by a message. Only companion devices listed here (written by the Mac
# operator, fail-closed when absent) may submit it.
DEVELOPMENT_ALLOWLIST_PATH = Path.home() / ".jarvis-link-test" / "development-companions.json"
_DEVELOPMENT_ALLOWLIST_LOCK = threading.RLock()


def development_companions(path: Path = DEVELOPMENT_ALLOWLIST_PATH) -> frozenset[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(item for item in data if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item))


def _read_development_allowlist(path: Path) -> set[str]:
    if path.is_symlink():
        raise ValueError("development allowlist must not be a symbolic link")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("development allowlist is unreadable or malformed") from exc
    if not isinstance(raw, list) or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{32}", item) for item in raw):
        raise ValueError("development allowlist contains invalid entries")
    return set(raw)


def _write_development_allowlist(path: Path, entries: set[str]) -> None:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or path.is_symlink():
        raise ValueError("development allowlist path must not traverse symbolic links")
    resolved_parent = parent.resolve(strict=True)
    if resolved_parent != parent.absolute():
        raise ValueError("development allowlist parent is not canonical")
    fd, temporary = tempfile.mkstemp(prefix=".development-companions-", suffix=".tmp", dir=resolved_parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(sorted(entries), separators=(",", ":"), ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(resolved_parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def authorize_development_companion(
    *,
    device_id: str,
    certificate: DeviceCertificate | Mapping[str, Any],
    trusted_host_signing_public_der: bytes,
    expected_tenant_id: str,
    expected_issuer_device_id: str,
    owner_authorized: bool,
    path: Path = DEVELOPMENT_ALLOWLIST_PATH,
    now_ms: int | None = None,
) -> bool:
    """Explicitly grant development scope to one already certified companion.

    The caller must first obtain the active certificate from the configured relay and
    must pass an explicit local-owner authorization. This changes only the development
    allowlist; it does not enroll, certify, or otherwise alter the companion identity.
    """
    if not owner_authorized:
        raise PermissionError("development enrollment requires explicit Mac-owner authorization")
    if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{32}", device_id):
        raise ValueError("development companion ID is invalid")
    verified = verify_device_certificate(
        certificate,
        trusted_host_signing_public_der=trusted_host_signing_public_der,
        expected_tenant_id=expected_tenant_id,
        expected_device_id=device_id,
        expected_role="companion",
        expected_issuer_device_id=expected_issuer_device_id,
        now_ms=now_ms,
    )
    with _DEVELOPMENT_ALLOWLIST_LOCK:
        entries = _read_development_allowlist(path)
        existed = device_id in entries
        entries.add(verified.device_id)
        if not existed:
            _write_development_allowlist(path, entries)
        return not existed


def revoke_development_companion(
    *, device_id: str, owner_authorized: bool, path: Path = DEVELOPMENT_ALLOWLIST_PATH
) -> bool:
    """Remove one companion's development scope without revoking its normal pairing."""
    if not owner_authorized:
        raise PermissionError("development revocation requires explicit Mac-owner authorization")
    if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{32}", device_id):
        raise ValueError("development companion ID is invalid")
    with _DEVELOPMENT_ALLOWLIST_LOCK:
        entries = _read_development_allowlist(path)
        if device_id not in entries:
            return False
        entries.remove(device_id)
        _write_development_allowlist(path, entries)
        return True
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEYS = {"command", "cmd", "shell", "shell_command", "script", "executable", "exec"}


class TaskEnvelopeError(EnvelopeError):
    pass


class ReplayDetected(EnvelopeError):
    """A valid envelope was already processed by this host process."""


def _reject_executable_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise TaskEnvelopeError("remote task envelope contains a prohibited executable field")
            _reject_executable_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_executable_fields(child)


def _task_submit_payload(payload: Mapping[str, Any]) -> tuple[str, list[str]]:
    if set(payload) != {"objective", "capability_scope"}:
        _reject_executable_fields(payload)
        raise TaskEnvelopeError("task.submit accepts only objective and capability_scope")
    _reject_executable_fields(payload)
    objective = payload.get("objective")
    scope = payload.get("capability_scope")
    if not isinstance(objective, str) or not objective.strip() or len(objective.encode("utf-8")) > 4096:
        raise TaskEnvelopeError("task objective must be 1–4096 characters")
    if not isinstance(scope, list) or not scope or any(not isinstance(item, str) for item in scope):
        raise TaskEnvelopeError("capability_scope must be a non-empty string array")
    if len(set(scope)) != len(scope) or not set(scope).issubset(_CAPABILITIES):
        raise TaskEnvelopeError("task capability scope contains an unsupported capability")
    return objective.strip(), scope


def _trusted_app_result(command: str, display_name: str, bundle_id: str,
                        result: Any) -> dict[str, Any]:
    """Convert a supervisor app result without manufacturing execution success."""
    if not isinstance(result, dict):
        raise TaskEnvelopeError("supervisor returned an invalid app activation result")
    state = result.get("state")
    if state == "COMPLETED":
        if (result.get("executed") is not True or result.get("success") is not True
                or result.get("bundle_id") != bundle_id):
            raise TaskEnvelopeError("supervisor did not verify the trusted app activation")
        summary = result.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise TaskEnvelopeError("supervisor returned no verified app activation summary")
        return {
            "kind": "fast.result",
            "command": command,
            "state": "COMPLETED",
            "bundle_id": bundle_id,
            "executed": True,
            "success": True,
            "summary": summary.strip()[:500],
        }
    if state == "FAILED":
        summary = result.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = f"Could not open {display_name}. Check that it is installed and try again."
        return {
            "kind": "fast.result",
            "command": command,
            "state": "FAILED",
            "bundle_id": bundle_id,
            "executed": result.get("executed") is True,
            "success": False,
            "summary": summary.strip()[:500],
        }
    if state in {"EXECUTION_PENDING", "RUNNING", "INDETERMINATE"}:
        task_id = result.get("task_id")
        summary = result.get("summary")
        if (not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id)
                or not isinstance(summary, str) or not summary.strip()):
            raise TaskEnvelopeError("supervisor returned an incomplete in-flight app result")
        return {
            "kind": "fast.result",
            "command": command,
            "state": state,
            "task_id": task_id,
            "bundle_id": bundle_id,
            "summary": summary.strip()[:500],
        }
    if state == "WAITING_APPROVAL":
        task_id = result.get("task_id")
        approval = result.get("approval")
        if (not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id)
                or not isinstance(approval, dict)
                or approval.get("task_id") != task_id
                or approval.get("operation") != "mcp:jarvis_mac:open_app"
                or approval.get("target") not in {bundle_id, display_name}
                or not isinstance(approval.get("approval_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", approval["approval_id"])
                or not isinstance(approval.get("canonical_arguments_digest"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", approval["canonical_arguments_digest"])
                or type(approval.get("expires_at_unix_ms")) is not int
                or approval["expires_at_unix_ms"] <= 0):
            raise TaskEnvelopeError("supervisor returned an app approval with a mismatched target")
        result_task = task_id
        summary = result.get("summary")
        safe_approval = {
            "approval_id": approval["approval_id"],
            "task_id": task_id,
            "operation": "mcp:jarvis_mac:open_app",
            "target": approval["target"],
            "canonical_arguments_digest": approval["canonical_arguments_digest"],
            "expires_at_unix_ms": approval["expires_at_unix_ms"],
            "redacted_summary": (
                approval["redacted_summary"].strip()[:500]
                if isinstance(approval.get("redacted_summary"), str) and approval["redacted_summary"].strip()
                else "Review the exact app activation before approving."
            ),
        }
        return {
            "kind": "fast.result",
            "command": command,
            "state": "WAITING_APPROVAL",
            "task_id": result_task,
            "bundle_id": bundle_id,
            "approval": safe_approval,
            "summary": (summary.strip()[:500] if isinstance(summary, str) and summary.strip()
                        else f"Jarvis is waiting for approval to open {display_name}."),
        }
    raise TaskEnvelopeError("supervisor returned an unsupported app activation state")


class JarvisLinkHost:
    """Processes inbound encrypted envelopes; never runs arbitrary commands."""
    def __init__(
        self,
        *,
        tenant_id: str,
        device_id: str,
        identity: DeviceIdentity,
        host_certificate: DeviceCertificate | Mapping[str, Any],
        supervisor: SupervisorPort,
        replay_cache: ReplayCache | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.device_id = device_id
        self.identity = identity
        self.host_certificate = verify_device_certificate(
            host_certificate,
            trusted_host_signing_public_der=identity.signing_public_der,
            expected_tenant_id=tenant_id,
            expected_device_id=device_id,
            expected_role="host",
            expected_issuer_device_id=device_id,
        )
        if device_id_for_signing_key(identity.signing_public_der) != device_id:
            raise EnvelopeError("host ID does not match its signing key")
        self.supervisor = supervisor
        self.replay_cache = replay_cache or ReplayCache()

    def receive(
        self,
        raw_envelope: Mapping[str, Any],
        *,
        sender_certificate: DeviceCertificate | Mapping[str, Any],
        expected_sender_id: str,
        now_ms: int | None = None,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        envelope = LinkEnvelope.from_dict(raw_envelope)
        if envelope.from_device_id != expected_sender_id:
            raise EnvelopeError("sender ID does not match the authenticated device")
        if envelope.to_device_id != self.device_id or envelope.tenant_id != self.tenant_id:
            raise EnvelopeError("envelope addressed to a different tenant or device")
        payload = decrypt_envelope(
            envelope,
            recipient=self.identity,
            sender_certificate=sender_certificate,
            trusted_host_signing_public_der=self.identity.signing_public_der,
            expected_tenant_id=self.tenant_id,
            expected_recipient_id=self.device_id,
            expected_recipient_certificate_id=self.host_certificate.certificate_id,
            expected_sender_id=expected_sender_id,
            expected_issuer_device_id=self.device_id,
            now_ms=now_ms,
        )
        current = int(time.time() * 1000) if now_ms is None else now_ms
        if not self.replay_cache.claim(envelope.message_id, envelope.expires_at_unix_ms, now_ms=current) and not allow_duplicate:
            raise ReplayDetected("message was already processed")
        result = self._dispatch(envelope, payload, now_ms=current)
        result_message_id = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {
            "message_id": envelope.message_id,
            "status": "processed",
            "result": result,
            "result_digest": result_message_id,
        }

    def certify_companion(
        self, *, signing_public_b64u: str, encryption_public_b64u: str,
        now_ms: int | None = None,
    ) -> DeviceCertificate:
        """Sign a companion key binding only after the caller's local approval."""
        return issue_device_certificate(
            host=self.identity,
            tenant_id=self.tenant_id,
            host_device_id=self.device_id,
            signing_public_b64u=signing_public_b64u,
            encryption_public_b64u=encryption_public_b64u,
            device_role="companion",
            now_ms=now_ms,
        )

    def encrypt_reply(
        self, *, recipient_certificate: DeviceCertificate | Mapping[str, Any],
        recipient_id: str, message_id: str, kind: str, payload: Mapping[str, Any],
        issued_at_unix_ms: int, expires_at_unix_ms: int,
    ) -> LinkEnvelope:
        """Encrypt host results/progress to a host-certified companion key."""
        return encrypt_envelope(
            sender=self.identity,
            recipient_certificate=recipient_certificate,
            trusted_host_signing_public_der=self.identity.signing_public_der,
            tenant_id=self.tenant_id,
            message_id=message_id,
            from_device_id=self.device_id,
            to_device_id=recipient_id,
            kind=kind,
            payload=payload,
            issued_at_unix_ms=issued_at_unix_ms,
            expires_at_unix_ms=expires_at_unix_ms,
            expected_recipient_role="companion",
        )

    def _dispatch(self, envelope: LinkEnvelope, payload: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
        if envelope.kind == "fast.command":
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise TaskEnvelopeError("fast command version is invalid")
            command = payload.get("command")
            handler = getattr(self.supervisor, "fast_command", None)
            if command == "mac.status" and set(payload) == {"command", "version"}:
                if handler is None:
                    raise TaskEnvelopeError("supervisor fast status is unavailable")
                result = handler(command=command, requester_device_id=envelope.from_device_id)
                if (not isinstance(result, dict) or result.get("state") != "COMPLETED"
                        or result.get("executed") is not True or result.get("success") is not True
                        or type(result.get("online")) is not bool or type(result.get("ai_available")) is not bool
                        or not isinstance(result.get("summary"), str) or not result["summary"].strip()):
                    raise TaskEnvelopeError("supervisor returned an invalid Mac status result")
                return {
                    "kind": "fast.result", "command": command, "state": "COMPLETED",
                    "executed": True, "success": True,
                    "online": result["online"], "ai_available": result["ai_available"],
                    "summary": result["summary"].strip()[:500],
                }
            if command == "mac.battery" and set(payload) == {"command", "version"}:
                if handler is None:
                    raise TaskEnvelopeError("supervisor fast battery status is unavailable")
                result = handler(command=command, requester_device_id=envelope.from_device_id)
                if not isinstance(result, dict):
                    raise TaskEnvelopeError("supervisor returned an invalid battery status")
                if result.get("state") == "FAILED":
                    if result.get("success") is not False or not isinstance(result.get("summary"), str) or not result["summary"].strip():
                        raise TaskEnvelopeError("supervisor returned an incomplete battery status failure")
                    return {"kind": "fast.result", "command": command, "state": "FAILED",
                            "success": False, "summary": result["summary"].strip()[:500]}
                if (result.get("state") != "COMPLETED" or result.get("executed") is not True
                        or result.get("success") is not True or type(result.get("battery_percent")) is not int
                        or not 0 <= result["battery_percent"] <= 100
                        or not isinstance(result.get("summary"), str) or not result["summary"].strip()
                        or (result.get("charging") is not None and type(result.get("charging")) is not bool)):
                    raise TaskEnvelopeError("supervisor returned an invalid battery status")
                output = {"kind": "fast.result", "command": command, "state": "COMPLETED",
                          "executed": True, "success": True,
                          "battery_percent": result["battery_percent"], "summary": result["summary"].strip()[:500]}
                if "charging" in result:
                    output["charging"] = result["charging"]
                return output
            if command == "task.status" and set(payload) == {"command", "task_id", "version"}:
                task_id = payload.get("task_id")
                if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
                    raise TaskEnvelopeError("fast task status ID is invalid")
                self._require_task_owner(envelope.from_device_id, task_id)
                if handler is None:
                    raise TaskEnvelopeError("typed task status is unavailable")
                result = handler(command=command, task_id=task_id,
                                 requester_device_id=envelope.from_device_id)
                result.pop("task_id", None)
                return {"kind": "fast.result", "command": command, "queried_task_id": task_id, **result,
                        "state": "COMPLETED", "summary": f"Task status: {result.get('state', 'unknown')}."}
            if command == "app.open" and set(payload) == {"app", "command", "idempotency_key", "version"}:
                app_value = payload.get("app")
                if payload.get("idempotency_key") != envelope.message_id:
                    raise TaskEnvelopeError("fast command idempotency does not match signed envelope")
                trusted_app = resolve_trusted_app(app_value)
                if trusted_app is not None:
                    if handler is None:
                        raise TaskEnvelopeError("supervisor fast application activation is unavailable")
                    # Only the canonical bundle ID crosses the host/supervisor boundary.
                    # No caller-provided path or arguments can reach the executor.
                    result = handler(command=command, app=trusted_app.bundle_id,
                                     requester_device_id=envelope.from_device_id,
                                     idempotency_key=envelope.message_id)
                    output = _trusted_app_result(command, trusted_app.display_name,
                                                 trusted_app.bundle_id, result)
                    task_id = output.get("task_id")
                    observer = getattr(self, "task_observer", None)
                    if isinstance(task_id, str) and observer is not None:
                        observer(task_id=task_id, idempotency_key=envelope.message_id,
                                 companion_id=envelope.from_device_id)
                    return output

                # An unknown but plain app label is not implicitly trusted. Route it
                # through the normal assistant/supervisor path so the existing
                # exact-operation approval and sandbox policy remain authoritative.
                if not is_safe_untrusted_app_label(app_value):
                    raise TaskEnvelopeError("untrusted app target must be a plain application name")
                objective = (
                    f"Open the installed application named {app_value!r} in the foreground. "
                    "This app is outside Jarvis's trusted automatic-launch list. Use only "
                    "the existing app-open capability and require its normal exact-operation "
                    "approval before activating it. Do not install software, run commands, "
                    "open paths, change settings, or take other actions."
                )
                accepted = self.supervisor.submit_task(
                    objective=objective,
                    capability_scope=["assistant"],
                    idempotency_key=envelope.message_id,
                    requester_device_id=envelope.from_device_id,
                )
                task_id = accepted.get("task_id") if isinstance(accepted, dict) else None
                if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
                    raise RuntimeError("supervisor returned an invalid app-review task")
                observer = getattr(self, "task_observer", None)
                if observer is not None:
                    observer(task_id=task_id, idempotency_key=envelope.message_id,
                             companion_id=envelope.from_device_id)
                state = accepted.get("state")
                return {
                    "kind": "task.accepted",
                    "task_id": task_id,
                    "state": state if isinstance(state, str) else "ACCEPTED",
                    "summary": "Target is not trusted for automatic launch; sent for normal supervisor review.",
                }
            raise TaskEnvelopeError("unsupported typed fast command")

        if envelope.kind == "task.submit":
            objective, capabilities = _task_submit_payload(payload)
            if "development" in capabilities and (capabilities != ["development"] or envelope.from_device_id not in development_companions()):
                raise TaskEnvelopeError("development scope is not enabled for this companion")
            result = self.supervisor.submit_task(
                objective=objective,
                capability_scope=capabilities,
                idempotency_key=envelope.message_id,
                requester_device_id=envelope.from_device_id,
            )
            if not isinstance(result, dict) or not isinstance(result.get("task_id"), str):
                raise RuntimeError("supervisor returned an invalid task result")
            # Lets the serve loop later publish the task's final state to the same companion.
            observer = getattr(self, "task_observer", None)
            if observer is not None:
                observer(task_id=result["task_id"], idempotency_key=envelope.message_id,
                         companion_id=envelope.from_device_id)
            return {"kind": "task.accepted", **result}

        if envelope.kind == "task.cancel":
            if set(payload) != {"task_id", "idempotency_key"}:
                raise TaskEnvelopeError("task.cancel payload has invalid fields")
            task_id = payload["task_id"]
            if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
                raise TaskEnvelopeError("task.cancel has invalid task_id")
            idem = payload["idempotency_key"]
            if not isinstance(idem, str) or not 1 <= len(idem) <= 128 or "\n" in idem or "\r" in idem:
                raise TaskEnvelopeError("task.cancel idempotency key is invalid")
            self._require_task_owner(envelope.from_device_id, task_id)
            result = self.supervisor.cancel_task(task_id, envelope.message_id)
            return {"kind": "task.cancelled", **result}

        if envelope.kind == "approval.decision":
            required = {"task_id", "approval_id", "decision", "operation_digest", "idempotency_key"}
            optional = {"continuation_objective"}
            if not required.issubset(payload) or set(payload) - (required | optional):
                raise TaskEnvelopeError("approval.decision payload has invalid fields")
            task_id, approval_id = payload["task_id"], payload["approval_id"]
            decision, digest, idem = payload["decision"], payload["operation_digest"], payload["idempotency_key"]
            continuation_objective = payload.get("continuation_objective")
            for label, value in (("task_id", task_id), ("approval_id", approval_id)):
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
                    raise TaskEnvelopeError(f"approval.decision has invalid {label}")
            if decision not in {"approve", "reject"}:
                raise TaskEnvelopeError("approval decision must be approve or reject")
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                raise TaskEnvelopeError("approval decision digest is invalid")
            if not isinstance(idem, str) or not 1 <= len(idem) <= 128 or "\n" in idem:
                raise TaskEnvelopeError("approval decision idempotency key is invalid")
            if continuation_objective is not None and (
                not isinstance(continuation_objective, str)
                or not continuation_objective.strip()
                or len(continuation_objective.encode("utf-8")) > 4096
            ):
                raise TaskEnvelopeError("approval continuation objective is invalid")
            self._require_task_owner(envelope.from_device_id, task_id)
            approval = self.supervisor.get_approval(approval_id)
            if approval is None or approval.task_id != task_id:
                raise TaskEnvelopeError("approval does not match task")
            if approval.status != "PENDING" or approval.expires_at_unix_ms <= now_ms:
                raise TaskEnvelopeError("approval is expired or no longer pending")
            if approval.arguments_digest != digest:
                raise TaskEnvelopeError("approval operation digest does not match")
            result = self.supervisor.decide_approval(
                task_id=task_id,
                approval_id=approval_id,
                decision=decision,
                expected_arguments_digest=digest,
                idempotency_key=idem,
                requester_device_id=envelope.from_device_id,
                continuation_objective=continuation_objective,
            )
            if (not isinstance(result, dict)
                    or result.get("task_id") != task_id
                    or result.get("approval_id") != approval_id
                    or not isinstance(result.get("status"), str)):
                raise TaskEnvelopeError("supervisor returned an approval result with mismatched identity")
            # The host binds the authenticated decision from the signed request
            # into the encrypted response. The client uses it to reconcile only
            # the matching local card; it is not proof that an approved operation
            # has completed.
            return {**result, "kind": "approval.decision_result", "task_id": task_id,
                    "approval_id": approval_id, "decision": decision}

        raise TaskEnvelopeError(f"host does not accept message kind {envelope.kind}")

    def _require_task_owner(self, companion_id: str, task_id: str) -> None:
        """Fail closed unless the caller provides a durable ownership authorizer."""
        authorizer = getattr(self, "task_authorizer", None)
        if authorizer is None or not authorizer(task_id=task_id, companion_id=companion_id):
            raise TaskEnvelopeError("task is not owned by this companion")
