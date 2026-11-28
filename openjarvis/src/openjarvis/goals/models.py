"""Typed contracts for short, durable goal executions.

The goal engine stores state and schedules finite steps. It does not expose a
shell, browser, LLM, payment, or other privileged action API.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable


class GoalError(RuntimeError):
    """Base error for goal state and validation failures."""


class GoalValidationError(GoalError, ValueError):
    pass


class GoalNotFound(GoalError, KeyError):
    pass


class GoalConflict(GoalError):
    pass


class LeaseLost(GoalConflict):
    pass


class GoalStatus(StrEnum):
    ACTIVE = "active"
    WAITING_UNTIL = "waiting_until"
    WAITING_APPROVAL = "waiting_approval"
    NEEDS_USER = "needs_user"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class GoalEventType(StrEnum):
    CREATED = "goal.created"
    CLAIMED = "goal.claimed"
    STEP_STARTED = "goal.step_started"
    STEP_FINISHED = "goal.step_finished"
    STEP_RETRY_SCHEDULED = "goal.step_retry_scheduled"
    LEASE_RECOVERED = "goal.lease_recovered"
    NEEDS_REVIEW = "goal.needs_review"
    WOKEN = "goal.woken"
    APPROVAL_RESOLVED = "goal.approval_resolved"
    APPROVAL_REJECTED = "goal.approval_rejected"
    APPROVAL_EXPIRED = "goal.approval_expired"
    CANCELLED = "goal.cancelled"
    COMPLETED = "goal.completed"
    FAILED = "goal.failed"
    EXPIRED = "goal.expired"


class GoalActionKind(StrEnum):
    READ_ONLY = "read_only"
    SUPERVISOR_ACTION = "supervisor_action"


class GoalDisposition(StrEnum):
    CONTINUE = "continue"
    WAITING_UNTIL = "waiting_until"
    WAITING_APPROVAL = "waiting_approval"
    NEEDS_USER = "needs_user"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    FAILED = "failed"


class GoalWakeReason(StrEnum):
    TIMER = "timer"
    USER_RESPONSE = "user_response"
    APPROVAL_RESOLVED = "approval_resolved"
    CONNECTION_RECOVERED = "connection_recovered"
    USER_REQUESTED = "user_requested"
    USER_CONFIRMED_STATE = "user_confirmed_state"


class SupervisorApprovalOutcome(StrEnum):
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"


_OWNER_RE = re.compile(r"^device:[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_GOAL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SECRET_KEY_RE = re.compile(r"(?:password|passwd|cookie|secret|token|otp|pin|credential|authorization|payment)", re.IGNORECASE)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:one[- ]time password|otp|upi\s*pin|password|passcode|pin|api[_ -]?key|access token|token|cookie|authorization|secret)\s*(?:is|=|:|#)\s*[^\s,;]{4,}"),
    re.compile(r"(?i)\b(?:otp|one[- ]time password|upi\s*pin|pin)\s+\d{4,8}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}"),
)
_ALLOWED_EVIDENCE_KEYS = frozenset({
    "source", "observed_at", "record_count", "availability", "stale",
    "step_id", "action_id", "digest", "approval_id", "provider", "origin_label",
})
MAX_GOAL_OBJECTIVE_CHARS = 20_000
MAX_GOAL_TITLE_CHARS = 96
MAX_GOAL_CHECKPOINT_BYTES = 24_000
MAX_GOAL_PREFERENCES_BYTES = 8_000
MAX_GOAL_EVENT_BYTES = 4_000
MAX_GOAL_STEP_SECONDS = 120


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_datetime(value: datetime | str, field_name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GoalValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise GoalValidationError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def datetime_text(value: datetime | str) -> str:
    return normalize_datetime(value, "timestamp").isoformat(timespec="milliseconds")


def _validate_json_object(value: dict[str, Any], *, label: str, limit: int, reject_secrets: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GoalValidationError(f"{label} must be an object")
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 80:
            raise GoalValidationError(f"{label} keys must be short strings")
        if reject_secrets and _SECRET_KEY_RE.search(key):
            raise GoalValidationError(f"{label} cannot store credential-like fields")
        if reject_secrets and _contains_credential_value(item):
            raise GoalValidationError(f"{label} cannot store credential-like values")
        if isinstance(item, (dict, list)):
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
            if len(encoded.encode("utf-8")) > limit:
                raise GoalValidationError(f"{label} is too large")
            # Walk nested objects so a credential-like key cannot hide in preferences.
            if reject_secrets:
                _validate_nested_keys(item, label=label)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise GoalValidationError(f"{label} must contain JSON-compatible values") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise GoalValidationError(f"{label} is too large")
    return value


def _validate_nested_keys(value: Any, *, label: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or _SECRET_KEY_RE.search(key):
                raise GoalValidationError(f"{label} cannot store credential-like fields")
            if _contains_credential_value(child):
                raise GoalValidationError(f"{label} cannot store credential-like values")
            _validate_nested_keys(child, label=label)
    elif isinstance(value, list):
        for child in value:
            if _contains_credential_value(child):
                raise GoalValidationError(f"{label} cannot store credential-like values")
            _validate_nested_keys(child, label=label)


def _contains_credential_value(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)
    if isinstance(value, dict):
        return any(_SECRET_KEY_RE.search(str(key)) or _contains_credential_value(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_credential_value(child) for child in value)
    return False


def validate_evidence(value: dict[str, Any] | None) -> dict[str, Any]:
    """Allow only small, non-sensitive evidence metadata into the event ledger."""
    value = value or {}
    if not isinstance(value, dict):
        raise GoalValidationError("evidence must be an object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _ALLOWED_EVIDENCE_KEYS:
            raise GoalValidationError("evidence contains an unsupported field")
        if isinstance(item, str):
            if len(item) > 240 or any(ord(char) < 0x20 for char in item):
                raise GoalValidationError("evidence text is invalid")
            result[key] = item
        elif isinstance(item, bool) and key == "stale":
            result[key] = item
        elif isinstance(item, int) and 0 <= item <= 1_000_000:
            result[key] = item
        else:
            raise GoalValidationError("evidence values must be short strings, booleans, or bounded counts")
        if key == "approval_id" and (not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{32}", item)):
            raise GoalValidationError("approval_id evidence must be a supervisor approval ID")
        if key == "digest" and (not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)):
            raise GoalValidationError("digest evidence must be a canonical SHA-256 digest")
    if len(json.dumps(result, separators=(",", ":")).encode("utf-8")) > MAX_GOAL_EVENT_BYTES:
        raise GoalValidationError("evidence is too large")
    return result


def sanitize_public_summary(value: str, limit: int = 500) -> str:
    """Bound a public status sentence and redact common inline credential forms."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    text = re.sub(r"(?i)\b(?:one[- ]time password|otp|upi\s*pin|pin|password|passcode|api[_ -]?key|access token|token|cookie|authorization|secret)\s*(?:is|=|:|#)\s*[^\s,;]{4,}", "[credential redacted]", text)
    text = re.sub(r"(?i)\b(?:otp|one[- ]time password|upi\s*pin|pin)\s+\d{4,8}\b", "[credential redacted]", text)
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}", "[credential redacted]", text)
    text = " ".join(text.split())[:limit]
    return text or "Goal status updated."


