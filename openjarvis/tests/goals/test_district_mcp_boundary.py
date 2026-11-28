from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from openjarvis.goals.browser.mcp_client import DistrictMcpClient, ProviderAccessDenied
from openjarvis.goals.browser.policy import (
    BrowserActionPolicy,
    PollIntervalGate,
    ProviderAuthorization,
    ProviderConfig,
)
from openjarvis.mcp.client import MCPClient


NOW = datetime(2026, 11, 28, tzinfo=timezone.utc)
ENDPOINT = "https://mcp-server.district.in/v2/mcp"
ORIGIN = "https://mcp-server.district.in"


class _FakeTransport:
    def close(self):
        return None


def _adapter():
    client = MCPClient(_FakeTransport())
    calls = []

    def initialize():
        return {"protocolVersion": "2025-03-26", "serverInfo": {"name": "district-mcp"}}

    def descriptors():
        return [
            {
                "name": "search", "description": "Search the official operation catalog.",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "execute", "description": "Execute a read-only District operation.",
                "inputSchema": {"type": "object", "properties": {"operation": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["operation"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "act", "description": "Changes something and needs confirmation.",
                "inputSchema": {"type": "object", "properties": {"operation": {"type": "string"}}, "required": ["operation"], "additionalProperties": False},
                "annotations": {"readOnlyHint": False},
            },
        ]

    operation = {
        "operation": "consumer.movie_showtimes",
        "call_with": "execute",
        "arguments_schema": {
            "type": "object", "properties": {
                "movie_id": {"type": "string", "minLength": 1},
                "date": {"type": "string"},
            }, "required": ["movie_id"], "additionalProperties": False,
        },
    }

    def call_tool(name, arguments):
        calls.append((name, arguments))
        if name == "search":
            return {"content": [{"type": "text", "text": json.dumps({"items": [operation, {"operation": "consumer.movie_book", "call_with": "act", "arguments_schema": {"type": "object"}}]})}], "isError": False}
        return {"content": [{"type": "text", "text": "{\"cinemas\": []}"}], "isError": False}

    client.initialize = initialize
    client.list_tool_descriptors = descriptors
    client.call_tool = call_tool

    provider = ProviderConfig(
        provider_id="district", origins=(ORIGIN,), enabled=True, partner_enabled=True,
        integration="official_mcp", min_poll_interval_seconds=1,
    )
    authorization = ProviderAuthorization(
        provider_id="district", integration="official_mcp", issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1), allowed_origins=(ORIGIN,), read_only=True,
        grant_type="urn:district:params:oauth:grant-type:guest", scopes=frozenset({"district"}),
        principal="guest",
    )
    policy = BrowserActionPolicy(
        provider, authorization, authorization_verifier=lambda _: True,
        now=lambda: NOW,
    )
    adapter = DistrictMcpClient(
        client, policy, poll_gate=PollIntervalGate(1), now=lambda: NOW,
    )
    return adapter, calls


def test_execute_requires_official_search_discovery_and_nested_schema():
    adapter, calls = _adapter()
    adapter.tools_list(observed_at=NOW)
    with pytest.raises(ProviderAccessDenied):
        adapter.call_read_tool("execute", {"operation": "consumer.movie_showtimes", "arguments": {"movie_id": "1"}}, observed_at=NOW + timedelta(seconds=1))
    discovered = adapter.call_read_tool("search", {"query": "movie showtimes in Chennai"}, observed_at=NOW + timedelta(seconds=2))
    assert discovered["isError"] is False
    result = adapter.call_read_tool(
        "execute", {"operation": "consumer.movie_showtimes", "arguments": {"movie_id": "movie-123", "date": "2026-12-18"}},
        observed_at=NOW + timedelta(seconds=4),
    )
    assert result["isError"] is False
    assert calls[-1][0] == "execute"
    assert calls[-1][1]["operation"] == "consumer.movie_showtimes"


def test_unknown_or_mutating_district_operation_is_denied():
    adapter, calls = _adapter()
    adapter.tools_list(observed_at=NOW)
    adapter.call_read_tool("search", {"query": "book movie tickets"}, observed_at=NOW + timedelta(seconds=2))
    with pytest.raises(ProviderAccessDenied):
        adapter.call_read_tool("execute", {"operation": "consumer.movie_book", "arguments": {}}, observed_at=NOW + timedelta(seconds=4))
    with pytest.raises(ProviderAccessDenied):
        adapter.call_read_tool("execute", {"operation": "consumer.movie_showtimes", "arguments": {"movie_id": "1", "unexpected": True}}, observed_at=NOW + timedelta(seconds=4))
    assert all(name != "execute" for name, _ in calls)


def test_guest_auth_never_authorizes_holds_or_booking():
    adapter, _ = _adapter()
    adapter.tools_list(observed_at=NOW)
    from openjarvis.goals.browser.policy import ActionKind, ActionDecision

    decision = adapter._policy.evaluate(ActionKind.HOLD_SEATS, ENDPOINT, action_digest="a" * 64, approval_id="b" * 32)
    assert decision.decision == ActionDecision.DENY
    assert decision.reason_code == "seat_holds_disabled"


def test_operation_schema_fragments_merge_without_dropping_required_fields():
    adapter, _ = _adapter()
    adapter._cache_discovered_operations({"operation": "consumer.movie_select_seat", "call_with": "execute", "arguments_schema": {
        "type": "object", "properties": {"cinema_id": {"type": "string"}},
        "required": ["cinema_id", "session_id"], "additionalProperties": False,
    }})
    adapter._cache_discovered_operations({"operation": "consumer.movie_select_seat", "call_with": "execute", "arguments_schema": {
        "type": "object", "properties": {"session_id": {"type": "string"}},
        "required": ["cinema_id", "session_id"], "additionalProperties": False,
    }})
    entry = adapter._operation_catalog["consumer.movie_select_seat"]
    assert not entry.get("invalid")
    assert set(entry["arguments_schema"]["properties"]) == {"cinema_id", "session_id"}
    assert set(entry["arguments_schema"]["required"]) == {"cinema_id", "session_id"}
