"""Offline tests for the opt-in private-Wi-Fi gateway trust bootstrap."""
from __future__ import annotations

import base64
import hashlib
import os
import contextlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import openjarvis.tasks.supervisor as supervisor
import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openjarvis.server.companion_wifi as wifi
from openjarvis.server.companion_wifi import ensure_local_gateway_tls_identity, validate_wifi_bind_host
from openjarvis.server import companion_wifi_gateway
from openjarvis.server.supervisor_routes import _canonical_pairing_payload, router
from openjarvis.tasks.supervisor import SupervisorStore


def test_tls_identity_is_stable_private_and_hostname_bound(tmp_path):
    first = ensure_local_gateway_tls_identity(tmp_path / "wifi", "jarvis-test.local")
    second = ensure_local_gateway_tls_identity(tmp_path / "wifi", "jarvis-test.local")
    assert first.certificate_sha256 == second.certificate_sha256
    assert first.hostname == second.hostname == "jarvis-test.local"
    assert first.port == second.port == 8443
    assert os.stat(first.private_key_path).st_mode & 0o777 == 0o600
    certificate = x509.load_pem_x509_certificate(first.certificate_path.read_bytes())
    assert "jarvis-test.local" in certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert first.certificate_sha256 == "sha256:" + hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    with pytest.raises(ValueError):
        ensure_local_gateway_tls_identity(tmp_path / "wifi", "attacker.local")


@pytest.mark.parametrize("host", ["10.1.2.3", "172.20.0.4", "192.168.1.10", "fd12::1"])
def test_wifi_bind_accepts_only_private_interface_addresses(host):
    assert validate_wifi_bind_host(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.2", "100.64.0.1", "::1"])
def test_wifi_bind_rejects_public_wildcard_and_link_local_addresses(host):
    with pytest.raises(ValueError):
        validate_wifi_bind_host(host)


def test_wifi_interface_ipv4_reader_validates_private_addresses(monkeypatch):
    class Result:
        returncode = 0
        stdout = "192.168.1.8\n"

    captured = []
    monkeypatch.setattr(wifi.subprocess, "run", lambda args, **kwargs: (captured.append(args) or Result()))
    assert wifi.wifi_interface_ipv4_address("en0") == "192.168.1.8"
    assert captured[0] == ["/usr/sbin/ipconfig", "getifaddr", "en0"]

    class PublicResult(Result):
        stdout = "8.8.8.8\n"

    monkeypatch.setattr(wifi.subprocess, "run", lambda args, **kwargs: PublicResult())
    with pytest.raises(ValueError):
        wifi.wifi_interface_ipv4_address("en0")


def _make_ssid_helper(tmp_path, *, executable=True):
    helper = tmp_path / "ThozhaGatewaySupervisor"
    helper.write_text("test fixture; subprocess is always mocked\n", encoding="utf-8")
    helper.chmod(0o700 if executable else 0o600)
    return helper


def test_networksetup_ssid_takes_precedence_over_helper(tmp_path, monkeypatch):
    helper = _make_ssid_helper(tmp_path)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_SSID_HELPER", str(helper))
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        assert args == ["/usr/sbin/networksetup", "-getairportnetwork", "en0"]
        return SimpleNamespace(returncode=0, stdout="Current Wi-Fi Network: HomeNet\n")

    monkeypatch.setattr(companion_wifi_gateway.subprocess, "run", fake_run)
    assert companion_wifi_gateway._wifi_ssid("en0") == "HomeNet"
    assert len(calls) == 1


def test_helper_is_used_only_for_unavailable_networksetup_and_has_minimal_environment(tmp_path, monkeypatch):
    helper = _make_ssid_helper(tmp_path)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_SSID_HELPER", str(helper))
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "/usr/sbin/networksetup":
            return SimpleNamespace(returncode=0, stdout="You are not associated with an AirPort network.\n")
        assert args == [str(helper), "--ssid", "en0"]
        assert kwargs["timeout"] == companion_wifi_gateway._SSID_HELPER_TIMEOUT_SECONDS
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
        assert kwargs["cwd"] == "/"
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout=b"HomeNet\n")

    monkeypatch.setattr(companion_wifi_gateway.subprocess, "run", fake_run)
    assert companion_wifi_gateway._wifi_ssid("en0") == "HomeNet"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "networksetup_code,networksetup_output",
    [
        (0, "You are not associated with an AirPort network.\n"),
        (0, ""),
        (1, ""),
    ],
)
def test_helper_can_recover_an_unavailable_networksetup_result(
    tmp_path, monkeypatch, networksetup_code, networksetup_output,
):
    helper = _make_ssid_helper(tmp_path)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_SSID_HELPER", str(helper))

    def fake_run(args, **kwargs):
        if args[0] == "/usr/sbin/networksetup":
            return SimpleNamespace(returncode=networksetup_code, stdout=networksetup_output)
        return SimpleNamespace(returncode=0, stdout="HomeNet\n".encode())

    monkeypatch.setattr(companion_wifi_gateway.subprocess, "run", fake_run)
    assert companion_wifi_gateway._wifi_ssid("en0") == "HomeNet"


