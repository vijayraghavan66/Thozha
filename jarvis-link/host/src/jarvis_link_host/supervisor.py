"""Narrow, injectable host-to-supervisor boundary.

Implementations receive structured task/approval data only. There is deliberately
no shell or generic tool execution method on this interface.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol

from .trusted_apps import resolve_trusted_app


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    task_id: str
    operation: str
    target: str
    arguments_digest: str
    expires_at_unix_ms: int
    status: str = "PENDING"


class SupervisorPort(Protocol):
    def submit_task(self, *, objective: str, capability_scope: list[str], idempotency_key: str,
                    requester_device_id: str) -> dict[str, Any]: ...
    def get_approval(self, approval_id: str) -> ApprovalRecord | None: ...
    def list_pending_approvals(self, task_id: str) -> list[dict[str, Any]]: ...
    def decide_approval(
        self, *, task_id: str, approval_id: str, decision: str,
        expected_arguments_digest: str, idempotency_key: str,
        requester_device_id: str,
        continuation_objective: str | None = None,
    ) -> dict[str, Any]: ...
    def cancel_task(self, task_id: str, idempotency_key: str) -> dict[str, Any]: ...
    def report_progress(self, task_id: str, stage: str, result_digest: str = "") -> dict[str, Any]: ...


class MockSupervisor:
    """Safe local simulator. It never invokes a process or a real tool."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, ApprovalRecord] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.decision_owners: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.app_open_result: dict[str, Any] | None = None
        self.task_owners: dict[str, str] = {}

    def add_approval(self, record: ApprovalRecord) -> None:
        self.approvals[record.approval_id] = record

    def fast_command(self, *, command: str, **arguments: Any) -> dict[str, Any]:
        """Return deterministic fixtures only; this test double never performs host actions."""
        self.calls.append(("fast_command", {"command": command, **arguments}))
        if command == "mac.status":
            return {"state": "COMPLETED", "executed": True, "success": True,
                    "online": True, "ai_available": True,
                    "summary": "Mac is online. AI is available."}
        if command == "mac.battery":
            return {"state": "COMPLETED", "executed": True, "success": True,
                    "battery_percent": 79, "charging": False, "summary": "Battery level: 79%."}
        if command == "task.status":
            task_id = arguments["task_id"]
            task = self.tasks.get(task_id)
            if task is None:
                return {"state": "UNKNOWN"}
            return {"task_id": task_id, "state": task["state"]}
        if command == "app.open":
            if self.app_open_result is not None:
                return dict(self.app_open_result)
            app = resolve_trusted_app(arguments.get("app"))
            if app is not None and arguments.get("app") == app.bundle_id:
                return {"state": "COMPLETED", "executed": True, "success": True,
                        "bundle_id": app.bundle_id, "summary": f"{app.display_name} opened."}
        raise ValueError("unsupported fast command")

    def submit_task(self, *, objective: str, capability_scope: list[str], idempotency_key: str,
                    requester_device_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", requester_device_id or ""):
            raise ValueError("invalid requester device ID")
        if idempotency_key in self.decisions:
            prior = self.decisions[idempotency_key]
            if self.task_owners.get(prior.get("task_id", "")) != requester_device_id:
                raise ValueError("task idempotency key belongs to another companion")
            return prior
        task_id = idempotency_key[:32].ljust(32, "0")
        result = {"task_id": task_id, "state": "RUNNING", "simulated": True}
        self.tasks[task_id] = {"objective": objective, "capability_scope": list(capability_scope), "state": "RUNNING"}
        self.task_owners[task_id] = requester_device_id
        self.calls.append(("submit_task", {
            "task_id": task_id,
            "objective": objective,
            "capability_scope": list(capability_scope),
            "idempotency_key": idempotency_key,
            "requester_device_id": requester_device_id,
        }))
        self.decisions[idempotency_key] = result
        self.decision_owners[idempotency_key] = requester_device_id
        return result

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return self.approvals.get(approval_id)

    def list_pending_approvals(self, task_id: str) -> list[dict[str, Any]]:
        return [
            {
                "approval_id": item.approval_id,
                "task_id": item.task_id,
                "operation": item.operation,
                "target": item.target,
                "canonical_arguments_digest": item.arguments_digest,
                "expires_at_unix_ms": item.expires_at_unix_ms,
                "redacted_summary": "Exact operation requires your approval.",
            }
            for item in self.approvals.values()
            if item.task_id == task_id and item.status == "PENDING"
        ]

    def decide_approval(self, *, task_id: str, approval_id: str, decision: str,
                        expected_arguments_digest: str, idempotency_key: str,
                        requester_device_id: str,
                        continuation_objective: str | None = None) -> dict[str, Any]:
        if (not re.fullmatch(r"[0-9a-f]{32}", requester_device_id or "")
                or self.task_owners.get(task_id) != requester_device_id):
            raise ValueError("approval task belongs to another companion")
        if idempotency_key in self.decisions:
            prior = self.decisions[idempotency_key]
            if self.decision_owners.get(idempotency_key) != requester_device_id:
                raise ValueError("approval idempotency key belongs to another companion")
            return prior
        record = self.approvals.get(approval_id)
        if record is None or record.task_id != task_id or record.arguments_digest != expected_arguments_digest:
            raise ValueError("supervisor approval binding mismatch")
        if record.status != "PENDING":
            raise ValueError("supervisor approval is not pending")
        status = "APPROVED" if decision == "approve" else "REJECTED"
        result = {
            "task_id": task_id,
            "approval_id": approval_id,
            "status": status,
            "continuation_objective": continuation_objective,
            "simulated": True,
        }
        self.calls.append(("decide_approval", {**result, "requester_device_id": requester_device_id}))
        self.decision_owners[idempotency_key] = requester_device_id
        if decision == "approve":
            self.tasks.setdefault(task_id, {})["state"] = "RUNNING"
        else:
            self.tasks.setdefault(task_id, {})["state"] = "PAUSED"
        self.approvals[approval_id] = ApprovalRecord(
            record.approval_id, record.task_id, record.operation, record.target,
            record.arguments_digest, record.expires_at_unix_ms, status,
        )
        self.decisions[idempotency_key] = result
        return result

    def cancel_task(self, task_id: str, idempotency_key: str) -> dict[str, Any]:
        if idempotency_key in self.decisions:
            return self.decisions[idempotency_key]
        if task_id not in self.tasks:
            raise KeyError(task_id)
        self.tasks[task_id]["state"] = "CANCELLED"
        result = {"task_id": task_id, "state": "CANCELLED", "simulated": True}
        self.calls.append(("cancel_task", dict(result)))
        self.decisions[idempotency_key] = result
        return result

    def report_progress(self, task_id: str, stage: str, result_digest: str = "") -> dict[str, Any]:
        if task_id not in self.tasks:
            raise KeyError(task_id)
        event = {"task_id": task_id, "stage": stage, "result_digest": result_digest}
        self.calls.append(("report_progress", event))
        return {"status": "recorded", **event}
