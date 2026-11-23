"""Plan-only MCP for assistant-scoped Jarvis Link requests.

This server can produce and persist one exact development-plan proposal. It has
no approval, capability issuance, project write, test, repair, shell, or process
launch tools. A separate development-scoped task is required before the normal
``jarvis_dev.approve_plan`` flow can request owner authorization.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path
from typing import Any

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
OPENJARVIS_SRC = REAL_HOME / ".openjarvis" / "src" / "src"
if str(OPENJARVIS_SRC) not in sys.path:
    sys.path.insert(0, str(OPENJARVIS_SRC))

from openjarvis.tasks.dev_capability import (  # noqa: E402
    PLAN_APPROVAL_OPERATION,
    CapabilityError,
    _spec_fields,
    build_spec,
    plan_approval_arguments,
    plan_approval_target,
)
from openjarvis.tasks.supervisor import (  # noqa: E402
    LINK_SCOPE_CAPABILITIES,
    SupervisorStore,
    canonical_json,
    operation_digest,
)

_TASK_ID = re.compile(r"^[0-9a-f]{32}$")
_DEVICE_ACTOR = re.compile(r"^device:([0-9a-f]{32})$")
_PLAN_INPUT_KEYS = {
    "plan_id",
    "allowed_ops",
    "allowed_paths",
    "max_file_bytes",
    "max_total_bytes",
    "max_writes",
    "max_test_runs",
    "max_repairs",
}
_MAX_TTL_S = 600
_MAX_DURATION_S = 900

TOOLS = [
    {
        "name": "propose_plan",
        "description": (
            "Propose one canonical, bounded development plan for owner review. "
            "This assistant-scoped tool does not create an approval or grant a "
            "capability or change files. Execution needs a separate "
            "development-scoped "
            "task and explicit owner authorization."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["plan"],
            "properties": {
                "plan": {
                    "type": "object",
                    "required": sorted(_PLAN_INPUT_KEYS),
                    "properties": {
                        "plan_id": {"type": "string"},
                        "allowed_ops": {"type": "array", "items": {"type": "string"}},
                        "allowed_paths": {"type": "array", "items": {"type": "string"}},
                        "max_file_bytes": {"type": "integer"},
                        "max_total_bytes": {"type": "integer"},
                        "max_writes": {"type": "integer"},
                        "max_test_runs": {"type": "integer"},
                        "max_repairs": {"type": "integer"},
                    },
                    "additionalProperties": False,
                }
            },
            "additionalProperties": False,
        },
    }
]


def _text(value: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}], "isError": error}


def _store() -> SupervisorStore:
    return SupervisorStore(
        db_path=os.environ.get("JARVIS_SUPERVISOR_DB") or None, run_recovery=False
    )


def _task_binding(store: SupervisorStore) -> tuple[str, str, str]:
    """Bind plan-only calls to the assistant task and its paired owner."""
    task_id = os.environ.get("JARVIS_SUPERVISOR_TASK_ID", "")
    if (
        not _TASK_ID.fullmatch(task_id)
        or os.environ.get("JARVIS_SUPERVISOR_TASK_SOURCE") != "jarvis_link"
        or os.environ.get("JARVIS_SUPERVISOR_TASK_SCOPE") != "assistant"
    ):
        raise PermissionError(
            "plan proposals require a supervisor-bound assistant Link task"
        )
    task = store.get_task(task_id)
    if task is None:
        raise PermissionError("assistant task does not exist")
    try:
        metadata = json.loads(task.get("recovery_json") or "{}")
        capabilities = json.loads(task.get("capabilities_json") or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermissionError(
            "assistant task authorization metadata is invalid"
        ) from exc
    owner = metadata.get("owner_actor", "")
    match = _DEVICE_ACTOR.fullmatch(owner) if isinstance(owner, str) else None
    if (
        metadata.get("source") != "jarvis_link"
        or metadata.get("scope") != "assistant"
        or metadata.get("capability_scope") != ["assistant"]
        or capabilities != sorted(LINK_SCOPE_CAPABILITIES["assistant"])
        or metadata.get("scope_digest")
        != hashlib.sha256(canonical_json(["assistant"]).encode("utf-8")).hexdigest()
        or match is None
        or task.get("state") not in {"CREATED", "PLANNING", "RUNNING", "VERIFYING"}
    ):
        raise PermissionError(
            "assistant task is not eligible to propose a development plan"
        )
    return task_id, owner, match.group(1)


def _workspace_root() -> Path:
    """Use only the pre-created evaluation workspace; never a caller-supplied path."""
    configured = os.environ.get("JARVIS_DEV_WORKSPACE")
    expected = REAL_HOME / "Documents" / "Jarvis-Lab" / "agent-evaluation" / "taskflow"
    raw = Path(configured).expanduser() if configured else expected
    if Path(os.path.abspath(raw)) != Path(os.path.abspath(expected)):
        raise PermissionError("plan workspace must be the fixed taskflow workspace")
    _reject_symlink_components(REAL_HOME, expected)
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PermissionError(
            "pre-created evaluation workspace is unavailable"
        ) from exc
    if not resolved.is_dir() or resolved != Path(os.path.abspath(expected)):
        raise PermissionError("plan workspace must be the isolated taskflow workspace")
    return resolved


def _reject_symlink_components(base: Path, target: Path) -> None:
    """Reject any existing symlink from the trusted home down to the workspace."""
    root = Path(os.path.abspath(base))
    destination = Path(os.path.abspath(target))
    if not destination.is_relative_to(root):
        raise PermissionError("plan workspace is outside the trusted home")
    current = root
    try:
        metadata = os.lstat(current)
    except OSError as exc:
        raise PermissionError("trusted home is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PermissionError("trusted home must not be a symlink")
    for part in destination.relative_to(root).parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise PermissionError(
                "pre-created evaluation workspace is unavailable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PermissionError("workspace path components must not be symlinks")


def _plan_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PLAN_INPUT_KEYS:
        raise PermissionError("plan fields do not match the bounded development schema")
    return value


def propose_plan(
    arguments: dict[str, Any], *, store: SupervisorStore | None = None
) -> dict[str, Any]:
    """Persist exactly one non-authorizing plan proposal on an assistant task."""
    owns_store = store is None
    store = store or _store()
    try:
        task_id, owner, device_id = _task_binding(store)
        if set(arguments) != {"plan"}:
            raise PermissionError("plan proposal accepts only a plan object")
        plan = _plan_input(arguments.get("plan"))
        if "" in plan.get("allowed_paths", []):
            raise PermissionError("plans cannot grant access to the entire workspace")
        workspace = _workspace_root()
        spec = build_spec(
            plan=plan,
            device_id=device_id,
            user_id=owner,
            workspace_root=str(workspace),
            ttl_s=_MAX_TTL_S,
            max_duration_s=_MAX_DURATION_S,
        )
        canonical = _spec_fields(spec)
        plan_digest = spec.plan_digest
        approval_args = plan_approval_arguments(spec)
        args_digest = operation_digest(
            PLAN_APPROVAL_OPERATION, plan_approval_target(spec), approval_args
        )
        proposal_id = hashlib.sha256(
            f"{task_id}:{plan_digest}".encode("utf-8")
        ).hexdigest()

        with store._tx() as db:
            row = db.execute(
                """SELECT payload_json FROM task_events WHERE task_id=?
                   AND event_type='development.plan_proposed' ORDER BY seq LIMIT 1""",
                (task_id,),
            ).fetchone()
            if row:
                previous = json.loads(row["payload_json"])
                if previous.get("plan_digest") != plan_digest:
                    return _text(
                        "This assistant task already has a different plan proposal; "
                        "no approval or capability was created.",
                        True,
                    )
                proposal_id = previous.get("proposal_id", proposal_id)
            else:
                store._event(
                    db,
                    task_id,
                    "development.plan_proposed",
                    {
                        "phase": "PLAN_PROPOSED",
                        "proposal_id": proposal_id,
                        "plan": canonical,
                        "plan_digest": plan_digest,
                        "operation_digest": args_digest,
                        "approval_state": "NOT_CREATED",
                        "execution_authorized": False,
                        "approval_created": False,
                        "capability_issued": False,
                    },
                )
                store._event(
                    db,
                    task_id,
                    "development.authorization_required",
                    {
                        "proposal_id": proposal_id,
                        "plan_digest": plan_digest,
                        "approval_state": "REQUIRED",
                        "reason": (
                            "A separate development-scoped task and explicit owner "
                            "authorization are required."
                        ),
                    },
                )

        return _text(
            json.dumps(
                {
                    "status": "PLAN_PROPOSED",
                    "task_id": task_id,
                    "proposal_id": proposal_id,
                    "plan": canonical,
                    "plan_digest": plan_digest,
                    "approval_operation_digest": args_digest,
                    "authorization_state": "REQUIRED",
                    "execution_authorized": False,
                    "approval_created": False,
                    "capability_issued": False,
                    "next_step": (
                        "Development enrollment and a separate development task "
                        "required before requesting exact plan approval."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (CapabilityError, PermissionError, ValueError, TypeError) as exc:
        return _text(
            f"Plan proposal refused ({getattr(exc, 'code', type(exc).__name__)}).", True
        )
    finally:
        if owns_store:
            store.close()


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method, request_id = message.get("method"), message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis-plan", "version": "0.1.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != "propose_plan":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _text("Unknown plan-only tool.", True),
            }
        try:
            result = propose_plan(params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 - fail closed without exposing internals
            result = _text(f"Plan proposal refused ({type(exc).__name__}).", True)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main() -> None:
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001 - protocol errors are intentionally generic
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32603, "message": type(exc).__name__},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
