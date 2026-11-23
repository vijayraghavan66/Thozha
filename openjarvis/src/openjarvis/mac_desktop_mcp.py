"""Local MCP bridge used by the OpenJarvis/Codex integration."""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil

from openjarvis.security.mac_mcp_policy import open_app_mcp_enabled, record_open_app_event

MODE = os.environ.get("JARVIS_DESKTOP_MODE", "basic").lower()
CLICLICK = shutil.which("cliclick") or "/opt/homebrew/bin/cliclick"
SENSITIVE = re.compile(r"\b(send|submit|delete|remove|purchase|buy|pay|transfer|publish|post|share|approve|confirm|place order)\b", re.I)
_SUPERVISOR_APPROVAL_CONSUMED = os.environ.get("JARVIS_SUPERVISOR_APPROVED_OPERATION") == "1"
_SUPERVISOR_APPROVAL_USED = False
_SUPERVISOR_OPERATION_VERIFIED = False
_SUPERVISOR_APPROVAL_ID = os.environ.get("JARVIS_SUPERVISOR_APPROVAL_ID", "")
_SUPERVISOR_TASK_ID = os.environ.get("JARVIS_SUPERVISOR_TASK_ID", "")
_SUPERVISOR_SOURCE = os.environ.get("JARVIS_SUPERVISOR_TASK_SOURCE", "")
_SUPERVISOR_SCOPE = os.environ.get("JARVIS_SUPERVISOR_TASK_SCOPE", "")
_SUPERVISOR_OPERATION = os.environ.get("JARVIS_SUPERVISOR_OPERATION", "")
_SUPERVISOR_TARGET = os.environ.get("JARVIS_SUPERVISOR_TARGET", "")
_SUPERVISOR_DIGEST = os.environ.get("JARVIS_SUPERVISOR_OPERATION_DIGEST", "")
_STORE = None

def _safe_application(args: dict[str, Any]) -> str:
    value = args.get("name")
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()[:120]

def _open_app_audit(stage: str, *, application: str, claim_outcome: str,
                    execution_attempted: bool, result: str = "", exit_code: int | None = None,
                    error_code: str = "", approval_id: str | None = None) -> None:
    """Persist a redacted, structured event for the privileged app-launch path."""
    record_open_app_event(
        _supervisor_store() if _SUPERVISOR_TASK_ID else None,
        stage=stage,
        task_id=_SUPERVISOR_TASK_ID,
        approval_id=_SUPERVISOR_APPROVAL_ID if approval_id is None else approval_id,
        application=application,
        dispatch_claim_outcome=claim_outcome,
        execution_attempted=execution_attempted,
        result=result,
        exit_code=exit_code,
        error_code=error_code,
    )

def _supervisor_store():
    global _STORE
    if _STORE is None:
        from openjarvis.tasks.supervisor import SupervisorStore
        _STORE = SupervisorStore(run_recovery=False)
    return _STORE

