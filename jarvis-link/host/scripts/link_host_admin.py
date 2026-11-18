#!/usr/bin/env python3
"""Mac-side operator tool for a Jarvis Link TEST relay (local development only).

It runs the real Python host (JarvisLinkHost + HostRelayRuntime) against a relay. The host keypair is
written to a state file OUTSIDE this repository (default ~/.jarvis-link-test/host.json, mode 600).
That file holds private keys for a test tenant; it must never be committed or shared.

Commands:
  bootstrap  --relay URL --tenant ID --bootstrap-secret SECRET   create the tenant host root (one time)
  root                                                           print the pinned host root values
  invite     --relay URL                                         print a one-use companion invitation
  pending    --relay URL                                         list companions awaiting certification
  certify    --relay URL --device-id ID --approve-local          sign a certificate for ONE device
  revoke     --relay URL --device-id ID --approve-local          revoke ONE companion device
  revoke     --relay URL --device-id ID --approve-local          revoke ONE companion device
  request-approval --device-id ID [--message M]                queue a harmless approval; serve sends it
  serve      --relay URL [--interval S]                          supervisor loop: create, receive, resume, publish

certify refuses to run without --approve-local: the operator must choose each device explicitly.
"""
from __future__ import annotations

import argparse
import asyncio
import time
import base64
import json
import os
import stat
import sys
import threading
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis_link_host.client import RelayClient  # noqa: E402
from jarvis_link_host.crypto import DeviceCertificate, DeviceIdentity, device_id_for_signing_key, issue_device_certificate  # noqa: E402
from jarvis_link_host.host import JarvisLinkHost  # noqa: E402
from jarvis_link_host.runtime import HostRelayRuntime  # noqa: E402
from jarvis_link_host.openjarvis_adapter import OpenJarvisSupervisorAdapter  # noqa: E402
from jarvis_link_host.response_outbox import HostResponseOutbox  # noqa: E402
from jarvis_link_host.simulated_operations import SIMULATED_OPERATION, SIMULATED_TARGET, resume_approved, simulated_arguments  # noqa: E402

OPENJARVIS_SRC = Path(os.environ.get("OPENJARVIS_SRC", Path.home() / ".openjarvis" / "src" / "src"))
TEST_DIR = Path(os.environ.get("JARVIS_LINK_TEST_DIR", Path.home() / ".jarvis-link-test"))

DEFAULT_STATE = Path(os.environ.get("JARVIS_LINK_TEST_STATE", Path.home() / ".jarvis-link-test" / "host.json"))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True))
    tmp.replace(path)


def _identity(state: dict) -> DeviceIdentity:
    sign = serialization.load_der_private_key(_unb64(state["signing_private_pkcs8"]), password=None)
    enc = serialization.load_der_private_key(_unb64(state["encryption_private_pkcs8"]), password=None)
    return DeviceIdentity(sign, enc)


def _open_supervisor():
    """The real OpenJarvis SupervisorStore, imported read-only from its checkout, behind the narrow adapter."""
    sys.path.insert(0, str(OPENJARVIS_SRC))
    from openjarvis.tasks.supervisor import SupervisorStore
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    store = SupervisorStore(db_path=str(TEST_DIR / "supervisor.db"))
    return store, OpenJarvisSupervisorAdapter(store, TEST_DIR / "link-adapter.db")


def _host(state: dict, identity: DeviceIdentity, supervisor) -> JarvisLinkHost:
    host = JarvisLinkHost(
        tenant_id=state["tenant_id"], device_id=state["host_device_id"], identity=identity,
        host_certificate=DeviceCertificate.from_dict(state["host_certificate"]), supervisor=supervisor,
    )
    host.task_observer = _record_task_watch
    host.task_authorizer = _task_owned_by
    return host


TASK_WATCH_PATH = TEST_DIR / "task-watch.json"
TERMINAL_TASK_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
_WATCH_LOCK = threading.RLock()