@pytest.mark.parametrize(
    "stdout",
    [b"", b"HomeNet\nOtherNet\n", b"A" * 65, b"A" * 33, b"Home\x00Net\n", b"\xff\xfe"],
)
def test_helper_rejects_invalid_or_multiline_output(tmp_path, monkeypatch, stdout):
    helper = _make_ssid_helper(tmp_path)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_SSID_HELPER", str(helper))

    def fake_run(args, **kwargs):
        if args[0] == "/usr/sbin/networksetup":
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(companion_wifi_gateway.subprocess, "run", fake_run)
    assert companion_wifi_gateway._wifi_ssid("en0") == ""


@pytest.mark.parametrize("helper_result", ["nonzero", "timeout"])
def test_helper_nonzero_and_timeout_fail_closed(tmp_path, monkeypatch, helper_result):
    helper = _make_ssid_helper(tmp_path)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_SSID_HELPER", str(helper))

    def fake_run(args, **kwargs):
        if args[0] == "/usr/sbin/networksetup":
            return SimpleNamespace(returncode=1, stdout="")
        if helper_result == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return SimpleNamespace(returncode=1, stdout=b"HomeNet\n")

    monkeypatch.setattr(companion_wifi_gateway.subprocess, "run", fake_run)
    assert companion_wifi_gateway._wifi_ssid("en0") == ""


@pytest.mark.parametrize("helper_state", ["missing", "relative", "not_executable", "symlink"])
def test_missing_or_invalid_helper_fails_closed(tmp_path, monkeypatch, helper_state):
    helper = _make_ssid_helper(tmp_path, executable=helper_state != "not_executable")
    if helper_state == "missing":
        helper = tmp_path / "missing-helper"
    elif helper_state == "relative":
        helper = Path("relative-helper")
    elif helper_state == "symlink":
        link = tmp_path / "helper-link"
        link.symlink_to(helper)
        helper = link
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_SSID_HELPER", str(helper))
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(companion_wifi_gateway.subprocess, "run", fake_run)
    assert companion_wifi_gateway._wifi_ssid("en0") == ""
    assert len(calls) == 1  # Only networksetup; an invalid helper is never invoked.


