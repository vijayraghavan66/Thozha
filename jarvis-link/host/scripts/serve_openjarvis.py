#!/usr/bin/env python3
"""Outbound-only Jarvis Link host that forwards tasks to loopback OpenJarvis.

The relay URL must be an HTTPS Worker origin. This command does not deploy a
Worker, modify networking, or open an inbound socket. The OpenJarvis API key is
read locally and sent only to 127.0.0.1:8000.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis_link_host.client import RelayClient  # noqa: E402
from jarvis_link_host.crypto import DeviceIdentity, device_id_for_signing_key  # noqa: E402
from jarvis_link_host.host import JarvisLinkHost  # noqa: E402
from jarvis_link_host.openjarvis_api_adapter import OpenJarvisApiSupervisorAdapter  # noqa: E402
from jarvis_link_host.response_outbox import HostResponseOutbox  # noqa: E402
from jarvis_link_host.runtime import HostRelayRuntime  # noqa: E402
from jarvis_link_host.crypto import DeviceCertificate  # noqa: E402


DEFAULT_STATE = Path(os.environ.get("JARVIS_LINK_HOST_STATE", Path.home() / ".jarvis-link-host" / "host.json"))
DEFAULT_REPLY_DB = Path(os.environ.get("JARVIS_LINK_HOST_REPLY_DB", Path.home() / ".jarvis-link-host" / "responses.db"))
DEFAULT_KEY_FILE = Path(os.environ.get("JARVIS_SUPERVISOR_API_KEY_FILE", Path.home() / ".openjarvis" / "supervisor_api_key"))


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_state(path: Path) -> dict:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Link host identity file is missing or invalid")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("Link host identity file must be owner-only")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        signing = serialization.load_der_private_key(_unb64(state["signing_private_pkcs8"]), password=None)
        encryption = serialization.load_der_private_key(_unb64(state["encryption_private_pkcs8"]), password=None)
    except Exception as exc:
        raise RuntimeError("Link host identity file is unreadable") from exc
    if not isinstance(state, dict) or not isinstance(state.get("tenant_id"), str):
        raise RuntimeError("Link host identity file is malformed")
    identity = DeviceIdentity(signing, encryption)
    if state.get("host_device_id") != device_id_for_signing_key(identity.signing_public_der):
        raise RuntimeError("Link host private key does not match its stored identity")
    state["_identity"] = identity
    return state


async def _serve(args: argparse.Namespace) -> None:
    import websockets

    state = _load_state(args.state)
    identity = state["_identity"]
    relay = RelayClient(
        base_url=args.relay,
        tenant_id=state["tenant_id"],
        device_id=state["host_device_id"],
        identity=identity,
    )
    # Digests of submitted idempotency keys, so a replayed submit can be recognised and its task returned.
    supervisor = OpenJarvisApiSupervisorAdapter(
        key_file=args.supervisor_key_file,
        submission_ledger=Path.home() / ".jarvis-link-test" / "submissions.json",
    )
    outbox = HostResponseOutbox(args.response_db)
    host = JarvisLinkHost(
        tenant_id=state["tenant_id"],
        device_id=state["host_device_id"],
        identity=identity,
        host_certificate=DeviceCertificate.from_dict(state["host_certificate"]),
        supervisor=supervisor,
    )
    runtime = HostRelayRuntime(host=host, relay=relay, response_outbox=outbox)
    host_id = state["host_device_id"]
    ws_url = relay.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + f"/v1/devices/{host_id}/stream"
    backoff = 2.0
    try:
        while True:
            try:
                cycle = await asyncio.to_thread(runtime.poll_once)
                print(json.dumps({"event": "cycle", "at": time.strftime("%H:%M:%S"), **cycle}), flush=True)
            except Exception as failure:
                print(json.dumps({"event": "cycle_error", "error": type(failure).__name__}), flush=True)
            timestamp = str(int(time.time() * 1000))
            nonce = _b64(secrets.token_bytes(32))
            path = f"/v1/devices/{host_id}/stream"
            signature = identity.sign_request(
                tenant_id=state["tenant_id"], device_id=host_id,
                timestamp_ms=int(timestamp), nonce_b64url=nonce,
                method="GET", path_and_query=path, body=b"",
            )
            headers = {
                "X-Jarvis-Link-Tenant": state["tenant_id"],
                "X-Jarvis-Link-Device": host_id,
                "X-Jarvis-Link-Timestamp": timestamp,
                "X-Jarvis-Link-Nonce": nonce,
                "X-Jarvis-Link-Signature": signature,
            }
            try:
                async with websockets.connect(ws_url, additional_headers=headers) as websocket:
                    backoff = 2.0
                    try:
                        await asyncio.wait_for(websocket.recv(), timeout=args.interval)
                    except asyncio.TimeoutError:
                        pass
            except Exception:
                print(json.dumps({"event": "relay_unavailable", "retry_s": backoff}), flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 60.0)
    finally:
        relay.close()
        supervisor.close()
        outbox.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay", required=True, help="HTTPS relay origin; no deploy is performed")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--supervisor-key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--response-db", type=Path, default=DEFAULT_REPLY_DB)
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args()
    if args.interval < 5 or args.interval > 600:
        parser.error("--interval must be between 5 and 600 seconds")
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # no key, request, envelope, or response body in logs
        print(f"Jarvis Link host stopped ({type(exc).__name__}).", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - process loop is operator-started
    raise SystemExit(main())