@dataclass(frozen=True)
class GoalPolicy:
    max_attempts: int = 8
    retry_base_seconds: int = 15
    retry_max_seconds: int = 900
    max_live_step_seconds: int = 60

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 50:
            raise GoalValidationError("max_attempts must be between 1 and 50")
        if not 1 <= self.retry_base_seconds <= 3600:
            raise GoalValidationError("retry_base_seconds must be between 1 and 3600")
        if not self.retry_base_seconds <= self.retry_max_seconds <= 86_400:
            raise GoalValidationError("retry_max_seconds must be between retry_base_seconds and 86400")
        if not 1 <= self.max_live_step_seconds <= MAX_GOAL_STEP_SECONDS:
            raise GoalValidationError(f"max_live_step_seconds must be between 1 and {MAX_GOAL_STEP_SECONDS}")


@dataclass(frozen=True)
class GoalCreate:
    owner_actor: str
    idempotency_key: str
    title: str
    objective: str
    deadline_at: datetime | str
    preferences: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    policy: GoalPolicy = field(default_factory=GoalPolicy)
    next_wake_at: datetime | str | None = None

    def validated(self) -> "GoalCreate":
        if not isinstance(self.owner_actor, str) or not _OWNER_RE.fullmatch(self.owner_actor):
            raise GoalValidationError("owner_actor must be a paired device identity")
        if not isinstance(self.idempotency_key, str) or not _IDEMPOTENCY_RE.fullmatch(self.idempotency_key):
            raise GoalValidationError("idempotency_key must be URL-safe and 16–128 characters")
        if not isinstance(self.title, str):
            raise GoalValidationError("title is required")
        title = " ".join(self.title.split())
        if not title or len(title) > MAX_GOAL_TITLE_CHARS or any(ord(char) < 0x20 for char in title):
            raise GoalValidationError("title must be short printable text")
        if _contains_credential_value(title):
            raise GoalValidationError("title cannot include credential-like values")
        if not isinstance(self.objective, str) or not self.objective.strip() or len(self.objective) > MAX_GOAL_OBJECTIVE_CHARS:
            raise GoalValidationError("objective must be non-empty and bounded")
        if _contains_credential_value(self.objective):
            raise GoalValidationError("objective cannot include credential-like values")
        deadline = normalize_datetime(self.deadline_at, "deadline_at")
        wake = normalize_datetime(self.next_wake_at, "next_wake_at") if self.next_wake_at is not None else utc_now()
        if wake >= deadline:
            raise GoalValidationError("next_wake_at must be before the goal deadline")
        if not isinstance(self.policy, GoalPolicy):
            raise GoalValidationError("policy must be a GoalPolicy")
        _validate_json_object(self.preferences, label="preferences", limit=MAX_GOAL_PREFERENCES_BYTES, reject_secrets=True)
        _validate_json_object(self.checkpoint, label="checkpoint", limit=MAX_GOAL_CHECKPOINT_BYTES, reject_secrets=True)
        return GoalCreate(
            owner_actor=self.owner_actor, idempotency_key=self.idempotency_key,
            title=title, objective=self.objective.strip(), deadline_at=deadline,
            preferences=self.preferences, checkpoint=self.checkpoint, policy=self.policy,
            next_wake_at=wake,
        )