def test_networksetup_timeout_and_malformed_output_fail_closed_without_helper(tmp_path, monkeypatch):
    helper = _make_ssid_helper(tmp_path)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_SSID_HELPER", str(helper))
    calls = []

    def timed_out(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(companion_wifi_gateway.subprocess, "run", timed_out)
    assert companion_wifi_gateway._wifi_ssid("en0") == ""
    assert len(calls) == 1

    def malformed(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="unexpected output\n")

    monkeypatch.setattr(companion_wifi_gateway.subprocess, "run", malformed)
    assert companion_wifi_gateway._wifi_ssid("en0") == ""
    assert len(calls) == 2


def test_no_helper_or_manual_ssid_environment_fails_closed(monkeypatch):
    monkeypatch.delenv("JARVIS_COMPANION_WIFI_SSID_HELPER", raising=False)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_OWNER_CONFIRMED_SSID", "HomeNet")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_OWNER_CONFIRMATION_UNTIL", "9999999999")
    monkeypatch.setattr(
        companion_wifi_gateway.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert not companion_wifi_gateway._network_matches_allowlist("en0", "HomeNet")


def test_dns_sd_advertisement_uses_pinned_hostname_and_private_interface_ip():
    args = companion_wifi_gateway._dns_sd_registration_args(
        "/usr/bin/dns-sd", "thozha-mac.local", "192.168.1.8", 8443,
    )
    assert args[1] == "-P"
    assert args[2:8] == [
        "Jarvis Companion", "_jarvis-companion._tcp.", "local.", "8443",
        "thozha-mac.local", "192.168.1.8",
    ]
    assert "tls-host=thozha-mac.local" in args


def test_tls_proxy_child_does_not_inherit_credentials():
    child = companion_wifi_gateway._tls_frontend_child_environment({
        "PATH": "/usr/bin",
        "HOME": "/tmp/user",
        "OPENJARVIS_API_KEY": "test-only-secret",
        "JARVIS_DEV_WORKER_CONFIG": "/private/config.json",
        "GOOGLE_APPLICATION_CREDENTIALS": "/private/firebase.json",
    })
    assert child == {"PATH": "/usr/bin", "HOME": "/tmp/user"}


def test_gateway_tls_pin_is_signed_over_loopback_and_requires_auth(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", store)
    monkeypatch.setenv("OPENJARVIS_API_KEY", "local-gateway-test-admin-key")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_HOSTNAME", "jarvis-test.local")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        nonce = "localGatewayNonce_20261116"
        response = client.post(
            "/v1/jarvis/local-gateway/identity",
            headers={"Authorization": "Bearer local-gateway-test-admin-key"},
            json={"nonce": nonce},
        )
        assert response.status_code == 200
        payload = response.json()
        signature = base64.b64decode(payload.pop("signature"), validate=True)
        public_der = base64.b64decode(payload["server_public_key"], validate=True)
        public_key = serialization.load_der_public_key(public_der)
        public_key.verify(signature, _canonical_pairing_payload(payload), ec.ECDSA(hashes.SHA256()))
        assert payload["protocol"] == "openjarvis-local-gateway-identity-v1"
        assert payload["nonce"] == nonce
        assert payload["hostname"] == "jarvis-test.local"
        assert payload["port"] == "8443"
        certificate = x509.load_pem_x509_certificate((tmp_path / "companion-wifi" / "tls-cert.pem").read_bytes())
        assert payload["certificate_sha256"] == "sha256:" + hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()

        monkeypatch.setenv("JARVIS_COMPANION_WIFI_INTERFACE", "en0")
        monkeypatch.setattr(wifi, "wifi_interface_ipv4_address", lambda interface: "192.168.1.8")
        address_nonce = "localGatewayAddressNonce_20261122"
        address_response = client.post(
            "/v1/jarvis/local-gateway/address",
            headers={"Authorization": "Bearer local-gateway-test-admin-key"},
            json={"nonce": address_nonce},
        )
        assert address_response.status_code == 200
        address_payload = address_response.json()
        address_signature = base64.b64decode(address_payload.pop("signature"), validate=True)
        address_public_der = base64.b64decode(address_payload["server_public_key"], validate=True)
        address_public_key = serialization.load_der_public_key(address_public_der)
        address_public_key.verify(address_signature, _canonical_pairing_payload(address_payload), ec.ECDSA(hashes.SHA256()))
        assert address_payload["protocol"] == "openjarvis-local-gateway-address-v1"
        assert address_payload["nonce"] == address_nonce
        assert address_payload["address"] == "192.168.1.8"
        assert address_payload["hostname"] == "jarvis-test.local"
        assert address_payload["certificate_sha256"] == payload["certificate_sha256"]

    unauthenticated = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))
    assert unauthenticated.post("/v1/jarvis/local-gateway/identity", json={"nonce": "N" * 20}).status_code == 401
    assert unauthenticated.post("/v1/jarvis/local-gateway/address", json={"nonce": "N" * 20}).status_code == 401
    remote = TestClient(app, base_url="http://192.168.1.5:8000", client=("192.168.1.5", 12345))
    assert remote.post(
        "/v1/jarvis/local-gateway/identity",
        headers={"Authorization": "Bearer local-gateway-test-admin-key"},
        json={"nonce": "N" * 20},
    ).status_code == 403
    assert remote.post(
        "/v1/jarvis/local-gateway/address",
        headers={"Authorization": "Bearer local-gateway-test-admin-key"},
        json={"nonce": "N" * 20},
    ).status_code == 403
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def _pairing_test_client(tmp_path, monkeypatch):
    store = SupervisorStore(str(tmp_path / "supervisor.db"))
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", store)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345))
    return client, store


