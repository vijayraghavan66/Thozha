"""Relay failures are diagnosable from logs: HTTP status and a whitelisted code, never bodies or secrets."""
from __future__ import annotations

import json
from types import SimpleNamespace

from jarvis_link_host.client import RelayClientError, relay_failure_log_fields


def test_relay_401_reports_status_and_fixed_error_code():
    failure = RelayClientError("relay returned HTTP 401 (TIMESTAMP_SKEW)", status_code=401, code="TIMESTAMP_SKEW")
    assert relay_failure_log_fields(failure) == {"relay_status": 401, "relay_code": "TIMESTAMP_SKEW"}


def test_unsafe_or_secret_looking_codes_are_dropped():
    leaked = RelayClientError("x", status_code=401, code="Bearer abc.def-ghi signature=AAAA")
    fields = relay_failure_log_fields(leaked)
    assert fields == {"relay_status": 401}
    assert "Bearer" not in json.dumps(fields) and "signature" not in json.dumps(fields)


def test_websocket_handshake_rejection_reports_its_status_only():
    handshake = SimpleNamespace(response=SimpleNamespace(status_code=401, headers={"X-Secret": "s"}, body=b"{}"))
    assert relay_failure_log_fields(handshake) == {"relay_status": 401}


def test_plain_exceptions_produce_no_fields():
    assert relay_failure_log_fields(OSError("connection reset")) == {}


def test_structured_event_is_valid_json_without_relay_body():
    fields = relay_failure_log_fields(RelayClientError("m", status_code=401, code="INVALID_SIGNATURE"))
    event = json.dumps({"event": "cycle_error", "error": "RelayClientError", **fields})
    assert json.loads(event) == {"event": "cycle_error", "error": "RelayClientError", "relay_status": 401, "relay_code": "INVALID_SIGNATURE"}
