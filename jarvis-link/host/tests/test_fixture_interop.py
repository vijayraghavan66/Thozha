"""Cross-language interop: decrypt the shared fixture with the Python implementation."""
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jarvis_link_host.crypto import (
    LinkEnvelope, b64u_decode, load_p256, envelope_aad, envelope_signing_bytes, _derive,
    verify_device_certificate,
)

FIXTURE_NOW_MS = 2_000_000_010_000
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "envelope-v1.test.json"


def _priv(scalar: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int(scalar), ec.SECP256R1())


def test_python_verifies_and_decrypts_shared_fixture():
    fx = json.loads(FIXTURE.read_text())
    root_der = b64u_decode(fx["host_root_signing_public_key_der_b64url"])
    sender_cert = verify_device_certificate(
        fx["sender_certificate"], trusted_host_signing_public_der=root_der,
        expected_tenant_id=fx["tenant_id"], expected_device_id=fx["host_device_id"],
        expected_role="host", expected_issuer_device_id=fx["host_device_id"], now_ms=FIXTURE_NOW_MS,
    )
    recipient_cert = verify_device_certificate(
        fx["recipient_certificate"], trusted_host_signing_public_der=root_der,
        expected_tenant_id=fx["tenant_id"], expected_device_id=fx["companion_device_id"],
        expected_role="companion", expected_issuer_device_id=fx["host_device_id"], now_ms=FIXTURE_NOW_MS,
    )
    env = LinkEnvelope.from_dict(fx["envelope"], now_ms=FIXTURE_NOW_MS)
    assert env.to_key_id == recipient_cert.certificate_id
    load_p256(b64u_decode(sender_cert.signing_public_key_b64url)).verify(
        b64u_decode(env.sender_signature_b64url), envelope_signing_bytes(env), ec.ECDSA(hashes.SHA256()))
    scalars = fx["test_only_private_scalars"]
    ephemeral = load_p256(b64u_decode(env.ephemeral_pubkey_b64url))
    shared = _priv(scalars["companion_encryption"]).exchange(ec.ECDH(), ephemeral)
    key = _derive(shared, env.tenant_id, env.from_device_id, env.to_device_id, env.message_id)
    plaintext = AESGCM(key).decrypt(b64u_decode(env.iv_b64url), b64u_decode(env.ciphertext_b64url), envelope_aad(env))
    assert json.loads(plaintext) == fx["expected_plaintext"]