def _record_task_watch(*, task_id: str, idempotency_key: str, companion_id: str) -> None:
    """Remember which companion submitted a task, so its final state can be published back to that companion."""
    with _WATCH_LOCK:
        _record_task_watch_locked(task_id=task_id, idempotency_key=idempotency_key, companion_id=companion_id)


def _record_task_watch_locked(*, task_id: str, idempotency_key: str, companion_id: str) -> None:
    entries = json.loads(TASK_WATCH_PATH.read_text()) if TASK_WATCH_PATH.is_file() else []
    for entry in entries:
        if entry["task_id"] == task_id:
            if entry["companion_id"] != companion_id or entry["idempotency_key"] != idempotency_key:
                raise ValueError("task ownership conflict")
            return
    entries.append({"task_id": task_id, "idempotency_key": idempotency_key, "companion_id": companion_id, "published": False})
    _save(TASK_WATCH_PATH, entries)


def _task_owned_by(*, task_id: str, companion_id: str) -> bool:
    with _WATCH_LOCK:
        try:
            entries = json.loads(TASK_WATCH_PATH.read_text())
            matches = [entry for entry in entries if entry.get("task_id") == task_id]
            return len(matches) == 1 and matches[0].get("companion_id") == companion_id
        except (OSError, ValueError, TypeError, AttributeError):
            return False


def _publish_task_updates(state, client, identity, adapter, runtime) -> int:
    """Announce new pending approvals of watched tasks, and publish each terminal state, to the submitting companion."""
    with _WATCH_LOCK:
        return _publish_task_updates_locked(state, client, identity, adapter, runtime)


def _publish_task_updates_locked(state, client, identity, adapter, runtime) -> int:
    if not TASK_WATCH_PATH.is_file() or not hasattr(adapter, "task_status"):
        return 0
    entries = json.loads(TASK_WATCH_PATH.read_text())
    published = 0
    for entry in entries:
        if entry["published"]:
            continue
        current = adapter.task_status(entry["idempotency_key"])
        if current["state"] not in TERMINAL_TASK_STATES:
            if current["state"] == "PAUSED":
                entry["paused_reported"] = True
            else:
                entry.pop("paused_reported", None)
            _save(TASK_WATCH_PATH, entries)
            if hasattr(adapter, "task_events") and hasattr(runtime, "send_task_progress"):
                cursor = int(entry.get("progress_seq", 0))
                page = adapter.task_events(entry["idempotency_key"], after_seq=cursor, limit=100)
                cert = _companion_certificate(client, entry["companion_id"])
                for event in page["events"]:
                    seq = event["seq"]
                    label = _progress_label(event)
                    if label is not None:
                        progress_state, summary = label
                        runtime.send_task_progress(
                            companion_certificate=cert,
                            companion_id=entry["companion_id"],
                            task_id=entry["task_id"],
                            event_seq=seq,
                            state=progress_state,
                            summary=summary,
                        )
                        published += 1
                    entry["progress_seq"] = seq
                    # Persist the cursor immediately after accepted relay delivery
                    # (or after an intentionally filtered event) for restart safety.
                    _save(TASK_WATCH_PATH, entries)
            # Active task: announce each pending approval exactly once, with its exact operation and expiry.
            announced = set(entry.get("announced", []))
            for approval in adapter.list_pending_approvals(entry["task_id"]):
                if approval["approval_id"] in announced:
                    continue
                runtime.send_approval_request(
                    companion_certificate=_companion_certificate(client, entry["companion_id"]),
                    companion_id=entry["companion_id"], request=approval,
                )
                announced.add(approval["approval_id"])
                published += 1
            entry["announced"] = sorted(announced)
            continue
        runtime.send_task_update(
            companion_certificate=_companion_certificate(client, entry["companion_id"]),
            companion_id=entry["companion_id"], task_id=entry["task_id"], state=current["state"],
            summary=current.get("summary") or "",
            result_text=current.get("result_text"),
        )
        entry["published"] = True
        published += 1
    _save(TASK_WATCH_PATH, entries)
    return published


