"""Integration: Python host + simulated companion against the relay running in real workerd.

Starts `wrangler dev --local` on a loopback port with a throwaway persistence dir. Nothing is
deployed, no Cloudflare account is used, and no production key or Mac/Android credential is touched.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sim_companion import SimCompanion  # noqa: E402

from jarvis_link_host.client import RelayClient, RelayClientError  # noqa: E402
from jarvis_link_host.crypto import (  # noqa: E402
    DeviceIdentity, b64u, device_id_for_signing_key, issue_device_certificate,
)
from jarvis_link_host.host import JarvisLinkHost  # noqa: E402
from jarvis_link_host.runtime import HostRelayRuntime  # noqa: E402
from jarvis_link_host.supervisor import MockSupervisor  # noqa: E402

RELAY_DIR = Path(__file__).resolve().parents[2] / "relay"
BOOTSTRAP_SECRET = "integration-only-bootstrap-secret"
SAFE_TASK = {"objective": "Summarise today's calendar", "capability_scope": ["read_only"]}

pytestmark = pytest.mark.skipif(
    shutil.which("npx") is None or not (RELAY_DIR / "node_modules" / ".bin" / "wrangler").exists(),
    reason="relay dependencies (npm ci in jarvis-link/relay) are not installed",
)


@pytest.fixture(scope="module")
def relay_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    state = tempfile.mkdtemp(prefix="jarvis-link-wr-")
    log = open(os.path.join(state, "wrangler.log"), "wb")
    proc = subprocess.Popen(
        ["npx", "wrangler", "dev", "--local", "--ip", "127.0.0.1", "--port", str(port),
         "--persist-to", os.path.join(state, "persist"),
         "--var", f"JARVIS_LINK_BOOTSTRAP_SECRET:{BOOTSTRAP_SECRET}"],
        cwd=RELAY_DIR, stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "WRANGLER_SEND_METRICS": "false", "CI": "1"}, start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                if httpx.get(url + "/health", timeout=2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            raise RuntimeError("wrangler dev did not become healthy: " + Path(log.name).read_text()[-2000:])
        yield url
    finally:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        proc.wait(timeout=20)
        log.close()
        shutil.rmtree(state, ignore_errors=True)


class World:
    """One isolated tenant: bootstrapped host, certified companion, mock supervisor."""

    def __init__(self, url: str, *, sim_companion: bool = True) -> None:
        self.url = url
        self.tenant = "it-" + uuid.uuid4().hex
        self.identity = DeviceIdentity.generate()
        self.host_id = device_id_for_signing_key(self.identity.signing_public_der)
        cert = issue_device_certificate(
            host=self.identity, tenant_id=self.tenant, host_device_id=self.host_id,
            signing_public_b64u=self.identity.signing_public_b64u,
            encryption_public_b64u=self.identity.encryption_public_b64u, device_role="host",
        )
        self.relay = RelayClient(base_url=url, tenant_id=self.tenant, device_id=self.host_id, identity=self.identity)
        self.relay.bootstrap_host(bootstrap_secret=BOOTSTRAP_SECRET, device_name="Simulated Mac", certificate=cert.to_dict())
        self.supervisor = MockSupervisor()
        self.host = JarvisLinkHost(tenant_id=self.tenant, device_id=self.host_id, identity=self.identity,
                                   host_certificate=cert, supervisor=self.supervisor)
        self.host.task_authorizer = lambda **_: True  # single-companion simulation
        self.runtime = HostRelayRuntime(host=self.host, relay=self.relay)
        self.companion = self.enroll_companion() if sim_companion else None
        # The host reads its own certificate (it is active in its tenant), so no companion is required.
        self.host_cert = self.relay._json(
            self.relay.signed_request("GET", f"/v1/devices/{self.host_id}/certificate"))["certificate"]

    def enroll_companion(self) -> SimCompanion:
        comp = SimCompanion(self.tenant, self.url, self.identity.signing_public_der, self.host_id)
        invitation = self.relay.create_enrollment_invitation()
        assert comp.enroll(invitation)["status"] == "PENDING_HOST_CERTIFICATION"
        (pending,) = [p for p in self.relay.pending_enrollments() if p["device_id"] == comp.device_id]
        cert = self.host.certify_companion(
            signing_public_b64u=pending["signing_public_key_b64url"],
            encryption_public_b64u=pending["encryption_public_key_b64url"])
        assert self.relay.certify_device(cert.to_dict())["status"] == "active"
        comp.certificate = cert.to_dict()
        return comp

    def submit(self, kind="task.submit", payload=None, **kw):
        env = self.companion.send(host_certificate=self.host_cert, kind=kind, payload=payload or SAFE_TASK, **kw)
        return env, self.companion.client.submit_envelope(env.to_dict())


@pytest.fixture()
def world(relay_url):
    return World(relay_url)


def err_code(exc: pytest.ExceptionInfo) -> str:
    return str(exc.value)


def test_offline_delivery_end_to_end_encrypted_roundtrip(world):
    # Host is "offline": nothing polls while the companion queues a task.
    env, receipt = world.submit()
    assert receipt["status"] == "queued"
    time.sleep(0.3)
    # Relay stores ciphertext only: the plaintext objective must not be in the queued record.
    queued = world.relay.inbox(device_id=world.host_id)
    assert len(queued) == 1 and "Summarise" not in str(queued[0])
    # At-least-once: without an ACK the same message is redelivered.
    assert len(world.relay.inbox(device_id=world.host_id)) == 1
    # Host comes online, processes once, replies, ACKs.
    assert world.runtime.poll_once() == {"received": 1, "rejected": 0, "replied": 1}
    assert len(world.supervisor.tasks) == 1
    assert world.relay.inbox(device_id=world.host_id) == []
    # Companion receives the encrypted result and decrypts it with the certified host key.
    (record,) = world.companion.client.inbox(device_id=world.companion.device_id)
    reply = world.companion.read(record)
    assert reply["in_reply_to"] == env.message_id and reply["result"]["kind"] == "task.accepted"
    world.companion.client.ack(message_id=record["envelope"]["message_id"], status="received")
    assert world.companion.client.inbox(device_id=world.companion.device_id) == []
    # No second execution on another poll.
    assert world.runtime.poll_once() == {"received": 0, "rejected": 0, "replied": 0}
    assert len(world.supervisor.tasks) == 1


def test_message_id_replay_is_idempotent_and_never_executes_twice(world):
    env, first = world.submit()
    again = world.companion.client.submit_envelope(env.to_dict())
    assert again["message_id"] == first["message_id"]
    world.runtime.poll_once()
    assert len(world.supervisor.tasks) == 1
    # Re-sending after ACK still must not re-run the task.
    try:
        world.companion.client.submit_envelope(env.to_dict())
    except RelayClientError:
        pass
    world.runtime.poll_once()
    assert len(world.supervisor.tasks) == 1
    # Same ID, different bytes -> conflict.
    other = world.companion.send(host_certificate=world.host_cert, kind="task.submit",
                                 payload={"objective": "different", "capability_scope": ["read_only"]},
                                 message_id=env.message_id)
    with pytest.raises(RelayClientError) as exc:
        world.companion.client.submit_envelope(other.to_dict())
    assert "409" in err_code(exc) or "MESSAGE_REPLAY" in err_code(exc)


def test_request_nonce_replay_rejected(world):
    c = world.companion.client
    captured = {}
    orig = c.http.request

    def spy(method, url, **kw):
        captured.update(method=method, url=url, **kw)
        return orig(method, url, **kw)

    c.http.request = spy
    c.inbox(device_id=world.companion.device_id)
    c.http.request = orig
    replay = httpx.request(captured["method"], captured["url"], content=captured["content"],
                           headers=captured["headers"], trust_env=False)
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "NONCE_REPLAY"


def test_unauthenticated_and_forged_requests_fail_closed(world):
    h = {"X-Jarvis-Link-Tenant": world.tenant}
    path = f"/v1/devices/{world.host_id}/inbox"
    assert httpx.get(world.url + path, headers=h, trust_env=False).status_code == 401
    # Bad signature from an unenrolled key claiming the companion's ID.
    rogue = DeviceIdentity.generate()
    timestamp_ms = int(time.time() * 1000)
    nonce = b64u(secrets.token_bytes(32))
    forged_path = f"/v1/devices/{world.companion.device_id}/inbox?limit=20"
    forged_signature = rogue.sign_request(
        tenant_id=world.tenant,
        device_id=world.companion.device_id,
        timestamp_ms=timestamp_ms,
        nonce_b64url=nonce,
        method="GET",
        path_and_query=forged_path,
        body=b"",
    )
    forged = httpx.get(
        world.url + forged_path,
        headers={
            "X-Jarvis-Link-Tenant": world.tenant,
            "X-Jarvis-Link-Device": world.companion.device_id,
            "X-Jarvis-Link-Timestamp": str(timestamp_ms),
            "X-Jarvis-Link-Nonce": nonce,
            "X-Jarvis-Link-Signature": forged_signature,
        },
        trust_env=False,
    )
    assert forged.status_code == 401 and forged.json()["error"]["code"] == "INVALID_SIGNATURE"
    # Wrong bootstrap secret cannot create a tenant.
    r = httpx.post(world.url + "/v1/enrollment/bootstrap", json={"tenant_id": "it-" + uuid.uuid4().hex},
                   headers={"X-Jarvis-Link-Bootstrap-Key": "wrong"}, trust_env=False)
    assert r.status_code == 401
    # A companion may not read another device's inbox.
    with pytest.raises(RelayClientError):
        world.companion.client.inbox(device_id=world.host_id)


def test_cross_tenant_signature_does_not_authenticate(relay_url):
    a, b = World(relay_url), World(relay_url)
    cross = RelayClient(base_url=relay_url, tenant_id=b.tenant, device_id=a.companion.device_id,
                        identity=a.companion.identity)
    with pytest.raises(RelayClientError):
        cross.inbox(device_id=a.companion.device_id)


def test_forged_sender_signature_and_wrong_recipient_key_rejected(world):
    env, _ = world.submit()  # sanity: valid message accepted
    d = world.companion.send(host_certificate=world.host_cert, kind="task.submit", payload=SAFE_TASK).to_dict()
    d["sender_signature_b64url"] = env.to_dict()["sender_signature_b64url"]  # signature for another message
    with pytest.raises(RelayClientError) as exc:
        world.companion.client.submit_envelope(d)
    assert "INVALID_SIGNATURE" in err_code(exc)
    d2 = world.companion.send(host_certificate=world.host_cert, kind="task.submit", payload=SAFE_TASK).to_dict()
    d2["to_key_id"] = uuid.uuid4().hex
    with pytest.raises(RelayClientError) as exc:
        world.companion.client.submit_envelope(d2)
    assert "INVALID_ENVELOPE" in err_code(exc) or "INVALID_SIGNATURE" in err_code(exc)


def test_expired_and_overlong_messages_rejected(world):
    past = int(time.time() * 1000) - 120_000
    with pytest.raises(RelayClientError) as exc:
        world.submit(issued_ms=past, ttl_ms=60_000)
    assert "MESSAGE_EXPIRED" in err_code(exc)
    with pytest.raises(Exception):
        world.submit(ttl_ms=25 * 3600 * 1000)  # > 24h cap (rejected by sender or relay)


def test_malicious_authenticated_task_is_rejected_without_poisoning_the_queue(world):
    world.submit(payload={"objective": "x", "capability_scope": ["read_only"], "command": "rm -rf /"})
    world.submit()  # a good message queued behind it
    counts = world.runtime.poll_once()
    assert counts["rejected"] == 1 and counts["received"] == 1
    assert len(world.supervisor.tasks) == 1  # only the safe task ran; no shell field accepted
    assert world.runtime.poll_once() == {"received": 0, "rejected": 0, "replied": 0}  # nothing redelivers


def test_revoked_companion_loses_all_access(world):
    c = world.companion
    world.submit()
    world.relay.revoke_device(c.device_id)
    for call in (lambda: c.client.inbox(device_id=c.device_id), lambda: world.submit()):
        with pytest.raises(RelayClientError) as exc:
            call()
        assert "DEVICE_REVOKED" in err_code(exc)
    # Revoked device cannot be messaged by the host either.
    reply = world.host.encrypt_reply(
        recipient_certificate=c.certificate, recipient_id=c.device_id, message_id=uuid.uuid4().hex,
        kind="task.result", payload={"ok": True}, issued_at_unix_ms=int(time.time() * 1000),
        expires_at_unix_ms=int(time.time() * 1000) + 60_000)
    with pytest.raises(RelayClientError) as exc:
        world.relay.submit_envelope(reply.to_dict())
    assert "NOT_RECIPIENT" in err_code(exc)


def test_invitation_is_single_use_and_revocation_blocks_a_second_enrollment_replay(world):
    invitation = world.relay.create_enrollment_invitation()
    comp = SimCompanion(world.tenant, world.url, world.identity.signing_public_der, world.host_id)
    comp.enroll(invitation)
    other = SimCompanion(world.tenant, world.url, world.identity.signing_public_der, world.host_id)
    with pytest.raises(httpx.HTTPStatusError):
        other.enroll(invitation)


def test_expired_message_is_not_delivered_or_ackable_and_alarm_cleans_it(world):
    env, _ = world.submit(ttl_ms=2_000)
    assert len(world.relay.inbox(device_id=world.host_id)) == 1
    time.sleep(3.5)  # real DO alarm should fire and expire the record
    assert world.relay.inbox(device_id=world.host_id) == []
    with pytest.raises(RelayClientError) as exc:
        world.relay.ack(message_id=env.message_id, status="received")
    assert "MESSAGE_EXPIRED" in err_code(exc)
    with pytest.raises(RelayClientError) as exc:  # re-sending the identical bytes must not 500
        world.companion.client.submit_envelope(env.to_dict())
    assert "MESSAGE_EXPIRED" in err_code(exc) or "MESSAGE_REPLAY" in err_code(exc)


def test_ack_by_non_recipient_and_conflicting_ack_rejected(world):
    env, _ = world.submit()
    with pytest.raises(RelayClientError) as exc:
        world.companion.client.ack(message_id=env.message_id, status="received")  # sender is not the recipient
    assert "NOT_FOUND" in err_code(exc)
    world.relay.ack(message_id=env.message_id, status="received", result_digest="a" * 64)
    world.relay.ack(message_id=env.message_id, status="received", result_digest="a" * 64)  # idempotent
    with pytest.raises(RelayClientError):
        world.relay.ack(message_id=env.message_id, status="rejected", result_digest="a" * 64)


def test_tenants_are_isolated(relay_url):
    a, b = World(relay_url), World(relay_url)
    a.submit()
    assert b.relay.inbox(device_id=b.host_id) == []
    with pytest.raises(RelayClientError):
        b.relay.inbox(device_id=a.host_id)


def test_websocket_hint_carries_no_content(world):
    import asyncio
    import base64
    import json
    import websockets
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from jarvis_link_host.crypto import b64u

    async def run():
        path = f"/v1/devices/{world.host_id}/stream"
        ts, nonce = str(int(time.time() * 1000)), b64u(os.urandom(32))
        sig = world.relay.identity.sign_request(
            tenant_id=world.tenant, device_id=world.host_id, timestamp_ms=int(ts), nonce_b64url=nonce,
            method="GET", path_and_query=path, body=b"")
        headers = {"X-Jarvis-Link-Tenant": world.tenant, "X-Jarvis-Link-Device": world.host_id,
                   "X-Jarvis-Link-Timestamp": ts, "X-Jarvis-Link-Nonce": nonce, "X-Jarvis-Link-Signature": sig}
        async with websockets.connect(world.url.replace("http", "ws") + path, additional_headers=headers) as ws:
            world.submit()
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            assert json.loads(msg) == {"type": "message_available"}
            assert "Summarise" not in msg

    asyncio.run(run())
