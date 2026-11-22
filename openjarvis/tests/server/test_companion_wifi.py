"""Offline tests for the opt-in private-Wi-Fi gateway trust bootstrap."""
from __future__ import annotations

import base64
import hashlib
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

import openjarvis.tasks.supervisor as supervisor
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


def test_manual_ssid_attestation_requires_exact_name_and_short_expiry(monkeypatch):
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_OWNER_CONFIRMED_SSID", "Airtel_Minnal Murali")
    now = 1_800_000_000
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_OWNER_CONFIRMATION_UNTIL", str(now + 300))
    assert companion_wifi_gateway._manual_ssid_confirmation_expiry("Airtel_Minnal Murali", now=now) == now + 300
    assert companion_wifi_gateway._manual_ssid_confirmation_expiry("different", now=now) is None
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_OWNER_CONFIRMATION_UNTIL", str(now + 601))
    assert companion_wifi_gateway._manual_ssid_confirmation_expiry("Airtel_Minnal Murali", now=now) is None
    monkeypatch.setenv("JARVIS_COMPANION_WIFI_OWNER_CONFIRMATION_UNTIL", str(now - 1))
    assert companion_wifi_gateway._manual_ssid_confirmation_expiry("Airtel_Minnal Murali", now=now) is None


def test_manual_ssid_attestation_is_only_used_when_ssid_unavailable(monkeypatch):
    monkeypatch.setattr(companion_wifi_gateway.time, "time", lambda: 1000)
    monkeypatch.setattr(companion_wifi_gateway, "_wifi_ssid", lambda _interface: "")
    monkeypatch.setattr(companion_wifi_gateway, "_wifi_interface_connected", lambda _interface: True)
    assert companion_wifi_gateway._network_matches_allowlist("en0", "Airtel_Minnal Murali", 2000)
    monkeypatch.setattr(companion_wifi_gateway, "_wifi_interface_connected", lambda _interface: False)
    assert not companion_wifi_gateway._network_matches_allowlist("en0", "Airtel_Minnal Murali", 2000)
    monkeypatch.setattr(companion_wifi_gateway, "_wifi_ssid", lambda _interface: "different")
    monkeypatch.setattr(companion_wifi_gateway, "_wifi_interface_connected", lambda _interface: True)
    assert not companion_wifi_gateway._network_matches_allowlist("en0", "Airtel_Minnal Murali", 2000)


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
