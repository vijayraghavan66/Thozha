"""Completed answers reach the phone whole, verified by hash; only a stated, bounded cut is ever applied."""
from __future__ import annotations

import hashlib

from jarvis_link_host.crypto import LinkEnvelope, decrypt_envelope
from jarvis_link_host.openjarvis_api_adapter import _result_text
from jarvis_link_host.runtime import (
    BODY_CUT_NOTICE,
    MAX_DELIVERED_BODY_BYTES,
    HostRelayRuntime,
    delivered_body,
    preview,
)
from jarvis_link_host.response_outbox import HostResponseOutbox
from test_host import setup_host
from test_response_delivery import RelayStub

PARAGRAPH = (
    "Java is a programming language and software platform. Developers use it to build web services, "
    "business applications, and Android apps. Java code runs on the Java Virtual Machine (JVM).\n\n"
)


def _send_and_decrypt(answer: str) -> dict:
    processor, _supervisor, host_identity, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    relay = RelayStub(host_id, {"tenant_id": processor.tenant_id}, phone_cert.to_dict())
    relay.fail_first_reply = False
    runtime = HostRelayRuntime(host=processor, relay=relay)
    runtime.send_task_update(
        companion_certificate=phone_cert.to_dict(),
        companion_id=phone_id,
        task_id="a" * 32,
        state="COMPLETED",
        summary="",
        result_text=answer,
    )
    assert len(relay.responses) == 1
    envelope = LinkEnvelope.from_dict(relay.responses[0])
    return decrypt_envelope(
        envelope,
        recipient=phone,
        sender_certificate=host_cert,
        trusted_host_signing_public_der=host_identity.signing_public_der,
        expected_tenant_id=processor.tenant_id,
        expected_recipient_id=phone_id,
        expected_recipient_certificate_id=phone_cert.certificate_id,
        expected_sender_id=host_id,
        expected_issuer_device_id=host_id,
        expected_sender_role="host",
    )


def test_long_multiparagraph_markdown_and_tamil_answer_arrives_exactly():
    answer = (PARAGRAPH * 30) + "```java\nSystem.out.println(\"hi\");\n```\n\nதமிழ் உரை ✓ — end."
    assert len(answer) > 200
    payload = _send_and_decrypt(answer)
    assert payload["body"] == answer
    assert payload["body_complete"] is True
    assert payload["body_bytes_total"] == len(answer.encode("utf-8"))
    assert payload["body_sha256"] == hashlib.sha256(answer.encode("utf-8")).hexdigest()
    assert payload["summary"] == preview(answer)
    assert len(payload["summary"]) <= 200


