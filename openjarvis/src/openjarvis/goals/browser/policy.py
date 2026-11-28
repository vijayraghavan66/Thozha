"""Origin, action, and polling policy for goal-provider sessions.

Policy is deny-by-default. Merely knowing a URL or having a browser profile does
not authorize requests. A provider must be explicitly enabled for an official
MCP integration or an owner-authorized browser session.
"""

from __future__ import annotations

import threading
import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable
from urllib.parse import urlsplit


class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    OBSERVE = "observe"
    READ_TOOL = "read_tool"
    SELECT_LOCAL_SEATS = "select_local_seats"
    HOLD_SEATS = "hold_seats"
    BOOK = "book"
    PAYMENT = "payment"
    LOGIN = "login"
    OTP = "otp"
    UPI_PIN = "upi_pin"
    CAPTCHA = "captcha"
    QUEUE = "queue"
    CAPTCHA_BYPASS = "captcha_bypass"
    QUEUE_BYPASS = "queue_bypass"
    GENERIC_CLICK = "generic_click"
    FORM_SUBMIT = "form_submit"


class ActionDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    HANDOFF = "handoff"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: ActionDecision
    reason_code: str


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Non-secret provider policy. Origins are exact scheme/host/port triples."""

    provider_id: str
    origins: tuple[str, ...]
    resource_origins: tuple[str, ...] = ()
    integration: str = "official_mcp"
    enabled: bool = False
    partner_enabled: bool = False
    owner_authorized: bool = False
    min_poll_interval_seconds: int = 900
    allow_seat_holds: bool = False
    allow_booking: bool = False
    allow_payment: bool = False
    read_tools: frozenset[str] = field(default_factory=frozenset)
    read_scope: str = "district"
    action_scopes: tuple[tuple[str, str], ...] = ()
    poll_interval_source: str = "application_conservative_default"

    def __post_init__(self) -> None:
        if not self.provider_id or len(self.provider_id) > 80:
            raise ValueError("provider_id is invalid")
        if self.integration not in {"official_mcp", "user_browser", "fixture"}:
            raise ValueError("provider integration is unsupported")
        if not 1 <= self.min_poll_interval_seconds <= 86_400:
            raise ValueError("minimum polling interval is out of bounds")
        if not self.read_scope or len(self.read_scope) > 100:
            raise ValueError("provider read scope is invalid")
        if self.poll_interval_source not in {"provider_published", "application_conservative_default"}:
            raise ValueError("poll interval source is invalid")
        for action_name, scope in self.action_scopes:
            if not action_name or not scope or len(action_name) > 80 or len(scope) > 100:
                raise ValueError("provider action scope is invalid")
        for origin in (*self.origins, *self.resource_origins):
            _canonical_origin(origin, allow_http=self.integration == "fixture")
        if self.integration == "official_mcp" and self.allow_booking:
            raise ValueError("official MCP booking requires a separate booking adapter")


@dataclass(frozen=True, slots=True)
class ProviderAuthorization:
    """Claims extracted by an OAuth/owner verifier; never raw token material."""

    provider_id: str
    integration: str
    issued_at: datetime
    expires_at: datetime
    allowed_origins: tuple[str, ...]
    read_only: bool = True
    grant_type: str = ""
    scopes: frozenset[str] = field(default_factory=frozenset)
    principal: str = ""

    def valid_at(self, now: datetime) -> bool:
        return (
            self.provider_id != ""
            and self.integration in {"official_mcp", "user_browser", "fixture"}
            and self.issued_at.tzinfo is not None
            and self.expires_at.tzinfo is not None
            and self.issued_at <= now < self.expires_at
            and (self.integration == "fixture" or (self.grant_type and len(self.grant_type) <= 120))
        )


@dataclass(frozen=True, slots=True)
class SupervisorApproval:
    """A projection of a supervisor-verified approval; never a caller boolean."""

    approval_id: str
    action_digest: str
    expires_at: datetime
    decision: str
    status: str
    execution_claimed: bool = False


ApprovalVerifier = Callable[[str, str], SupervisorApproval | None]
ProviderAuthorizationVerifier = Callable[[ProviderAuthorization], bool]


class BrowserActionPolicy:
    """A small action firewall; callers still use the existing supervisor."""

    _HANDOFF = {ActionKind.LOGIN, ActionKind.OTP, ActionKind.UPI_PIN, ActionKind.CAPTCHA, ActionKind.QUEUE}
    _FORBIDDEN = {ActionKind.CAPTCHA_BYPASS, ActionKind.QUEUE_BYPASS}
    _MUTATING = {ActionKind.HOLD_SEATS, ActionKind.BOOK, ActionKind.PAYMENT, ActionKind.FORM_SUBMIT}

    def __init__(
        self,
        provider: ProviderConfig,
        authorization: ProviderAuthorization | None,
        *,
        approval_verifier: ApprovalVerifier | None = None,
        authorization_verifier: ProviderAuthorizationVerifier | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.authorization = authorization
        self._approval_verifier = approval_verifier
        self._authorization_verifier = authorization_verifier
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._allowed_origins = frozenset(
            _canonical_origin(value, allow_http=provider.integration == "fixture")
            for value in provider.origins
        )
        self._resource_origins = frozenset(
            _canonical_origin(value, allow_http=provider.integration == "fixture")
            for value in (*provider.origins, *provider.resource_origins)
        )

    @property
    def authorization_ready(self) -> bool:
        auth = self.authorization
        if not self.provider.enabled or auth is None:
            return False
        if auth.provider_id != self.provider.provider_id or auth.integration != self.provider.integration:
            return False
        if not auth.valid_at(self._now()):
            return False
        if self.provider.integration == "fixture":
            return True
        if self._authorization_verifier is None:
            return False
        try:
            if not self._authorization_verifier(auth):
                return False
        except Exception:
            return False
        if self.provider.integration == "official_mcp":
            return self.provider.partner_enabled and self.provider.read_scope in auth.scopes
        if self.provider.integration == "user_browser":
            return self.provider.owner_authorized and self.provider.read_scope in auth.scopes
        return False

    def allows_origin(self, url: str, *, resource: bool = False) -> bool:
        try:
            origin = _request_origin(url, allow_http=self.provider.integration == "fixture")
        except ValueError:
            return False
        allowlist = self._resource_origins if resource else self._allowed_origins
        if origin not in allowlist:
            return False
        auth = self.authorization
        if auth is None:
            return False
        try:
            authorized = frozenset(
                _canonical_origin(value, allow_http=self.provider.integration == "fixture")
                for value in auth.allowed_origins
            )
        except ValueError:
            return False
        return origin in authorized and self.authorization_ready

    def evaluate(
        self,
        action: ActionKind,
        url: str,
        *,
        action_digest: str | None = None,
        approval_id: str | None = None,
        resource: bool = False,
    ) -> PolicyDecision:
        """Evaluate an explicit action without logging URL or supplied data."""
        if action in self._FORBIDDEN:
            return PolicyDecision(ActionDecision.DENY, "challenge_bypass_forbidden")
        if not self.authorization_ready:
            return PolicyDecision(ActionDecision.DENY, "provider_not_authorized")
        if not self.allows_origin(url, resource=resource):
            return PolicyDecision(ActionDecision.DENY, "origin_not_allowed")
        if action in self._HANDOFF:
            return PolicyDecision(ActionDecision.HANDOFF, "user_authentication_required")
        if action == ActionKind.SELECT_LOCAL_SEATS:
            return PolicyDecision(ActionDecision.ALLOW, "local_fixture_selection_only")
        if action == ActionKind.NAVIGATE or action == ActionKind.OBSERVE or action == ActionKind.READ_TOOL:
            return PolicyDecision(ActionDecision.ALLOW, "authorized_read")
        if action not in self._MUTATING:
            return PolicyDecision(ActionDecision.DENY, "action_not_allowlisted")
        if action == ActionKind.HOLD_SEATS and not self.provider.allow_seat_holds:
            return PolicyDecision(ActionDecision.DENY, "seat_holds_disabled")
        if action == ActionKind.BOOK and not self.provider.allow_booking:
            return PolicyDecision(ActionDecision.DENY, "booking_disabled")
        if action == ActionKind.PAYMENT and not self.provider.allow_payment:
            return PolicyDecision(ActionDecision.DENY, "payment_disabled")
        auth = self.authorization
        if auth is None or auth.read_only:
            return PolicyDecision(ActionDecision.DENY, "provider_grant_read_only")
        required_scope = dict(self.provider.action_scopes).get(action.value)
        if not required_scope or required_scope not in auth.scopes:
            return PolicyDecision(ActionDecision.DENY, "provider_action_scope_missing")
        if not action_digest or not approval_id or self._approval_verifier is None:
            return PolicyDecision(ActionDecision.REQUIRE_APPROVAL, "supervisor_approval_required")
        approval = self._approval_verifier(approval_id, action_digest)
        if (
            approval is None
            or approval.action_digest != action_digest
            or approval.decision != "approve"
            or approval.status not in {"APPROVED", "CONSUMED"}
            or approval.execution_claimed
        ):
            return PolicyDecision(ActionDecision.DENY, "approval_invalid_or_replayed")
        if approval.expires_at.tzinfo is None or approval.expires_at <= self._now():
            return PolicyDecision(ActionDecision.DENY, "approval_expired")
        # Payment remains a user handoff even when a goal is approved; credentials
        # and the final payment control are never driven by the browser worker.
        if action == ActionKind.PAYMENT:
            return PolicyDecision(ActionDecision.HANDOFF, "payment_requires_user")
        return PolicyDecision(ActionDecision.ALLOW, "approved_exact_action")


class PollIntervalGate:
    """Persistent-store-friendly minimum interval guard; it never sleeps/polls."""

    def __init__(
        self,
        minimum_interval_seconds: int,
        *,
        last_reads: dict[str, str] | None = None,
        max_keys: int = 10_000,
    ) -> None:
        if not 1 <= minimum_interval_seconds <= 86_400:
            raise ValueError("minimum polling interval is out of bounds")
        if not 1 <= max_keys <= 100_000:
            raise ValueError("poll checkpoint size is out of bounds")
        self.minimum_interval = timedelta(seconds=minimum_interval_seconds)
        self._max_keys = max_keys
        self._last_read: dict[str, datetime] = {}
        for key, stamp in (last_reads or {}).items():
            if not isinstance(key, str) or not key or len(key) > 200:
                continue
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError):
                continue
            if parsed.tzinfo is not None:
                self._last_read[key] = parsed.astimezone(timezone.utc)
        if len(self._last_read) > self._max_keys:
            self._last_read = dict(list(self._last_read.items())[-self._max_keys :])
        self._lock = threading.Lock()

    def permit(self, key: str, *, now: datetime | None = None) -> tuple[bool, datetime | None]:
        if not key or len(key) > 200:
            return False, None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("poll timestamps must include a timezone")
        current = current.astimezone(timezone.utc)
        with self._lock:
            previous = self._last_read.get(key)
            if previous is not None:
                eligible = previous + self.minimum_interval
                if current < eligible:
                    return False, eligible
            if key not in self._last_read and len(self._last_read) >= self._max_keys:
                cutoff = current - self.minimum_interval
                self._last_read = {
                    existing_key: stamp
                    for existing_key, stamp in self._last_read.items()
                    if stamp > cutoff
                }
                if len(self._last_read) >= self._max_keys:
                    return False, current + self.minimum_interval
            self._last_read[key] = current
            return True, None

    def snapshot(self) -> dict[str, str]:
        """Export a bounded checkpoint for the durable goal store."""
        with self._lock:
            return {
                key: stamp.isoformat()
                for key, stamp in self._last_read.items()
            }

    def defer_until(self, key: str, next_allowed_at: datetime) -> None:
        """Persist a provider Retry-After deadline in the exported gate state."""
        if not key or len(key) > 200 or next_allowed_at.tzinfo is None:
            raise ValueError("poll deferral is invalid")
        next_allowed_at = next_allowed_at.astimezone(timezone.utc)
        with self._lock:
            self._last_read[key] = next_allowed_at - self.minimum_interval


def _canonical_origin(value: str, *, allow_http: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("provider origin is invalid")
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise ValueError("provider origins must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("provider origin contains disallowed URL components")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "*" in host or host in {"localhost", "127.0.0.1", "::1"} and not allow_http:
        raise ValueError("provider host is not allowed")
    if not allow_http:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("provider origins must use an explicit DNS name")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider port is invalid") from exc
    if parsed.path not in {"", "/"}:
        raise ValueError("provider origins cannot contain a path")
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


def origin_of(url: str) -> str:
    """Return an HTTP(S) origin, rejecting credentials and opaque URLs."""
    return _request_origin(url, allow_http=False)


def _request_origin(value: str, *, allow_http: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError("provider URL is invalid")
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise ValueError("provider URLs must use HTTPS")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("provider URL contains disallowed components")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "*" in host or host in {"localhost", "127.0.0.1", "::1"} and not allow_http:
        raise ValueError("provider host is not allowed")
    if not allow_http:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("provider URLs must use an explicit DNS name")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider port is invalid") from exc
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


def sanitize_provider_value(value: object, *, key: str = "") -> object:
    """Remove credential-shaped values before a provider result reaches an LLM/log."""
    if key.lower() in {
        "authorization", "cookie", "set-cookie", "password", "passcode",
        "token", "access_token", "refresh_token", "session", "session_id",
        "temptransid", "confirmation_id", "authorization_code", "code_verifier",
        "otp", "verification_code", "upi_pin", "pin",
    }:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize_provider_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_provider_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_provider_value(item) for item in value)
    if isinstance(value, str):
        import re

        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
        text = re.sub(r"(?i)\b(?:otp|one[- ]time (?:password|code)|verification code)\s*[:=]?\s*\d{4,8}\b", "[REDACTED]", text)
        text = re.sub(r"(?i)\bUPI\s*PIN\s*[:=]?\s*\d{4,6}\b", "UPI PIN [REDACTED]", text)
        return text
    return value
