"""Outbound polling loop that bridges encrypted Link envelopes to SupervisorPort."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .crypto import DeviceCertificate, EnvelopeError, b64u
from .host import JarvisLinkHost
from typing import Any

from .client import RelayClient


class HostRelayRuntime:
    """One-shot poll/dispatch/response/ACK cycle for tests and local simulation."""
    def __init__(self, *, host: JarvisLinkHost, relay: RelayClient) -> None:
        self.host = host
        self.relay = relay

    def send_approval_request(self, *, companion_certificate: dict, companion_id: str, request: dict[str, Any],
                              now_ms: int | None = None) -> str:
        """Encrypts an approval.request to one certified companion. Fields follow protocol/v1.md."""
        issued = int(time.time() * 1000) if now_ms is None else now_ms
        expires = min(int(request["expires_at_unix_ms"]), issued + 24 * 60 * 60 * 1000)
        payload = {
            "approval_id": request["approval_id"],
            "task_id": request["task_id"],
            "operation": request["operation"],
            "target": request["target"],
            "canonical_arguments_digest": request["canonical_arguments_digest"],
            "expires_at_unix_ms": int(request["expires_at_unix_ms"]),
            "redacted_summary": request["redacted_summary"][:500],
        }
        message_id = uuid.uuid4().hex
        envelope = self.host.encrypt_reply(
            recipient_certificate=companion_certificate, recipient_id=companion_id, message_id=message_id,
            kind="approval.request", payload=payload, issued_at_unix_ms=issued, expires_at_unix_ms=expires,
        )
        self.relay.submit_envelope(envelope.to_dict())
        return message_id

    def send_task_update(self, *, companion_certificate: dict, companion_id: str, task_id: str,
                         state: str, summary: str) -> str:
        """Sends a task.result update (flat task_id/state/summary) to one certified companion."""
        issued = int(time.time() * 1000)
        payload = {"in_reply_to": "", "status": state.lower(), "task_id": task_id, "state": state,
                   "summary": summary[:200], "result_digest": ""}
        message_id = uuid.uuid4().hex
        response = self.host.encrypt_reply(
            recipient_certificate=companion_certificate, recipient_id=companion_id, message_id=message_id,
            kind="task.result", payload=payload, issued_at_unix_ms=issued, expires_at_unix_ms=issued + 60 * 60 * 1000,
        )
        self.relay.submit_envelope(response.to_dict())
        return message_id

    def _reply_refused(self, envelope, cert, message_id: str) -> None:
        issued = int(time.time() * 1000)
        payload = {
            "in_reply_to": message_id, "status": "refused", "task_id": "", "state": "REFUSED",
            "summary": "Request refused by the supervisor", "result_digest": "",
        }
        response = self.host.encrypt_reply(
            recipient_certificate=cert, recipient_id=envelope["from_device_id"], message_id=uuid.uuid4().hex,
            kind="task.result", payload=payload, issued_at_unix_ms=issued, expires_at_unix_ms=issued + 60 * 60 * 1000,
        )
        self.relay.submit_envelope(response.to_dict())

    def poll_once(self, *, limit: int = 20, now_ms: int | None = None) -> dict[str, int]:
        """Poll over outbound HTTPS and process each encrypted message once."""
        if self.relay.device_id != self.host.device_id or self.relay.tenant_id != self.host.tenant_id:
            raise ValueError("relay client identity does not match host")
        records = self.relay.inbox(device_id=self.host.device_id, limit=limit)
        counts = {"received": 0, "rejected": 0, "replied": 0}
        for record in records:
            envelope = record.get("envelope")
            cert = record.get("sender_certificate")
            if not isinstance(envelope, dict) or not isinstance(cert, dict):
                continue
            message_id = envelope.get("message_id")
            if not isinstance(message_id, str):
                continue
            try:
                result = self.host.receive(
                    envelope,
                    sender_certificate=cert,
                    expected_sender_id=str(envelope.get("from_device_id", "")),
                    now_ms=now_ms,
                )
                body = result["result"]
                payload = {
                    "in_reply_to": message_id,
                    "status": result["status"],
                    "result": body,
                    "result_digest": result["result_digest"],
                    # Flat fields the companion decodes as a TaskUpdate (protocol/v1.md, task.result).
                    "task_id": str(body.get("task_id", "")),
                    "state": str(body.get("state") or body.get("status") or body.get("kind", "accepted")),
                    "summary": str(body.get("summary") or f"{body.get('kind', 'result')}: {body.get('status', 'accepted')}"),
                }
                issued = int(time.time() * 1000) if now_ms is None else now_ms
                response = self.host.encrypt_reply(
                    recipient_certificate=cert,
                    recipient_id=envelope["from_device_id"],
                    message_id=uuid.uuid4().hex,
                    kind="task.result",
                    payload=payload,
                    issued_at_unix_ms=issued,
                    expires_at_unix_ms=issued + 60 * 60 * 1000,
                )
                self.relay.submit_envelope(response.to_dict())
                digest = result["result_digest"]
                self.relay.ack(message_id=message_id, status="received", result_digest=digest)
                counts["received"] += 1
                counts["replied"] += 1
            except EnvelopeError as exc:
                # Error text/content is never propagated to the relay or logged;
                # ACKs carry only an error-class digest for safe diagnostics.
                reason = type(exc).__name__ + ":rejected"
                safe_digest = hashlib.sha256(reason.encode("ascii")).hexdigest()
                self.relay.ack(message_id=message_id, status="rejected", result_digest=safe_digest)
                counts["rejected"] += 1
            except Exception as exc:  # noqa: BLE001 - isolate one bad message from the batch
                # A supervisor refusal or adapter fault must not abort the poll cycle. The phone is told the
                # request was refused with a fixed, non-secret reason; only the exception class is recorded.
                reason = type(exc).__name__ + ":refused"
                safe_digest = hashlib.sha256(reason.encode("ascii")).hexdigest()
                try:
                    self._reply_refused(envelope, cert, message_id)
                except Exception:  # noqa: BLE001 - never let the reply path abort the batch
                    pass
                self.relay.ack(message_id=message_id, status="rejected", result_digest=safe_digest)
                counts["rejected"] += 1
        return counts
