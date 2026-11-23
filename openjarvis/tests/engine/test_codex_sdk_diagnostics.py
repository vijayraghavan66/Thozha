"""Sanitized causal diagnostics for Codex SDK failures."""

import json
import io
import logging
import sys
import types

import pytest

from openjarvis.core.types import Message, Role
from openjarvis.engine import codex_sdk
from openjarvis.engine._base import EngineConnectionError
from openjarvis.engine.codex_sdk import _codex_error_diagnostics


def test_codex_failure_diagnostics_keep_cause_chain_but_redact_sensitive_text():
    prompt = "private request text"
    secret = "secret-token-value"
    try:
        try:
            raise RuntimeError(f"provider failed Authorization: Bearer {secret} api_key=sk-test-secret-value {prompt}")
        except RuntimeError as cause:
            raise ConnectionError("Codex transport wrapper failed") from cause
    except ConnectionError as error:
        diagnostics = _codex_error_diagnostics(error, sensitive_values=(prompt,))

    rendered = json.dumps(diagnostics)
    assert [item["type"] for item in diagnostics["chain"]] == ["ConnectionError", "RuntimeError"]
    assert prompt not in rendered
    assert secret not in rendered
    assert "sk-test-secret-value" not in rendered
    assert "[redacted]" in rendered
    assert diagnostics["traceback"]
    assert all(set(frame) == {"file", "line", "function"} for frame in diagnostics["traceback"])
    assert diagnostics["locals_captured"] is False
    assert diagnostics["source_lines_captured"] is False


def test_codex_failure_diagnostics_bound_chain_depth():
    error: BaseException = RuntimeError("root")
    for index in range(12):
        try:
            raise ValueError(f"wrapper-{index}") from error
        except ValueError as caught:
            error = caught
    diagnostics = _codex_error_diagnostics(error, max_chain=5)
    assert len(diagnostics["chain"]) == 5


def test_codex_generate_logs_cause_chain_without_prompt_or_secret(monkeypatch, tmp_path):
    prompt = "private prompt unique phrase"
    secret = "sk-test-secret-value-123456"

    class FakeThread:
        def run(self, input_text, **kwargs):
            try:
                raise ConnectionError(f"socket reset while handling {prompt}")
            except ConnectionError as cause:
                raise RuntimeError(f"provider wrapper failed api_key={secret} {prompt}") from cause

    class FakeCodex:
        def __init__(self, **kwargs):
            self._client = types.SimpleNamespace()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def thread_start(self, **kwargs):
            return FakeThread()

    fake_sdk = types.ModuleType("openai_codex")
    fake_sdk.ApprovalMode = types.SimpleNamespace(auto_review="auto_review")
    fake_sdk.Codex = FakeCodex
    fake_sdk.CodexConfig = lambda **kwargs: kwargs
    fake_sdk.Sandbox = types.SimpleNamespace(read_only="read_only")
    monkeypatch.setitem(sys.modules, "openai_codex", fake_sdk)
    monkeypatch.setattr(codex_sdk, "CODEX_HOME", tmp_path / "codex-home")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_handlers = codex_sdk.PERF_LOGGER.handlers[:]
    old_propagate = codex_sdk.PERF_LOGGER.propagate
    codex_sdk.PERF_LOGGER.handlers = [handler]
    codex_sdk.PERF_LOGGER.propagate = False
    try:
        with pytest.raises(EngineConnectionError):
            codex_sdk.CodexSDKEngine().generate(
                [Message(role=Role.USER, content=prompt)], model=codex_sdk.MODEL
            )
    finally:
        codex_sdk.PERF_LOGGER.handlers = old_handlers
        codex_sdk.PERF_LOGGER.propagate = old_propagate

    diagnostic = stream.getvalue()
    assert "codex_failure_diagnostic" in diagnostic
    assert "RuntimeError" in diagnostic and "ConnectionError" in diagnostic
    assert prompt not in diagnostic and secret not in diagnostic
    assert '"locals_captured":false' in diagnostic
    assert '"source_lines_captured":false' in diagnostic
