"""jarvis_dev: bounded project tools for the Link `development` scope.

Tools: list_project_files, read_project_file (no approval), write_project_file and run_project_tests
(each needs one exact supervisor approval, consumed once). The workspace boundary lives in
jarvis_link_host.dev_workspace; this file only maps tool calls onto it and onto the supervisor.

Approval identifiers are immutable:
- write_project_file: the approval binds the workspace-relative path, the SHA-256 and size of the proposed
  content. The content is staged privately under a hash name; execution refuses any mismatch.
- run_project_tests: the approval binds a manifest hash of every workspace file, so the approved suite runs
  only against the exact files that were reviewed. Any change after approval refuses the run.
"""
import hashlib
import json
import os
import pwd
import re
import sys
import uuid
from pathlib import Path

# Code and defaults come from the account's real home, not from an overridden HOME (tests set HOME).
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
from typing import Any

HOST_SRC = REAL_HOME / "Documents" / "Jarvis-Lab" / "jarvis-link" / "host" / "src"
OPENJARVIS_SRC = REAL_HOME / ".openjarvis" / "src" / "src"
for candidate in (HOST_SRC, OPENJARVIS_SRC):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from jarvis_link_host import dev_workspace as dw  # noqa: E402
from openjarvis.tasks.dev_capability import (
    PLAN_APPROVAL_OPERATION,
    CapabilityError,
    CapabilityStore,
    capability_db_path,
    plan_approval_arguments,
    plan_approval_target,
)
from openjarvis.tasks.dev_capability_adapter import DevelopmentCapabilityMCPAdapter
from openjarvis.tasks.supervisor import LINK_SCOPE_CAPABILITIES, operation_digest

STAGING = Path(os.environ.get("JARVIS_DEV_STAGING") or (REAL_HOME / ".jarvis-dev-staging"))
AUDIT = Path(os.environ.get("JARVIS_DEV_AUDIT") or (REAL_HOME / ".jarvis-link-test" / "dev-audit.jsonl"))
CAPABILITY_DB = Path(capability_db_path())
PROTOCOL = "2025-03-26"
_TASK_ID_OK = __import__("re").compile(r"^[0-9a-f]{32}$")
_STORE = None
_CAPABILITIES = None

