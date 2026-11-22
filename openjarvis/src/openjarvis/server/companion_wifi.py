"""Private-LAN TLS identity and bind validation for the optional companion gateway.

The raw OpenJarvis API remains loopback-only. The companion gateway is a
separate, allowlisted service and is disabled unless an operator explicitly
enables it with a private-interface address.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_LOCAL_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local$")
_WIFI_PORT = 8443


@dataclass(frozen=True)
class LocalGatewayTLSIdentity:
    hostname: str
    port: int
    certificate_path: Path
    private_key_path: Path
    certificate_sha256: str


def local_gateway_hostname() -> str:
    configured = os.environ.get("JARVIS_COMPANION_WIFI_HOSTNAME", "").strip().lower()
    candidate = configured or (socket.gethostname().split(".", 1)[0].lower() + ".local")
    if not _LOCAL_HOST_RE.fullmatch(candidate):
        raise ValueError("Local companion gateway hostname must be a valid single-label .local name")
    return candidate


def validate_wifi_bind_host(host: str) -> str:
    """Bind only to an explicitly configured RFC1918/ULA interface address."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("Local Wi-Fi gateway bind host must be a private interface IP") from exc
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        raise ValueError("Local Wi-Fi gateway bind host must be a private interface IP")
    if isinstance(address, ipaddress.IPv4Address):
        allowed = any(address in network for network in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ))
    else:
        allowed = address in ipaddress.ip_network("fc00::/7")
    if not allowed:
        raise ValueError("Local Wi-Fi gateway bind host is outside private address ranges")
    return str(address)


def wifi_gateway_enabled() -> bool:
    return os.environ.get("JARVIS_COMPANION_WIFI_ENABLED", "").strip() == "1"


def wifi_interface_ipv4_address(interface: str) -> str:
    """Return a validated private IPv4 assigned to the configured macOS Wi-Fi interface."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", interface):
        raise ValueError("Wi-Fi interface name is invalid")
    result = subprocess.run(
        ["/usr/sbin/ipconfig", "getifaddr", interface],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    if result.returncode != 0:
        raise RuntimeError("configured Wi-Fi interface has no IPv4 address")
    address = validate_wifi_bind_host(result.stdout.strip())
    if ":" in address:
        raise ValueError("the local companion gateway requires an IPv4 Wi-Fi address")
    return address


def ensure_local_gateway_tls_identity(
    directory: str | Path,
    hostname: str | None = None,
    *,
    port: int = _WIFI_PORT,
) -> LocalGatewayTLSIdentity:
    """Create or validate a persistent self-signed TLS cert pinned over USB.

    The app still performs normal hostname verification. The certificate's
    exact DER fingerprint is delivered over the paired loopback API, signed by
    the existing Mac P-256 identity, before the app sends a bearer token to it.
    """
    host = hostname or local_gateway_hostname()
    if not _LOCAL_HOST_RE.fullmatch(host):
        raise ValueError("Local companion gateway hostname is invalid")
    if not 1 <= int(port) <= 65535:
        raise ValueError("Local companion gateway port is invalid")
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Local gateway identity directory must not be a symbolic link")
    os.chmod(root, 0o700)
    cert_path = root / "tls-cert.pem"
    key_path = root / "tls-key.pem"
    if cert_path.exists() or key_path.exists():
        if cert_path.is_symlink() or key_path.is_symlink() or not cert_path.is_file() or not key_path.is_file():
            raise ValueError("Local gateway TLS identity files are invalid")
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if host not in names.get_values_for_type(x509.DNSName):
            raise ValueError("Local gateway TLS certificate does not match the configured hostname")
        cert_public = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_public = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if not secrets.compare_digest(cert_public, key_public):
            raise ValueError("Local gateway TLS certificate and private key do not match")
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before((now - timedelta(minutes=5)).replace(tzinfo=None))
            .not_valid_after((now + timedelta(days=3650)).replace(tzinfo=None))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(private_key, hashes.SHA256())
        )
        _write_private_file(key_path, private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        _write_private_file(cert_path, certificate.public_bytes(serialization.Encoding.PEM))

    os.chmod(key_path, 0o600)
    os.chmod(cert_path, 0o600)
    fingerprint = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    return LocalGatewayTLSIdentity(host, int(port), cert_path, key_path, "sha256:" + fingerprint)


def _write_private_file(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "LocalGatewayTLSIdentity",
    "ensure_local_gateway_tls_identity",
    "local_gateway_hostname",
    "validate_wifi_bind_host",
    "wifi_interface_ipv4_address",
    "wifi_gateway_enabled",
]
