"""Host reply delivery survives transport loss/restart without a second task dispatch."""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from jarvis_link_host.crypto import DeviceIdentity, LinkEnvelope, decrypt_envelope
from jarvis_link_host.host import JarvisLinkHost
from jarvis_link_host.replay import ReplayCache
from jarvis_link_host.response_outbox import HostResponseOutbox, ResponseReplayConflict
from jarvis_link_host.runtime import HostRelayRuntime
from jarvis_link_host.supervisor import ApprovalRecord
from test_host import setup_host, task_envelope


class RelayStub:
    def __init__(self, host_id, envelope, sender_certificate):
        self.device_id = host_id
        self.tenant_id = envelope["tenant_id"]
        self.envelope = envelope
        self.sender_certificate = sender_certificate
        self.fail_first_reply = True
        self.responses = []
        self.acks = []

    def inbox(self, *, device_id, limit=20):
        assert device_id == self.device_id
        return [] if self.acks else [{"envelope": self.envelope, "sender_certificate": self.sender_certificate}]

    def device_certificate(self, device_id):
        assert device_id == self.envelope["from_device_id"]
        return self.sender_certificate

    def submit_envelope(self, envelope):
        if self.fail_first_reply:
            self.fail_first_reply = False
            raise OSError("test-only network interruption")
        self.responses.append(envelope)
        return {"status": "queued"}

    def ack(self, *, message_id, status, result_digest=""):
        self.acks.append((message_id, status, result_digest))
        return {"status": "acked"}


def test_lost_reply_delivery_reuses_encrypted_outbox_after_host_restart(tmp_path):
    processor, supervisor, host_identity, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    now = int(time.time() * 1000)
    request = task_envelope(
        host_identity,
        phone,
        host_id,
        phone_id,
        host_cert,
        {"objective": "Summarize my current task", "capability_scope": ["read_only"]},
        now=now,
    ).to_dict()
    relay = RelayStub(host_id, request, phone_cert.to_dict())
    outbox_path = tmp_path / "response-outbox.db"
    outbox = HostResponseOutbox(outbox_path)
    runtime = HostRelayRuntime(host=processor, relay=relay, response_outbox=outbox)

    # Supervisor accepts exactly once, then the first encrypted response send fails.
    first = runtime.poll_once(now_ms=now)
    assert first == {"received": 0, "rejected": 0, "replied": 0}
    assert len(supervisor.tasks) == 1
    assert relay.acks == []
    assert outbox.get(request, now_ms=now)
    outbox.close()

    # Recreate the host runtime, simulating a service restart before the ACK.
    restarted_host = JarvisLinkHost(
        tenant_id=processor.tenant_id,
        device_id=host_id,
        identity=host_identity,
        host_certificate=host_cert,
        supervisor=supervisor,
        replay_cache=ReplayCache(),
    )
    restarted_outbox = HostResponseOutbox(outbox_path)
    restarted = HostRelayRuntime(host=restarted_host, relay=relay, response_outbox=restarted_outbox)
    second = restarted.poll_once(now_ms=now)
    assert second == {"received": 1, "rejected": 0, "replied": 1}
    assert len(supervisor.tasks) == 1
    assert len(relay.responses) == 1
    assert relay.acks[0][1] == "received"
    # The durable reply is still ciphertext; the task objective is not in the database.
    restarted_outbox.close()
    assert b"Summarize my current task" not in outbox_path.read_bytes()


def test_response_outbox_is_tenant_scoped_expiring_and_replay_bound(tmp_path):
    processor, _supervisor, host_identity, phone, host_id, phone_id, host_cert, _phone_cert = setup_host()
    now = int(time.time() * 1000)
    request = task_envelope(
        host_identity, phone, host_id, phone_id, host_cert,
        {"objective": "read only", "capability_scope": ["read_only"]}, now=now,
    ).to_dict()
    outbox = HostResponseOutbox(tmp_path / "outbox.db")
    cached_response = {"message_id": "r" * 32, "ciphertext_b64url": "opaque"}
    assert outbox.put(request, [cached_response], task_id="t" * 32, approvals_checked=False, now_ms=now) == [cached_response]
    assert outbox.task_id(request) == "t" * 32
    assert not outbox.approvals_checked(request)
    appended = {"message_id": "s" * 32, "ciphertext_b64url": "approval-opaque"}
    values = outbox.append_responses(request, [appended], approvals_checked=True, now_ms=now)
    assert [item["message_id"] for item in values] == ["r" * 32, "s" * 32]
    assert outbox.approvals_checked(request)

    # The same sender/message ID in another tenant is a separate namespace;
    # a cross-tenant lookup cannot retrieve or append the cached ciphertext.
    other_tenant = {**request, "tenant_id": "other-tenant"}
    assert outbox.get(other_tenant, now_ms=now) is None
    with pytest.raises(KeyError):
        outbox.append_responses(other_tenant, [appended], now_ms=now)

    changed = {**request, "ciphertext_b64url": "tampered"}
    with pytest.raises(ResponseReplayConflict):
        outbox.get(changed, now_ms=now)
    assert outbox.get(request, now_ms=request["expires_at_unix_ms"]) is None
    with pytest.raises(ValueError, match="expired request"):
        outbox.append_responses(request, [appended], now_ms=request["expires_at_unix_ms"])
    outbox.close()