def _progress_label(event: dict) -> tuple[str, str] | None:
    """Translate safe supervisor events to user-facing states, never model reasoning."""
    event_type = event.get("event_type")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event_type == "link_task.queued":
        return "QUEUED", "Task queued"
    if event_type == "link_task.started":
        return "RUNNING", "AI execution started"
    if event_type == "link_task.waiting_approval":
        return "WAITING_APPROVAL", "Waiting for your approval"
    if event_type == "task.recovered_paused":
        return "PAUSED", "Paused safely after Mac service restart"
    if event_type == "task.cancelled":
        return "CANCELLED", "Task cancelled"
    if event_type == "tool.mcp_approval_dispatch_claimed":
        return "RUNNING", "Approved Mac action is executing"
    if event_type == "approval.consumed":
        return "RUNNING", "Approved action started"
    if event_type == "approval.execution_completed":
        return "RUNNING", "Approved action completed; continuing task"
    if event_type == "approval.mac_mcp_dispatched":
        if payload.get("success") is True:
            return "RUNNING", "Approved Mac action completed; continuing task"
        if payload.get("success") is False:
            return "FAILED", "Approved Mac action failed"
    if event_type == "task.state":
        target = payload.get("to")
        labels = {
            "PLANNING": ("PLANNING", "Planning task"),
            "RUNNING": ("RUNNING", "Working on your request"),
            "VERIFYING": ("VERIFYING", "Checking the result"),
            "WAITING_APPROVAL": ("WAITING_APPROVAL", "Waiting for your approval"),
            "PAUSED": ("PAUSED", "Task paused safely"),
            "CANCELLED": ("CANCELLED", "Task cancelled"),
        }
        return labels.get(target)
    return None


def _has_open_task_watch(*, include_paused: bool = False) -> bool:
    if not TASK_WATCH_PATH.is_file():
        return False
    try:
        rows = json.loads(TASK_WATCH_PATH.read_text())
        return any(
            isinstance(row, dict)
            and not row.get("published", False)
            and (include_paused or not row.get("paused_reported", False))
            for row in rows
        )
    except (OSError, ValueError, TypeError):
        return False


def _load(path: Path) -> tuple[dict, DeviceIdentity]:
    if not path.is_file():
        raise SystemExit(f"no host state at {path}; run bootstrap first")
    state = json.loads(path.read_text())
    return state, _identity(state)


def _relay(args, path: Path) -> tuple[dict, RelayClient, DeviceIdentity]:
    state, identity = _load(path)
    client = RelayClient(base_url=args.relay, tenant_id=state["tenant_id"], device_id=state["host_device_id"], identity=identity)
    return state, client, identity


def cmd_bootstrap(args, path: Path) -> dict:
    if path.exists():
        raise SystemExit("host state already exists; refusing to overwrite a tenant root")
    identity = DeviceIdentity.generate()
    host_id = device_id_for_signing_key(identity.signing_public_der)
    cert = issue_device_certificate(
        host=identity, tenant_id=args.tenant, host_device_id=host_id,
        signing_public_b64u=identity.signing_public_b64u, encryption_public_b64u=identity.encryption_public_b64u,
        device_role="host",
    )
    client = RelayClient(base_url=args.relay, tenant_id=args.tenant, device_id=host_id, identity=identity)
    client.bootstrap_host(bootstrap_secret=args.bootstrap_secret, device_name="Test Mac host", certificate=cert.to_dict())
    pkcs8 = dict(format=serialization.PrivateFormat.PKCS8, encoding=serialization.Encoding.DER,
                 encryption_algorithm=serialization.NoEncryption())
    _save(path, {
        "tenant_id": args.tenant, "host_device_id": host_id,
        "signing_private_pkcs8": _b64(identity.signing_private.private_bytes(**pkcs8)),
        "encryption_private_pkcs8": _b64(identity.encryption_private.private_bytes(**pkcs8)),
        "host_certificate": cert.to_dict(),
    })
    return {"tenant_id": args.tenant, "host_device_id": host_id, "state_file": str(path)}


