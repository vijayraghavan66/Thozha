"""Outbound Jarvis Link host processor and safe supervisor adapter boundary."""
from __future__ import annotations

import hashlib
import json
import re
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

_CAPABILITIES = {"read_only", "web_research", "desktop_observation", "assistant", "development"}
# The development scope is never granted by a message. Only companion devices listed here (written by the Mac
# operator, fail-closed when absent) may submit it.
DEVELOPMENT_ALLOWLIST_PATH = Path.home() / ".jarvis-link-test" / "development-companions.json"


def development_companions(path: Path = DEVELOPMENT_ALLOWLIST_PATH) -> frozenset[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(item for item in data if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item))
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
        if envelope.kind == "task.submit":
            objective, capabilities = _task_submit_payload(payload)
            if "development" in capabilities and (capabilities != ["development"] or envelope.from_device_id not in development_companions()):
                raise TaskEnvelopeError("development scope is not enabled for this companion")
            result = self.supervisor.submit_task(
                objective=objective,
                capability_scope=capabilities,
                idempotency_key=envelope.message_id,
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
                continuation_objective=continuation_objective,
            )
            return {"kind": "approval.decision_result", **result}

        raise TaskEnvelopeError(f"host does not accept message kind {envelope.kind}")
