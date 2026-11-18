"""Bounded relay dispatch without acknowledging unfinished work.

The relay retains each signed ciphertext until the existing runtime has persisted
and delivered its response. After a host restart it can be redelivered, and the
supervisor's idempotency key prevents a second task from being created. This is
not an independent durable AI work queue and does not claim prompt acceptance.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .runtime import HostRelayRuntime


class ConcurrentRelayDispatch:
    def __init__(self, runtime: HostRelayRuntime, *, task_workers: int = 3) -> None:
        if not 1 <= task_workers <= 8:
            raise ValueError("task worker count must be between 1 and 8")
        self.runtime = runtime
        self.task_workers = task_workers
        self._tasks = ThreadPoolExecutor(max_workers=task_workers, thread_name_prefix="link-task")
        self._control = ThreadPoolExecutor(max_workers=1, thread_name_prefix="link-control")
        self._active: dict[tuple[str, str], tuple[Future, bool]] = {}

    def poll_once(self, *, limit: int = 50) -> dict[str, int]:
        # Called by one coordinator only. No executor queue can grow without bound.
        for key, (future, _) in list(self._active.items()):
            if future.done():
                try:
                    future.result()
                except Exception:
                    # No ACK is emitted by this coordinator. Relay redelivery is safe.
                    pass
                del self._active[key]
        records = self.runtime.relay.inbox(device_id=self.runtime.host.device_id, limit=limit)
        task_count = sum(is_task for _, is_task in self._active.values())
        control_count = len(self._active) - task_count
        scheduled = 0
        for record in records:
            envelope = record.get("envelope")
            if not isinstance(envelope, dict):
                continue
            sender = envelope.get("from_device_id")
            message_id = envelope.get("message_id")
            if not isinstance(sender, str) or not isinstance(message_id, str):
                continue
            key = (sender, message_id)
            if key in self._active:
                continue
            # This untrusted header only chooses a bounded scheduler; the runtime
            # independently authenticates and validates every envelope.
            is_task = envelope.get("kind") == "task.submit"
            if (is_task and task_count >= self.task_workers) or (not is_task and control_count >= 1):
                continue
            pool = self._tasks if is_task else self._control
            future = pool.submit(self.runtime.poll_once, records=[record])
            self._active[key] = (future, is_task)
            task_count += int(is_task)
            control_count += int(not is_task)
            scheduled += 1
        return {"scheduled": scheduled, "in_flight": len(self._active)}

    def close(self) -> None:
        self._tasks.shutdown(wait=True, cancel_futures=True)
        self._control.shutdown(wait=True, cancel_futures=True)