def _request_supervisor_approval(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        from openjarvis.security.mcp_supervisor_bridge import create_mcp_approval

        approval_id, operation, target, _digest = create_mcp_approval(
            _supervisor_store(),
            task_id=_SUPERVISOR_TASK_ID,
            source=_SUPERVISOR_SOURCE,
            scope=_SUPERVISOR_SCOPE,
            tool=name,
            arguments=args,
        )
        if name == "open_app":
            _open_app_audit(
                "approval_requested", application=_safe_application(args),
                claim_outcome="not_attempted", execution_attempted=False,
                result="awaiting_owner_decision",
                approval_id=approval_id,
            )
        return text_result(
            f"This exact Mac operation is waiting for supervisor approval (approval_id={approval_id}). No action was performed.",
            True,
        )
    except Exception as exc:
        return text_result(f"Mac control was blocked because supervisor approval could not be recorded ({type(exc).__name__}).", True)

def _authorize_consumed_supervisor_operation(name: str, args: dict[str, Any]) -> bool:
    global _SUPERVISOR_APPROVAL_USED, _SUPERVISOR_OPERATION_VERIFIED
    _SUPERVISOR_OPERATION_VERIFIED = False
    if (
        not _SUPERVISOR_APPROVAL_CONSUMED
        or _SUPERVISOR_APPROVAL_USED
        or _SUPERVISOR_OPERATION != f"mcp:jarvis_mac:{name}"
    ):
        if name == "open_app":
            _open_app_audit(
                "dispatch_claim", application=_safe_application(args),
                claim_outcome="denied", execution_attempted=False,
                result="missing_or_mismatched_supervisor_grant",
            )
        return False
    if name == "open_app" and not open_app_mcp_enabled():
        _open_app_audit(
            "dispatch_claim", application=_safe_application(args),
            claim_outcome="disabled", execution_attempted=False,
            result="open_app_mcp_disabled",
        )
        return False
    try:
        from openjarvis.security.mcp_supervisor_bridge import claim_consumed_mcp_approval

        accepted = claim_consumed_mcp_approval(
            _supervisor_store(),
            approval_id=_SUPERVISOR_APPROVAL_ID,
            task_id=_SUPERVISOR_TASK_ID,
            operation=_SUPERVISOR_OPERATION,
            target=_SUPERVISOR_TARGET,
            arguments=args,
            expected_digest=_SUPERVISOR_DIGEST,
        )
        if accepted:
            _SUPERVISOR_APPROVAL_USED = True
            _SUPERVISOR_OPERATION_VERIFIED = True
            if name == "open_app":
                _open_app_audit(
                    "dispatch_claim", application=_safe_application(args),
                    claim_outcome="claimed", execution_attempted=False,
                    result="exact_consumed_grant_claimed",
                )
            return True
    except Exception:
        if name == "open_app":
            _open_app_audit(
                "dispatch_claim", application=_safe_application(args),
                claim_outcome="error", execution_attempted=False,
                result="claim_error", error_code="CLAIM_ERROR",
            )
        return False
    if name == "open_app":
        _open_app_audit(
            "dispatch_claim", application=_safe_application(args),
            claim_outcome="denied", execution_attempted=False,
            result="exact_consumed_grant_not_claimed",
        )
    return False

def run(args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)

def text_result(value, error=False):
    return {"content": [{"type": "text", "text": value}], "isError": error}

def active_app():
    script='''tell application "System Events"
set p to first application process whose frontmost is true
set a to name of p
set w to ""
try
set w to name of front window of p
end try
return a & " — " & w
end tell'''
    r=run(["/usr/bin/osascript", "-e", script], 8)
    if r.returncode: raise RuntimeError("macOS Automation permission is needed to read the active window")
    return r.stdout.strip() or "Unknown active window"

def screenshot():
    fd, name=tempfile.mkstemp(prefix="jarvis-screen-", suffix=".png"); os.close(fd)
    p=Path(name)
    try:
        r=run(["/usr/sbin/screencapture", "-x", str(p)], 20)
        if r.returncode or not p.exists() or not p.stat().st_size:
            raise RuntimeError("Screen capture failed. Grant Screen Recording to the Jarvis host app.")
        data=p.read_bytes(); header=data[:24]
        if header[:8] != b"\x89PNG\r\n\x1a\n": raise RuntimeError("Screen capture returned an invalid PNG")
        w,h=struct.unpack(">II", header[16:24])
        return {"content":[{"type":"text","text":f"Active window: {active_app()}; screen {w}x{h}."},{"type":"image","mimeType":"image/png","data":base64.b64encode(data).decode("ascii")}],"isError":False}
    finally: p.unlink(missing_ok=True)

def disk_usage():
    d=shutil.disk_usage("/"); gib=1024**3; gb=1000**3
    return text_result(json.dumps({
        "mount":"/",
        "capacity_gb":round(d.total/gb,1),
        "capacity_gib":round(d.total/gib,1),
        "used_gib":round((d.total-d.free)/gib,1),
        "free_gb":round(d.free/gb,1),
        "free_gib":round(d.free/gib,1),
    }))

def ask_sensitive(description,payload):
    # Any extra local sensitivity check is satisfied only for the exact call
    # whose supervisor approval was consumed by the parent process.
    return _SUPERVISOR_OPERATION_VERIFIED

def system_info():
    """Return fixed, read-only macOS version and uptime facts."""
    task_id = ""
    audit_store = None
    try:
        task_id, audit_store = _begin_mac_read_audit("get_system_info")
        if sys.platform != "darwin":
            raise RuntimeError("system_info_unavailable_on_host")
        version = platform.mac_ver()[0]
        if not version:
            raise RuntimeError("macos_version_unavailable")
        uptime_seconds = max(0, int(time.time() - psutil.boot_time()))
        payload = json.dumps(
            {"macos_version": version, "uptime_seconds": uptime_seconds}
        )
        if audit_store is not None:
            audit_store.record_event(
                task_id,
                "mac.read_only_tool.completed",
                {"operation": "get_system_info", "success": True},
            )
        return text_result(payload)
    except Exception as exc:
        if audit_store is not None:
            try:
                audit_store.record_event(
                    task_id,
                    "mac.read_only_tool.failed",
                    {
                        "operation": "get_system_info",
                        "error_code": type(exc).__name__,
                    },
                )
            except Exception:
                pass
        return text_result(
            f"System information could not be read ({type(exc).__name__}).", True
        )
    finally:
        if audit_store is not None:
            audit_store.close()


def _begin_mac_read_audit(operation: str):
    """Validate server-issued task scope and persist a content-free start event.

    This executes inside the MCP server process. It deliberately opens the
    supervisor database with recovery disabled so a short-lived MCP process
    cannot alter task recovery state.
    """
    if MODE != "mac_read":
        return "", None
    task_id = os.environ.get("JARVIS_SUPERVISOR_TASK_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise PermissionError("supervisor_task_context_missing")
    if (
        os.environ.get("JARVIS_SUPERVISOR_TASK_SOURCE") != "jarvis_link"
        or os.environ.get("JARVIS_SUPERVISOR_TASK_SCOPE") != "mac_read"
    ):
        raise PermissionError("supervisor_task_scope_mismatch")
    from openjarvis.core.paths import get_config_dir
    from openjarvis.tasks.supervisor import SupervisorStore

    configured_db = os.environ.get("JARVIS_SUPERVISOR_DB", "").strip()
    db_path = Path(configured_db).expanduser() if configured_db else get_config_dir() / "supervisor.db"
    if not db_path.is_absolute():
        raise PermissionError("supervisor_database_path_invalid")
    store = SupervisorStore(
        str(db_path), run_recovery=False
    )
    try:
        task = store.get_task(task_id)
        metadata = json.loads(task.get("recovery_json") or "{}") if task else {}
        if (
            task is None
            or task.get("state") != "RUNNING"
            or metadata.get("source") != "jarvis_link"
            or metadata.get("capability_scope") != ["mac_read"]
        ):
            raise PermissionError("supervisor_task_not_authorized")
        store.record_event(
            task_id,
            "mac.read_only_tool.started",
            {"operation": operation},
        )
        return task_id, store
    except Exception:
        store.close()
        raise

def tool_list():
    common=[
      {"name":"get_disk_usage","description":"Read total, used, and free root disk space on the Mac.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False}},
    ]
    if MODE in {"mac_read", "read", "control", "workspace"}:
        common.append({"name":"get_system_info","description":"Read the macOS product version and uptime seconds. This is read-only and accepts no arguments.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False}})
    if MODE in {"read","control","workspace"}:
        common += [
          {"name":"get_active_app","description":"Read the frontmost Mac app and window title.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False}},
          {"name":"capture_screen","description":"Capture the current screen for visual analysis; screenshot is sent to GPT-6 Luna.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":False}},
        ]
    if MODE in {"control","workspace"}:
        common += [
          {"name":"click","description":"Click a visible coordinate. Include target label and reason; high-impact actions require phone approval.","inputSchema":{"type":"object","properties":{"x":{"type":"integer","minimum":0},"y":{"type":"integer","minimum":0},"button":{"type":"string","enum":["left","right","double"]},"target":{"type":"string"},"reason":{"type":"string"}},"required":["x","y","target","reason"],"additionalProperties":False}},
          {"name":"type_text","description":"Type into the focused field. Never type passwords, one-time codes, API keys, or payment details.","inputSchema":{"type":"object","properties":{"text":{"type":"string","maxLength":2000},"target":{"type":"string"},"reason":{"type":"string"}},"required":["text","target","reason"],"additionalProperties":False}},
          {"name":"press_key","description":"Press one supported key. Enter always requires phone approval.","inputSchema":{"type":"object","properties":{"key":{"type":"string","enum":["enter","tab","esc","space","delete","arrow-up","arrow-down","arrow-left","arrow-right"]},"target":{"type":"string"},"reason":{"type":"string"}},"required":["key","target","reason"],"additionalProperties":False}},
          {"name":"scroll","description":"Scroll a small amount in the current app.","inputSchema":{"type":"object","properties":{"direction":{"type":"string","enum":["up","down","left","right"]},"amount":{"type":"integer","minimum":1,"maximum":8},"target":{"type":"string"}},"required":["direction","target"],"additionalProperties":False}},
          {"name":"open_app","description":"Open a named macOS application at the user's request.","inputSchema":{"type":"object","properties":{"name":{"type":"string","maxLength":120}},"required":["name"],"additionalProperties":False}},
        ]
    if MODE=="workspace":
        common.append({"name":"open_browser_workspace","description":"Open a separate Chrome profile for this task.","inputSchema":{"type":"object","properties":{"url":{"type":"string","maxLength":2048}},"additionalProperties":False}})
    if not open_app_mcp_enabled():
        common = [tool for tool in common if tool.get("name") != "open_app"]
    return common

def call_tool(name,args):
    if not isinstance(args, dict): return text_result("Mac tool arguments must be an object.", True)
    read_only_tools = {"get_disk_usage", "get_system_info", "get_active_app", "capture_screen"}
    if name in read_only_tools:
        if MODE not in {"basic", "mac_read", "read", "control", "workspace"}:
            return text_result("This task has no Mac observation capability.", True)
        if MODE == "basic" and name != "get_disk_usage":
            return text_result("This task has no system diagnostic capability.", True)
        if MODE == "mac_read" and name not in {"get_disk_usage", "get_system_info"}:
            return text_result("This task has no screen observation capability.", True)
        if args:
            return text_result("Read-only Mac tools do not accept arguments.", True)
        if name == "get_disk_usage": return disk_usage()
        if name == "get_system_info": return system_info()
        if name == "get_active_app": return text_result(active_app())
        if name == "capture_screen": return screenshot()
    if name == "open_app" and not open_app_mcp_enabled():
        _open_app_audit(
            "disabled", application=_safe_application(args),
            claim_outcome="not_attempted", execution_attempted=False,
            result="open_app_mcp_disabled",
        )
        return text_result("Approval-gated MCP app launching is temporarily disabled; no application was opened.", True)
    if MODE not in {"control","workspace"}: return text_result("This task has read-only screen access.",True)
    target=str(args.get("target") or "visible control"); reason=str(args.get("reason") or "")
    if _SUPERVISOR_APPROVAL_CONSUMED:
        if not _authorize_consumed_supervisor_operation(name,args):
            return text_result("Stored supervisor approval did not match this Mac operation; nothing was changed.",True)
    else:
        return _request_supervisor_approval(name,args)
    sensitive=bool(SENSITIVE.search(" ".join((name,target,reason,str(args.get("text") or "")))))
    if name=="press_key" and args.get("key")=="enter": sensitive=True
    if sensitive and not ask_sensitive(f"Confirm {name} on {target}: {reason}",{"tool":name,"target":target,"reason":reason}):
        return text_result("Action denied or confirmation expired; nothing was changed.",True)
    if name=="click":
        x,y=int(args["x"]),int(args["y"])
        fd,path=tempfile.mkstemp(suffix=".png"); os.close(fd)
        try:
            sc=run(["/usr/sbin/screencapture","-x",path],20)
            if sc.returncode or not Path(path).exists(): return text_result("Screen capture failed; grant Screen Recording permission.",True)
            h=Path(path).read_bytes()[:24]; w,hgt=struct.unpack(">II",h[16:24])
            if x>=w or y>=hgt: return text_result(f"Coordinate outside screen {w}x{hgt}.",True)
        finally: Path(path).unlink(missing_ok=True)
        button=args.get("button","left"); cmd={"left":f"c:{x},{y}","right":f"rc:{x},{y}","double":f"dc:{x},{y}"}[button]
        r=run([CLICLICK,cmd],8)
    elif name=="type_text":
        value=str(args.get("text") or "")
        if not value or len(value)>2000 or "\x00" in value: return text_result("Text is invalid or too long.",True)
        r=run([CLICLICK,"t:"+value],20)
    elif name=="press_key":
        key=str(args.get("key") or ""); r=run([CLICLICK,"kp:"+key],8)
    elif name=="scroll":
        direction=str(args.get("direction") or "down"); amount=max(1,min(8,int(args.get("amount") or 3)))
        scroll={"up":"scru","down":"scrd","left":"scrl","right":"scrr"}[direction]
        r=run([CLICLICK,f"{scroll}:{amount}"],8)
    elif name=="open_app":
        app=_safe_application(args)
        if not app or len(app)>120: return text_result("Invalid application name.",True)
        _open_app_audit(
            "execution_attempt", application=app,
            claim_outcome="claimed", execution_attempted=True,
            result="open_command_started",
        )
        try:
            r=run(["/usr/bin/open","-a",app],12)
        except subprocess.TimeoutExpired:
            _open_app_audit(
                "execution_result", application=app, claim_outcome="claimed",
                execution_attempted=True, result="launch_command_timeout",
                error_code="TIMEOUT",
            )
            raise
        except Exception as exc:
            _open_app_audit(
                "execution_result", application=app, claim_outcome="claimed",
                execution_attempted=True, result="launch_command_error",
                error_code=type(exc).__name__,
            )
            raise
        _open_app_audit(
            "execution_result", application=app, claim_outcome="claimed",
            execution_attempted=True,
            result="launch_command_accepted" if r.returncode == 0 else "launch_command_failed",
            exit_code=r.returncode,
            error_code="" if r.returncode == 0 else "OPEN_COMMAND_FAILED",
        )
    elif name=="open_browser_workspace":
        chrome=Path("/Applications/Google Chrome.app")
        if not chrome.exists(): return text_result("Google Chrome was not found.",True)
        url=str(args.get("url") or "about:blank")
        if not (url.startswith("https://") or url.startswith("http://") or url=="about:blank"): return text_result("Only http(s) URLs or about:blank are allowed.",True)
        profile=Path.home()/".openjarvis"/"browser-profile"; profile.mkdir(parents=True,exist_ok=True)
        subprocess.Popen(["/usr/bin/open","-na",str(chrome),"--args",f"--user-data-dir={profile}","--new-window",url],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        return text_result(f"Opened a separate Chrome profile for {url}.")
    else: return text_result(f"Unknown tool: {name}",True)
    warning=(r.stderr or "").strip()
    if r.returncode or "Accessibility privileges not enabled" in warning:
        return text_result("macOS blocked the action. Grant Accessibility to Jarvis in System Settings → Privacy & Security → Accessibility. "+warning,True)
    return text_result(f"{name} completed on {target}.")

def handle(m):
    method=m.get("method"); rid=m.get("id")
    if method=="notifications/initialized": return None
    if method=="initialize": return {"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"jarvis-mac","version":"0.1.0"}}}
    if method=="ping": return {"jsonrpc":"2.0","id":rid,"result":{}}
    if method=="tools/list": return {"jsonrpc":"2.0","id":rid,"result":{"tools":tool_list()}}
    if method=="tools/call":
        p=m.get("params",{})
        try: return {"jsonrpc":"2.0","id":rid,"result":call_tool(str(p.get("name","")),p.get("arguments") or {})}
        except Exception as exc: return {"jsonrpc":"2.0","id":rid,"result":text_result(f"Mac tool failed: {type(exc).__name__}: {exc}",True)}
    if rid is not None: return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Method not found: {method}"}}
    return None

def main():
    for line in os.sys.stdin:
        try:
            response=handle(json.loads(line))
            if response is not None:
                os.sys.stdout.write(json.dumps(response,separators=(",",":"))+"\n"); os.sys.stdout.flush()
        except Exception as exc:
            os.sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":None,"error":{"code":-32603,"message":type(exc).__name__}})+"\n"); os.sys.stdout.flush()

if __name__=="__main__": main()
