"""Host -> REAL OpenJarvis SupervisorStore integration (temporary database, simulated operations only).

The OpenJarvis checkout is imported read-only from OPENJARVIS_SRC (default ~/.openjarvis/src/src). No tool is
executed by these tests: the store records tasks and approval state, and the adapter only calls its public API.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

OJ_SRC = Path(os.environ.get("OPENJARVIS_SRC", Path.home() / ".openjarvis" / "src" / "src"))
if OJ_SRC.is_dir():
    sys.path.insert(0, str(OJ_SRC))
try:
    from openjarvis.tasks.supervisor import SupervisorStore
except Exception:  # pragma: no cover - environment without the OpenJarvis checkout
    SupervisorStore = None

from jarvis_link_host.host import TaskEnvelopeError  # noqa: E402
from jarvis_link_host.openjarvis_adapter import OpenJarvisSupervisorAdapter, SupervisorRefused  # noqa: E402
from jarvis_link_host.replay import ReplayCache  # noqa: E402
from test_host import setup_host, task_envelope  # noqa: E402

pytestmark = pytest.mark.skipif(SupervisorStore is None, reason="OpenJarvis supervisor not importable")


@pytest.fixture()
def world(tmp_path):
    store = SupervisorStore(db_path=str(tmp_path / "supervisor.db"))
    adapter = OpenJarvisSupervisorAdapter(store, tmp_path / "link-adapter.db")
    yield store, adapter
    store.close()


def _event_types(store, task_id):
    return [event["event_type"] for event in store.events(task_id)]


def test_submit_is_recorded_once_per_idempotency_key(world):
    store, adapter = world
    key = uuid.uuid4().hex
    first = adapter.submit_task(objective="Summarise calendar", capability_scope=["read_only"], idempotency_key=key,
                                requester_device_id="d" * 32)
    second = adapter.submit_task(objective="Summarise calendar", capability_scope=["read_only"], idempotency_key=key,
                                 requester_device_id="d" * 32)
    assert first["task_id"] == second["task_id"]
    task = store.get_task(first["task_id"])
    assert task["state"] == "CREATED" and task["capabilities_json"] == '["read_only"]'
    assert _event_types(store, first["task_id"]) == ["task.created"]  # recorded, never executed


def test_distinct_messages_create_isolated_tasks(world):
    store, adapter = world
    a = adapter.submit_task(objective="A", capability_scope=["read_only"], idempotency_key=uuid.uuid4().hex,
                            requester_device_id="d" * 32)
    b = adapter.submit_task(objective="B", capability_scope=["web_research"], idempotency_key=uuid.uuid4().hex,
                            requester_device_id="d" * 32)
    assert a["task_id"] != b["task_id"]
    assert store.get_task(a["task_id"])["capabilities_json"] == '["read_only"]'
    assert store.get_task(b["task_id"])["capabilities_json"] == '["web_research"]'


def test_task_idempotency_and_approval_decisions_are_device_bound(world):
    store, adapter = world
    owner = "d" * 32
    other = "e" * 32
    key = uuid.uuid4().hex
    task = adapter.submit_task(objective="Owner task", capability_scope=["read_only"],
                               idempotency_key=key, requester_device_id=owner)
    with pytest.raises(SupervisorRefused, match="another companion"):
        adapter.submit_task(objective="Owner task", capability_scope=["read_only"],
                            idempotency_key=key, requester_device_id=other)
    approval_id, digest = store.create_approval(task["task_id"], "shell_exec", "/tmp", {"command": ["true"]}, "test")
    with pytest.raises(SupervisorRefused, match="another companion"):
        adapter.decide_approval(task_id=task["task_id"], approval_id=approval_id, decision="approve",
                                expected_arguments_digest=digest, idempotency_key=uuid.uuid4().hex,
                                requester_device_id=other)
    assert store.approval(approval_id)["status"] == "PENDING"


def test_encrypted_task_reaches_real_store_and_replay_is_refused(world):
    store, adapter = world
    hostproc, _supervisor, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    hostproc.supervisor = adapter
    now = int(time.time() * 1000)
    env = task_envelope(host, phone, host_id, phone_id, host_cert,
                        {"objective": "Check system status", "capability_scope": ["read_only"]}, now=now)
    result = hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    task_id = result["result"]["task_id"]
    assert store.get_task(task_id)["state"] == "CREATED"
    with pytest.raises(Exception):  # same message ID again: replay is rejected before the supervisor
        hostproc.receive(env.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    assert len(store.recent_tasks(limit=50)) == 1


def test_approval_digest_and_single_use_are_enforced_by_the_store(world):
    store, adapter = world
    task_id = adapter.submit_task(objective="Needs approval", capability_scope=["read_only"],
                                  idempotency_key=uuid.uuid4().hex,
                                  requester_device_id="d" * 32)["task_id"]
    approval_id, digest = store.create_approval(task_id, "shell_exec", "/tmp",
                                                {"command": ["echo", "JARVIS_SIMULATED"]}, "simulated approval")
    record = adapter.get_approval(approval_id)
    assert record.status == "PENDING" and record.arguments_digest == digest
    assert record.expires_at_unix_ms > int(time.time() * 1000)
    with pytest.raises(SupervisorRefused):
        adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="approve",
                                expected_arguments_digest="0" * 64, idempotency_key="d-wrong",
                                requester_device_id="d" * 32)
    decided = adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="approve",
                                      expected_arguments_digest=digest, idempotency_key="d-ok",
                                      requester_device_id="d" * 32)
    assert decided["status"] == "APPROVED"
    assert adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="approve",
                                   expected_arguments_digest=digest, idempotency_key="d-ok",
                                   requester_device_id="d" * 32) == decided
    with pytest.raises(SupervisorRefused):  # a different decision key cannot decide it again
        adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="reject",
                                expected_arguments_digest=digest, idempotency_key="d-second",
                                requester_device_id="d" * 32)
    assert store.approval(approval_id)["status"] == "APPROVED"


def test_expired_approval_is_refused(world):
    store, adapter = world
    task_id = adapter.submit_task(objective="Short window", capability_scope=["read_only"],
                                  idempotency_key=uuid.uuid4().hex,
                                  requester_device_id="d" * 32)["task_id"]
    approval_id, digest = store.create_approval(task_id, "shell_exec", "/tmp", {"command": ["true"]}, "short", ttl_seconds=1)
    time.sleep(1.5)
    with pytest.raises(SupervisorRefused):
        adapter.decide_approval(task_id=task_id, approval_id=approval_id, decision="approve",
                                expected_arguments_digest=digest, idempotency_key="late",
                                requester_device_id="d" * 32)
    assert store.approval(approval_id)["status"] != "APPROVED"


def test_reject_pauses_the_task_and_forged_digest_never_reaches_the_store(world):
    store, adapter = world
    hostproc, _s, host, phone, host_id, phone_id, host_cert, phone_cert = setup_host()
    hostproc.supervisor = adapter
    task_id = adapter.submit_task(objective="Reject me", capability_scope=["read_only"],
                                  idempotency_key=uuid.uuid4().hex, requester_device_id=phone_id)["task_id"]
    approval_id, digest = store.create_approval(task_id, "shell_exec", "/tmp", {"command": ["true"]}, "reject case")
    now = int(time.time() * 1000)
    forged = task_envelope(host, phone, host_id, phone_id, host_cert, {
        "task_id": task_id, "approval_id": approval_id, "decision": "approve",
        "operation_digest": "f" * 64, "idempotency_key": "forged"}, kind="approval.decision", now=now)
    with pytest.raises(TaskEnvelopeError):
        hostproc.receive(forged.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    assert store.approval(approval_id)["status"] == "PENDING"
    rejected = task_envelope(host, phone, host_id, phone_id, host_cert, {
        "task_id": task_id, "approval_id": approval_id, "decision": "reject",
        "operation_digest": digest, "idempotency_key": "reject-1"}, kind="approval.decision", now=now)
    result = hostproc.receive(rejected.to_dict(), sender_certificate=phone_cert, expected_sender_id=phone_id, now_ms=now)
    assert result["result"]["status"] == "REJECTED"
    assert store.get_task(task_id)["state"] == "PAUSED"


def test_progress_and_cancellation_are_recorded(world):
    store, adapter = world
    task_id = adapter.submit_task(objective="Cancel me", capability_scope=["read_only"],
                                  idempotency_key=uuid.uuid4().hex, requester_device_id="d" * 32)["task_id"]
    adapter.report_progress(task_id, "planning", "a" * 64)
    assert "link.progress" in _event_types(store, task_id)
    assert adapter.cancel_task(task_id, "cancel-1")["state"] == "CANCELLED"
    assert store.get_task(task_id)["state"] == "CANCELLED"
    assert adapter.cancel_task(task_id, "cancel-1")["state"] == "CANCELLED"  # idempotent
    with pytest.raises(SupervisorRefused):
        adapter.report_progress(uuid.uuid4().hex, "stage")