def test_wifi_qr_creation_stays_loopback_origin_guarded_and_v1_default(tmp_path, monkeypatch):
    client, store = _pairing_test_client(tmp_path, monkeypatch)
    headers = {"Origin": "http://127.0.0.1:8000"}

    bad_origin = client.post(
        "/v1/jarvis/pair/invitations",
        headers={"Origin": "https://attacker.example"},
    )
    assert bad_origin.status_code == 403
    unexpected = client.post(
        "/v1/jarvis/pair/invitations",
        headers=headers,
        json={"transport": "wifi", "debug": True},
    )
    assert unexpected.status_code == 422
    oversized = client.post(
        "/v1/jarvis/pair/invitations",
        headers={**headers, "Content-Type": "application/json"},
        content=b"{" + b" " * 1025 + b"}",
    )
    assert oversized.status_code == 413
    remote = TestClient(app=client.app, base_url="http://192.168.1.4:8000", client=("192.168.1.4", 12345))
    assert remote.post("/v1/jarvis/pair/invitations", headers=headers).status_code == 403

    response = client.post("/v1/jarvis/pair/invitations", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()["qr_payload"]
    assert set(payload) == {
        "protocol", "server_url", "server_fingerprint", "server_public_key",
        "invitation_id", "challenge", "expires_at", "server_signature",
    }
    assert payload["protocol"] == "openjarvis-pairing-v1"
    assert payload["server_url"] == "http://127.0.0.1:8000"
    signature = base64.b64decode(payload.pop("server_signature"), validate=True)
    public_key = serialization.load_der_public_key(base64.b64decode(payload["server_public_key"], validate=True))
    public_key.verify(signature, _canonical_pairing_payload(payload), ec.ECDSA(hashes.SHA256()))
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def test_pairing_invitation_cancellation_is_owner_guarded_and_blocks_valid_phone_proof(tmp_path, monkeypatch):
    client, store = _pairing_test_client(tmp_path, monkeypatch)
    headers = {"Origin": "http://127.0.0.1:8000"}
    invitation_id, challenge, _expires = store.begin_device_pairing()

    remote = TestClient(app=client.app, base_url="http://192.168.1.4:8000", client=("192.168.1.4", 12345))
    denied = remote.post(f"/v1/jarvis/pair/{invitation_id}/cancel", headers=headers)
    assert denied.status_code == 403
    assert store.device_pairing_status(invitation_id)["status"] == "INVITED"

    cancelled = client.post(f"/v1/jarvis/pair/{invitation_id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.headers["cache-control"] == "no-store"
    assert cancelled.json() == {"invitation_id": invitation_id, "status": "CANCELLED"}
    assert client.post(f"/v1/jarvis/pair/{invitation_id}/cancel", headers=headers).json()["status"] == "CANCELLED"
    assert store.device_pairing_status(invitation_id)["status"] == "CANCELLED"

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key_b64 = base64.b64encode(private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )).decode("ascii")
    signature_b64 = base64.b64encode(private_key.sign(challenge.encode("utf-8"), ec.ECDSA(hashes.SHA256()))).decode("ascii")
    with pytest.raises(ValueError, match="missing, expired, or already used"):
        store.complete_device_pairing(invitation_id, public_key_b64, signature_b64)
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def test_approved_unclaimed_pairing_can_be_cancelled_but_issued_device_cannot(tmp_path, monkeypatch):
    _client, store = _pairing_test_client(tmp_path, monkeypatch)

    def approve_pairing():
        invitation_id, challenge, _expires = store.begin_device_pairing()
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key_b64 = base64.b64encode(private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )).decode("ascii")
        proof = base64.b64encode(private_key.sign(challenge.encode("utf-8"), ec.ECDSA(hashes.SHA256()))).decode("ascii")
        store.complete_device_pairing(invitation_id, public_key_b64, proof)
        assert store.decide_device_pairing(invitation_id, "approve") == "APPROVED"
        return invitation_id, challenge, private_key

    cancelled_id, _challenge, _key = approve_pairing()
    assert store.cancel_device_pairing(cancelled_id) == "CANCELLED"
    assert store.device_pairing_status(cancelled_id) == {"invitation_id": cancelled_id, "status": "CANCELLED"}

    issued_id, _challenge, issued_key = approve_pairing()
    claim_challenge = store.device_pairing_status(issued_id)["claim_challenge"]
    claim_signature = base64.b64encode(issued_key.sign(claim_challenge.encode("utf-8"), ec.ECDSA(hashes.SHA256()))).decode("ascii")
    issued = store.claim_device_token(issued_id, claim_signature)
    with pytest.raises(RuntimeError, match="already claimed"):
        store.cancel_device_pairing(issued_id)
    assert store.authenticate_device_token(issued["access_token"]) == issued["device_id"]
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