def cmd_root(_args, path: Path) -> dict:
    state, identity = _load(path)
    return {"tenant_id": state["tenant_id"], "root_device_id": state["host_device_id"], "root_public_key": identity.signing_public_b64u}


def cmd_invite(args, path: Path) -> dict:
    state, client, _ = _relay(args, path)
    inv = client.create_enrollment_invitation(device_role="companion", expires_in_seconds=args.expires)
    return {"tenant_id": state["tenant_id"], "invitation_id": inv["invitation_id"],
            "invitation_secret": inv["invitation_secret_b64url"], "invitation_expires_ms": inv["expires_at_unix_ms"]}


def cmd_pending(args, path: Path) -> dict:
    _, client, _ = _relay(args, path)
    return {"enrollments": [{k: p[k] for k in ("device_id", "device_name", "device_role", "expires_at_unix_ms")}
                            for p in client.pending_enrollments()]}


def cmd_certify(args, path: Path) -> dict:
    if not args.approve_local:
        raise SystemExit("refusing to certify without --approve-local (explicit operator approval)")
    state, client, identity = _relay(args, path)
    matches = [p for p in client.pending_enrollments() if p["device_id"] == args.device_id]
    if len(matches) != 1:
        raise SystemExit("no single pending enrollment with that device ID")
    store, adapter = _open_supervisor()
    cert = _host(state, identity, adapter).certify_companion(
        signing_public_b64u=matches[0]["signing_public_key_b64url"],
        encryption_public_b64u=matches[0]["encryption_public_key_b64url"],
    )
    return {"device_id": args.device_id, "status": client.certify_device(cert.to_dict())["status"]}


def _companion_certificate(client: RelayClient, device_id: str) -> dict:
    return client.signed_request("GET", f"/v1/devices/{device_id}/certificate").json()["certificate"]


def _pending_records() -> list[dict]:
    path = TEST_DIR / "approvals.json"
    return json.loads(path.read_text()) if path.is_file() else []


def _save_records(records: list[dict]) -> None:
    _save(TEST_DIR / "approvals.json", records)


def cmd_request_approval(args, path: Path) -> dict:
    """Queues a harmless approval request. The running `serve` loop creates it in the supervisor and sends it.
    Opening the supervisor store runs its restart recovery, which pauses in-flight tasks, so one-off commands
    never open it."""
    queue = TEST_DIR / "requests"
    item = {"id": uuid.uuid4().hex, "device_id": args.device_id, "message": args.message, "approval_ttl": args.approval_ttl}
    _save(queue / f"{item['id']}.json", item)
    return {"queued": item["id"], "note": "serve creates and sends it on its next cycle"}


def _create_and_send(state, client, identity, store, adapter, item: dict) -> dict:
    message = item["message"]
    submission_key = uuid.uuid4().hex
    task_id = adapter.submit_task(objective=f"Simulated echo: {message}", capability_scope=["read_only"],
                                  idempotency_key=submission_key)["task_id"]
    _record_task_watch(task_id=task_id, idempotency_key=submission_key, companion_id=item["device_id"])
    approval_id, digest = store.create_approval(
        task_id, SIMULATED_OPERATION, SIMULATED_TARGET, simulated_arguments(message),
        "Harmless simulated echo for the Jarvis Link approval test. No file or network effect.",
        ttl_seconds=item["approval_ttl"],
    )
    record = adapter.get_approval(approval_id)
    runtime = HostRelayRuntime(host=_host(state, identity, adapter), relay=client)
    runtime.send_approval_request(
        companion_certificate=_companion_certificate(client, item["device_id"]), companion_id=item["device_id"],
        request={"approval_id": approval_id, "task_id": task_id, "operation": SIMULATED_OPERATION,
                 "target": SIMULATED_TARGET, "canonical_arguments_digest": digest,
                 "expires_at_unix_ms": record.expires_at_unix_ms,
                 "redacted_summary": f"Simulated echo of '{message}'. Harmless: no file or network effect."},
    )
    records = _pending_records()
    records.append({"approval_id": approval_id, "task_id": task_id, "message": message,
                    "companion_id": item["device_id"], "digest": digest, "done": False})
    _save_records(records)
    return {"approval_id": approval_id, "task_id": task_id, "expires_at_unix_ms": record.expires_at_unix_ms}


