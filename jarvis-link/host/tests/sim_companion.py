"""Simulated Android companion used by the relay integration tests (Python, no real device)."""
from __future__ import annotations

import secrets
import time
import uuid
from typing import Any

import httpx

from jarvis_link_host.client import RelayClient
from jarvis_link_host.crypto import (
    DeviceIdentity, b64u, b64u_decode, decrypt_envelope, device_id_for_signing_key,
    encrypt_envelope, LinkEnvelope,
)


class SimCompanion:
    def __init__(self, tenant_id: str, base_url: str, host_signing_public_der: bytes, host_id: str) -> None:
        self.tenant_id = tenant_id
        self.base_url = base_url
        self.host_der = host_signing_public_der
        self.host_id = host_id
        self.identity = DeviceIdentity.generate()
        self.device_id = device_id_for_signing_key(self.identity.signing_public_der)
        self.certificate: dict[str, Any] | None = None
        self.client = RelayClient(base_url=base_url, tenant_id=tenant_id, device_id=self.device_id, identity=self.identity)

    def enroll(self, invitation: dict[str, Any], *, name: str = "Simulated Android") -> dict[str, Any]:
        reg = {
            "tenant_id": self.tenant_id, "invitation_id": invitation["invitation_id"],
            "invitation_secret_b64url": invitation["invitation_secret_b64url"],
            "device_name": name, "device_role": "companion",
            "signing_public_key_b64url": self.identity.signing_public_b64u,
            "encryption_public_key_b64url": self.identity.encryption_public_b64u,
            "client_nonce_b64url": b64u(secrets.token_bytes(32)),
        }
        h = {"X-Jarvis-Link-Tenant": self.tenant_id}
        r = httpx.post(self.base_url + "/v1/enrollment/challenge", json=reg, headers=h, trust_env=False)
        r.raise_for_status()
        ch = r.json()
        proof = "\n".join([
            "JARVIS-LINK-ENROLL-V1", self.tenant_id, reg["invitation_id"], ch["challenge_id"], ch["device_id"],
            b64u(name.encode()), "companion", reg["signing_public_key_b64url"],
            reg["encryption_public_key_b64url"], reg["client_nonce_b64url"], ch["challenge_b64url"],
        ]).encode()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        sig = b64u(self.identity.signing_private.sign(proof, ec.ECDSA(hashes.SHA256())))
        done = {**reg, "device_id": ch["device_id"], "challenge_id": ch["challenge_id"],
                "challenge_b64url": ch["challenge_b64url"], "signature_b64url": sig}
        r = httpx.post(self.base_url + "/v1/enrollment/complete", json=done, headers=h, trust_env=False)
        r.raise_for_status()
        return r.json()

    def send(self, *, host_certificate: dict[str, Any], kind: str, payload: dict[str, Any],
             message_id: str | None = None, ttl_ms: int = 60_000, issued_ms: int | None = None) -> LinkEnvelope:
        issued = int(time.time() * 1000) if issued_ms is None else issued_ms
        return encrypt_envelope(
            sender=self.identity, recipient_certificate=host_certificate,
            trusted_host_signing_public_der=self.host_der, tenant_id=self.tenant_id,
            message_id=message_id or uuid.uuid4().hex, from_device_id=self.device_id,
            to_device_id=self.host_id, kind=kind, payload=payload,
            issued_at_unix_ms=issued, expires_at_unix_ms=issued + ttl_ms, expected_recipient_role="host",
        )

    def read(self, record: dict[str, Any]) -> dict[str, Any]:
        env = LinkEnvelope.from_dict(record["envelope"])
        return decrypt_envelope(
            env, recipient=self.identity, sender_certificate=record["sender_certificate"],
            trusted_host_signing_public_der=self.host_der, expected_tenant_id=self.tenant_id,
            expected_recipient_id=self.device_id, expected_recipient_certificate_id=self.certificate["certificate_id"],
            expected_sender_id=self.host_id, expected_issuer_device_id=self.host_id,
            expected_sender_role="host",
        )
