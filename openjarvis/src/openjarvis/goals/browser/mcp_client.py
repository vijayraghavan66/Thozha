"""Goal-safe adapter over OpenJarvis' existing MCP client/transport."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from openjarvis.mcp.client import MCPClient
from openjarvis.mcp.protocol import MCPError
from openjarvis.mcp.transport import MCPRateLimitError

from .policy import (
    ActionDecision,
    ActionKind,
    BrowserActionPolicy,
    PollIntervalGate,
    PolicyDecision,
    sanitize_provider_value,
)
from .providers import ProviderAccessDenied, ProviderToolClass, classify_provider_tool

DISTRICT_MCP_ENDPOINT = "https://mcp-server.district.in/v2/mcp"
MAX_TOOL_ARGUMENT_BYTES = 64_000
MAX_TOOL_RESPONSE_BYTES = 1_000_000
DEFAULT_PROVIDER_POLL_INTERVAL_SECONDS = 900


class McpProtocolError(RuntimeError):
    """Sanitized MCP protocol error; never contains a provider response body."""


class ProviderRateLimited(RuntimeError):
    """A durable goal wake time derived from provider Retry-After metadata."""

    def __init__(self, next_wake_at: datetime) -> None:
        if next_wake_at.tzinfo is None:
            raise ValueError("next_wake_at must include a timezone")
        self.next_wake_at = next_wake_at.astimezone(timezone.utc)
        super().__init__("provider read is rate limited; goal was rescheduled")


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    tool_class: ProviderToolClass


@dataclass(frozen=True, slots=True)
class GoalActionContext:
    """Owner-bound IDs loaded from the durable goal store, never model prose."""

    goal_id: str
    owner_actor: str

    def __post_init__(self) -> None:
        if not _valid_identifier(self.goal_id) or not _valid_identifier(self.owner_actor):
            raise ValueError("goal action context is invalid")


@dataclass(frozen=True, slots=True)
class ProviderActionProposal:
    """Immutable, digest-bound action proposal for the supervisor approval card."""

    provider_id: str
    goal_id: str
    owner_actor: str
    tool_name: str
    action_kind: ActionKind
    tool_class: ProviderToolClass
    canonical_arguments: str = field(repr=False)
    action_digest: str
    idempotency_key: str

    def public_payload(self) -> dict[str, Any]:
        """Return only the exact approved action fields; no session credentials."""
        return {
            "provider_id": self.provider_id,
            "goal_id": self.goal_id,
            "owner_actor": self.owner_actor,
            "tool_name": self.tool_name,
            "action_kind": self.action_kind.value,
            "action_digest": self.action_digest,
            "idempotency_key": self.idempotency_key,
            "arguments": json.loads(self.canonical_arguments),
        }


ActionDispatcher = Callable[[str, ProviderActionProposal], bool]


class DistrictMcpClient:
    """District provider adapter using OpenJarvis MCPClient/MCPTransport.

    No HTTP/JSON-RPC stack, OAuth flow, or bearer storage is implemented here.
    The caller creates ``MCPClient(StreamableHTTPTransport(...))`` with an
    OS-backed token provider, then injects the client and a verified
    BrowserActionPolicy. This adapter only calls discovered read tools. Booking
    mutations are converted to immutable proposals and handed to an injected
    supervisor dispatcher, which must atomically claim the approval/idempotency
    key before any provider action. This adapter never calls a payment tool.
    """

    provider_id = "district"

    def __init__(
        self,
        client: MCPClient,
        policy: BrowserActionPolicy,
        *,
        action_dispatcher: ActionDispatcher | None = None,
        poll_gate: PollIntervalGate | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(client, MCPClient):
            raise TypeError("provider must use OpenJarvis MCPClient")
        if policy.provider.provider_id != self.provider_id:
            raise ValueError("provider policy is bound to a different provider")
        if policy.provider.integration != "official_mcp":
            raise ValueError("District adapter requires an official MCP provider policy")
        self._client = client
        self._policy = policy
        self._action_dispatcher = action_dispatcher
        self._poll_gate = poll_gate or PollIntervalGate(
            policy.provider.min_poll_interval_seconds or DEFAULT_PROVIDER_POLL_INTERVAL_SECONDS
        )
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._tool_catalog: dict[str, DiscoveredTool] = {}
        self._operation_catalog: dict[str, dict[str, Any]] = {}
        self._initialized = False
        self._cycle_expires_at: datetime | None = None
        self._cycle_calls = 0

    def begin_read_cycle(self, *, observed_at: datetime | None = None) -> datetime:
        """Reserve one bounded provider poll; several related reads share it."""
        now = observed_at or self._now()
        if now.tzinfo is None:
            raise ValueError("provider poll timestamp must include a timezone")
        now = now.astimezone(timezone.utc)
        if self._cycle_expires_at is not None and now < self._cycle_expires_at:
            return self._cycle_expires_at
        permitted, next_at = self._poll_gate.permit(self.provider_id, now=now)
        if not permitted:
            raise ProviderRateLimited(next_at or now + timedelta(seconds=DEFAULT_PROVIDER_POLL_INTERVAL_SECONDS))
        self._cycle_expires_at = now + timedelta(seconds=120)
        self._cycle_calls = 0
        return self._cycle_expires_at

    def _consume_read_cycle_call(self, now: datetime) -> None:
        if self._cycle_expires_at is None or now >= self._cycle_expires_at:
            self.begin_read_cycle(observed_at=now)
        if self._cycle_calls >= 30:
            raise ProviderAccessDenied("provider read cycle exceeded its bounded call limit")
        self._cycle_calls += 1

    @staticmethod
    def validate_endpoint(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        if (
            endpoint != DISTRICT_MCP_ENDPOINT
            or parsed.scheme != "https"
            or parsed.hostname != "mcp-server.district.in"
            or parsed.path != "/v2/mcp"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("District MCP endpoint is fixed to its official HTTPS origin")
        return endpoint

    def initialize(self) -> dict[str, Any]:
        if not self._policy.authorization_ready:
            raise ProviderAccessDenied("provider authorization is not verified")
        try:
            result = self._client.initialize()
        except Exception:
            # Never propagate an MCP exception body that could contain account
            # information or a bearer-related diagnostic.
            raise McpProtocolError("provider MCP initialization failed") from None
        if not isinstance(result, dict):
            raise McpProtocolError("provider MCP initialization is malformed")
        self._initialized = True
        return sanitize_provider_value(result)  # type: ignore[return-value]

    def tools_list(self, *, observed_at: datetime | None = None) -> tuple[DiscoveredTool, ...]:
        if not self._initialized:
            self.initialize()
        now = observed_at or self._now()
        if now.tzinfo is None:
            raise ValueError("tool discovery timestamp must include a timezone")
        self._consume_read_cycle_call(now.astimezone(timezone.utc))
        try:
            descriptors = self._client.list_tool_descriptors()
        except MCPRateLimitError as exc:
            next_at = now.astimezone(timezone.utc) + timedelta(seconds=exc.retry_after_seconds)
            self._poll_gate.defer_until(self.provider_id, next_at)
            self._cycle_expires_at = None
            raise ProviderRateLimited(next_at) from None
        except Exception:
            raise McpProtocolError("provider tool discovery failed") from None
        if not isinstance(descriptors, list) or len(descriptors) > 500:
            raise McpProtocolError("provider tool list is malformed")
        discovered: dict[str, DiscoveredTool] = {}
        for raw in descriptors:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            schema = raw.get("inputSchema", {})
            if not _valid_tool_name(name) or not isinstance(schema, dict):
                continue
            if len(json.dumps(schema, ensure_ascii=False).encode("utf-8")) > 64_000:
                continue
            description = raw.get("description")
            safe_description = sanitize_provider_value(description if isinstance(description, str) else "")
            try:
                safe_schema = _sanitize_schema(schema)
            except (TypeError, ValueError):
                continue
            discovered[name] = DiscoveredTool(
                name=name, description=str(safe_description)[:1_000],
                input_schema=safe_schema, tool_class=classify_provider_tool(raw),
            )
        self._tool_catalog = discovered
        return tuple(discovered.values())

    def poll_checkpoint(self) -> dict[str, str]:
        """Persist this state with the goal checkpoint before sleeping."""
        return self._poll_gate.snapshot()

    def call_read_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        observed_at: datetime | None = None,
    ) -> Any:
        if not self._initialized:
            self.initialize()
        tool = self._tool_catalog.get(tool_name)
        if tool is None or tool.tool_class != ProviderToolClass.READ:
            raise ProviderAccessDenied("provider operation is not classified read-only")
        decision = self._policy.evaluate(ActionKind.READ_TOOL, DISTRICT_MCP_ENDPOINT)
        if decision.decision != ActionDecision.ALLOW:
            raise ProviderAccessDenied(decision.reason_code)
        try:
            _validate_call_arguments(tool.input_schema, arguments)
        except ProviderAccessDenied:
            raise ProviderAccessDenied("provider tool arguments failed the discovered tool schema") from None
        if tool_name == "execute":
            operation = arguments.get("operation")
            operation_entry = self._operation_catalog.get(operation) if isinstance(operation, str) else None
            if not operation_entry or operation_entry.get("call_with") != "execute":
                raise ProviderAccessDenied("District operation was not discovered as read-only")
            operation_schema = operation_entry.get("arguments_schema")
            operation_args = arguments.get("arguments", {})
            if (not isinstance(operation_schema, dict) or not isinstance(operation_args, dict)
                    or operation_entry.get("invalid")
                    or any(name not in operation_schema.get("properties", {}) for name in operation_schema.get("required", []))):
                raise ProviderAccessDenied("District operation schema or arguments are invalid")
            try:
                _validate_call_arguments(operation_schema, operation_args)
            except ProviderAccessDenied:
                raise ProviderAccessDenied("District operation arguments failed its discovered operation schema") from None
        now = observed_at or self._now()
        if now.tzinfo is None:
            raise ValueError("provider poll timestamp must include a timezone")
        self._consume_read_cycle_call(now.astimezone(timezone.utc))
        try:
            result = self._client.call_tool(tool_name, arguments)
        except MCPRateLimitError as exc:
            provider_wake = now.astimezone(timezone.utc) + timedelta(seconds=exc.retry_after_seconds)
            self._poll_gate.defer_until(self.provider_id, provider_wake)
            self._cycle_expires_at = None
            raise ProviderRateLimited(provider_wake) from None
        except Exception:
            raise McpProtocolError("provider read tool failed") from None
        if not isinstance(result, dict):
            raise McpProtocolError("provider read tool response is malformed")
        if result.get("isError") is True:
            raise McpProtocolError("provider read tool returned an error")
        if len(json.dumps(result, default=str).encode("utf-8")) > MAX_TOOL_RESPONSE_BYTES:
            raise McpProtocolError("provider read response exceeded the size limit")
        safe = sanitize_provider_value(result)
        if tool_name == "search":
            self._cache_discovered_operations(safe)
        return safe

    def _cache_discovered_operations(self, value: Any) -> None:
        """Cache only explicit `call_with: execute` operations from official search."""
        budget = [0]

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 12 or budget[0] > 20_000:
                return
            budget[0] += 1
            if isinstance(node, dict):
                operation = node.get("operation")
                if (
                    isinstance(operation, str)
                    and _valid_tool_name(operation)
                    and node.get("call_with") == "execute"
                    and isinstance(node.get("arguments_schema"), dict)
                ):
                    schema = _sanitize_schema(node["arguments_schema"])
                    if len(json.dumps(schema, ensure_ascii=False).encode("utf-8")) <= MAX_TOOL_ARGUMENT_BYTES:
                        self._merge_discovered_operation(operation, schema)
                for child in node.values():
                    walk(child, depth + 1)
            elif isinstance(node, list):
                for child in node:
                    walk(child, depth + 1)
            elif isinstance(node, str) and node.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(node)
                except (TypeError, ValueError):
                    return
                walk(parsed, depth + 1)

        walk(value)

    def _merge_discovered_operation(self, operation: str, schema: dict[str, Any]) -> None:
        prior = self._operation_catalog.get(operation)
        if prior is None:
            self._operation_catalog[operation] = {"call_with": "execute", "arguments_schema": schema}
            return
        old_schema = prior.get("arguments_schema", {})
        old_props = old_schema.get("properties", {}) if isinstance(old_schema, dict) else {}
        new_props = schema.get("properties", {})
        merged = dict(old_schema) if isinstance(old_schema, dict) else {}
        props = dict(old_props) if isinstance(old_props, dict) else {}
        invalid = bool(prior.get("invalid"))
        for name, field_schema in new_props.items():
            if name in props and props[name] != field_schema:
                invalid = True
            else:
                props[name] = field_schema
        required = set(merged.get("required", [])) | set(schema.get("required", []))
        merged.update(schema)
        merged["properties"] = props
        merged["required"] = sorted(required)
        if old_schema.get("additionalProperties") is False or schema.get("additionalProperties") is False:
            merged["additionalProperties"] = False
        self._operation_catalog[operation] = {
            "call_with": "execute", "arguments_schema": merged, "invalid": invalid,
        }

    def build_action_proposal(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: GoalActionContext,
    ) -> ProviderActionProposal:
        """Build an approval payload from the server-discovered tool and exact arguments."""
        tool = self._tool_catalog.get(tool_name)
        if tool is None or tool.tool_class not in {
            ProviderToolClass.HOLD,
            ProviderToolClass.BOOK,
            ProviderToolClass.PAY,
        }:
            raise ProviderAccessDenied("tool is not a recognized supervised booking action")
        _validate_call_arguments(tool.input_schema, arguments)
        kind = _action_kind(tool.tool_class)
        if kind == ActionKind.PAYMENT:
            # Payment is always completed by the human in the provider's
            # authenticated surface, never by this browser/MCP adapter.
            raise ProviderAccessDenied("payment requires direct user handoff")
        canonical_args = _canonical_arguments(arguments)
        digest = canonical_action_digest(
            provider_id=self.provider_id,
            goal_id=context.goal_id,
            owner_actor=context.owner_actor,
            tool_name=tool.name,
            action_kind=kind,
            canonical_arguments=canonical_args,
        )
        idempotency_key = hashlib.sha256(
            f"district-goal-action-v1:{context.goal_id}:{digest}".encode("utf-8")
        ).hexdigest()
        return ProviderActionProposal(
            provider_id=self.provider_id,
            goal_id=context.goal_id,
            owner_actor=context.owner_actor,
            tool_name=tool.name,
            action_kind=kind,
            tool_class=tool.tool_class,
            canonical_arguments=canonical_args,
            action_digest=digest,
            idempotency_key=idempotency_key,
        )

    def dispatch_supervised_action(
        self,
        proposal: ProviderActionProposal,
        *,
        approval_id: str,
    ) -> Any:
        """Hand an exact, revalidated action to the supervisor dispatcher.

        The injected dispatcher must atomically verify/consume the supervisor
        approval and claim the idempotency key before performing the provider
        action. This adapter never calls an MCP mutating tool directly.
        """
        if self._action_dispatcher is None:
            raise ProviderAccessDenied("supervised provider actions are unavailable")
        if proposal.provider_id != self.provider_id:
            raise ProviderAccessDenied("action proposal belongs to another provider")
        if proposal.action_kind == ActionKind.PAYMENT:
            raise ProviderAccessDenied("payment requires direct user handoff")
        try:
            args = json.loads(proposal.canonical_arguments)
        except json.JSONDecodeError:
            raise ProviderAccessDenied("action proposal is malformed") from None
        current = self.build_action_proposal(
            proposal.tool_name,
            args,
            context=GoalActionContext(proposal.goal_id, proposal.owner_actor),
        )
        if current != proposal:
            raise ProviderAccessDenied("action proposal changed after approval")
        decision = self._policy.evaluate(
            current.action_kind,
            DISTRICT_MCP_ENDPOINT,
            action_digest=current.action_digest,
            approval_id=approval_id,
        )
        if decision.decision != ActionDecision.ALLOW:
            raise ProviderAccessDenied(decision.reason_code)
        try:
            return self._action_dispatcher(approval_id, current)
        except Exception:
            # The supervisor may have started an action. Never auto-retry a
            # transport failure; the goal enters needs_review on ambiguity.
            raise McpProtocolError("supervised provider action result is unknown") from None


def canonical_action_digest(
    *,
    provider_id: str,
    goal_id: str,
    owner_actor: str,
    tool_name: str,
    action_kind: ActionKind,
    canonical_arguments: str,
) -> str:
    """Recompute a digest binding provider, owner, goal, action, and full args."""
    try:
        arguments = json.loads(canonical_arguments)
    except json.JSONDecodeError:
        raise ValueError("canonical action arguments are malformed") from None
    canonical_args = _canonical_arguments(arguments)
    payload = {
        "version": 1,
        "provider_id": provider_id,
        "goal_id": goal_id,
        "owner_actor": owner_actor,
        "tool_name": tool_name,
        "action_kind": action_kind.value,
        "arguments": json.loads(canonical_args),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _valid_identifier(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value))


def _valid_tool_name(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value))


def _action_kind(tool_class: ProviderToolClass) -> ActionKind:
    mapping = {
        ProviderToolClass.HOLD: ActionKind.HOLD_SEATS,
        ProviderToolClass.BOOK: ActionKind.BOOK,
        ProviderToolClass.PAY: ActionKind.PAYMENT,
    }
    try:
        return mapping[tool_class]
    except KeyError:
        raise ProviderAccessDenied("provider tool is not an approved booking action") from None


def _sanitize_schema(value: dict[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Keep a small JSON Schema subset; discard extension keywords by default."""
    if depth > 12 or not isinstance(value, dict):
        raise ValueError("provider schema is malformed")
    allowed = {
        "type", "properties", "required", "additionalProperties", "items",
        "enum", "minLength", "maxLength", "minimum", "maximum", "description",
    }
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in allowed:
            continue
        if key == "properties" and isinstance(item, dict):
            if len(item) > 100:
                raise ValueError("provider schema has too many properties")
            result[key] = {
                str(prop)[:80]: _sanitize_schema(schema, depth=depth + 1)
                for prop, schema in item.items()
                if isinstance(prop, str) and isinstance(schema, dict)
            }
        elif key == "items" and isinstance(item, dict):
            result[key] = _sanitize_schema(item, depth=depth + 1)
        elif key == "required" and isinstance(item, list):
            result[key] = [entry for entry in item[:100] if isinstance(entry, str) and len(entry) <= 80]
        elif key == "enum" and isinstance(item, list):
            if len(item) > 100:
                raise ValueError("provider schema enum is too large")
            result[key] = item
        elif key == "additionalProperties" and isinstance(item, dict):
            result[key] = _sanitize_schema(item, depth=depth + 1)
        elif key == "description" and isinstance(item, str):
            result[key] = str(sanitize_provider_value(item))[:1_000]
        elif key in {"minLength", "maxLength", "minimum", "maximum"} and isinstance(item, (int, float)) and not isinstance(item, bool):
            result[key] = item
        elif key == "type" and item in {"object", "array", "string", "number", "integer", "boolean", "null"}:
            result[key] = item
        elif key == "additionalProperties" and isinstance(item, bool):
            result[key] = item
    return result