TOOLS = [
    {"name": "list_project_files", "description": "List files in a directory of the approved project workspace.",
     "inputSchema": {"type": "object", "properties": {"dir": {"type": "string"}}, "additionalProperties": False}},
    {"name": "read_project_file", "description": "Read one UTF-8 text file from the approved project workspace.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}, "additionalProperties": False}},
    {"name": "approve_plan", "description": "Request one exact, bounded user approval for a project development plan. No files are changed until approved.",
     "inputSchema": {"type": "object", "required": ["plan", "reason"], "properties": {
         "plan": {"type": "object", "required": ["plan_id", "allowed_ops", "allowed_paths", "max_file_bytes", "max_total_bytes", "max_writes", "max_test_runs", "max_repairs"],
                  "properties": {"plan_id": {"type": "string"}, "allowed_ops": {"type": "array", "items": {"type": "string"}},
                                 "allowed_paths": {"type": "array", "items": {"type": "string"}}, "max_file_bytes": {"type": "integer"},
                                 "max_total_bytes": {"type": "integer"}, "max_writes": {"type": "integer"}, "max_test_runs": {"type": "integer"},
                                 "max_repairs": {"type": "integer"}}, "additionalProperties": False},
         "reason": {"type": "string"}}, "additionalProperties": False}},
    {"name": "write_project_file", "description": "Create or replace one file in the approved project workspace. Needs one exact approval.",
     "inputSchema": {"type": "object", "required": ["path", "content"],
                     "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "capability_id": {"type": "string"}}, "additionalProperties": False}},
    {"name": "run_project_tests", "description": "Run the project's own unittest suite (tests/). Needs one exact approval.",
     "inputSchema": {"type": "object", "properties": {"capability_id": {"type": "string"}}, "additionalProperties": False}},
    {"name": "consume_repair", "description": "Consume one repair allowance from the active approved development plan.",
     "inputSchema": {"type": "object", "required": ["capability_id", "reason"], "properties": {"capability_id": {"type": "string"}, "reason": {"type": "string"}}, "additionalProperties": False}},
    {"name": "artifact_manifest", "description": "Create a hash and size manifest for explicitly listed files within the approved plan.",
     "inputSchema": {"type": "object", "required": ["capability_id", "paths"], "properties": {"capability_id": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False}},
]
WRITE_TOOLS = {"write_project_file", "run_project_tests"}


def _text(value: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}], "isError": error}


def _workspace() -> dw.Workspace:
    return dw.Workspace.open(os.environ.get("JARVIS_DEV_WORKSPACE") or dw.DEFAULT_WORKSPACE)


def _audit(event: str, **fields: Any) -> None:
    dw.AuditLog(AUDIT).record(event, **fields)


def _store():
    global _STORE
    if _STORE is None:
        from openjarvis.tasks.supervisor import SupervisorStore
        # JARVIS_SUPERVISOR_DB is for tests only; production uses the supervisor's default database.
        _STORE = SupervisorStore(db_path=os.environ.get("JARVIS_SUPERVISOR_DB") or None, run_recovery=False)
    return _STORE


def _capability_store():
    global _CAPABILITIES
    if _CAPABILITIES is None:
        CAPABILITY_DB.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _CAPABILITIES = CapabilityStore(str(CAPABILITY_DB))
        os.chmod(CAPABILITY_DB, 0o600)
    return _CAPABILITIES


def _bound_task() -> tuple[str, str, str]:
    """Validate the supervisor-injected task against its canonical stored owner and scope."""
    task_id = os.environ.get("JARVIS_SUPERVISOR_TASK_ID", "")
    if not _TASK_ID_OK.fullmatch(task_id):
        raise PermissionError("development tools are available only to a development Link task")
    if os.environ.get("JARVIS_SUPERVISOR_TASK_SOURCE", "jarvis_link") != "jarvis_link" or os.environ.get("JARVIS_SUPERVISOR_TASK_SCOPE", "development") != "development":
        raise PermissionError("development tool context does not match the supervisor task scope")
    task = _store().get_task(task_id)
    if task is None:
        raise PermissionError("development task does not exist")
    try:
        metadata = json.loads(task.get("recovery_json") or "{}")
        scope = json.loads(task.get("capabilities_json") or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermissionError("development task authorization metadata is invalid") from exc
    if (
        metadata.get("source") != "jarvis_link"
        or metadata.get("scope") != "development"
        or metadata.get("capability_scope") != ["development"]
        or scope != sorted(LINK_SCOPE_CAPABILITIES["development"])
        or task.get("state") not in {"CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL"}
        or not str(metadata.get("owner_actor", "")).startswith("device:")
    ):
        raise PermissionError("development task scope or paired-device owner is invalid")
    return task_id, "jarvis_link", "development"


def _capability_adapter() -> DevelopmentCapabilityMCPAdapter:
    task_id, _, _ = _bound_task()
    task = _store().get_task(task_id) or {}
    try:
        metadata = json.loads(task.get("recovery_json") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermissionError("development task owner is invalid") from exc
    owner = metadata.get("owner_actor")
    if not isinstance(owner, str) or not re.fullmatch(r"device:[0-9a-f]{32}", owner):
        raise PermissionError("development task is not paired to a valid device")
    return DevelopmentCapabilityMCPAdapter.from_task_environment(
        _store(),
        _capability_store(),
        user_id=owner,
        workspace_root=str(_workspace().root),
    )


def _record_task_event(event: str, fields: dict[str, Any]) -> None:
    task_id, _, _ = _bound_task()
    _store().record_event(task_id, event, fields)


def manifest_sha256(workspace: dw.Workspace) -> str:
    """Hash of every regular workspace file (path and content). Symlinks are refused, not followed."""
    digest = hashlib.sha256()
    count = 0
    for path in sorted(workspace.root.rglob("*")):
        if path.is_symlink():
            raise dw.WorkspaceError("symbolic links are not allowed in the project workspace")
        if path.is_file():
            count += 1
            if count > 2000:
                raise dw.WorkspaceError("workspace has too many files to approve")
            rel = path.relative_to(workspace.root).as_posix()
            digest.update(rel.encode("utf-8") + b"\x00" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _request_approval(tool: str, args: dict[str, Any], target: str) -> str:
    task_id, _, _ = _bound_task()
    operation = f"mcp:jarvis_dev:{tool}"
    approval_id, digest = _store().create_approval(
        task_id, operation, target, args,
        "Project change or test run needs one exact user approval on the Vivo.",
    )
    _store().record_event(task_id, "dev.authorization_required", {
        "approval_id": approval_id, "operation": operation, "target": target, "operation_digest": digest,
    })
    _audit("approval_requested", tool=tool, target=target, approval_id=approval_id, digest=digest)
    return approval_id


def _plan_from_args(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PermissionError("plan must be an object")
    required = {"plan_id", "allowed_ops", "allowed_paths", "max_file_bytes", "max_total_bytes", "max_writes", "max_test_runs", "max_repairs"}
    if set(value) != required:
        raise PermissionError("plan fields do not match the bounded development schema")
    if not isinstance(value["allowed_ops"], list) or not isinstance(value["allowed_paths"], list):
        raise PermissionError("plan operation and path restrictions must be lists")
    return value


def _claim_approved(tool: str, args: dict[str, Any], target: str) -> str:
    """Single-use: the supervisor must already have consumed this exact approval."""
    task_id, _, _ = _bound_task()
    approval_id = os.environ.get("JARVIS_SUPERVISOR_APPROVAL_ID", "")
    if os.environ.get("JARVIS_SUPERVISOR_APPROVED_OPERATION") != "1" or not _TASK_ID_OK.fullmatch(approval_id):
        raise PermissionError("no approved operation for this call")
    operation = f"mcp:jarvis_dev:{tool}"
    if os.environ.get("JARVIS_SUPERVISOR_OPERATION") != operation or os.environ.get("JARVIS_SUPERVISOR_TARGET") != target:
        raise PermissionError("approved operation does not match this call")
    if not _store().claim_mcp_dispatch(approval_id, task_id=task_id, operation=operation, target=target, arguments=args):
        raise PermissionError("approval was stale, modified, or already used")
    return approval_id


def _stage_path(sha: str) -> Path:
    STAGING.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STAGING / f"{sha}.bin"


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return _text("Tool arguments must be an object.", True)
    workspace = _workspace()
    if name == "list_project_files":
        return _text(json.dumps(workspace.list_dir(str(args.get("dir", "."))), ensure_ascii=False))
    if name == "read_project_file":
        return _text(workspace.read_text(str(args.get("path", ""))))
    if name == "approve_plan":
        approved_dispatch = os.environ.get("JARVIS_SUPERVISOR_APPROVED_OPERATION") == "1"
        if approved_dispatch:
            canonical_plan = args.get("plan")
            canonical_keys = {
                "plan_id", "device_id", "user_id", "workspace_root", "allowed_ops", "allowed_paths",
                "max_file_bytes", "max_total_bytes", "max_writes", "max_test_runs", "max_repairs",
                "max_duration_s", "ttl_s",
            }
            if set(args) != {"plan", "plan_digest"} or not isinstance(canonical_plan, dict) or set(canonical_plan) != canonical_keys:
                return _text("The stored approved plan arguments are malformed.", True)
            plan = {key: canonical_plan[key] for key in (
                "plan_id", "allowed_ops", "allowed_paths", "max_file_bytes", "max_total_bytes",
                "max_writes", "max_test_runs", "max_repairs",
            )}
            reason = "The supervisor is issuing the exact approved development plan."
        else:
            plan = _plan_from_args(args.get("plan"))
            reason = args.get("reason")
            if not isinstance(reason, str):
                return _text("approval reason must be text", True)
        adapter = _capability_adapter()
        approval = adapter.request_plan_approval(plan, reason=reason)
        if not approved_dispatch:
            _record_task_event("development.plan_approval_requested", {
                "approval_id": approval.approval_id,
                "plan_digest": approval.plan_digest,
                "args_digest": approval.args_digest,
                "status": approval.status,
            })
            return _text(
                f"Waiting for your one-time approval of the bounded project plan (approval_id={approval.approval_id}). "
                "No project files were changed."
            )
        if args.get("plan_digest") != approval.plan_digest:
            return _text("The approved plan digest does not match the registered plan.", True)
        approval_id = os.environ.get("JARVIS_SUPERVISOR_APPROVAL_ID", "")
        with adapter.capability_store._lock:
            binding = adapter.capability_store._db.execute(
                "SELECT spec_json FROM dev_plan_approval_bindings WHERE task_id=?",
                (adapter.task_id,),
            ).fetchone()
        if binding is None:
            return _text("This task has no registered development plan. No capability was issued.", True)
        spec = adapter._decode_spec(binding["spec_json"])
        expected_target = plan_approval_target(spec)
        expected_digest = operation_digest(PLAN_APPROVAL_OPERATION, expected_target, plan_approval_arguments(spec))
        if (
            approval.approval_id != approval_id
            or os.environ.get("JARVIS_SUPERVISOR_OPERATION") != PLAN_APPROVAL_OPERATION
            or os.environ.get("JARVIS_SUPERVISOR_TARGET") != expected_target
            or os.environ.get("JARVIS_SUPERVISOR_OPERATION_DIGEST") != expected_digest
        ):
            return _text("The consumed approval did not match this exact plan. No capability was issued.", True)
        capability_id = adapter.issue_approved_plan(approval_id)
        _record_task_event("development.plan_capability_issued", {
            "approval_id": approval_id,
            "plan_digest": approval.plan_digest,
            "capability_id_digest": hashlib.sha256(capability_id.encode("utf-8")).hexdigest(),
        })
        return _text(f"The exact approved plan is active. Use capability_id={capability_id} only for its bounded operations.")
    if name == "write_project_file":
        path = str(args.get("path", ""))
        capability_id = args.get("capability_id")
        if capability_id is not None:
            content = args.get("content")
            if not isinstance(content, str):
                return _text("content must be UTF-8 text", True)
            adapter = _capability_adapter()
            try:
                authorization = adapter.authorize_write(
                    capability_id, op_id=uuid.uuid4().hex, rel_path=path, content=content,
                )
                receipt = workspace.write_text(path, content)
            except CapabilityError as exc:
                return _text(f"Development capability refused this write ({exc.code}).", True)
            _record_task_event("development.file_written", {
                "path": path, "bytes": receipt["bytes"], "sha256": receipt["sha256"],
                "plan_target": authorization["target"],
            })
            _audit("capability_write", path=path, bytes=receipt["bytes"], sha256=receipt["sha256"])
            return _text(f"Wrote {receipt['path']} ({receipt['bytes']} bytes, sha256 {receipt['sha256'][:12]}).")
        approved = os.environ.get("JARVIS_SUPERVISOR_APPROVED_OPERATION") == "1"
        if approved:
            # Execution uses only the approved immutable identifiers; the content comes from the staged copy.
            sha = str(args.get("content_sha256", ""))
            size = args.get("size")
            if not __import__("re").fullmatch(r"[0-9a-f]{64}", sha) or not isinstance(size, int):
                return _text("approved write arguments are invalid", True)
            exact = {"path": path, "content_sha256": sha, "size": size}
            target = f"taskflow:{path}"
            _claim_approved(name, exact, target)
            workspace.resolve(path, for_write=True)  # containment is enforced again at execution
            staged = _stage_path(sha)
            if not staged.is_file():
                return _text("approved content is no longer staged", True)
            data_now = staged.read_bytes()
            if hashlib.sha256(data_now).hexdigest() != sha or len(data_now) != size:
                return _text("staged content does not match the approval", True)
            receipt = workspace.write_text(path, data_now.decode("utf-8"))
            staged.unlink(missing_ok=True)
            _audit("write", path=path, sha256=receipt["sha256"], bytes=receipt["bytes"])
            return _text(f"Wrote {receipt['path']} ({receipt['bytes']} bytes, sha256 {receipt['sha256'][:12]}).")
        content = args.get("content")
        if not isinstance(content, str):
            return _text("content must be a string", True)
        workspace.resolve(path, for_write=True)  # refuse early; the same check runs again at execution
        data = content.encode("utf-8")
        if len(data) > dw.MAX_FILE_BYTES:
            return _text("content is too large", True)
        sha = hashlib.sha256(data).hexdigest()
        target = f"taskflow:{path}"
        exact = {"path": path, "content_sha256": sha, "size": len(data)}
        _stage_path(sha).write_bytes(data)
        os.chmod(_stage_path(sha), 0o600)
        approval_id = _request_approval(name, exact, target)
        return _text(f"Waiting for exact approval of this file change (approval_id={approval_id}). Nothing was written.")
    if name == "run_project_tests":
        capability_id = args.get("capability_id")
        if capability_id is not None:
            adapter = _capability_adapter()
            try:
                argv = adapter.authorize_test(capability_id, op_id=uuid.uuid4().hex, command="unittest-tests")
            except CapabilityError as exc:
                return _text(f"Development capability refused this test run ({exc.code}).", True)
            result = dw.run_bounded(list(argv), cwd=workspace.root, timeout_s=min(600, dw.TEST_TIMEOUT_S))
            result.update(dw.parse_unittest_summary(result["output"]))
            _record_task_event("development.tests_completed", {
                "passed": result["passed"], "tests_ran": result["tests_ran"],
                "failures": result["failures"], "errors": result["errors"],
                "timed_out": result["timed_out"], "duration_s": result["duration_s"],
            })
            summary = (f"Tests: ran {result['tests_ran']}, failures {result['failures']}, errors {result['errors']}, "
                       f"passed={result['passed']}, timed_out={result['timed_out']}, duration {result['duration_s']}s.")
            return _text(summary + "\n" + result["output"][-3000:], error=not result["passed"])
        manifest = manifest_sha256(workspace)
        exact = {"command": "unittest-tests", "manifest_sha256": manifest}
        target = "taskflow:tests"
        if os.environ.get("JARVIS_SUPERVISOR_APPROVED_OPERATION") == "1":
            _claim_approved(name, exact, target)
            if manifest_sha256(workspace) != manifest:
                return _text("Project files changed after approval; the test run was refused.", True)
            result = workspace.run_tests()
            _audit("tests", passed=result["passed"], tests_ran=result["tests_ran"], failures=result["failures"],
                   errors=result["errors"], timed_out=result["timed_out"], returncode=result["returncode"])
            summary = (f"Tests: ran {result['tests_ran']}, failures {result['failures']}, errors {result['errors']}, "
                       f"passed={result['passed']}, timed_out={result['timed_out']}, duration {result['duration_s']}s.")
            return _text(summary + "\n" + result["output"][-3000:], error=not result["passed"])
        approval_id = _request_approval(name, exact, target)
        return _text(f"Waiting for exact approval to run the project tests (approval_id={approval_id}). Nothing was run.")
    if name == "consume_repair":
        adapter = _capability_adapter()
        capability_id = args.get("capability_id")
        reason = args.get("reason")
        if not isinstance(capability_id, str) or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            return _text("A capability ID and concise repair reason are required.", True)
        try:
            attempt = adapter.consume_repair(capability_id, op_id=uuid.uuid4().hex)
        except CapabilityError as exc:
            return _text(f"Repair allowance refused ({exc.code}). Request a new bounded plan if needed.", True)
        _record_task_event("development.repair_started", {"attempt": attempt, "reason_digest": hashlib.sha256(reason.encode("utf-8")).hexdigest()})
        return _text(f"Repair attempt {attempt} of the approved budget is now available.")
    if name == "artifact_manifest":
        adapter = _capability_adapter()
        capability_id = args.get("capability_id")
        paths = args.get("paths")
        if not isinstance(capability_id, str) or not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            return _text("A capability ID and explicit relative artifact paths are required.", True)
        try:
            manifest = adapter.artifact_manifest(capability_id, op_id=uuid.uuid4().hex, rel_paths=paths)
        except CapabilityError as exc:
            return _text(f"Artifact collection refused ({exc.code}).", True)
        _record_task_event("development.artifacts_collected", {
            "files": len(manifest["files"]), "bytes": manifest["total_bytes"],
            "manifest_sha256": manifest["manifest_sha256"],
        })
        return _text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    return _text(f"Unknown tool: {name}", True)


def handle(message: dict[str, Any]):
    method, rid = message.get("method"), message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                                                          "serverInfo": {"name": "jarvis-dev", "version": "0.1.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            _bound_task()
            result = call_tool(str(params.get("name", "")), params.get("arguments") or {})
        except CapabilityError as exc:
            result = _text(f"Refused: {exc.code}", True)
        except (PermissionError, dw.WorkspaceError) as exc:
            result = _text(f"Refused: {exc}", True)
        except Exception as exc:  # never leak internals; the type is enough for the audit trail
            result = _text(f"Development tool failed: {type(exc).__name__}", True)
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return None


def main() -> None:
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": type(exc).__name__}}
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
