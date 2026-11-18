from __future__ import annotations

import time
import uuid

import pytest

from jarvis_link_host.crypto import (
    DeviceIdentity, EnvelopeError, device_id_for_signing_key, encrypt_envelope,
    issue_device_certificate, verify_device_certificate,
)
from jarvis_link_host.host import JarvisLinkHost, TaskEnvelopeError
from jarvis_link_host.replay import ReplayCache
from jarvis_link_host.supervisor import ApprovalRecord, MockSupervisor

TENANT = "test-tenant"


def setup_host():
    host = DeviceIdentity.generate()
    phone = DeviceIdentity.generate()
    host_id = device_id_for_signing_key(host.signing_public_der)
    phone_id = device_id_for_signing_key(phone.signing_public_der)
    host_cert = issue_device_certificate(
        host=host, tenant_id=TENANT, host_device_id=host_id,
        signing_public_b64u=host.signing_public_b64u,
        encryption_public_b64u=host.encryption_public_b64u, device_role="host",
    )
    phone_cert = issue_device_certificate(
        host=host, tenant_id=TENANT, host_device_id=host_id,
        signing_public_b64u=phone.signing_public_b64u,
        encryption_public_b64u=phone.encryption_public_b64u,
    )
    supervisor = MockSupervisor()
    processor = JarvisLinkHost(
        tenant_id=TENANT, device_id=host_id, identity=host,
        host_certificate=host_cert, supervisor=supervisor, replay_cache=ReplayCache(),
    )
    # Synthetic tests below model a single authorized companion.
    processor.task_authorizer = lambda **_: True
    return processor, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert


def task_envelope(host, phone, host_id, phone_id, host_cert, payload, *, kind="task.submit", now=None):
    issued = int(time.time() * 1000) if now is None else now
    return encrypt_envelope(
        sender=phone, recipient_certificate=host_cert,
        trusted_host_signing_public_der=host.signing_public_der,
        tenant_id=TENANT, message_id=uuid.uuid4().hex,
        from_device_id=phone_id, to_device_id=host_id,
        kind=kind, payload=payload,
        issued_at_unix_ms=issued, expires_at_unix_ms=issued + 60_000,
        expected_recipient_role="host",
    )


def test_encrypted_safe_task_uses_supervisor_port():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"objective": "summarize project state", "capability_scope": ["read_only"]})
    result = hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id)
    assert result["status"] == "processed"
    assert result["result"]["kind"] == "task.accepted"
    assert supervisor.calls[0][0] == "submit_task"
    assert "command" not in supervisor.calls[0][1]


def test_typed_fast_mac_status_bypasses_task_submission():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"command": "mac.status", "version": 1}, kind="fast.command")
    result = hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id)
    assert result["result"] == {"kind": "fast.result", "command": "mac.status", "state": "COMPLETED",
                                 "summary": "Mac is online. AI is available.", "online": True, "ai_available": True}
    assert [call[0] for call in supervisor.calls] == ["fast_command"]
    assert supervisor.calls[0][1]["command"] == "mac.status"


def test_fast_command_rejects_extra_fields_and_unallowlisted_app():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    for payload in (
        {"command": "mac.status", "version": 1, "shell": "anything"},
        {"command": "app.open", "version": 1, "app": "Terminal"},
    ):
        env = task_envelope(host, phone, host_id, phone_id, host_cert, payload, kind="fast.command")
        with pytest.raises(TaskEnvelopeError, match="unsupported typed fast command"):
            hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id)
    assert supervisor.calls == []


def test_fast_calculator_routes_to_exact_supervisor_approval_without_task_submission():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"app": "Calculator", "command": "app.open", "version": 1}, kind="fast.command")
    result = hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id)
    assert result["result"] == {
        "kind": "fast.result", "command": "app.open", "state": "WAITING_APPROVAL",
        "operation": "mcp:jarvis_mac:open_app", "target": "Calculator",
        "summary": "Calculator requires your approval on this phone.",
    }
    assert [call[0] for call in supervisor.calls] == ["fast_command"]


