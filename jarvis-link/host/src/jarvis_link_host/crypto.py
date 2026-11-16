"""Jarvis Link v1 P-256/ECDH, ECDSA, HKDF-SHA256 and AES-GCM primitives."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ID_RE = re.compile(r"^[0-9a-f]{32}$")
B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
KIND_RE = re.compile(r"^(task\.submit|task\.progress|task\.result|approval\.request|approval\.decision|task\.cancel)$")
MAX_TTL_MS = 24 * 60 * 60 * 1000
FUTURE_SKEW_MS = 5 * 60 * 1000
MAX_CERT_TTL_MS = 90 * 24 * 60 * 60 * 1000


class EnvelopeError(ValueError):
    """Invalid, expired, replayed, or unauthentic Link protocol data."""


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(value: str) -> bytes:
    if not isinstance(value, str) or not B64URL_RE.fullmatch(value):
        raise EnvelopeError("invalid base64url field")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise EnvelopeError("invalid base64url field") from exc


def load_p256(spki_der: bytes) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_der_public_key(spki_der)
    except Exception as exc:
        raise EnvelopeError("invalid SPKI public key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise EnvelopeError("public key must be P-256")
    return key


def spki(public_key: ec.EllipticCurvePublicKey) -> bytes:
    if not isinstance(public_key.curve, ec.SECP256R1):
        raise EnvelopeError("only P-256 keys are supported")
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def device_id_for_signing_key(public_der: bytes) -> str:
    load_p256(public_der)
    return hashlib.sha256(public_der).digest()[:16].hex()


@dataclass(frozen=True)
class DeviceIdentity:
    """Device keys held in memory by a simulator; never serialized by this package."""
    signing_private: ec.EllipticCurvePrivateKey
    encryption_private: ec.EllipticCurvePrivateKey

    @classmethod
    def generate(cls) -> "DeviceIdentity":
        return cls(ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1()))

    @property
    def signing_public_der(self) -> bytes:
        return spki(self.signing_private.public_key())

    @property
    def encryption_public_der(self) -> bytes:
        return spki(self.encryption_private.public_key())

    @property
    def signing_public_b64u(self) -> str:
        return b64u(self.signing_public_der)

    @property
    def encryption_public_b64u(self) -> str:
        return b64u(self.encryption_public_der)

    def sign_request(self, *, tenant_id: str, device_id: str, timestamp_ms: int, nonce_b64url: str,
                     method: str, path_and_query: str, body: bytes) -> str:
        message = request_signing_bytes(tenant_id, device_id, timestamp_ms, nonce_b64url, method, path_and_query, body)
        return b64u(self.signing_private.sign(message, ec.ECDSA(hashes.SHA256())))


def request_signing_bytes(tenant_id: str, device_id: str, timestamp_ms: int, nonce: str,
                          method: str, path_and_query: str, body: bytes) -> bytes:
    if (not ID_RE.fullmatch(device_id) or not tenant_id or "\n" in tenant_id or "\r" in tenant_id
            or "\n" in path_and_query or "\r" in path_and_query):
        raise EnvelopeError("invalid signed-request fields")
    digest = hashlib.sha256(body).hexdigest()
    value = "\n".join(("JARVIS-LINK-REQUEST-V1", tenant_id, device_id, str(timestamp_ms), nonce,
                       method.upper(), path_and_query, digest))
    return value.encode("ascii")


@dataclass(frozen=True)
class DeviceCertificate:
    v: int
    tenant_id: str
    device_id: str
    device_role: str
    signing_public_key_b64url: str
    encryption_public_key_b64url: str
    not_before_unix_ms: int
    expires_at_unix_ms: int
    certificate_id: str
    issuer_device_id: str
    signature_b64url: str

    def signing_bytes(self) -> bytes:
        fields = (
            "JARVIS-LINK-DEVICE-CERT-V1", str(self.v), self.tenant_id, self.device_id,
            self.device_role, self.signing_public_key_b64url, self.encryption_public_key_b64url,
            str(self.not_before_unix_ms), str(self.expires_at_unix_ms), self.certificate_id,
            self.issuer_device_id,
        )
        if any("\n" in f or "\r" in f for f in fields):
            raise EnvelopeError("device certificate contains a line break")
        return "\n".join(fields).encode("ascii")

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DeviceCertificate":
        required = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != required:
            raise EnvelopeError("device certificate fields are incomplete or unexpected")
        try:
            return cls(**{k: value[k] for k in required})
        except (TypeError, KeyError) as exc:
            raise EnvelopeError("device certificate fields are invalid") from exc


def issue_device_certificate(*, host: DeviceIdentity, tenant_id: str, host_device_id: str,
                             signing_public_b64u: str, encryption_public_b64u: str,
                             device_role: str = "companion", now_ms: int | None = None,
                             ttl_ms: int = MAX_CERT_TTL_MS) -> DeviceCertificate:
    sign_der = b64u_decode(signing_public_b64u)
    enc_der = b64u_decode(encryption_public_b64u)
    load_p256(sign_der); load_p256(enc_der)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if not 1 <= ttl_ms <= MAX_CERT_TTL_MS:
        raise EnvelopeError("certificate lifetime exceeds limit")
    if device_role not in {"host", "companion"}:
        raise EnvelopeError("unsupported device role")
    unsigned = DeviceCertificate(
        1, tenant_id, device_id_for_signing_key(sign_der), device_role, signing_public_b64u,
        encryption_public_b64u, now, now + ttl_ms, uuid.uuid4().hex, host_device_id, "",
    )
    signature = host.signing_private.sign(unsigned.signing_bytes(), ec.ECDSA(hashes.SHA256()))
    return DeviceCertificate(**{**unsigned.__dict__, "signature_b64url": b64u(signature)})


def verify_device_certificate(value: DeviceCertificate | Mapping[str, Any], *,
                              trusted_host_signing_public_der: bytes, expected_tenant_id: str,
                              expected_device_id: str | None = None,
                              expected_role: str | None = None,
                              expected_issuer_device_id: str | None = None,
                              now_ms: int | None = None) -> DeviceCertificate:
    cert = value if isinstance(value, DeviceCertificate) else DeviceCertificate.from_dict(value)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if type(cert.v) is not int or cert.v != 1 or cert.tenant_id != expected_tenant_id:
        raise EnvelopeError("device certificate version or tenant mismatch")
    if cert.device_role not in {"host", "companion"} or (expected_role and cert.device_role != expected_role):
        raise EnvelopeError("device certificate role mismatch")
    if expected_device_id and cert.device_id != expected_device_id:
        raise EnvelopeError("device certificate ID mismatch")
    if expected_issuer_device_id and cert.issuer_device_id != expected_issuer_device_id:
        raise EnvelopeError("device certificate issuer mismatch")
    if not all(ID_RE.fullmatch(x) for x in (cert.device_id, cert.issuer_device_id, cert.certificate_id)):
        raise EnvelopeError("device certificate identifiers are invalid")
    if type(cert.not_before_unix_ms) is not int or type(cert.expires_at_unix_ms) is not int:
        raise EnvelopeError("device certificate validity timestamps are invalid")
    lifetime = cert.expires_at_unix_ms - cert.not_before_unix_ms
    if lifetime <= 0 or lifetime > MAX_CERT_TTL_MS or cert.not_before_unix_ms > now + FUTURE_SKEW_MS or cert.expires_at_unix_ms <= now:
        raise EnvelopeError("device certificate is outside its validity window")
    signing_der = b64u_decode(cert.signing_public_key_b64url)
    encryption_der = b64u_decode(cert.encryption_public_key_b64url)
    if device_id_for_signing_key(signing_der) != cert.device_id:
        raise EnvelopeError("device certificate signing key does not match device ID")
    load_p256(encryption_der)
    issuer = load_p256(trusted_host_signing_public_der)
    try:
        issuer.verify(b64u_decode(cert.signature_b64url), cert.signing_bytes(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise EnvelopeError("device certificate is not signed by the trusted host") from exc
    return cert


@dataclass(frozen=True)
class LinkEnvelope:
    v: int
    tenant_id: str
    message_id: str
    from_device_id: str
    to_device_id: str
    to_key_id: str
    kind: str
    issued_at_unix_ms: int
    expires_at_unix_ms: int
    ephemeral_pubkey_b64url: str
    iv_b64url: str
    ciphertext_b64url: str
    sender_signature_b64url: str

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, now_ms: int | None = None) -> "LinkEnvelope":
        required = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != required:
            raise EnvelopeError("envelope has missing or unexpected fields")
        env = cls(**{k: value[k] for k in required})
        env.validate(now_ms=now_ms)
        return env

    def validate(self, *, now_ms: int | None = None) -> None:
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if type(self.v) is not int or self.v != 1:
            raise EnvelopeError("unsupported envelope version")
        if not all(ID_RE.fullmatch(x) for x in (self.message_id, self.from_device_id, self.to_device_id, self.to_key_id)):
            raise EnvelopeError("envelope identifiers must be 32 lowercase hex characters")
        if not isinstance(self.tenant_id, str) or not self.tenant_id or "\n" in self.tenant_id or "\r" in self.tenant_id:
            raise EnvelopeError("invalid tenant ID")
        if not KIND_RE.fullmatch(self.kind):
            raise EnvelopeError("unsupported envelope kind")
        if type(self.issued_at_unix_ms) is not int or type(self.expires_at_unix_ms) is not int:
            raise EnvelopeError("timestamps must be integer milliseconds")
        ttl = self.expires_at_unix_ms - self.issued_at_unix_ms
        if ttl <= 0 or ttl > MAX_TTL_MS or self.issued_at_unix_ms > now + FUTURE_SKEW_MS or self.expires_at_unix_ms <= now:
            raise EnvelopeError("envelope is expired or has invalid timestamps")
        _load_p256(b64u_decode(self.ephemeral_pubkey_b64url))
        if len(b64u_decode(self.iv_b64url)) != 12 or len(b64u_decode(self.ciphertext_b64url)) < 16:
            raise EnvelopeError("invalid AES-GCM IV or ciphertext")
        b64u_decode(self.sender_signature_b64url)


def _load_p256(der: bytes) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_der_public_key(der)
    except Exception as exc:
        raise EnvelopeError("invalid SPKI public key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise EnvelopeError("public key must use P-256")
    return key


def _header(env: LinkEnvelope) -> tuple[str, ...]:
    return ("JARVIS-LINK-ENVELOPE-V1", env.tenant_id, env.message_id, env.from_device_id,
            env.to_device_id, env.to_key_id, env.kind, str(env.issued_at_unix_ms), str(env.expires_at_unix_ms),
            env.ephemeral_pubkey_b64url, env.iv_b64url)


def envelope_aad(env: LinkEnvelope) -> bytes:
    return "\n".join(_header(env)).encode("ascii")


def envelope_signing_bytes(env: LinkEnvelope) -> bytes:
    digest = hashlib.sha256(b64u_decode(env.ciphertext_b64url)).hexdigest()
    return "\n".join((*_header(env), digest)).encode("ascii")


def _derive(shared: bytes, tenant_id: str, from_id: str, to_id: str, message_id: str) -> bytes:
    info = f"JarvisLink/v1\n{tenant_id}\n{from_id}\n{to_id}".encode("utf-8")
    return HKDF(hashes.SHA256(), 32, bytes.fromhex(message_id), info).derive(shared)


def encrypt_envelope(*, sender: DeviceIdentity, recipient_certificate: DeviceCertificate | Mapping[str, Any],
                     trusted_host_signing_public_der: bytes, tenant_id: str, message_id: str,
                     from_device_id: str, to_device_id: str,
                     kind: str, payload: Mapping[str, Any], issued_at_unix_ms: int,
                     expires_at_unix_ms: int, expected_recipient_role: str = "companion") -> LinkEnvelope:
    recipient_cert = verify_device_certificate(
        recipient_certificate,
        trusted_host_signing_public_der=trusted_host_signing_public_der,
        expected_tenant_id=tenant_id,
        expected_device_id=to_device_id,
        expected_role=expected_recipient_role,
        expected_issuer_device_id=device_id_for_signing_key(trusted_host_signing_public_der),
    )
    if from_device_id != device_id_for_signing_key(sender.signing_public_der):
        raise EnvelopeError("sender ID does not match local signing key")
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    recipient = _load_p256(b64u_decode(recipient_cert.encryption_public_key_b64url))
    shared = ephemeral.exchange(ec.ECDH(), recipient)
    key = _derive(shared, tenant_id, from_device_id, to_device_id, message_id)
    eph_b64u = b64u(spki(ephemeral.public_key()))
    iv = secrets.token_bytes(12)
    iv_b64u = b64u(iv)
    placeholder = LinkEnvelope(1, tenant_id, message_id, from_device_id, to_device_id,
                              recipient_cert.certificate_id, kind,
                              issued_at_unix_ms, expires_at_unix_ms, eph_b64u, iv_b64u, b64u(b"x"*16), "AA")
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(iv, plaintext, envelope_aad(placeholder))
    unsigned = LinkEnvelope(1, tenant_id, message_id, from_device_id, to_device_id,
                            recipient_cert.certificate_id, kind,
                            issued_at_unix_ms, expires_at_unix_ms, eph_b64u, iv_b64u, b64u(ciphertext), "AA")
    signature = sender.signing_private.sign(envelope_signing_bytes(unsigned), ec.ECDSA(hashes.SHA256()))
    return LinkEnvelope(1, tenant_id, message_id, from_device_id, to_device_id,
                        recipient_cert.certificate_id, kind,
                        issued_at_unix_ms, expires_at_unix_ms, eph_b64u, iv_b64u, b64u(ciphertext), b64u(signature))


def decrypt_envelope(env: LinkEnvelope, *, recipient: DeviceIdentity,
                     sender_certificate: DeviceCertificate | Mapping[str, Any],
                     trusted_host_signing_public_der: bytes, expected_tenant_id: str,
                     expected_recipient_id: str, expected_recipient_certificate_id: str,
                     expected_sender_id: str, expected_issuer_device_id: str,
                     expected_sender_role: str = "companion",
                     now_ms: int | None = None) -> dict[str, Any]:
    env.validate(now_ms=now_ms)
    if env.tenant_id != expected_tenant_id or env.to_device_id != expected_recipient_id or env.to_key_id != expected_recipient_certificate_id:
        raise EnvelopeError("envelope tenant or recipient mismatch")
    sender_cert = verify_device_certificate(
        sender_certificate,
        trusted_host_signing_public_der=trusted_host_signing_public_der,
        expected_tenant_id=expected_tenant_id,
        expected_device_id=expected_sender_id,
        expected_role=expected_sender_role,
        expected_issuer_device_id=expected_issuer_device_id,
    )
    if env.from_device_id != sender_cert.device_id:
        raise EnvelopeError("sender certificate device ID mismatch")
    sender = _load_p256(b64u_decode(sender_cert.signing_public_key_b64url))
    try:
        sender.verify(b64u_decode(env.sender_signature_b64url), envelope_signing_bytes(env), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise EnvelopeError("sender envelope signature invalid") from exc
    ephemeral = _load_p256(b64u_decode(env.ephemeral_pubkey_b64url))
    shared = recipient.encryption_private.exchange(ec.ECDH(), ephemeral)
    key = _derive(shared, env.tenant_id, env.from_device_id, env.to_device_id, env.message_id)
    try:
        clear = AESGCM(key).decrypt(b64u_decode(env.iv_b64url), b64u_decode(env.ciphertext_b64url), envelope_aad(env))
    except InvalidTag as exc:
        raise EnvelopeError("message AES-GCM authentication failed") from exc
    try:
        payload = json.loads(clear)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EnvelopeError("decrypted payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise EnvelopeError("decrypted payload must be an object")
    return payload