@dataclass(frozen=True)
class GoalSnapshot:
    goal_id: str
    owner_actor: str
    idempotency_key: str
    title: str
    summary: str
    objective: str
    preferences: dict[str, Any]
    status: GoalStatus
    deadline_at: datetime
    next_wake_at: datetime | None
    checkpoint: dict[str, Any]
    checkpoint_version: int
    step_index: int
    attempts_used: int
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int
    max_live_step_seconds: int
    lease_expires_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime

    def to_wire(self) -> dict[str, Any]:
        """Typed phone-safe projection; private objective/preferences/checkpoint are omitted by default."""
        result = {
            "goal_id": self.goal_id,
            "title": self.title,
            "summary": self.summary,
            "status": self.status.value,
            "deadline_at": datetime_text(self.deadline_at),
            "next_wake_at": datetime_text(self.next_wake_at) if self.next_wake_at else None,
            "checkpoint_version": self.checkpoint_version,
            "step_index": self.step_index,
            "attempts_used": self.attempts_used,
            "attempts_remaining": max(0, self.max_attempts - self.attempts_used),
            "lease_expires_at": datetime_text(self.lease_expires_at) if self.lease_expires_at else None,
            "last_error_code": self.last_error_code,
            "created_at": datetime_text(self.created_at),
            "updated_at": datetime_text(self.updated_at),
        }
        return result

    def to_execution_view(self) -> "GoalExecutionView":
        """Return only the owner-independent task input required by pure planners/adapters."""
        return GoalExecutionView(
            goal_id=self.goal_id,
            title=self.title,
            objective=self.objective,
            preferences=json.loads(json.dumps(self.preferences, ensure_ascii=False, allow_nan=False)),
            status=self.status,
            deadline_at=self.deadline_at,
            next_wake_at=self.next_wake_at,
            checkpoint=json.loads(json.dumps(self.checkpoint, ensure_ascii=False, allow_nan=False)),
            checkpoint_version=self.checkpoint_version,
            step_index=self.step_index,
            attempts_remaining=max(0, self.max_attempts - self.attempts_used),
            max_live_step_seconds=self.max_live_step_seconds,
        )


