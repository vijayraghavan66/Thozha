"""Provider boundary for official, explicitly enabled MCP integrations."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .policy import (
    ActionDecision,
    ActionKind,
    BrowserActionPolicy,
    sanitize_provider_value,
)


class ProviderAccessDenied(PermissionError):
    """A provider is disabled or a requested tool is outside its read allowlist."""


class ProviderToolClass(str, Enum):
    READ = "read"
    RESERVATION = "reservation"
    HOLD = "hold"
    BOOK = "book"
    PAY = "pay"
    ACCOUNT = "account"
    UNKNOWN = "unknown"


def classify_provider_tool(tool: dict[str, Any]) -> ProviderToolClass:
    """Classify an advertised MCP tool conservatively; unknown means deny.

    A provider `readOnlyHint` is required to promote an otherwise ambiguous
    tool to READ. Name/description matching only classifies mutating/account
    verbs downward; it never grants access.
    """
    if not isinstance(tool, dict):
        return ProviderToolClass.UNKNOWN
    name = tool.get("name")
    description = tool.get("description", "")
    annotations = tool.get("annotations")
    if not isinstance(name, str) or not name or len(name) > 100:
        return ProviderToolClass.UNKNOWN
    name_text = name.lower()
    text = f"{name} {description if isinstance(description, str) else ''}".lower()
    if any(term in text for term in ("captcha bypass", "queue bypass", "bypass captcha", "bypass queue")):
        return ProviderToolClass.UNKNOWN
    if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
        # District's read-only search description includes booking examples.
        # Trust the explicit annotation before interpreting descriptive prose.
        if any(term in name_text for term in ("payment", "pay_option", "pay_status")):
            return ProviderToolClass.PAY
        if any(term in name_text for term in ("booking", "ticket", "refund", "account")):
            return ProviderToolClass.ACCOUNT
        return ProviderToolClass.READ
    if name_text in {"generate_otp", "validate_otp", "resend_otp"} or any(
        term in name_text for term in ("login", "account", "my_booking", "my_ticket", "refund")
    ):
        return ProviderToolClass.ACCOUNT
    if "payment" in name_text or name_text in {"pay", "cancel_payment"}:
        return ProviderToolClass.PAY
    if any(term in name_text for term in ("hold", "reserve")):
        return ProviderToolClass.HOLD if any(term in name_text for term in ("seat", "ticket")) else ProviderToolClass.RESERVATION
    if any(term in name_text for term in ("book", "purchase", "confirm")):
        return ProviderToolClass.BOOK
    if name_text in {"act", "perform_action", "execute_action"}:
        return ProviderToolClass.RESERVATION
    if any(term in name_text for term in ("search", "discover", "showtime", "seat_map", "seatmap", "offer", "detail", "catalog")):
        return ProviderToolClass.READ
    return ProviderToolClass.UNKNOWN


class ReadOnlyMcpTransport(Protocol):
    """Injected authenticated transport; token storage stays outside this adapter."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider_id: str
    tool_name: str
    payload: Any

    def to_model_context(self) -> dict[str, Any]:
        """Return the minimized, credential-scrubbed provider result."""
        return {
            "provider": self.provider_id,
            "tool": self.tool_name,
            "result": sanitize_provider_value(self.payload),
        }


class OfficialMcpProvider:
    """Read-only official MCP adapter with an injected user-authorized session.

    This class never authenticates, refreshes tokens, or creates a network
    client. The caller injects an official provider transport after the owner
    has enabled the app and completed sign-in in the system browser. Booking
    and payment tools must be implemented through a separate, supervisor-bound
    action adapter; this class only invokes the provider's read allowlist.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        endpoint: str,
        read_tools: frozenset[str],
        policy: BrowserActionPolicy,
        transport: ReadOnlyMcpTransport,
    ) -> None:
        if not read_tools or any(not _valid_tool_name(name) for name in read_tools):
            raise ValueError("official provider requires a valid read-tool allowlist")
        self.provider_id = provider_id
        self.endpoint = endpoint
        self.read_tools = read_tools
        self.policy = policy
        self._transport = transport

    async def read(self, tool_name: str, arguments: dict[str, Any]) -> ProviderResult:
        if tool_name not in self.read_tools:
            raise ProviderAccessDenied("provider tool is not in the read-only allowlist")
        if not isinstance(arguments, dict):
            raise ValueError("provider arguments must be an object")
        decision = self.policy.evaluate(ActionKind.READ_TOOL, self.endpoint)
        if decision.decision != ActionDecision.ALLOW:
            raise ProviderAccessDenied(decision.reason_code)
        result = self._transport.call_tool(tool_name, arguments)
        if inspect.isawaitable(result):
            result = await result
        return ProviderResult(
            provider_id=self.provider_id,
            tool_name=tool_name,
            payload=sanitize_provider_value(result),
        )


def _valid_tool_name(value: str) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 80 and all(
        char.isalnum() or char in "_-." for char in value
    )
