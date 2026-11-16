"""Approval workflow: real Kotlin companion (CLI) <-> relay (workerd) <-> Python host <-> OpenJarvis store.

All operations are the harmless simulated echo: no file, network, or tool effect. The supervisor store is a
temporary database; the approval digest, expiry, and single-use checks are the store's own.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from jarvis_link_host.openjarvis_adapter import OpenJarvisSupervisorAdapter, SupervisorRefused
from jarvis_link_host.simulated_operations import SIMULATED_OPERATION, SIMULATED_TARGET, resume_approved, simulated_arguments
from test_kotlin_companion_relay import cli, dead_port_url, enroll_and_certify
from test_relay_integration import World, relay_url  # noqa: F401
from test_supervisor_adapter import SupervisorStore

pytestmark = pytest.mark.skipif(SupervisorStore is None, reason="OpenJarvis supervisor not importable")
MESSAGE = "JARVIS_SIMULATED_APPROVAL_1"


class Flow:
    def __init__(self, world, state, store, adapter, companion_id):
        self.world, self.state, self.store, self.adapter, self.companion_id = world, state, store, adapter, companion_id

    def request(self, ttl: int = 300, message: str = MESSAGE):
        task_id = self.adapter.submit_task(objective=f"Simulated echo: {message}", capability_scope=["read_only"],
                                           idempotency_key=uuid.uuid4().hex)["task_id"]
        approval_id, digest = self.store.create_approval(
            task_id, SIMULATED_OPERATION, SIMULATED_TARGET, simulated_arguments(message),
            "Harmless simulated echo (test)", ttl_seconds=ttl)
        record = self.adapter.get_approval(approval_id)
        cert = self.world.relay.signed_request("GET", f"/v1/devices/{self.companion_id}/certificate").json()["certificate"]
        self.world.runtime.send_approval_request(
            companion_certificate=cert, companion_id=self.companion_id,
            request={"approval_id": approval_id, "task_id": task_id, "operation": SIMULATED_OPERATION,
                     "target": SIMULATED_TARGET, "canonical_arguments_digest": digest,
                     "expires_at_unix_ms": record.expires_at_unix_ms, "redacted_summary": "Harmless simulated echo"})
        return task_id, approval_id, digest, record.expires_at_unix_ms

    def phone_poll(self):
        return json.loads(cli(self.state, "poll", relay=self.world.url).stdout)["events"]

    def phone_approvals(self):
        return json.loads(cli(self.state, "approvals", relay=self.world.url).stdout)["approvals"]


@pytest.fixture()
def flow(relay_url, tmp_path):  # noqa: F811
    world = World(relay_url, sim_companion=False)
    store = SupervisorStore(db_path=str(tmp_path / "supervisor.db"))
    adapter = OpenJarvisSupervisorAdapter(store, tmp_path / "adapter.db")
    world.host.supervisor = adapter
    state = tmp_path / "companion.json"
    companion_id = enroll_and_certify(world, state)
    cli(state, "sync", relay=world.url)
    yield Flow(world, state, store, adapter, companion_id)
    store.close()


def test_request_is_shown_exactly_and_approve_resumes_the_task(flow):
    task_id, approval_id, digest, expires = flow.request()
    shown = flow.phone_approvals()  # the phone has not polled yet
    assert shown == []
    flow.phone_poll()
    (shown,) = flow.phone_approvals()
    assert shown["approval_id"] == approval_id and shown["task_id"] == task_id
    assert shown["operation"] == SIMULATED_OPERATION and shown["target"] == SIMULATED_TARGET
    assert shown["digest"] == digest and shown["expires_at_unix_ms"] == expires
    decided = json.loads(cli(flow.state, "approve", "--approval-id", approval_id, relay=flow.world.url).stdout)
    assert decided == {"decision": "approve", "approval_id": approval_id, "queued_offline": False}
    assert flow.world.runtime.poll_once() == {"received": 1, "rejected": 0, "replied": 1}
    assert flow.store.approval(approval_id)["status"] == "APPROVED"
    outcome = resume_approved(flow.store, task_id=task_id, approval_id=approval_id, message=MESSAGE)
    assert outcome == {"outcome": "COMPLETED", "result": f"echo:{MESSAGE}"}
    flow.world.runtime.send_task_update(companion_certificate=flow.world.relay.signed_request(
        "GET", f"/v1/devices/{flow.companion_id}/certificate").json()["certificate"], companion_id=flow.companion_id,
        task_id=task_id, state="COMPLETED", summary=outcome["result"])
    assert flow.store.get_task(task_id)["state"] == "COMPLETED"
    kinds = [e["event_type"] for e in flow.store.events(task_id)]
    assert kinds.index("approval.consumed") < kinds.index("simulated.result")
    assert flow.store.approval(approval_id)["status"] == "CONSUMED"
    states = [u["state"] for u in flow.phone_poll() if u["type"] == "task_update"]
    assert "COMPLETED" in states
    assert flow.phone_approvals() == []


def test_reject_pauses_the_task_and_nothing_runs(flow):
    task_id, approval_id, _digest, _ = flow.request()
    flow.phone_poll()
    cli(flow.state, "reject", "--approval-id", approval_id, relay=flow.world.url)
    flow.world.runtime.poll_once()
    assert flow.store.approval(approval_id)["status"] == "REJECTED"
    assert flow.store.get_task(task_id)["state"] == "PAUSED"
    assert resume_approved(flow.store, task_id=task_id, approval_id=approval_id, message=MESSAGE) == {"outcome": "NOT_CONSUMED"}
    assert "simulated.result" not in [e["event_type"] for e in flow.store.events(task_id)]


def test_expired_approval_is_not_shown_and_cannot_be_decided(flow):
    task_id, approval_id, digest, _ = flow.request(ttl=2)
    flow.phone_poll()
    assert len(flow.phone_approvals()) == 1
    time.sleep(2.5)
    assert flow.phone_approvals() == []
    proc = cli(flow.state, "approve", "--approval-id", approval_id, relay=flow.world.url, expect_ok=False)
    assert proc.returncode != 0
    # The supervisor refuses too, even if a decision were forged past the phone.
    with pytest.raises(SupervisorRefused):
        flow.adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="approve",
                                     expected_arguments_digest=digest, idempotency_key=uuid.uuid4().hex)
    assert flow.store.approval(approval_id)["status"] == "EXPIRED"
    assert flow.store.get_task(task_id)["state"] == "WAITING_APPROVAL"


def test_replay_of_a_decision_and_second_execution_are_refused(flow):
    task_id, approval_id, digest, _ = flow.request()
    flow.phone_poll()
    key = "decision-" + uuid.uuid4().hex
    first = flow.adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="approve",
                                         expected_arguments_digest=digest, idempotency_key=key)
    assert flow.adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="approve",
                                        expected_arguments_digest=digest, idempotency_key=key) == first
    with pytest.raises(SupervisorRefused):  # a fresh key cannot decide the same approval again
        flow.adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="approve",
                                     expected_arguments_digest=digest, idempotency_key=uuid.uuid4().hex)
    assert resume_approved(flow.store, task_id=task_id, approval_id=approval_id, message=MESSAGE)["outcome"] == "COMPLETED"
    assert resume_approved(flow.store, task_id=task_id, approval_id=approval_id, message=MESSAGE) == {"outcome": "NOT_CONSUMED"}
    assert [e["event_type"] for e in flow.store.events(task_id)].count("simulated.result") == 1


def test_decision_made_during_network_loss_is_delivered_once(flow):
    task_id, approval_id, _digest, _ = flow.request()
    flow.phone_poll()
    queued = json.loads(cli(flow.state, "approve", "--approval-id", approval_id, relay=dead_port_url()).stdout)
    assert queued["queued_offline"] is True
    assert flow.world.runtime.poll_once()["received"] == 0
    assert flow.store.approval(approval_id)["status"] == "PENDING"
    flushed = json.loads(cli(flow.state, "flush", relay=flow.world.url).stdout)
    assert flushed["delivered"] == 1 and flushed["remaining"] == 0
    assert flow.world.runtime.poll_once() == {"received": 1, "rejected": 0, "replied": 1}
    assert flow.world.runtime.poll_once()["received"] == 0
    assert flow.store.approval(approval_id)["status"] == "APPROVED"
    assert resume_approved(flow.store, task_id=task_id, approval_id=approval_id, message=MESSAGE)["outcome"] == "COMPLETED"
