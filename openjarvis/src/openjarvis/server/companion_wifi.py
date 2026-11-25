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
import ssl
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
_SSID_HELPER_TIMEOUT_SECONDS = 3
_MAX_SSID_UTF8_BYTES = 32
_MAX_SSID_HELPER_OUTPUT_BYTES = 64
_NETWORKSETUP_PREFIX = "Current Wi-Fi Network:"
_NETWORKSETUP_UNAVAILABLE = re.compile(
    r"^You are not associated with (?:an )?(?:AirPort|Wi-Fi) network\.?$",
    re.IGNORECASE,
)


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


def verify_local_gateway_tls_listener(
    address: str,
    hostname: str,
    port: int,
    expected_certificate_sha256: str,
    *,
    timeout: float = 0.75,
) -> bool:
    """Verify the configured private listener presents the stored pinned cert.

    This probe sends no HTTP request or credential. It completes only a TLS
    handshake and compares the leaf DER fingerprint to the identity already
    stored for the signed QR. Standard CA validation is intentionally not used
    for this self-signed local identity; exact pinned fingerprint matching is
    the trust check performed by the Android client as well.
    """
    bind_address = validate_wifi_bind_host(address)
    if not _LOCAL_HOST_RE.fullmatch(hostname) or int(port) != _WIFI_PORT:
        raise ValueError("local gateway TLS endpoint is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_certificate_sha256):
        raise ValueError("local gateway TLS fingerprint is invalid")
    if not 0.05 <= timeout <= 3.0:
        raise ValueError("local gateway TLS probe timeout is invalid")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((bind_address, int(port)), timeout=timeout) as connection:
        with context.wrap_socket(connection, server_hostname=hostname) as secure:
            certificate_der = secure.getpeercert(binary_form=True)
    if not certificate_der:
        return False
    actual = "sha256:" + hashlib.sha256(certificate_der).hexdigest()
    return secrets.compare_digest(actual, expected_certificate_sha256)


def read_wifi_ssid(
    interface: str,
    *,
    runner=None,
    environment: dict[str, str] | None = None,
) -> str:
    """Return the verified active Wi-Fi SSID, or an empty string when unknown.

    Both the gateway runner and QR issuer must use the same fail-closed
    verification path. macOS may withhold SSID metadata from
    ``networksetup``; in that specific case an explicitly installed,
    owner-authorized helper may read the SSID through CoreWLAN. This never
    accepts an owner assertion or falls back to the configured allowlist.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", interface):
        return ""
    run = runner or subprocess.run
    env = os.environ if environment is None else environment
    try:
        result = run(
            ["/usr/sbin/networksetup", "-getairportnetwork", interface],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=_SSID_HELPER_TIMEOUT_SECONDS,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ""

    output = result.stdout
    if result.returncode == 0:
        if not output.strip():
            return _wifi_ssid_from_helper(interface, run=run, environment=env)
        lines = output.splitlines()
        if len(lines) == 1 and lines[0].startswith(_NETWORKSETUP_PREFIX):
            value = lines[0][len(_NETWORKSETUP_PREFIX):]
            if value.startswith(" "):
                value = value[1:]
            return _validate_ssid_line(value)
        if len(lines) != 1 or not _NETWORKSETUP_UNAVAILABLE.fullmatch(lines[0]):
            return ""
    return _wifi_ssid_from_helper(interface, run=run, environment=env)


def _validate_ssid_line(value: str) -> str:
    if not value or len(value.encode("utf-8")) > _MAX_SSID_UTF8_BYTES:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        return ""
    return value


def _wifi_ssid_from_helper(
    interface: str,
    *,
    run,
    environment: dict[str, str],
) -> str:
    helper = environment.get("JARVIS_COMPANION_WIFI_SSID_HELPER", "").strip()
    if not helper:
        return ""
    executable = Path(helper)
    if (
        not executable.is_absolute()
        or executable.is_symlink()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        return ""
    try:
        result = run(
            [str(executable), "--ssid", interface],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
            timeout=_SSID_HELPER_TIMEOUT_SECONDS,
            env={"PATH": "/usr/bin:/bin"},
            cwd="/",
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0 or len(result.stdout) > _MAX_SSID_HELPER_OUTPUT_BYTES:
        return ""
    try:
        output = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return ""
    if output.endswith("\r\n"):
        output = output[:-2]
    elif output.endswith("\n"):
        output = output[:-1]
    if "\n" in output or "\r" in output:
        return ""
    return _validate_ssid_line(output)


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
    "read_wifi_ssid",
    "verify_local_gateway_tls_listener",
]
