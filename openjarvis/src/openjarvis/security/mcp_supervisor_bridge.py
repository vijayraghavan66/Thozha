"""Supervisor boundary shared by the Codex client and its local Mac MCP child."""
from __future__ import annotations

import json
import hashlib
import re
from typing import Any

CONTROL_MCP_TOOLS = frozenset({
    "click", "type_text", "press_key", "scroll", "open_app", "open_browser_workspace",
})
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def create_mcp_approval(
    store: Any,
    *,
    task_id: str,
    source: str,
    scope: str,
    tool: str,
    arguments: dict[str, Any],
) -> tuple[str, str, str, str]:
    """Persist one exact requested Mac MCP operation; do not execute it."""
    if tool not in CONTROL_MCP_TOOLS or not _TASK_ID_RE.fullmatch(task_id):
        raise PermissionError("Mac control request is outside the supported operation set")
    if source not in {"agent_chat", "jarvis_link"}:
        raise PermissionError("Mac control requires a supervised task")
    if source == "jarvis_link" and scope != "assistant":
        raise PermissionError("This Link task has read-only scope")
    task = store.get_task(task_id)
    if task is None or task["state"] != "RUNNING":
        raise PermissionError("The supervised task is not running")
    try:
        metadata = json.loads(task.get("recovery_json") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermissionError("Task authorization metadata is invalid") from exc
    if metadata.get("source") != source:
        raise PermissionError("Task source does not match its stored authorization")
    if source == "jarvis_link" and metadata.get("scope") != "assistant":
        raise PermissionError("Link task scope does not match its stored authorization")

    from openjarvis.tasks.supervisor import LINK_SCOPE_CAPABILITIES, canonical_json, operation_target

    if source == "jarvis_link":
        stored_scope = metadata.get("capability_scope")
        stored_digest = metadata.get("scope_digest")
        scope_json = canonical_json(stored_scope)
        expected_digest = hashlib.sha256(scope_json.encode("utf-8")).hexdigest()
        if (
            stored_scope != ["assistant"]
            or stored_digest != expected_digest
            or json.loads(task.get("capabilities_json") or "[]") != sorted(LINK_SCOPE_CAPABILITIES["assistant"])
        ):
            raise PermissionError("Link assistant scope is not intact")

    operation = f"mcp:jarvis_mac:{tool}"
    target = operation_target(operation, arguments)
    approval_id, digest = store.create_approval(
        task_id,
        operation,
        target,
        arguments,
        "Mac control changes the current computer and needs one exact user approval.",
    )
    store.record_event(task_id, "mac_control.authorization_required", {
        "approval_id": approval_id,
        "operation": operation,
        "target": target,
        "operation_digest": digest,
    })
    return approval_id, operation, target, digest


def claim_consumed_mcp_approval(
    store: Any,
    *,
    approval_id: str,
    task_id: str,
    operation: str,
    target: str,
    arguments: dict[str, Any],
    expected_digest: str,
) -> bool:
    """Claim the single local MCP invocation after the supervisor consumed it."""
    from openjarvis.tasks.supervisor import operation_digest

    if not _TASK_ID_RE.fullmatch(task_id) or operation not in {
        f"mcp:jarvis_mac:{tool}" for tool in CONTROL_MCP_TOOLS
    }:
        return False
    actual = operation_digest(operation, target, arguments)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest) or actual != expected_digest:
        return False
    return bool(store.claim_mcp_dispatch(
        approval_id,
        task_id=task_id,
        operation=operation,
        target=target,
        arguments=arguments,
    ))


__all__ = ["CONTROL_MCP_TOOLS", "create_mcp_approval", "claim_consumed_mcp_approval"]