def test_assistant_scope_is_forwarded_to_supervisor_without_executable_fields():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    env = task_envelope(
        host,
        phone,
        host_id,
        phone_id,
        host_cert,
        {"objective": "Open TextEdit only after approval", "capability_scope": ["assistant"]},
    )
    result = hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id)
    assert result["result"]["kind"] == "task.accepted"
    assert supervisor.tasks[result["result"]["task_id"]]["capability_scope"] == ["assistant"]
    assert "command" not in supervisor.calls[0][1]


def test_arbitrary_shell_field_is_rejected_before_supervisor():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"objective": "inspect", "capability_scope": ["read_only"], "command": "rm -rf /"})
    with pytest.raises(TaskEnvelopeError, match="prohibited executable"):
        hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id)
    assert supervisor.calls == []


def test_replay_and_expiry_are_rejected():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    now = int(time.time() * 1000)
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"objective": "inspect", "capability_scope": ["read_only"]}, now=now)
    hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    with pytest.raises(EnvelopeError, match="processed"):
        hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    expired = task_envelope(host, phone, host_id, phone_id, host_cert,
                            {"objective": "inspect", "capability_scope": ["read_only"]}, now=now-120_000)
    with pytest.raises(EnvelopeError, match="expired"):
        hostproc.receive(expired.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    assert len(supervisor.calls) == 1


def test_relay_substituted_device_certificate_is_rejected():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"objective": "inspect", "capability_scope": ["read_only"]})
    substituted = phone_cert.to_dict()
    substituted["encryption_public_key_b64url"] = DeviceIdentity.generate().encryption_public_b64u
    with pytest.raises(EnvelopeError):
        hostproc.receive(env.to_dict(), sender_certificate=substituted, expected_sender_id=phone_id)
    assert supervisor.calls == []


def test_approval_digest_mismatch_does_not_reach_supervisor():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    now = int(time.time() * 1000)
    task_id, approval_id, digest = uuid.uuid4().hex, uuid.uuid4().hex, "a" * 64
    supervisor.add_approval(ApprovalRecord(approval_id, task_id, "shell_exec", "/tmp", digest, now + 60_000))
    payload = {"task_id": task_id, "approval_id": approval_id, "decision": "approve",
               "operation_digest": "b" * 64, "idempotency_key": "decision-1"}
    env = task_envelope(host, phone, host_id, phone_id, host_cert, payload, kind="approval.decision", now=now)
    with pytest.raises(TaskEnvelopeError, match="digest"):
        hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    assert supervisor.calls == []


def test_valid_approval_decision_is_revalidated_and_forwarded():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    now = int(time.time() * 1000)
    task_id, approval_id, digest = uuid.uuid4().hex, uuid.uuid4().hex, "a" * 64
    supervisor.add_approval(ApprovalRecord(approval_id, task_id, "shell_exec", "/tmp", digest, now + 60_000))
    payload = {"task_id": task_id, "approval_id": approval_id, "decision": "approve",
               "operation_digest": digest, "idempotency_key": "decision-2"}
    env = task_envelope(host, phone, host_id, phone_id, host_cert, payload, kind="approval.decision", now=now)
    result = hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    assert result["result"]["status"] == "APPROVED"
    assert supervisor.calls[-1][0] == "decide_approval"


def test_approval_decision_binds_continuation_objective_to_encrypted_payload():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    now = int(time.time() * 1000)
    task_id, approval_id, digest = uuid.uuid4().hex, uuid.uuid4().hex, "a" * 64
    supervisor.add_approval(ApprovalRecord(approval_id, task_id, "mcp:jarvis_mac:open_app", "TextEdit", digest, now + 60_000))
    objective = "Open TextEdit after I approve, then tell me it is open."
    payload = {
        "task_id": task_id,
        "approval_id": approval_id,
        "decision": "approve",
        "operation_digest": digest,
        "idempotency_key": "decision-continuation-1",
        "continuation_objective": objective,
    }
    env = task_envelope(host, phone, host_id, phone_id, host_cert, payload, kind="approval.decision", now=now)
    result = hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    assert result["result"]["kind"] == "approval.decision_result"
    assert result["result"]["continuation_objective"] == objective
    assert supervisor.calls[-1][1]["continuation_objective"] == objective