@dataclass(frozen=True)
class GoalExecutionView:
    goal_id: str
    title: str
    objective: str
    preferences: dict[str, Any]
    status: GoalStatus
    deadline_at: datetime
    next_wake_at: datetime | None
    checkpoint: dict[str, Any]
    checkpoint_version: int
    step_index: int
    attempts_remaining: int
    max_live_step_seconds: int


@dataclass(frozen=True)
class GoalEvent:
    seq: int
    goal_id: str
    event_type: GoalEventType
    created_at: datetime
    summary: str
    checkpoint_version: int
    progress: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "goal_id": self.goal_id,
            "type": self.event_type.value,
            "created_at": datetime_text(self.created_at),
            "summary": self.summary,
            "checkpoint_version": self.checkpoint_version,
            "progress": self.progress,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class GoalLease:
    goal: GoalSnapshot
    worker_id: str
    token: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class GoalApprovalBinding:
    goal_id: str
    owner_actor: str
    run_key: str
    approval_id: str
    supervisor_task_id: str
    action_digest: str
    expires_at: datetime | None
    status: str


@dataclass(frozen=True)
class ProviderPollDecision:
    allowed: bool
    provider: str
    minimum_interval_seconds: int
    next_allowed_at: datetime
    last_read_at: datetime | None
    policy_source: str = "local_policy"

    def to_wire(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "provider": self.provider,
            "minimum_interval_seconds": self.minimum_interval_seconds,
            "next_allowed_at": datetime_text(self.next_allowed_at),
            "last_read_at": datetime_text(self.last_read_at) if self.last_read_at else None,
            "policy_source": self.policy_source,
        }


