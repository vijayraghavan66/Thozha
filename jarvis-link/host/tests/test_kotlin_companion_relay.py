"""Real Kotlin transport against the real local relay (workerd).

Each companion action runs the production Kotlin code (JarvisLinkCompanion + HttpJarvisLinkRelay +
file-backed stores) in a separate JVM via the CLI jar built from android-companion. The Python host
processes messages with the same protocol code used elsewhere. Nothing is deployed and no device key
or production credential is used; companion keys exist only in a pytest temporary directory.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from jarvis_link_host.crypto import DeviceIdentity
from test_relay_integration import World, relay_url  # noqa: F401  (fixture re-export)

REPO = Path(__file__).resolve().parents[3]
CLI_JAR = Path(os.environ.get("JARVIS_LINK_CLI_JAR", REPO / "android-companion/app/build/libs/jarvis-link-cli.jar"))
# The CLI is compiled for Java 17; /usr/bin/java here is Java 11, so pick a 17+ runtime explicitly.
JAVA = os.environ.get("JARVIS_LINK_JAVA") or (
    str(Path(os.environ["JAVA_HOME"]) / "bin/java") if os.environ.get("JAVA_HOME")
    else "/opt/homebrew/opt/openjdk@17/bin/java"
)
OBJECTIVE = "Summarise my simulated calendar"

pytestmark = pytest.mark.skipif(
    not CLI_JAR.is_file(),
    reason="Kotlin CLI jar not built (run ./gradlew :app:linkCompanionCliJar in android-companion)",
)


def cli(state: Path, command: str, *args: str, relay: str | None = None, expect_ok: bool = True) -> subprocess.CompletedProcess:
    cmd = [JAVA, "-jar", str(CLI_JAR), command, "--state", str(state), *args]
    if relay is not None:
        cmd += ["--relay", relay]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if expect_ok:
        assert proc.returncode == 0, f"CLI {command} failed ({proc.returncode}): {proc.stderr.strip()}"
    return proc


def dead_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def enroll_and_certify(world: World, state: Path, *, name: str = "Simulated Vivo", root_key: str | None = None) -> str:
    inv = world.relay.create_enrollment_invitation(device_role="companion", expires_in_seconds=600)
    out = json.loads(cli(
        state, "enroll", "--tenant", world.tenant, "--invitation-id", inv["invitation_id"],
        "--invitation-secret", inv["invitation_secret_b64url"],
        "--invitation-expires-ms", str(inv["expires_at_unix_ms"]), "--name", name,
        "--root-device-id", world.host_id, "--root-public-key", root_key or world.identity.signing_public_b64u,
        relay=world.url,
    ).stdout)
    assert out["status"] == "PENDING_HOST_CERTIFICATION"
    (pending,) = [p for p in world.relay.pending_enrollments() if p["device_id"] == out["device_id"]]
    cert = world.host.certify_companion(
        signing_public_b64u=pending["signing_public_key_b64url"],
        encryption_public_b64u=pending["encryption_public_key_b64url"],
    )
    assert world.relay.certify_device(cert.to_dict())["status"] == "active"
    return out["device_id"]


@pytest.fixture()
def tenant_with_companion(relay_url, tmp_path):  # noqa: F811
    world = World(relay_url, sim_companion=False)
    state = tmp_path / "companion.json"
    device_id = enroll_and_certify(world, state)
    sync = json.loads(cli(state, "sync", relay=world.url).stdout)
    assert sync == {"companion_device_id": device_id, "host_device_id": world.host_id, "verified": True}
    return world, state, tmp_path


def test_kotlin_round_trip_is_encrypted_both_ways(tenant_with_companion):
    world, state, _ = tenant_with_companion
    sent = json.loads(cli(state, "submit", "--objective", OBJECTIVE, "--scope", "read_only", relay=world.url).stdout)
    assert sent["queued_offline"] is False and sent["relay_status"] == "queued"
    # The relay holds only ciphertext: the objective is not visible in the stored envelope.
    stored = world.relay.inbox(device_id=world.host_id)
    assert len(stored) == 1 and OBJECTIVE not in json.dumps(stored)
    # Host (Python) decrypts with the certified companion key and replies.
    assert world.runtime.poll_once() == {"received": 1, "rejected": 0, "replied": 1}
    assert [t["objective"] for t in world.supervisor.tasks.values()] == [OBJECTIVE]
    # Companion (Kotlin) decrypts the host reply, verifies the host certificate, and ACKs.
    events = json.loads(cli(state, "poll", relay=world.url).stdout)["events"]
    assert len(events) == 1 and events[0]["type"] == "task_update"
    assert events[0]["task_id"] == sent["message_id"] and events[0]["state"] == "RUNNING"
    # Redelivery after ACK must not repeat the event (duplicate suppression, persisted in the state dir).
    assert json.loads(cli(state, "poll", relay=world.url).stdout)["events"] == []


def test_offline_submit_queues_then_flush_delivers_exactly_once(tenant_with_companion):
    world, state, _ = tenant_with_companion
    queued = json.loads(cli(state, "submit", "--objective", OBJECTIVE, "--scope", "read_only", relay=dead_port_url()).stdout)
    assert queued["queued_offline"] is True and queued["relay_status"] == "queued_locally"
    assert world.runtime.poll_once()["received"] == 0  # nothing reached the relay yet
    flushed = json.loads(cli(state, "flush", relay=world.url).stdout)
    assert flushed == {"delivered": 1, "remaining": 0, "connection": "CONNECTED", "last_error": ""}
    assert world.runtime.poll_once() == {"received": 1, "rejected": 0, "replied": 1}
    assert len(world.supervisor.tasks) == 1
    # A second flush is a no-op: nothing queued, nothing re-sent, no second task.
    assert json.loads(cli(state, "flush", relay=world.url).stdout)["delivered"] == 0
    assert world.runtime.poll_once() == {"received": 0, "rejected": 0, "replied": 0}
    assert len(world.supervisor.tasks) == 1


def test_expired_queued_message_is_dropped_and_never_executed(tenant_with_companion):
    world, state, _ = tenant_with_companion
    cli(state, "submit", "--objective", OBJECTIVE, "--scope", "read_only", "--ttl-ms", "1500", relay=dead_port_url())
    time.sleep(2.5)
    flushed = json.loads(cli(state, "flush", relay=world.url).stdout)
    assert flushed["delivered"] == 0 and flushed["remaining"] == 0
    assert world.runtime.poll_once() == {"received": 0, "rejected": 0, "replied": 0}
    assert world.supervisor.tasks == {}


def test_wrong_pinned_root_is_rejected_by_identity_validation(relay_url, tmp_path):  # noqa: F811
    world = World(relay_url, sim_companion=False)
    state = tmp_path / "companion.json"
    other_root = DeviceIdentity.generate().signing_public_b64u
    enroll_and_certify(world, state, root_key=other_root)
    proc = cli(state, "sync", relay=world.url, expect_ok=False)
    assert proc.returncode != 0
    assert "PROTOCOL_ERROR" in proc.stderr
    with pytest.raises(AssertionError):
        cli(state, "submit", "--objective", OBJECTIVE, "--scope", "read_only", relay=world.url)


def test_revoked_companion_is_refused_by_the_relay(tenant_with_companion):
    world, state, _ = tenant_with_companion
    device_id = json.loads(cli(state, "status").stdout)["device_id"]
    world.relay.revoke_device(device_id)
    proc = cli(state, "submit", "--objective", OBJECTIVE, "--scope", "read_only", relay=world.url, expect_ok=False)
    assert proc.returncode == 2 and "DEVICE_REVOKED" in proc.stderr
    assert world.supervisor.tasks == {}


def test_unsupported_capability_is_rejected_by_host_not_executed(tenant_with_companion):
    world, state, _ = tenant_with_companion
    # The companion's own enum refuses unknown capabilities before anything is sent.
    proc = cli(state, "submit", "--objective", OBJECTIVE, "--scope", "shell.exec", relay=world.url, expect_ok=False)
    assert proc.returncode != 0
    assert world.supervisor.tasks == {}