def test_answer_over_delivery_limit_is_cut_visibly_with_matching_hash():
    answer = "தமிழ் " * 20000  # well over 24 KiB of multi-byte text
    payload = _send_and_decrypt(answer)
    body = payload["body"]
    assert payload["body_complete"] is False
    assert body.endswith(BODY_CUT_NOTICE)
    assert len(body.encode("utf-8")) <= MAX_DELIVERED_BODY_BYTES
    assert payload["body_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    body.encode("utf-8").decode("utf-8")  # no broken multi-byte character at the cut


def test_short_answer_is_complete_and_unchanged():
    payload = delivered_body("JARVIS_CHAT_OK")
    assert payload["body"] == "JARVIS_CHAT_OK"
    assert payload["body_complete"] is True


def test_preview_is_only_for_activity_and_never_replaces_the_body():
    long_answer = "x" * 1000
    assert len(preview(long_answer)) == 200
    assert preview(long_answer).endswith("…")
    assert preview("short") == "short"


def test_approval_decision_result_uses_execution_state_and_exact_request_correlation():
    processor, _supervisor, host_identity, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    relay = RelayStub(host_id, {"tenant_id": processor.tenant_id}, phone_cert.to_dict())
    runtime = HostRelayRuntime(host=processor, relay=relay)
    decision_message_id = "d" * 32
    approval_id = "a" * 32
    task_id = "b" * 32
    body = {
        "kind": "approval.decision_result",
        "approval_id": approval_id,
        "task_id": task_id,
        "decision": "approve",
        "status": "CONSUMED",
        "task_state": "COMPLETED",
        "execution_state": "COMPLETED",
        "executed": True,
        "success": True,
        "continued": False,
        "simulated": False,
        "result_summary": "Calculator opened.",
    }

    envelope = runtime._task_result_envelope(
        {"message_id": decision_message_id, "from_device_id": phone_id},
        phone_cert.to_dict(),
        body,
        hashlib.sha256(b"approval-result").hexdigest(),
    )
    payload = decrypt_envelope(
        envelope,
        recipient=phone,
        sender_certificate=host_cert,
        trusted_host_signing_public_der=host_identity.signing_public_der,
        expected_tenant_id=processor.tenant_id,
        expected_recipient_id=phone_id,
        expected_recipient_certificate_id=phone_cert.certificate_id,
        expected_sender_id=host_id,
        expected_issuer_device_id=host_id,
        expected_sender_role="host",
    )
    assert payload["in_reply_to"] == decision_message_id
    assert payload["state"] == "COMPLETED"
    assert payload["result"]["approval_id"] == approval_id
    assert payload["result"]["execution_state"] == "COMPLETED"
    assert payload["result"]["executed"] is True and payload["result"]["success"] is True


def test_task_progress_is_encrypted_and_contains_only_sanitized_stage_fields():
    processor, _supervisor, host_identity, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    relay = RelayStub(host_id, {"tenant_id": processor.tenant_id}, phone_cert.to_dict())
    relay.fail_first_reply = False
    runtime = HostRelayRuntime(host=processor, relay=relay)
    runtime.send_task_progress(
        companion_certificate=phone_cert.to_dict(),
        companion_id=phone_id,
        task_id="a" * 32,
        event_seq=42,
        state="RUNNING",
        summary="Approved Mac action is executing",
    )
    envelope = LinkEnvelope.from_dict(relay.responses[0])
    payload = decrypt_envelope(
        envelope,
        recipient=phone,
        sender_certificate=host_cert,
        trusted_host_signing_public_der=host_identity.signing_public_der,
        expected_tenant_id=processor.tenant_id,
        expected_recipient_id=phone_id,
        expected_recipient_certificate_id=phone_cert.certificate_id,
        expected_sender_id=host_id,
        expected_issuer_device_id=host_id,
        expected_sender_role="host",
    )
    assert envelope.kind == "task.progress"
    assert payload == {
        "event_seq": 42,
        "in_reply_to": "",
        "state": "RUNNING",
        "summary": "Approved Mac action is executing",
        "task_id": "a" * 32,
    }


def test_progress_retries_reuse_exact_ciphertext_after_lost_relay_ack(tmp_path):
    processor, _supervisor, _host_identity, _phone, host_id, phone_id, _host_cert, phone_cert = setup_host()
    path = tmp_path / "host-outbox.db"
    sent = []
    fail_after_accept = True

    class Relay:
        device_id = host_id
        tenant_id = "test-tenant"

        def submit_envelope(self, envelope):
            nonlocal fail_after_accept
            sent.append(dict(envelope))
            if fail_after_accept:
                fail_after_accept = False
                raise OSError("relay accepted; response was lost")

    relay = Relay()
    first_outbox = HostResponseOutbox(path)
    first_runtime = HostRelayRuntime(host=processor, relay=relay, response_outbox=first_outbox)
    args = dict(
        companion_certificate=phone_cert.to_dict(),
        companion_id=phone_id,
        task_id="a" * 32,
        event_seq=77,
        state="RUNNING",
        summary="AI execution started",
    )
    try:
        first_runtime.send_task_progress(**args)
    except OSError:
        pass
    else:
        raise AssertionError("the simulated lost acknowledgment should be raised")
    first_outbox.close()

    reopened = HostResponseOutbox(path)
    runtime = HostRelayRuntime(host=processor, relay=relay, response_outbox=reopened)
    runtime.send_task_progress(**{**args, "summary": "This retry must use the cached envelope"})
    assert len(sent) == 2
    assert sent[0] == sent[1]
    assert reopened.get_progress("a" * 32, 77)["delivered"] is True
    # A host restart after the cursor write cannot send this event again.
    runtime.send_task_progress(**args)
    assert len(sent) == 2
    reopened.close()


def test_adapter_passes_full_result_text_and_falls_back_to_summary_for_old_supervisors():
    assert _result_text({"result_text": "A" * 5000, "summary": "A" * 1000}) == "A" * 5000
    assert _result_text({"summary": "older supervisor preview"}) == "older supervisor preview"
    assert _result_text({}) == ""