def _validate_call_arguments(schema: dict[str, Any], arguments: dict[str, Any], *, depth: int = 0) -> None:
    """Validate bounded MCP inputs against the provider's discovered schema."""
    if depth > 12 or not isinstance(schema, dict) or not isinstance(arguments, dict):
        raise ProviderAccessDenied("provider tool arguments are invalid")
    try:
        if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise ProviderAccessDenied("provider tool arguments are too large")
    except (TypeError, ValueError):
        raise ProviderAccessDenied("provider tool arguments are not JSON values") from None
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if any(name not in arguments for name in required):
        raise ProviderAccessDenied("required provider arguments are missing")
    if schema.get("additionalProperties") is False and any(name not in properties for name in arguments):
        raise ProviderAccessDenied("unexpected provider argument")
    for name, value in arguments.items():
        prop_schema = properties.get(name) if isinstance(properties, dict) else None
        if prop_schema is None:
            extra_schema = schema.get("additionalProperties")
            if isinstance(extra_schema, dict):
                _validate_value(extra_schema, value, depth=depth + 1)
            continue
        _validate_value(prop_schema, value, depth=depth + 1)


def _validate_value(schema: dict[str, Any], value: Any, *, depth: int) -> None:
    if depth > 12 or not isinstance(schema, dict):
        raise ProviderAccessDenied("provider argument schema is invalid")
    expected = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected in valid and not valid[expected]:
        raise ProviderAccessDenied("provider argument has the wrong type")
    if "enum" in schema and value not in schema["enum"]:
        raise ProviderAccessDenied("provider argument is outside its allowed values")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", MAX_TOOL_ARGUMENT_BYTES):
            raise ProviderAccessDenied("provider string argument is out of bounds")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise ProviderAccessDenied("provider numeric argument is out of bounds")
    if isinstance(value, dict):
        _validate_call_arguments(schema, value, depth=depth)
    elif isinstance(value, list):
        if len(value) > 500:
            raise ProviderAccessDenied("provider array argument is too large")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for item in value:
                _validate_value(item_schema, item, depth=depth + 1)


def _canonical_arguments(arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict):
        raise ProviderAccessDenied("provider arguments must be an object")
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ProviderAccessDenied("provider arguments are not canonical JSON") from None
    if len(encoded.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
        raise ProviderAccessDenied("provider arguments are too large")
    return encoded
