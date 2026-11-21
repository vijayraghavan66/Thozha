"""Supervisor-bound MCP adapter for bounded Jarvis development capabilities.

This adapter is intended for a trusted, task-scoped MCP process. The task ID is
provided by that process's supervisor launch context; owner/device identity is
always re-read from the supervisor database and is never accepted from tool args.
It creates one plan-level approval per development task and delegates every
operation to ``CapabilityStore`` for durable quota, expiry, replay and cancellation
checks. It does not execute tests or write files itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from openjarvis.tasks.dev_capability import (
    PLAN_APPROVAL_OPERATION,
    CapabilityError,
    CapabilitySpec,
    CapabilityStore,
    build_spec,
    plan_approval_arguments,
    plan_approval_target,
)
from openjarvis.tasks.supervisor import (
    LINK_SCOPE_CAPABILITIES,
    SupervisorStore,
    operation_digest,
    utc_now,
)

_DEVICE_ACTOR = re.compile(r"device:([0-9a-f]{32})\Z")
_OP_ID = re.compile(r"[A-Za-z0-9._~-]{8,128}\Z")
_ACTIVE_STATES = {"CREATED", "PLANNING", "RUNNING", "VERIFYING"}
_LIVE_STATES = {"PLANNING", "RUNNING", "VERIFYING"}


@dataclass(frozen=True)
class PlanApproval:
    approval_id: str
    args_digest: str
    plan_digest: str
    status: str


__all__ = ["DevelopmentCapabilityMCPAdapter", "PlanApproval"]


class DevelopmentCapabilityMCPAdapter:
    """Bound MCP facade for one supervisor-owned development task.

    ``task_id``, ``user_id`` and ``workspace_root`` are server-side configuration,
    never model-provided tool arguments. The owner actor and device ID come from the
    task's supervisor metadata. Plan quotas and operation/path lists remain user-visible
    and are covered by the single supervisor approval before any capability is issued.
    """

    def __init__(
        self,
        supervisor_store: SupervisorStore,
        capability_store: CapabilityStore,
        *,
        task_id: str,
        user_id: str,
        workspace_root: str,
        capability_ttl_s: int = 600,
        max_duration_s: int = 900,
    ) -> None:
        if not isinstance(supervisor_store, SupervisorStore) or not isinstance(
            capability_store, CapabilityStore
        ):
            raise TypeError(
                "canonical SupervisorStore and CapabilityStore instances are required"
            )
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise CapabilityError("TASK_INVALID", "task ID is invalid")
        if (
            not isinstance(user_id, str)
            or not 1 <= len(user_id) <= 128
            or any(ord(c) < 32 for c in user_id)
        ):
            raise CapabilityError("IDENTITY_MISMATCH", "configured user ID is invalid")
        if (
            not 1 <= capability_ttl_s <= 4 * 60 * 60
            or not 1 <= max_duration_s <= 4 * 60 * 60
        ):
            raise CapabilityError(
                "INVALID_SPEC", "configured capability lifetime is out of bounds"
            )
        from openjarvis.tasks.dev_capability import canonical_workspace

        self.supervisor_store = supervisor_store
        self.capability_store = capability_store
        self.task_id = task_id
        self.user_id = user_id
        self.workspace_root = canonical_workspace(workspace_root)
        self.capability_ttl_s = capability_ttl_s
        self.max_duration_s = max_duration_s
        with capability_store._lock:
            capability_store._db.execute(
                """CREATE TABLE IF NOT EXISTS dev_plan_approval_bindings (
                    task_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL UNIQUE,
                    plan_digest TEXT NOT NULL, args_digest TEXT NOT NULL,
                    spec_json TEXT NOT NULL, created_at REAL NOT NULL)"""
            )

    @classmethod
    def from_task_environment(
        cls,
        supervisor_store: SupervisorStore,
        capability_store: CapabilityStore,
        *,
        user_id: str,
        workspace_root: str,
        capability_ttl_s: int = 600,
        max_duration_s: int = 900,
    ) -> "DevelopmentCapabilityMCPAdapter":
        """Bind to supervisor-injected task context; tools cannot select a task."""
        task_id = os.environ.get("JARVIS_SUPERVISOR_TASK_ID", "")
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise CapabilityError(
                "TASK_REQUIRED", "supervisor task context is missing or invalid"
            )
        return cls(
            supervisor_store,
            capability_store,
            task_id=task_id,
            user_id=user_id,
            workspace_root=workspace_root,
            capability_ttl_s=capability_ttl_s,
            max_duration_s=max_duration_s,
        )

    def _task_binding(
        self,
        *,
        allow_waiting: bool = False,
        allow_cancelled: bool = False,
    ) -> tuple[dict[str, Any], str, str]:
        task = self.supervisor_store.get_task(self.task_id)
        if task is None:
            raise CapabilityError("TASK_UNKNOWN", "supervisor task does not exist")
        try:
            metadata = json.loads(task.get("recovery_json") or "{}")
            scope = json.loads(task.get("capabilities_json") or "[]")
        except (TypeError, json.JSONDecodeError) as exc:
            raise CapabilityError(
                "TASK_INVALID", "supervisor task metadata is invalid"
            ) from exc
        owner = metadata.get("owner_actor")
        match = _DEVICE_ACTOR.fullmatch(owner or "")
        if (
            metadata.get("source") != "jarvis_link"
            or metadata.get("scope") != "development"
            or metadata.get("capability_scope") != ["development"]
            or scope != sorted(LINK_SCOPE_CAPABILITIES["development"])
            or match is None
        ):
            raise CapabilityError(
                "TASK_SCOPE_MISMATCH",
                "task is not an owned Jarvis Link development task",
            )
        allowed = _ACTIVE_STATES | ({"WAITING_APPROVAL"} if allow_waiting else set())
        if allow_cancelled:
            allowed.add("CANCELLED")
        if task.get("state") not in allowed:
            raise CapabilityError(
                "TASK_INACTIVE", "development task is not in an executable state"
            )
        return task, owner, match.group(1)

    @staticmethod
    def _encode_spec(spec: CapabilitySpec) -> str:
        return json.dumps(
            {
                **spec.__dict__,
                "allowed_ops": sorted(spec.allowed_ops),
                "allowed_paths": list(spec.allowed_paths),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @staticmethod
    def _decode_spec(payload: str) -> CapabilitySpec:
        try:
            raw = json.loads(payload)
            raw["allowed_ops"] = frozenset(raw["allowed_ops"])
            raw["allowed_paths"] = tuple(raw["allowed_paths"])
            spec = CapabilitySpec(**raw)
            spec.validate_bounds()
            return spec
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise CapabilityError(
                "PLAN_INVALID", "persisted development plan is invalid"
            ) from exc

    def request_plan_approval(
        self, plan: dict[str, Any], *, reason: str
    ) -> PlanApproval:
        """Create or recover this task's exact plan approval; never decides it."""
        task, owner, device_id = self._task_binding(allow_waiting=True)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise CapabilityError(
                "PLAN_INVALID", "approval reason must be between 1 and 1000 characters"
            )
        spec = build_spec(
            plan=plan,
            device_id=device_id,
            user_id=self.user_id,
            workspace_root=self.workspace_root,
            ttl_s=self.capability_ttl_s,
            max_duration_s=self.max_duration_s,
        )
        target = plan_approval_target(spec)
        arguments = plan_approval_arguments(spec)
        digest = operation_digest(PLAN_APPROVAL_OPERATION, target, arguments)
        encoded_spec = self._encode_spec(spec)
        store = self.capability_store

        # Lock the local plan slot while interacting with SupervisorStore. If this
        # process exits after supervisor approval creation but before binding commit,
        # the exact pending approval is recovered by digest on the next call.
        with store._lock:
            store._db.execute("BEGIN IMMEDIATE")
            try:
                row = store._db.execute(
                    "SELECT approval_id,plan_digest,args_digest,spec_json "
                    "FROM dev_plan_approval_bindings WHERE task_id=?",
                    (self.task_id,),
                ).fetchone()
                if row:
                    if (
                        row["plan_digest"] != spec.plan_digest
                        or row["args_digest"] != digest
                        or row["spec_json"] != encoded_spec
                    ):
                        raise CapabilityError(
                            "PLAN_ALREADY_BOUND",
                            "this task already has a different bounded plan",
                        )
                    approval_id = row["approval_id"]
                    approval = self.supervisor_store.approval(approval_id)
                    self._validate_approval(
                        approval, spec, target, digest, owner, self.task_id
                    )
                    store._db.execute("COMMIT")
                    return PlanApproval(
                        approval_id, digest, spec.plan_digest, approval["status"]
                    )

                # Recover an orphaned external approval if the prior process stopped
                # between the SupervisorStore commit and this store's binding insert.
                approval_id = self._find_orphaned_approval(spec, target, digest)
                if approval_id:
                    approval = self.supervisor_store.approval(approval_id)
                    self._validate_approval(
                        approval, spec, target, digest, owner, self.task_id
                    )
                else:
                    if task.get("state") not in _LIVE_STATES:
                        raise CapabilityError(
                            "TASK_STATE_MISMATCH",
                            "task cannot request a development plan approval",
                        )
                    approval_id, created_digest = self.supervisor_store.create_approval(
                        self.task_id,
                        PLAN_APPROVAL_OPERATION,
                        target,
                        arguments,
                        reason.strip(),
                        ttl_seconds=self.capability_ttl_s,
                    )
                    if created_digest != digest:
                        raise CapabilityError(
                            "PLAN_MISMATCH",
                            "supervisor approval digest did not match the plan",
                        )
                    approval = self.supervisor_store.approval(approval_id)
                    self._validate_approval(
                        approval, spec, target, digest, owner, self.task_id
                    )

                store._db.execute(
                    "INSERT INTO dev_plan_approval_bindings "
                    "(task_id,approval_id,plan_digest,args_digest,"
                    "spec_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        self.task_id,
                        approval_id,
                        spec.plan_digest,
                        digest,
                        encoded_spec,
                        time.time(),
                    ),
                )
                store._db.execute("COMMIT")
                return PlanApproval(
                    approval_id, digest, spec.plan_digest, approval["status"]
                )
            except Exception:
                store._db.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_approval(
        approval: dict[str, Any] | None,
        spec: CapabilitySpec,
        target: str,
        args_digest: str,
        owner_actor: str,
        task_id: str,
    ) -> None:
        if approval is None:
            raise CapabilityError(
                "APPROVAL_UNKNOWN", "bound supervisor approval no longer exists"
            )
        if (
            approval.get("task_id") != task_id
            or approval.get("operation") != PLAN_APPROVAL_OPERATION
            or approval.get("target") != target
            or approval.get("args_digest") != args_digest
        ):
            raise CapabilityError(
                "PLAN_MISMATCH", "supervisor approval does not match this exact plan"
            )
        status = approval.get("status")
        if status in {"REJECTED", "EXPIRED"}:
            raise CapabilityError(
                "APPROVAL_INACTIVE", "plan approval was rejected or expired"
            )
        if status not in {"PENDING", "APPROVED", "CONSUMED"}:
            raise CapabilityError(
                "APPROVAL_INACTIVE", "plan approval is not in an issuable state"
            )
        if (
            status in {"PENDING", "APPROVED"}
            and approval.get("expires_at", "") <= utc_now()
        ):
            raise CapabilityError("APPROVAL_EXPIRED", "plan approval has expired")
        if (
            status in {"APPROVED", "CONSUMED"}
            and approval.get("decided_by") != owner_actor
        ):
            raise CapabilityError(
                "APPROVER_IDENTITY_MISMATCH",
                "approval was not decided by this task's paired device",
            )
        if status == "CONSUMED" and (
            not approval.get("consumed_at")
            or not approval.get("expires_at")
            or approval["consumed_at"] >= approval["expires_at"]
        ):
            raise CapabilityError(
                "APPROVAL_INACTIVE", "approval was not consumed before its expiry"
            )
        if status in {"PENDING", "APPROVED"}:
            try:
                arguments = json.loads(approval.get("arguments_json") or "null")
            except (TypeError, json.JSONDecodeError) as exc:
                raise CapabilityError(
                    "PLAN_MISMATCH", "supervisor approval arguments are invalid"
                ) from exc
            if arguments != plan_approval_arguments(spec):
                raise CapabilityError(
                    "PLAN_MISMATCH",
                    "supervisor approval arguments do not match this plan",
                )

    def _find_orphaned_approval(
        self, spec: CapabilitySpec, target: str, args_digest: str
    ) -> str | None:
        candidates: set[str] = set()
        for event in self.supervisor_store.events(self.task_id):
            if event.get("event_type") != "approval.created":
                continue
            payload = event.get("payload") or {}
            approval_id = payload.get("approval_id")
            if not isinstance(approval_id, str):
                continue
            approval = self.supervisor_store.approval(approval_id)
            if (
                approval
                and approval.get("task_id") == self.task_id
                and approval.get("operation") == PLAN_APPROVAL_OPERATION
                and approval.get("target") == target
                and approval.get("args_digest") == args_digest
                and approval.get("status") in {"PENDING", "APPROVED", "CONSUMED"}
            ):
                candidates.add(approval_id)
        if len(candidates) > 1:
            raise CapabilityError(
                "APPROVAL_AMBIGUOUS", "multiple matching plan approvals exist"
            )
        return next(iter(candidates), None)

    def issue_approved_plan(self, approval_id: str) -> str:
        """Issue the task's registered plan only after its real approval."""
        _task, owner, _device_id = self._task_binding(allow_waiting=True)
        with self.capability_store._lock:
            row = self.capability_store._db.execute(
                "SELECT approval_id,spec_json FROM dev_plan_approval_bindings "
                "WHERE task_id=?",
                (self.task_id,),
            ).fetchone()
        if row is None or row["approval_id"] != approval_id:
            raise CapabilityError(
                "APPROVAL_MISMATCH",
                "approval ID is not the one registered for this task",
            )
        spec = self._decode_spec(row["spec_json"])
        _task, actual_owner, device_id = self._task_binding(allow_waiting=True)
        if (
            actual_owner != owner
            or device_id != spec.device_id
            or self.user_id != spec.user_id
            or self.workspace_root != spec.workspace_root
        ):
            raise CapabilityError(
                "IDENTITY_MISMATCH", "task identity or configured workspace changed"
            )
        return self.capability_store.issue(
            spec,
            supervisor_store=self.supervisor_store,
            approval_id=approval_id,
            task_id=self.task_id,
            requester_actor=owner,
        )

    def prepare_approved_plan_issuance(self, approval_id: str) -> None:
        """Persist an inert issuance intent before an outer dispatcher consumes the approval."""
        _task, owner, device_id = self._task_binding(allow_waiting=True)
        with self.capability_store._lock:
            row = self.capability_store._db.execute(
                "SELECT approval_id,spec_json FROM dev_plan_approval_bindings WHERE task_id=?",
                (self.task_id,),
            ).fetchone()
        if row is None or row["approval_id"] != approval_id:
            raise CapabilityError(
                "APPROVAL_MISMATCH", "approval ID is not the plan registered for this task"
            )
        spec = self._decode_spec(row["spec_json"])
        if (
            spec.device_id != device_id
            or spec.user_id != self.user_id
            or spec.workspace_root != self.workspace_root
        ):
            raise CapabilityError(
                "IDENTITY_MISMATCH", "plan identity or workspace changed before approval consumption"
            )
        approval = self.supervisor_store.approval(approval_id)
        self._validate_approval(
            approval, spec, plan_approval_target(spec),
            operation_digest(
                PLAN_APPROVAL_OPERATION,
                plan_approval_target(spec),
                plan_approval_arguments(spec),
            ),
            owner,
            self.task_id,
        )
        if approval.get("status") != "APPROVED":
            raise CapabilityError(
                "SUPERVISOR_APPROVAL_REQUIRED", "plan approval is not ready for consumption"
            )
        self.capability_store._reserve_issuance_intent(
            spec,
            approval_id=approval_id,
            task_id=self.task_id,
            requester_actor=owner,
            approved_by=owner,
        )

    def _operation_context(self, capability_id: str) -> tuple[str, str, str]:
        _task, owner, device_id = self._task_binding()
        try:
            spec, *_ = self.capability_store._load(capability_id)
        except CapabilityError:
            raise
        if (
            spec.get("device_id") != device_id
            or spec.get("user_id") != self.user_id
            or spec.get("workspace_root") != self.workspace_root
        ):
            raise CapabilityError(
                "IDENTITY_MISMATCH",
                "capability does not match this task identity and workspace",
            )
        return device_id, self.user_id, owner

    @staticmethod
    def _validate_operation_id(op_id: str) -> None:
        if not isinstance(op_id, str) or not _OP_ID.fullmatch(op_id):
            raise CapabilityError(
                "OP_ID_INVALID", "operation ID must be 8–128 URL-safe characters"
            )

    def authorize_write(
        self, capability_id: str, *, op_id: str, rel_path: str, content: str | bytes
    ) -> dict[str, Any]:
        """Bound one exact payload before the MCP caller writes it."""
        self._validate_operation_id(op_id)
        if not isinstance(content, (str, bytes)):
            raise CapabilityError(
                "CONTENT_INVALID", "file content must be UTF-8 text or bytes"
            )
        data = content.encode("utf-8") if isinstance(content, str) else content
        device_id, user_id, owner = self._operation_context(capability_id)
        content_digest = hashlib.sha256(data).hexdigest()
        target = self.capability_store.authorize_write(
            capability_id,
            device_id=device_id,
            user_id=user_id,
            op_id=op_id,
            rel_path=rel_path,
            size=len(data),
            content_sha256=content_digest,
            supervisor_store=self.supervisor_store,
            task_id=self.task_id,
            requester_actor=owner,
        )
        return {"target": target, "bytes": len(data), "sha256": content_digest}

    def authorize_test(
        self, capability_id: str, *, op_id: str, command: str
    ) -> tuple[str, ...]:
        """Authorize a fixed test identifier; arbitrary shell is impossible."""
        self._validate_operation_id(op_id)
        device_id, user_id, owner = self._operation_context(capability_id)
        return self.capability_store.authorize_test(
            capability_id,
            device_id=device_id,
            user_id=user_id,
            op_id=op_id,
            command=command,
            supervisor_store=self.supervisor_store,
            task_id=self.task_id,
            requester_actor=owner,
        )

    def artifact_manifest(
        self, capability_id: str, *, op_id: str, rel_paths: list[str],
        file_reader: Callable[[str, int], bytes] | None = None,
    ) -> dict[str, Any]:
        """Return a bounded hash manifest for artifacts under the approved workspace."""
        self._validate_operation_id(op_id)
        device_id, user_id, owner = self._operation_context(capability_id)
        return self.capability_store.artifact_manifest(
            capability_id,
            device_id=device_id,
            user_id=user_id,
            task_id=self.task_id,
            requester_actor=owner,
            supervisor_store=self.supervisor_store,
            op_id=op_id,
            rel_paths=rel_paths,
            file_reader=file_reader,
        )

    def artifact_export(
        self, capability_id: str, *, op_id: str, rel_paths: list[str],
        file_reader: Callable[[str, int], bytes],
    ) -> dict[str, Any]:
        """Authorize bounded archive inputs and retrieve only those guest files."""
        self._validate_operation_id(op_id)
        device_id, user_id, owner = self._operation_context(capability_id)
        return self.capability_store.artifact_export(
            capability_id, device_id=device_id, user_id=user_id, task_id=self.task_id,
            requester_actor=owner, supervisor_store=self.supervisor_store, op_id=op_id,
            rel_paths=rel_paths, file_reader=file_reader,
        )

    def consume_repair(self, capability_id: str, *, op_id: str) -> int:
        """Spend one repair allowance without increasing any other plan limit."""
        self._validate_operation_id(op_id)
        device_id, user_id, owner = self._operation_context(capability_id)
        return self.capability_store.consume_repair(
            capability_id,
            device_id=device_id,
            user_id=user_id,
            op_id=op_id,
            supervisor_store=self.supervisor_store,
            task_id=self.task_id,
            requester_actor=owner,
        )

    def revoke_after_cancellation(self) -> int:
        """Revoke capabilities only after supervisor cancellation is durable."""
        _task, owner, _device_id = self._task_binding(allow_cancelled=True)
        return self.capability_store.revoke_for_supervisor_task(
            self.supervisor_store,
            task_id=self.task_id,
            requester_actor=owner,
        )