@pytest.mark.parametrize(
    "bind,ssid,enabled,expected_status",
    [
        ("203.0.113.8", "HomeNet", "1", 409),
        ("127.0.0.1", "HomeNet", "1", 409),
        ("169.254.1.4", "HomeNet", "1", 409),
        ("192.168.1.8", "OtherNet", "1", 409),
        ("192.168.1.8", "HomeNet", "0", 409),
    ],
)
def test_wifi_qr_refuses_invalid_or_disabled_gateway_configuration(
    tmp_path, monkeypatch, bind, ssid, enabled, expected_status,
):
    client, store = _pairing_test_client(tmp_path, monkeypatch)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_ENABLED", enabled)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_INTERFACE", "en0")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_ALLOWED_SSID", "HomeNet")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_BIND_HOST", bind)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_PORT", "8443")
    monkeypatch.setattr(wifi, "wifi_interface_ipv4_address", lambda _interface: "192.168.1.8")
    monkeypatch.setattr(
        "openjarvis.server.supervisor_routes.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"Current Wi-Fi Network: {ssid}\n"),
    )
    headers = {"Origin": "http://127.0.0.1:8000"}
    response = client.post(
        "/v1/jarvis/pair/invitations", headers=headers, json={"transport": "wifi"},
    )
    assert response.status_code == expected_status
    assert response.json().get("protocol") is None
    assert store._db.execute("SELECT COUNT(*) FROM device_pairings").fetchone()[0] == 0
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


