"""Short-lived, isolated, read-only browser sessions for goal providers."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .policy import ActionDecision, ActionKind, BrowserActionPolicy, sanitize_provider_value


class BrowserPolicyError(PermissionError):
    """A browser navigation or session request failed closed."""


@dataclass(frozen=True, slots=True)
class BrowserObservation:
    provider_id: str
    origin: str
    title: str
    visible_text: str
    observed_at: str
    handoff_required: bool = False
    handoff_reason: str | None = None

    def to_model_context(self) -> dict[str, Any]:
        """Minimized observation; never contains a profile path, URL query, or raw HTML."""
        return {
            "provider": self.provider_id,
            "origin": self.origin,
            "title": self.title,
            "visible_text": self.visible_text,
            "observed_at": self.observed_at,
            "handoff_required": self.handoff_required,
            "handoff_reason": self.handoff_reason,
        }


@dataclass(slots=True)
class BrowserSession:
    """One bounded browser context. Closing it keeps its isolated user profile."""

    provider_id: str
    policy: BrowserActionPolicy
    context: Any = field(repr=False)
    page: Any = field(repr=False)
    _playwright: Any = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def navigate(self, url: str) -> None:
        if self._closed:
            raise BrowserPolicyError("browser session is closed")
        decision = self.policy.evaluate(ActionKind.NAVIGATE, url)
        if decision.decision != ActionDecision.ALLOW:
            raise BrowserPolicyError(decision.reason_code)
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        except Exception as exc:
            # Do not propagate Playwright exception strings: they can include
            # the full URL, query parameters, or browser profile details.
            raise BrowserPolicyError("provider navigation failed") from None
        final_url = getattr(self.page, "url", "")
        final_decision = self.policy.evaluate(ActionKind.OBSERVE, final_url)
        if final_decision.decision != ActionDecision.ALLOW:
            raise BrowserPolicyError("provider redirected outside its authorized origin")

    async def observe(self, *, max_chars: int = 20_000) -> BrowserObservation:
        if self._closed:
            raise BrowserPolicyError("browser session is closed")
        if not 1 <= max_chars <= 50_000:
            raise ValueError("observation size is out of bounds")
        url = getattr(self.page, "url", "")
        decision = self.policy.evaluate(ActionKind.OBSERVE, url)
        if decision.decision != ActionDecision.ALLOW:
            raise BrowserPolicyError(decision.reason_code)
        try:
            title = await self.page.title()
            body = await self.page.locator("body").inner_text(timeout=5_000)
        except Exception:
            raise BrowserPolicyError("provider page could not be read") from None
        safe_title = _sanitize_visible_text(title, max_chars=300)
        safe_body = _sanitize_visible_text(body, max_chars=max_chars)
        handoff = _classify_handoff(f"{title}\n{body}")
        if handoff is not None:
            # Suppress challenge text and surrounding page content; the user
            # receives only a generic handoff reason and completes it directly.
            safe_body = ""
        origin = _safe_origin(url)
        return BrowserObservation(
            provider_id=self.provider_id,
            origin=origin,
            title=safe_title,
            visible_text=safe_body,
            observed_at=datetime.now(timezone.utc).isoformat(),
            handoff_required=handoff is not None,
            handoff_reason=handoff,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.context.close()
        finally:
            if self._playwright is not None:
                await self._playwright.stop()

    async def __aenter__(self) -> "BrowserSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


class BrowserSessionManager:
    """Create separate persistent Playwright profiles per owner/provider.

    Playwright is imported lazily and is an optional dependency. The browser
    context is always closed after one finite goal step; its owner-only profile
    persists for later steps. Browser controls intentionally expose only
    navigation and sanitized observation. Site-specific mutations use an
    official provider adapter and the existing supervisor approval boundary.
    """

    def __init__(
        self,
        profile_root: str | os.PathLike[str],
        *,
        launcher: Any | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.profile_root = Path(profile_root).expanduser().absolute()
        self._launcher = launcher
        self._now = now or (lambda: datetime.now(timezone.utc))

    def profile_path(self, owner_id: str, provider_id: str) -> Path:
        if not owner_id or len(owner_id) > 256 or not provider_id or len(provider_id) > 80:
            raise ValueError("owner or provider identity is invalid")
        if "\x00" in owner_id or "\x00" in provider_id:
            raise ValueError("owner or provider identity is invalid")
        self._ensure_private_root()
        key = hashlib.sha256(f"{owner_id}\0{provider_id}".encode("utf-8")).hexdigest()
        profile = self.profile_root / key
        if profile.is_symlink():
            raise PermissionError("browser profile must not be a symlink")
        if not profile.exists():
            profile.mkdir(mode=0o700)
        info = profile.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("browser profile must be an owner-owned directory")
        os.chmod(profile, 0o700)
        if stat.S_IMODE(profile.stat().st_mode) != 0o700:
            raise PermissionError("browser profile permissions could not be restricted")
        return profile

    async def open(
        self,
        owner_id: str,
        policy: BrowserActionPolicy,
        *,
        headless: bool = True,
        timeout_ms: int = 10_000,
    ) -> BrowserSession:
        if not policy.authorization_ready:
            raise BrowserPolicyError("provider session is not explicitly authorized")
        if policy.provider.integration not in {"user_browser", "fixture"}:
            raise BrowserPolicyError("provider is configured for MCP, not browser automation")
        if not 1_000 <= timeout_ms <= 60_000:
            raise ValueError("browser timeout is out of bounds")
        profile = self.profile_path(owner_id, policy.provider.provider_id)
        playwright = None
        launcher = self._launcher
        if launcher is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError("install the optional browser extra to use a browser session") from exc
            playwright = await async_playwright().start()
            launcher = playwright.chromium
        try:
            context = await launcher.launch_persistent_context(
                user_data_dir=str(profile),
                headless=headless,
                accept_downloads=False,
                permissions=[],
                service_workers="block",
            )
            context.set_default_timeout(timeout_ms)
            await context.route("**/*", self._route_guard(policy))
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
        except Exception:
            if playwright is not None:
                await playwright.stop()
            raise BrowserPolicyError("isolated browser session could not start") from None
        return BrowserSession(
            provider_id=policy.provider.provider_id,
            policy=policy,
            context=context,
            page=page,
            _playwright=playwright,
        )

    @staticmethod
    def _route_guard(policy: BrowserActionPolicy) -> Callable[[Any], Any]:
        async def guard(route: Any) -> None:
            request = getattr(route, "request", None)
            url = getattr(request, "url", "")
            resource_type = getattr(request, "resource_type", "document")
            decision = policy.evaluate(
                ActionKind.NAVIGATE,
                url,
                resource=resource_type != "document",
            )
            if decision.decision == ActionDecision.ALLOW:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")

        return guard

    def _ensure_private_root(self) -> None:
        root = self.profile_root
        if _has_symlink_component(root):
            raise PermissionError("browser profile path must not contain symlinks")
        if not root.exists():
            root.mkdir(mode=0o700, parents=True)
        info = root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("browser profile root must be an owner-owned directory")
        os.chmod(root, 0o700)
        if stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise PermissionError("browser profile root permissions could not be restricted")


def _classify_handoff(text: str) -> str | None:
    lowered = text.lower()
    if re.search(r"\b(up[iy]\s*pin)\b", lowered):
        return "upi_authentication_required"
    if re.search(r"\b(otp|one[- ]time (?:password|code)|verification code)\b", lowered):
        return "one_time_code_required"
    if re.search(r"\b(captcha|verify you are human|prove you are human|robot check)\b", lowered):
        return "captcha_requires_user"
    if re.search(r"\b(queue|waiting room|high demand)\b", lowered):
        return "provider_queue_requires_user"
    if re.search(r"\b(sign in|log in|login required)\b", lowered):
        return "provider_sign_in_required"
    return None


_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(?:otp|one[- ]time (?:password|code)|verification code)\s*[:=]?\s*\d{4,8}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\bUPI\s*PIN\s*[:=]?\s*\d{4,6}\b"), "UPI PIN [REDACTED]"),
    (re.compile(r"(?i)\b(?:password|passcode|secret|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*\S+"), "[REDACTED]"),
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "[EMAIL REDACTED]"),
)


def _sanitize_visible_text(text: object, *, max_chars: int) -> str:
    value = text if isinstance(text, str) else ""
    value = value[:max_chars]
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    value = sanitize_provider_value(value)
    return value if isinstance(value, str) else ""


def _safe_origin(url: str) -> str:
    from .policy import origin_of

    try:
        return origin_of(url)
    except ValueError:
        return ""


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False
