#!/usr/bin/env python3
"""Drive the DEBUG Jarvis Link harness on an authorized development phone and print structured results.

Each command sends one debug broadcast to com.jarvis.companion.debug and reads the JSON result from logcat
(tag JarvisLinkDebug). Arguments are passed as an argv list, not through a shell, so values are not mangled.
Invitation and root values are read from ~/.jarvis-link-test (written by the Mac operator tool); nothing
secret is written into this repository.

  python3 scripts/vivo_link_drive.py --serial SERIAL selftest|status|enroll|sync|submit|poll|flush|forget
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

PACKAGE = "com.jarvis.companion.debug"
RECEIVER = f"{PACKAGE}/com.jarvis.companion.link.LinkDebugReceiver"
TEST_DIR = Path.home() / ".jarvis-link-test"
ACTIONS = {
    "selftest": "SELFTEST", "status": "STATUS", "enroll": "ENROLL", "sync": "SYNC",
    "submit": "SUBMIT", "poll": "POLL", "flush": "FLUSH", "forget": "FORGET",
    "approvals": "APPROVALS", "approve": "APPROVE", "reject": "REJECT",
}


def adb(serial: str, *args: str) -> str:
    return subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=True, check=True).stdout


def broadcast(serial: str, action: str, extras: list[str]) -> None:
    out = adb(serial, "shell", "am", "broadcast", "-a", f"com.jarvis.link.debug.{action}", "-n", RECEIVER, *extras)
    if "result=0" not in out:
        raise SystemExit(f"broadcast {action} not delivered: {out.strip()}")


def read_result(serial: str, action: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = adb(serial, "logcat", "-d", "-s", "JarvisLinkDebug:I").splitlines()
        for line in reversed(lines):
            if "JarvisLinkDebug:" not in line:
                continue
            payload = json.loads(line.split("JarvisLinkDebug:", 1)[1].strip())
            if payload.get("action") == action:
                return payload
        time.sleep(1)
    raise SystemExit(f"no result for {action} within {timeout}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(ACTIONS))
    parser.add_argument("--serial", required=True)
    parser.add_argument("--relay", default="http://127.0.0.1:8787")
    parser.add_argument("--objective", default="Harmless status check")
    parser.add_argument("--scope", default="assistant", choices=["read_only", "assistant"],
                        help="submit: capability scope (assistant is required for Mac control)")
    parser.add_argument("--ttl-ms")
    parser.add_argument("--approval-id")
    args = parser.parse_args()
    action = ACTIONS[args.command]
    extras = ["--es", "relay", args.relay]
    if args.command == "enroll":
        invitation = json.loads((TEST_DIR / "invitation.json").read_text())
        root = json.loads((TEST_DIR / "root.json").read_text())
        extras += [
            "--es", "tenant", invitation["tenant_id"],
            "--es", "invitation_id", invitation["invitation_id"],
            "--es", "invitation_secret", invitation["invitation_secret"],
            "--el", "invitation_expires_ms", str(invitation["invitation_expires_ms"]),
            "--es", "name", "vivo-v2514-debug",
            "--es", "root_device_id", root["root_device_id"],
            "--es", "root_public_key", root["root_public_key"],
        ]
    if args.command == "submit":
        # base64url (no padding) keeps spaces intact through adb argument parsing; the receiver decodes objective_b64.
        import base64
        encoded = base64.urlsafe_b64encode(args.objective.encode("utf-8")).decode("ascii").rstrip("=")
        extras += ["--es", "objective_b64", encoded, "--es", "scope", args.scope]
        if args.ttl_ms:
            extras += ["--el", "ttl_ms", args.ttl_ms]
    if args.command in {"approve", "reject"}:
        extras += ["--es", "approval_id", args.approval_id]
    adb(args.serial, "logcat", "-c")
    broadcast(args.serial, action, extras)
    print(json.dumps(read_result(args.serial, action), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