def test_wifi_qr_signs_every_connection_parameter_and_requires_live_gateway(tmp_path, monkeypatch):
    client, store = _pairing_test_client(tmp_path, monkeypatch)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_ENABLED", "1")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_INTERFACE", "en0")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_ALLOWED_SSID", "HomeNet")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_BIND_HOST", "192.168.1.8")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_PORT", "8443")
    monkeypatch.setattr(wifi, "wifi_interface_ipv4_address", lambda _interface: "192.168.1.8")
    monkeypatch.setattr(wifi, "local_gateway_hostname", lambda: "thozha-mac.local")
    monkeypatch.setattr(
        "openjarvis.server.supervisor_routes.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Current Wi-Fi Network: HomeNet\n"),
    )
    monkeypatch.setattr(
        wifi,
        "ensure_local_gateway_tls_identity",
        lambda directory, hostname, *, port: SimpleNamespace(
            hostname=hostname, port=port, certificate_sha256="sha256:" + "a" * 64,
        ),
    )
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: contextlib.nullcontext())
    headers = {"Origin": "http://127.0.0.1:8000"}
    response = client.post(
        "/v1/jarvis/pair/invitations", headers=headers, json={"transport": "wifi"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()["qr_payload"]
    assert set(payload) == {
        "protocol", "server_url", "gateway_address", "gateway_port", "gateway_certificate_sha256",
        "server_fingerprint", "server_public_key", "invitation_id", "challenge", "expires_at", "server_signature",
    }
    assert payload["protocol"] == "openjarvis-pairing-v2"
    assert payload["server_url"] == "https://thozha-mac.local:8443"
    assert payload["gateway_address"] == "192.168.1.8"
    assert payload["gateway_port"] == "8443"
    assert payload["gateway_certificate_sha256"] == "sha256:" + "a" * 64
    unsigned = dict(payload)
    signature = base64.b64decode(unsigned.pop("server_signature"), validate=True)
    public_key = serialization.load_der_public_key(base64.b64decode(unsigned["server_public_key"], validate=True))
    public_key.verify(signature, _canonical_pairing_payload(unsigned), ec.ECDSA(hashes.SHA256()))
    unsigned["gateway_address"] = "192.168.1.9"
    with pytest.raises(InvalidSignature):
        public_key.verify(signature, _canonical_pairing_payload(unsigned), ec.ECDSA(hashes.SHA256()))
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)


@pytest.mark.parametrize("failure", ["hostname", "certificate", "listener"])
def test_wifi_qr_rejects_unverifiable_tls_endpoint(tmp_path, monkeypatch, failure):
    client, store = _pairing_test_client(tmp_path, monkeypatch)
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_ENABLED", "1")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_INTERFACE", "en0")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_ALLOWED_SSID", "HomeNet")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_BIND_HOST", "192.168.1.8")
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_PORT", "8443")
    monkeypatch.setattr(wifi, "wifi_interface_ipv4_address", lambda _interface: "192.168.1.8")
    monkeypatch.setattr(
        "openjarvis.server.supervisor_routes.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Current Wi-Fi Network: HomeNet\n"),
    )
    monkeypatch.setattr(wifi, "local_gateway_hostname", lambda: "attacker.example" if failure == "hostname" else "thozha-mac.local")
    fingerprint = "sha256:invalid" if failure == "certificate" else "sha256:" + "a" * 64
    monkeypatch.setattr(
        wifi,
        "ensure_local_gateway_tls_identity",
        lambda directory, hostname, *, port: SimpleNamespace(hostname=hostname, port=port, certificate_sha256=fingerprint),
    )
    if failure == "listener":
        def unavailable(*_args, **_kwargs):
            raise OSError("connection refused")
        monkeypatch.setattr("socket.create_connection", unavailable)
    else:
        monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: contextlib.nullcontext())

    headers = {"Origin": "http://127.0.0.1:8000"}
    response = client.post(
        "/v1/jarvis/pair/invitations", headers=headers, json={"transport": "wifi"},
    )
    assert response.status_code == 409
    assert store._db.execute("SELECT COUNT(*) FROM device_pairings").fetchone()[0] == 0
    store.close()
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)