def _process_requests(state, client, identity, store, adapter) -> list[dict]:
    queue = TEST_DIR / "requests"
    # The simulated-echo approval harness needs the local store. The API engine path never uses it.
    if store is None or not queue.is_dir():
        return []
    created = []
    for item_path in sorted(queue.glob("*.json")):
        item = json.loads(item_path.read_text())
        created.append(_create_and_send(state, client, identity, store, adapter, item))
        item_path.unlink()
    return created


def _cycle(state, client, identity, store, adapter, response_outbox=None, dispatcher=None) -> dict:
    """One supervisor cycle: create queued requests, receive relay messages, resume approved tasks, publish results."""
    created = _process_requests(state, client, identity, store, adapter)
    runtime = dispatcher.runtime if dispatcher else HostRelayRuntime(host=_host(state, identity, adapter), relay=client, response_outbox=response_outbox)
    counts = dispatcher.poll_once() if dispatcher else runtime.poll_once()
    published = _publish_task_updates(state, client, identity, adapter, runtime)
    report = []
    for record in (_pending_records() if store is not None else []):
        if record["done"]:
            continue
        status = store.approval(record["approval_id"])["status"]
        outcome = status
        if status == "APPROVED":
            outcome_info = resume_approved(store, task_id=record["task_id"], approval_id=record["approval_id"], message=record["message"])
            outcome = outcome_info["outcome"]
            if outcome == "COMPLETED":
                runtime.send_task_update(
                    companion_certificate=_companion_certificate(client, record["companion_id"]),
                    companion_id=record["companion_id"], task_id=record["task_id"], state="COMPLETED",
                    summary=f"Simulated echo complete: {outcome_info['result']}")
            record["done"] = True
        elif status in {"REJECTED", "EXPIRED", "CONSUMED"}:
            record["done"] = True
        report.append({"approval_id": record["approval_id"], "approval_status": status, "outcome": outcome,
                       "task_state": store.get_task(record["task_id"])["state"]})
    _save_records(_merge_done(report))
    return {**counts, "created": created, "resumed": report, "published": published}


def _merge_done(report: list[dict]) -> list[dict]:
    done_ids = {r["approval_id"] for r in report if r["approval_status"] in {"APPROVED", "REJECTED", "EXPIRED", "CONSUMED"}}
    return [dict(r, done=True) if r["approval_id"] in done_ids else r for r in _pending_records()]


def cmd_serve(args, path: Path) -> dict:
    """Mac supervisor loop. Opens the supervisor store once (its restart recovery runs once, at start).
    Outbound connections only. Wakes on relay stream hints and on a slow fallback timer. Stop with Ctrl-C."""
    asyncio.run(_serve(args, path))
    return {"stopped": True}


