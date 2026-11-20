"""Runtime switches for privileged Mac MCP tools.

The typed, idempotent trusted-app fast path does not use this policy. This
switch only controls the approval-gated Codex MCP ``open_app`` capability.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

OPEN_APP_MCP_ENV = "JARVIS_MCP_OPEN_APP_ENABLED"


def open_app_mcp_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether approval-gated MCP app launching is explicitly enabled.

    Fail closed by default. The trusted typed Calculator/Spotify fast-command
    route is independent and does not consult this setting.
    """
    values = os.environ if environ is None else environ
    return values.get(OPEN_APP_MCP_ENV, "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def record_open_app_event(
    store,
    *,
    stage: str,
    task_id: str,
    approval_id: str = "",
    application: str = "",
    dispatch_claim_outcome: str,
    execution_attempted: bool,
    result: str,
    error_code: str = "",
    exit_code: int | None = None,
) -> None:
    """Write one structured event without logging arguments or process output."""
    safe_application = re.sub(r"[\x00-\x1f\x7f]+", " ", str(application))[:120]
    payload = {
        "task_id": task_id,
        "approval_id": approval_id,
        "operation": "mcp:jarvis_mac:open_app",
        "requested_application": safe_application,
        "dispatch_claim_outcome": dispatch_claim_outcome,
        "execution_attempted": bool(execution_attempted),
        "result": result,
        "error_code": error_code,
        "exit_code": exit_code,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "process_context": {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "program": Path(sys.argv[0]).name,
        },
    }
    try:
        if store is not None and task_id:
            store.record_event(task_id, f"mac.open_app.{stage}", payload)
            return
    except Exception:
        pass
    # MCP stdout is reserved for JSON-RPC; keep fallback diagnostics on stderr.
    try:
        diagnostic = {"event": f"mac.open_app.{stage}", **payload}
        sys.stderr.write(json.dumps(diagnostic, separators=(",", ":")) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


__all__ = ["OPEN_APP_MCP_ENV", "open_app_mcp_enabled", "record_open_app_event"]