def test_response_outbox_never_changes_shared_directory_permissions_or_follows_symlinks(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    with pytest.raises(PermissionError, match="refusing to change its permissions"):
        HostResponseOutbox(shared / "responses.db")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    victim = tmp_path / "victim.db"
    victim.write_text("do not modify", encoding="utf-8")
    link = private / "responses.db"
    link.symlink_to(victim)
    with pytest.raises(ValueError, match="symbolic link"):
        HostResponseOutbox(link)
    assert victim.read_text(encoding="utf-8") == "do not modify"

    linked_parent = tmp_path / "private-alias"
    linked_parent.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        HostResponseOutbox(linked_parent / "another.db")

    database = HostResponseOutbox(private / "safe.db")
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE((private / "safe.db").stat().st_mode) == 0o600
    database.close()


def test_operator_delivery_helpers_return_the_actual_envelope_id():
    processor, _supervisor, host_identity, _phone, host_id, phone_id, _host_cert, phone_cert = setup_host()
    placeholder = task_envelope(
        host_identity, _phone, host_id, phone_id, processor.host_certificate,
        {"objective": "safe", "capability_scope": ["read_only"]},
    ).to_dict()
    relay = RelayStub(host_id, placeholder, phone_cert.to_dict())
    relay.fail_first_reply = False
    runtime = HostRelayRuntime(host=processor, relay=relay)
    approval_id = runtime.send_approval_request(
        companion_certificate=phone_cert.to_dict(),
        companion_id=phone_id,
        request={
            "approval_id": "b" * 32,
            "task_id": "a" * 32,
            "operation": "mcp:jarvis_mac:open_app",
            "target": "TextEdit",
            "canonical_arguments_digest": "c" * 64,
            "expires_at_unix_ms": int(time.time() * 1000) + 30_000,
            "redacted_summary": "Harmless test approval",
        },
    )
    task_update_id = runtime.send_task_update(
        companion_certificate=phone_cert.to_dict(),
        companion_id=phone_id,
        task_id="a" * 32,
        state="COMPLETED",
        summary="Harmless test completed",
    )
    assert len(approval_id) == len(task_update_id) == 32
    assert [item["message_id"] for item in relay.responses] == [approval_id, task_update_id]


def test_pending_supervisor_approval_is_sent_as_a_separate_encrypted_relay_message(tmp_path):
    processor, base_supervisor, host_identity, phone, host_id, phone_id, host_cert, phone_cert = setup_host()

    class ApprovalSupervisor(type(base_supervisor)):
        def submit_task(self, *, objective, capability_scope, idempotency_key):
            accepted = super().submit_task(objective=objective, capability_scope=capability_scope, idempotency_key=idempotency_key)
            task_id = accepted["task_id"]
            accepted["state"] = "WAITING_APPROVAL"
            self.tasks[task_id]["state"] = "WAITING_APPROVAL"
            self.add_approval(ApprovalRecord(
                approval_id="b" * 32,
                task_id=task_id,
                operation="mcp:jarvis_mac:open_app",
                target="TextEdit",
                arguments_digest="c" * 64,
                expires_at_unix_ms=int(time.time() * 1000) + 30_000,
            ))
            return accepted

    supervisor = ApprovalSupervisor()
    processor.supervisor = supervisor
    request = task_envelope(
        host_identity,
        phone,
        host_id,
        phone_id,
        host_cert,
        {"objective": "Open TextEdit only after approval", "capability_scope": ["assistant"]},
    ).to_dict()
    relay = RelayStub(host_id, request, phone_cert.to_dict())
    relay.fail_first_reply = False
    outbox = HostResponseOutbox(tmp_path / "approval-outbox.db")
    runtime = HostRelayRuntime(host=processor, relay=relay, response_outbox=outbox)
    counts = runtime.poll_once()
    assert counts == {"received": 1, "rejected": 0, "replied": 2}
    assert len(relay.responses) == 2

    decoded = []
    for response in relay.responses:
        envelope = LinkEnvelope.from_dict(response)
        decoded.append(decrypt_envelope(
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
        ))
    approval = next(item for item in decoded if item.get("approval_id") == "b" * 32)
    assert approval["operation"] == "mcp:jarvis_mac:open_app"
    assert approval["target"] == "TextEdit"
    assert approval["canonical_arguments_digest"] == "c" * 64
    outbox.close()


def test_persisted_owner_binding_fails_closed_for_unknown_and_ambiguous_tasks(tmp_path):
    now = int(time.time() * 1000)
    path = tmp_path / "outbox.db"
    store = HostResponseOutbox(path)
    request = {"tenant_id": "tenant", "from_device_id": "a" * 32, "message_id": "b" * 32,
               "expires_at_unix_ms": now + 60_000}
    store.put(request, [], task_id="c" * 32, now_ms=now)
    store.close()
    store = HostResponseOutbox(path)
    assert store.owns_task(task_id="c" * 32, companion_id="a" * 32)
    assert not store.owns_task(task_id="c" * 32, companion_id="d" * 32)
    assert not store.owns_task(task_id="e" * 32, companion_id="a" * 32)
    store.put({**request, "message_id": "f" * 32, "from_device_id": "d" * 32}, [], task_id="c" * 32, now_ms=now)
    assert not store.owns_task(task_id="c" * 32, companion_id="a" * 32)
    assert not store.owns_task(task_id="c" * 32, companion_id="d" * 32)
    store.close()
