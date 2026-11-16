"""Outbound polling loop that bridges encrypted Link envelopes to SupervisorPort."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .crypto import DeviceCertificate, EnvelopeError, b64u
from .host import JarvisLinkHost
from .client import RelayClient


class HostRelayRuntime:
    """One-shot poll/dispatch/response/ACK cycle for tests and local simulation."""
    def __init__(self, *, host: JarvisLinkHost, relay: RelayClient) -> None:
        self.host = host
        self.relay = relay

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
                payload = {
                    "in_reply_to": message_id,
                    "status": result["status"],
                    "result": result["result"],
                    "result_digest": result["result_digest"],
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
                # A supervisor/adapter fault must not abort the poll cycle or leave
                # the message redelivering forever; only the exception class is recorded.
                safe_digest = hashlib.sha256((type(exc).__name__ + ":internal").encode("ascii")).hexdigest()
                self.relay.ack(message_id=message_id, status="rejected", result_digest=safe_digest)
                counts["rejected"] += 1
        return counts
