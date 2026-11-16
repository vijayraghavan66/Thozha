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
  poll       --relay URL                                         process companion messages once
  revoke     --relay URL --device-id ID --approve-local          revoke ONE companion device

certify refuses to run without --approve-local: the operator must choose each device explicitly.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis_link_host.client import RelayClient  # noqa: E402
from jarvis_link_host.crypto import DeviceCertificate, DeviceIdentity, device_id_for_signing_key, issue_device_certificate  # noqa: E402
from jarvis_link_host.host import JarvisLinkHost  # noqa: E402
from jarvis_link_host.runtime import HostRelayRuntime  # noqa: E402
from jarvis_link_host.supervisor import MockSupervisor  # noqa: E402

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


def _host(state: dict, identity: DeviceIdentity) -> JarvisLinkHost:
    return JarvisLinkHost(
        tenant_id=state["tenant_id"], device_id=state["host_device_id"], identity=identity,
        host_certificate=DeviceCertificate.from_dict(state["host_certificate"]), supervisor=MockSupervisor(),
    )


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
    cert = _host(state, identity).certify_companion(
        signing_public_b64u=matches[0]["signing_public_key_b64url"],
        encryption_public_b64u=matches[0]["encryption_public_key_b64url"],
    )
    return {"device_id": args.device_id, "status": client.certify_device(cert.to_dict())["status"]}


def cmd_poll(args, path: Path) -> dict:
    state, client, identity = _relay(args, path)
    return HostRelayRuntime(host=_host(state, identity), relay=client).poll_once()


def cmd_revoke(args, path: Path) -> dict:
    if not args.approve_local:
        raise SystemExit("refusing to revoke without --approve-local (explicit operator approval)")
    _, client, _ = _relay(args, path)
    return client.revoke_device(args.device_id, reason_code="owner_revoked")


COMMANDS = {"bootstrap": cmd_bootstrap, "root": cmd_root, "invite": cmd_invite,
            "pending": cmd_pending, "certify": cmd_certify, "poll": cmd_poll, "revoke": cmd_revoke}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--relay", default="http://127.0.0.1:8787")
    parser.add_argument("--tenant")
    parser.add_argument("--bootstrap-secret")
    parser.add_argument("--device-id")
    parser.add_argument("--approve-local", action="store_true")
    parser.add_argument("--expires", type=int, default=600)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    print(json.dumps(COMMANDS[args.command](args, args.state), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
