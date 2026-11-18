from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

from jarvis_link_host.concurrent_dispatch import ConcurrentRelayDispatch


def test_slow_task_does_not_block_other_task_or_control_and_no_duplicate_dispatch():
    release = Event()
    began = Event()
    completed = Event()
    control = Event()
    messages = [
        {"envelope": {"from_device_id": "p", "message_id": "slow", "kind": "task.submit"}},
        {"envelope": {"from_device_id": "p", "message_id": "fast", "kind": "task.submit"}},
        {"envelope": {"from_device_id": "p", "message_id": "cancel", "kind": "task.cancel"}},
    ]
    seen = []

    class Runtime:
        host = SimpleNamespace(device_id="host")
        relay = SimpleNamespace(inbox=lambda **kwargs: list(messages))

        def poll_once(self, *, records):
            item = records[0]["envelope"]["message_id"]
            seen.append(item)
            if item == "slow":
                began.set()
                assert release.wait(5)
            elif item == "fast":
                completed.set()
            else:
                control.set()
            messages.remove(records[0])

    runner = ConcurrentRelayDispatch(Runtime(), task_workers=2)
    try:
        runner.poll_once()
        assert began.wait(2)
        assert completed.wait(2)
        assert control.wait(2)
        # Relay still contains the slow, unacknowledged record.
        runner.poll_once()
        assert seen.count("slow") == 1
    finally:
        release.set()
        runner.close()
    assert sorted(seen) == ["cancel", "fast", "slow"]


def test_failed_work_remains_eligible_for_redelivery():
    message = {"envelope": {"from_device_id": "p", "message_id": "x", "kind": "task.submit"}}
    calls = []

    class Runtime:
        host = SimpleNamespace(device_id="host")
        relay = SimpleNamespace(inbox=lambda **kwargs: [message])

        def poll_once(self, *, records):
            calls.append(1)
            raise RuntimeError("temporary")

    runner = ConcurrentRelayDispatch(Runtime(), task_workers=1)
    try:
        runner.poll_once()
        deadline = monotonic() + 3
        while len(calls) < 2 and monotonic() < deadline:
            sleep(.01)
            runner.poll_once()
        assert len(calls) >= 2
    finally:
        runner.close()


def test_saturated_workers_do_not_fill_executor_backlog():
    release = Event()
    messages = [{"envelope": {"from_device_id": "p", "message_id": str(n), "kind": "task.submit"}} for n in range(20)]
    control = {"envelope": {"from_device_id": "p", "message_id": "control", "kind": "task.cancel"}}
    messages.append(control)
    started = []
    control_seen = Event()

    class Runtime:
        host = SimpleNamespace(device_id="host")
        relay = SimpleNamespace(inbox=lambda **kwargs: list(messages))

        def poll_once(self, *, records):
            message = records[0]["envelope"]["message_id"]
            started.append(message)
            if message == "control":
                control_seen.set()
            else:
                assert release.wait(5)

    runner = ConcurrentRelayDispatch(Runtime(), task_workers=2)
    try:
        result = runner.poll_once()
        assert result == {"scheduled": 3, "in_flight": 3}
        assert control_seen.wait(2)
        assert runner.poll_once()["in_flight"] <= 3
        assert len([x for x in started if x != "control"]) <= 2
    finally:
        release.set()
        runner.close()


def test_submission_ledger_binds_a_key_to_one_digest():
    import pytest
    from jarvis_link_host.openjarvis_api_adapter import _SubmissionLedger
    from jarvis_link_host.openjarvis_adapter import SupervisorRefused

    ledger = _SubmissionLedger(None)
    assert ledger.reserve("key", "a") is None
    assert ledger.reserve("key", "a")["digest"] == "a"
    with pytest.raises(SupervisorRefused):
        ledger.reserve("key", "b")