@dataclass(frozen=True)
class GoalStepPlan:
    step_key: str
    action_kind: GoalActionKind
    description: str
    max_live_step_seconds: int
    action_digest: str | None = None

    def validated(self, policy: GoalPolicy) -> "GoalStepPlan":
        if not isinstance(self.step_key, str) or not re.fullmatch(r"[a-zA-Z0-9._:-]{1,80}", self.step_key):
            raise GoalValidationError("step_key is invalid")
        try:
            kind = GoalActionKind(self.action_kind)
        except (ValueError, TypeError) as exc:
            raise GoalValidationError("action_kind is invalid") from exc
        description = " ".join(str(self.description).split())
        if not description or len(description) > 240:
            raise GoalValidationError("step description must be short")
        if not 1 <= self.max_live_step_seconds <= policy.max_live_step_seconds:
            raise GoalValidationError("step duration exceeds the goal policy")
        action_digest = self.action_digest
        if kind == GoalActionKind.SUPERVISOR_ACTION:
            if not isinstance(action_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", action_digest):
                raise GoalValidationError("supervisor actions require the exact canonical action digest")
        elif action_digest is not None:
            raise GoalValidationError("read-only steps cannot carry an action approval digest")
        return GoalStepPlan(self.step_key, kind, description, self.max_live_step_seconds, action_digest)


@dataclass(frozen=True)
class GoalStepContext:
    goal: GoalExecutionView
    plan: GoalStepPlan
    idempotency_key: str
    timeout_seconds: int
    cancel_event: Any
    approved_action: GoalApprovalBinding | None = None
    mark_supervisor_action_dispatched: Callable[[str], bool] | None = None
    reserve_provider_poll: Callable[[str, int], ProviderPollDecision] | None = None


@dataclass(frozen=True)
class GoalStepResult:
    disposition: GoalDisposition
    summary: str
    checkpoint: dict[str, Any] | None = None
    progress: int | None = None
    next_wake_at: datetime | str | None = None
    retryable: bool = False
    retry_after_seconds: int | None = None
    error_code: str | None = None
    approval_id: str | None = None
    supervisor_task_id: str | None = None
    approval_expires_at: datetime | str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    supervisor_action_result: str | None = None

    def validated(self, plan: GoalStepPlan, *, now: datetime) -> "GoalStepResult":
        try:
            disposition = GoalDisposition(self.disposition)
        except (ValueError, TypeError) as exc:
            raise GoalValidationError("step disposition is invalid") from exc
        summary = sanitize_public_summary(self.summary)
        if self.progress is not None and not 0 <= self.progress <= 100:
            raise GoalValidationError("progress must be between 0 and 100")
        if self.retry_after_seconds is not None and not 1 <= self.retry_after_seconds <= 86_400:
            raise GoalValidationError("retry_after_seconds must be between 1 and 86400")
        checkpoint = self.checkpoint
        if checkpoint is not None:
            _validate_json_object(checkpoint, label="checkpoint", limit=MAX_GOAL_CHECKPOINT_BYTES, reject_secrets=True)
        wake = normalize_datetime(self.next_wake_at, "next_wake_at") if self.next_wake_at is not None else None
        if disposition == GoalDisposition.WAITING_UNTIL and (wake is None or wake <= now):
            raise GoalValidationError("waiting_until requires a future next_wake_at")
        if disposition != GoalDisposition.WAITING_UNTIL and wake is not None and wake <= now:
            raise GoalValidationError("next_wake_at must be in the future")
        if plan.action_kind == GoalActionKind.SUPERVISOR_ACTION and disposition not in {
            GoalDisposition.WAITING_APPROVAL, GoalDisposition.NEEDS_USER,
            GoalDisposition.NEEDS_REVIEW, GoalDisposition.FAILED,
            GoalDisposition.COMPLETED,
        }:
            raise GoalValidationError("supervisor actions must stop at approval or a user-review boundary")
        approval_id = self.approval_id
        if approval_id is not None and not re.fullmatch(r"[0-9a-f]{32}", approval_id):
            raise GoalValidationError("approval_id must be a supervisor approval ID")
        supervisor_task_id = self.supervisor_task_id
        if supervisor_task_id is not None and not re.fullmatch(r"[0-9a-f]{32}", supervisor_task_id):
            raise GoalValidationError("supervisor_task_id must be a supervisor task ID")
        if disposition == GoalDisposition.WAITING_APPROVAL:
            if plan.action_kind != GoalActionKind.SUPERVISOR_ACTION or not approval_id or not supervisor_task_id or not self.approval_expires_at:
                raise GoalValidationError("waiting_approval requires a bound supervisor task, approval ID, and expiry")
        elif approval_id is not None or supervisor_task_id is not None or self.approval_expires_at is not None:
            raise GoalValidationError("supervisor approval identifiers are only valid for waiting_approval")
        action_result = self.supervisor_action_result
        if action_result is not None and action_result not in {"completed", "failed", "unknown"}:
            raise GoalValidationError("supervisor_action_result is invalid")
        if action_result is not None and plan.action_kind != GoalActionKind.SUPERVISOR_ACTION:
            raise GoalValidationError("read-only steps cannot carry a supervisor action result")
        approval_expires_at = normalize_datetime(self.approval_expires_at, "approval_expires_at") if self.approval_expires_at else None
        if approval_expires_at and approval_expires_at <= now:
            raise GoalValidationError("approval_expires_at must be in the future")
        evidence = validate_evidence(self.evidence)
        error_code = self.error_code
        if error_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code):
            raise GoalValidationError("error_code must be a symbolic label")
        return GoalStepResult(
            disposition, summary, checkpoint, self.progress, wake,
            bool(self.retryable), self.retry_after_seconds, error_code, approval_id,
            supervisor_task_id, approval_expires_at, evidence, action_result,
        )


@dataclass(frozen=True)
class GoalRunReport:
    goal_id: str
    status: GoalStatus
    step_key: str | None
    summary: str
    next_wake_at: datetime | None = None


Planner = Callable[[GoalExecutionView], Any]