def test_approval_continuation_objective_is_bounded():
    hostproc, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    now = int(time.time() * 1000)
    task_id, approval_id, digest = uuid.uuid4().hex, uuid.uuid4().hex, "a" * 64
    supervisor.add_approval(ApprovalRecord(approval_id, task_id, "mcp:jarvis_mac:open_app", "TextEdit", digest, now + 60_000))
    payload = {
        "task_id": task_id,
        "approval_id": approval_id,
        "decision": "approve",
        "operation_digest": digest,
        "idempotency_key": "decision-continuation-2",
        "continuation_objective": "x" * 4097,
    }
    env = task_envelope(host, phone, host_id, phone_id, host_cert, payload, kind="approval.decision", now=now)
    with pytest.raises(TaskEnvelopeError, match="continuation"):
        hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    assert supervisor.calls == []


def test_envelope_to_key_id_uses_verified_recipient_certificate():
    _, _, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    now = int(time.time() * 1000)
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"objective": "inspect", "capability_scope": ["read_only"]}, now=now)
    assert env.to_key_id == host_cert.certificate_id
    verified = verify_device_certificate(
        phone_cert, trusted_host_signing_public_der=host.signing_public_der,
        expected_tenant_id=TENANT, expected_device_id=phone_id,
        expected_role="companion", expected_issuer_device_id=host_id, now_ms=now,
    )
    assert verified == phone_cert


def test_certified_other_companion_cannot_decide_or_cancel_owned_task():
    processor, supervisor, host, owner, host_id, owner_id, host_cert, owner_cert = setup_host()
    other = DeviceIdentity.generate()
    other_id = device_id_for_signing_key(other.signing_public_der)
    other_cert = issue_device_certificate(host=host, tenant_id=TENANT, host_device_id=host_id,
        signing_public_b64u=other.signing_public_b64u, encryption_public_b64u=other.encryption_public_b64u)
    task_id, approval_id, digest = uuid.uuid4().hex, uuid.uuid4().hex, "a" * 64
    now = int(time.time() * 1000)
    supervisor.tasks[task_id] = {"state": "RUNNING"}
    supervisor.add_approval(ApprovalRecord(approval_id, task_id, "mcp:jarvis_mac:open_app", "Calculator", digest, now + 60_000))
    processor.task_authorizer = lambda *, task_id, companion_id: companion_id == owner_id
    decision = {"task_id": task_id, "approval_id": approval_id, "decision": "approve",
                "operation_digest": digest, "idempotency_key": "decision-one"}
    cancel = {"task_id": task_id, "idempotency_key": "cancel-one"}
    for kind, payload in [("approval.decision", decision), ("task.cancel", cancel)]:
        env = task_envelope(host, other, host_id, other_id, host_cert, payload, kind=kind)
        with pytest.raises(TaskEnvelopeError, match="not owned"):
            processor.receive(env.to_dict(), sender_certificate=other_cert, expected_sender_id=other_id)
    assert supervisor.calls == []
    assert supervisor.approvals[approval_id].status == "PENDING"
    env = task_envelope(host, owner, host_id, owner_id, host_cert, decision, kind="approval.decision")
    assert processor.receive(env.to_dict(), sender_certificate=owner_cert, expected_sender_id=owner_id)["result"]["status"] == "APPROVED"
    env = task_envelope(host, owner, host_id, owner_id, host_cert, cancel, kind="task.cancel")
    assert processor.receive(env.to_dict(), sender_certificate=owner_cert, expected_sender_id=owner_id)["result"]["state"] == "CANCELLED"


def test_task_control_without_ownership_authorizer_fails_closed():
    processor, supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    del processor.task_authorizer
    task_id = uuid.uuid4().hex
    supervisor.tasks[task_id] = {"state": "RUNNING"}
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"task_id": task_id, "idempotency_key": "cancel-one"}, kind="task.cancel")
    with pytest.raises(TaskEnvelopeError, match="not owned"):
        processor.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id)
    assert supervisor.calls == []
