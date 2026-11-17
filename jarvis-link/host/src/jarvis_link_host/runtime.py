"""Outbound polling loop with durable encrypted-reply delivery retries."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Mapping

import httpx

from .client import RelayClient, RelayClientError
from .crypto import DeviceCertificate, EnvelopeError, LinkEnvelope
from .host import JarvisLinkHost, ReplayDetected
from .openjarvis_adapter import SupervisorRefused
from .openjarvis_api_adapter import SupervisorUnavailable
from .response_outbox import HostResponseOutbox, ResponseReplayConflict


class HostRelayRuntime:
    """One poll/dispatch/ACK cycle for signed relay envelopes.

    The outbox contains only E2E-encrypted response envelopes. It is persisted
    before any network send, so a timeout after supervisor execution never
    loses the task result or turns a safe retry into a second execution.
    """

    def __init__(
        self,
        *,
        host: JarvisLinkHost,
        relay: RelayClient,
        response_outbox: HostResponseOutbox | None = None,
    ) -> None:
        self.host = host
        self.relay = relay
        self.response_outbox = response_outbox or HostResponseOutbox()

    def _approval_envelope(
        self,
        *,
        companion_certificate: dict,
        companion_id: str,
        request: dict[str, Any],
        now_ms: int | None = None,
    ) -> LinkEnvelope:
        issued = int(time.time() * 1000) if now_ms is None else now_ms
        expires = min(int(request["expires_at_unix_ms"]), issued + 24 * 60 * 60 * 1000)
        payload = {
            "approval_id": request["approval_id"],
            "task_id": request["task_id"],
            "operation": request["operation"],
            "target": request["target"],
            "canonical_arguments_digest": request["canonical_arguments_digest"],
            "expires_at_unix_ms": int(request["expires_at_unix_ms"]),
            "redacted_summary": str(request["redacted_summary"])[:500],
        }
        return self.host.encrypt_reply(
            recipient_certificate=companion_certificate,
            recipient_id=companion_id,
            message_id=uuid.uuid4().hex,
            kind="approval.request",
            payload=payload,
            issued_at_unix_ms=issued,
            expires_at_unix_ms=expires,
        )

    def send_approval_request(
        self,
        *,
        companion_certificate: dict,
        companion_id: str,
        request: dict[str, Any],
        now_ms: int | None = None,
    ) -> str:
        """Operator/test helper; production poll uses the durable reply outbox."""
        envelope = self._approval_envelope(
            companion_certificate=companion_certificate,
            companion_id=companion_id,
            request=request,
            now_ms=now_ms,
        )
        self.relay.submit_envelope(envelope.to_dict())
        return envelope.message_id

    def send_task_update(
        self,
        *,
        companion_certificate: dict,
        companion_id: str,
        task_id: str,
        state: str,
        summary: str,
    ) -> str:
        """Operator/test helper for a flat task.result update."""
        issued = int(time.time() * 1000)
        payload = {
            "in_reply_to": "",
            "status": state.lower(),
            "task_id": task_id,
            "state": state,
            "summary": summary[:200],
            "result_digest": "",
        }
        response = self.host.encrypt_reply(
            recipient_certificate=companion_certificate,
            recipient_id=companion_id,
            message_id=uuid.uuid4().hex,
            kind="task.result",
            payload=payload,
            issued_at_unix_ms=issued,
            expires_at_unix_ms=issued + 60 * 60 * 1000,
        )
        self.relay.submit_envelope(response.to_dict())
        return response.message_id

    def _refusal_envelope(self, original: Mapping[str, Any], cert: DeviceCertificate | Mapping[str, Any], message_id: str) -> LinkEnvelope:
        issued = int(time.time() * 1000)
        payload = {
            "in_reply_to": message_id,
            "status": "refused",
            "task_id": "",
            "state": "REFUSED",
            "summary": "Request refused by the supervisor",
            "result_digest": "",
        }
        return self.host.encrypt_reply(
            recipient_certificate=cert,
            recipient_id=str(original["from_device_id"]),
            message_id=uuid.uuid4().hex,
            kind="task.result",
            payload=payload,
            issued_at_unix_ms=issued,
            expires_at_unix_ms=issued + 60 * 60 * 1000,
        )

    def _task_result_envelope(self, original: Mapping[str, Any], cert: DeviceCertificate | Mapping[str, Any], body: Mapping[str, Any], result_digest: str) -> LinkEnvelope:
        message_id = str(original["message_id"])
        state = str(body.get("state") or body.get("status") or body.get("kind", "accepted"))
        summary = str(body.get("summary") or f"{body.get('kind', 'result')}: {body.get('status', 'accepted')}")
        payload = {
            "in_reply_to": message_id,
            "status": str(body.get("status") or "processed"),
            "result": dict(body),
            "result_digest": result_digest,
            "task_id": str(body.get("task_id", "")),
            "state": state,
            "summary": summary[:1000],
        }
        issued = int(time.time() * 1000)
        return self.host.encrypt_reply(
            recipient_certificate=cert,
            recipient_id=str(original["from_device_id"]),
            message_id=uuid.uuid4().hex,
            kind="task.result",
            payload=payload,
            issued_at_unix_ms=issued,
            expires_at_unix_ms=issued + 60 * 60 * 1000,
        )

    def _approval_envelopes(self, *, original: Mapping[str, Any], cert: DeviceCertificate | Mapping[str, Any], task_id: str, now_ms: int | None = None) -> list[LinkEnvelope]:
        list_pending = getattr(self.host.supervisor, "list_pending_approvals", None)
        if not callable(list_pending):
            return []
        approvals = list_pending(task_id)
        sender_id = str(original["from_device_id"])
        output = []
        current = int(time.time() * 1000) if now_ms is None else now_ms
        for approval in approvals:
            if not isinstance(approval, dict) or int(approval.get("expires_at_unix_ms", 0)) <= current:
                continue
            output.append(self._approval_envelope(
                companion_certificate=cert,
                companion_id=sender_id,
                request=approval,
                now_ms=now_ms,
            ))
        return output

    @staticmethod
    def _transient(exc: Exception) -> bool:
        if isinstance(exc, (httpx.TransportError, TimeoutError, OSError, SupervisorUnavailable)):
            return True
        if isinstance(exc, RelayClientError):
            return exc.status_code is None or exc.status_code in {408, 425, 429} or exc.status_code >= 500
        return False

    def _deliver_batch(
        self,
        *,
        original: Mapping[str, Any],
        responses: list[Mapping[str, Any]],
        status: str,
        result_digest: str,
        task_id: str | None = None,
        approvals_checked: bool | None = None,
    ) -> int:
        cached = self.response_outbox.put(
            original,
            responses,
            task_id=task_id,
            approvals_checked=approvals_checked,
        )
        return self._send_cached_batch(original=original, responses=cached, status=status, result_digest=result_digest)

    def _send_cached_batch(
        self,
        *,
        original: Mapping[str, Any],
        responses: list[Mapping[str, Any]],
        status: str,
        result_digest: str,
    ) -> int:
        cached = responses
        for response in cached:
            self.relay.submit_envelope(response)
        self.relay.ack(message_id=str(original["message_id"]), status=status, result_digest=result_digest)
        self.response_outbox.mark_delivered(original)
        return len(cached)

    def _verify_sender_is_active(self, sender_id: str, supplied_certificate: Mapping[str, Any]) -> None:
        try:
            current = self.relay.device_certificate(sender_id)
        except RelayClientError as exc:
            if exc.status_code == 404:
                from .host import EnvelopeError

                raise EnvelopeError("sender device was revoked before dispatch") from exc
            raise
        if current != dict(supplied_certificate):
            from .host import EnvelopeError

            raise EnvelopeError("sender certificate changed before dispatch")

    def poll_once(self, *, limit: int = 20, now_ms: int | None = None) -> dict[str, int]:
        """Poll, dispatch, durably cache encrypted replies, then ACK once deliverable."""
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
            sender_id = envelope.get("from_device_id")
            if not isinstance(message_id, str) or not isinstance(sender_id, str):
                continue
            try:
                self._verify_sender_is_active(sender_id, cert)
                cached = self.response_outbox.get(envelope, now_ms=now_ms)
                if cached is not None:
                    task_id = self.response_outbox.task_id(envelope)
                    if task_id and not self.response_outbox.approvals_checked(envelope):
                        approvals = self._approval_envelopes(
                            original=envelope,
                            cert=cert,
                            task_id=task_id,
                            now_ms=now_ms,
                        )
                        cached = self.response_outbox.append_responses(
                            envelope,
                            [item.to_dict() for item in approvals],
                            now_ms=now_ms,
                        )
                    digest = hashlib.sha256(json.dumps(cached, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    sent = self._send_cached_batch(
                        original=envelope,
                        responses=cached,
                        status="received",
                        result_digest=digest,
                    )
                    counts["received"] += 1
                    counts["replied"] += sent
                    continue

                try:
                    result = self.host.receive(
                        envelope,
                        sender_certificate=cert,
                        expected_sender_id=sender_id,
                        now_ms=now_ms,
                    )
                except ReplayDetected:
                    # The first attempt may have reached the idempotent
                    # supervisor but failed before the encrypted reply was
                    # persisted. A repeated identical relay envelope is safe
                    # to dispatch again: supervisor idempotency keys prevent
                    # executing the task or approval twice.
                    result = self.host.receive(
                        envelope,
                        sender_certificate=cert,
                        expected_sender_id=sender_id,
                        now_ms=now_ms,
                        allow_duplicate=True,
                    )
                body = result["result"]
                task_id = body.get("task_id")
                task_id = task_id if isinstance(task_id, str) and task_id else None
                reply = self._task_result_envelope(envelope, cert, body, result["result_digest"]).to_dict()
                replies = self.response_outbox.put(
                    envelope,
                    [reply],
                    task_id=task_id,
                    approvals_checked=task_id is None,
                    now_ms=now_ms,
                )
                if task_id and not self.response_outbox.approvals_checked(envelope):
                    approvals = self._approval_envelopes(
                        original=envelope,
                        cert=cert,
                        task_id=task_id,
                        now_ms=now_ms,
                    )
                    replies = self.response_outbox.append_responses(
                        envelope,
                        [item.to_dict() for item in approvals],
                        approvals_checked=True,
                        now_ms=now_ms,
                    )
                sent = self._send_cached_batch(
                    original=envelope,
                    responses=replies,
                    status="received",
                    result_digest=result["result_digest"],
                )
                counts["received"] += 1
                counts["replied"] += sent
            except ResponseReplayConflict as exc:
                reason = type(exc).__name__ + ":rejected"
                safe_digest = hashlib.sha256(reason.encode("ascii")).hexdigest()
                try:
                    self.relay.ack(message_id=message_id, status="rejected", result_digest=safe_digest)
                    counts["rejected"] += 1
                except Exception:
                    pass
            except EnvelopeError as exc:
                reason = type(exc).__name__ + ":rejected"
                safe_digest = hashlib.sha256(reason.encode("ascii")).hexdigest()
                try:
                    self.relay.ack(message_id=message_id, status="rejected", result_digest=safe_digest)
                    counts["rejected"] += 1
                except Exception as ack_error:  # transient ACK failures leave the message queued
                    if not self._transient(ack_error):
                        continue
            except SupervisorRefused as refused:
                # Diagnostic only: the reason class and a short detail, never objectives or payloads.
                import sys as _sys
                print(json.dumps({"event": "supervisor_refused", "detail": str(refused)[:160]}), file=_sys.stderr, flush=True)
                try:
                    refusal = self._refusal_envelope(envelope, cert, message_id).to_dict()
                    refusal_digest = hashlib.sha256(json.dumps(refusal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    sent = self._deliver_batch(
                        original=envelope,
                        responses=[refusal],
                        status="rejected",
                        result_digest=refusal_digest,
                        approvals_checked=True,
                    )
                    counts["rejected"] += 1
                    counts["replied"] += sent
                except Exception as failure:
                    if not self._transient(failure):
                        continue
            except Exception as failure:  # never ACK a possibly executed operation without a cached reply
                if not self._transient(failure):
                    # A non-transient adapter/serialization failure still may
                    # have happened after idempotent supervisor submission.
                    # Leave the envelope queued for a safe idempotent retry.
                    continue
        return counts