async def _serve(args, path: Path) -> None:
    import websockets

    state, client, identity = _relay(args, path)
    host_id = state["host_device_id"]
    if args.engine == "api":
        # Real engine path. The OpenJarvis server owns the supervisor database; this process never opens it,
        # so its restart recovery cannot pause tasks that the running server is executing.
        from jarvis_link_host.openjarvis_api_adapter import OpenJarvisApiSupervisorAdapter
        store, adapter = None, OpenJarvisApiSupervisorAdapter(
            submission_ledger=TEST_DIR / "submission-ledger.json" if args.concurrent_dispatch else None,
            strict_recovery=args.concurrent_dispatch,
            async_submission=args.concurrent_dispatch,
        )
    else:
        store, adapter = _open_supervisor()
    response_outbox = HostResponseOutbox(TEST_DIR / "response-outbox.db")
    dispatcher = None
    if args.concurrent_dispatch:
        if args.engine != "api":
            raise ValueError("concurrent dispatch requires the API supervisor")
        from jarvis_link_host.concurrent_dispatch import ConcurrentRelayDispatch
        runtime = HostRelayRuntime(host=_host(state, identity, adapter), relay=client, response_outbox=response_outbox)
        dispatcher = ConcurrentRelayDispatch(runtime)
    ws_url = args.relay.replace("http", "ws", 1) + f"/v1/devices/{host_id}/stream"
    backoff = 2.0
    try:
        while True:
            try:
                cycle = await asyncio.to_thread(_cycle, state, client, identity, store, adapter, response_outbox, dispatcher)
                print(json.dumps({"event": "cycle", "at": time.strftime("%H:%M:%S"), **cycle}), flush=True)
            except Exception as failure:  # keep the loop alive; report only the exception class
                print(json.dumps({"event": "cycle_error", "error": type(failure).__name__}), flush=True)
            ts, nonce = str(int(time.time() * 1000)), _b64(os.urandom(32))
            signature = identity.sign_request(tenant_id=state["tenant_id"], device_id=host_id, timestamp_ms=int(ts),
                                              nonce_b64url=nonce, method="GET",
                                              path_and_query=f"/v1/devices/{host_id}/stream", body=b"")
            headers = {"X-Jarvis-Link-Tenant": state["tenant_id"], "X-Jarvis-Link-Device": host_id,
                       "X-Jarvis-Link-Timestamp": ts, "X-Jarvis-Link-Nonce": nonce, "X-Jarvis-Link-Signature": signature}
            try:
                async with websockets.connect(ws_url, additional_headers=headers) as ws:
                    backoff = 2.0
                    try:
                        if _has_open_task_watch():
                            active_interval = min(args.interval, args.task_watch_interval)
                        elif _has_open_task_watch(include_paused=True):
                            active_interval = min(args.interval, args.paused_watch_interval)
                        else:
                            active_interval = args.interval
                        await asyncio.wait_for(ws.recv(), timeout=active_interval)
                    except asyncio.TimeoutError:
                        pass
            except Exception:  # relay down or restarting: back off, never spin
                print(json.dumps({"event": "relay_unavailable", "at": time.strftime("%H:%M:%S"), "retry_s": backoff}), flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    finally:
        if dispatcher is not None:
            await asyncio.to_thread(dispatcher.close)
        if store is not None:
            store.close()
        response_outbox.close()


def cmd_revoke(args, path: Path) -> dict:
    if not args.approve_local:
        raise SystemExit("refusing to revoke without --approve-local (explicit operator approval)")
    _, client, _ = _relay(args, path)
    return client.revoke_device(args.device_id, reason_code="owner_revoked")


COMMANDS = {"serve": cmd_serve, "bootstrap": cmd_bootstrap, "root": cmd_root, "invite": cmd_invite,
            "pending": cmd_pending, "certify": cmd_certify, "revoke": cmd_revoke,
            "request-approval": cmd_request_approval}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--relay", default="http://127.0.0.1:8787")
    parser.add_argument("--tenant")
    parser.add_argument("--bootstrap-secret")
    parser.add_argument("--device-id")
    parser.add_argument("--approve-local", action="store_true")
    parser.add_argument("--expires", type=int, default=600)
    parser.add_argument("--message", default="JARVIS_SIMULATED_APPROVAL_1")
    parser.add_argument("--approval-ttl", type=int, default=300)
    parser.add_argument("--interval", type=float, default=60.0, help="serve: fallback cycle interval (s)")
    parser.add_argument("--task-watch-interval", type=float, default=2.0,
                        help="serve: poll active supervisor task events at this bounded interval (s)")
    parser.add_argument("--paused-watch-interval", type=float, default=15.0,
                        help="serve: poll tasks paused after recovery less often until explicit resume (s)")
    parser.add_argument("--concurrent-dispatch", action="store_true", help="experimental bounded relay dispatch; relay retains unacknowledged requests")
    parser.add_argument("--engine", choices=["api", "store"], default="api",
                        help="serve: 'api' dispatches to the running OpenJarvis server (real AI engine); "
                             "'store' is the legacy local-store path that records tasks without executing them")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    print(json.dumps(COMMANDS[args.command](args, args.state), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
