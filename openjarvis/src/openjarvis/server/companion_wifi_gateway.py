"""Explicitly opt-in local Wi-Fi runner for the restricted companion gateway.

It binds only to one configured RFC1918/ULA interface address, checks the
configured Wi-Fi SSID before advertising, uses a persistent TLS certificate,
and serves only the allowlisted routes in ``companion_gateway``. The raw Jarvis
API remains on 127.0.0.1:8000.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

from openjarvis.core.paths import get_config_dir
from openjarvis.server.companion_gateway import create_gateway_app
from openjarvis.server.companion_wifi import (
    ensure_local_gateway_tls_identity,
    local_gateway_hostname,
    validate_wifi_bind_host,
    wifi_interface_ipv4_address,
    wifi_gateway_enabled,
)

_LOOPBACK_GATEWAY_HOST = "127.0.0.1"
_LOOPBACK_GATEWAY_PORT = 8001
_SSID_HELPER_TIMEOUT_SECONDS = 3
_MAX_SSID_UTF8_BYTES = 32
_MAX_SSID_HELPER_OUTPUT_BYTES = 64
_NETWORKSETUP_PREFIX = "Current Wi-Fi Network:"
_NETWORKSETUP_UNAVAILABLE = re.compile(
    r"^You are not associated with (?:an )?(?:AirPort|Wi-Fi) network\.?$",
    re.IGNORECASE,
)


def _wifi_address(interface: str) -> str:
    return wifi_interface_ipv4_address(interface)


def _wifi_ssid(interface: str) -> str:
    """Read the active SSID without logging it; optionally use the signed app helper.

    `networksetup` is authoritative when it returns a valid result. The helper
    is consulted only when that command reports the expected unassociated
    state or returns a nonzero status. Unknown/malformed output and timeouts
    fail closed instead of turning into an implicit manual attestation.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", interface):
        return ""
    try:
        result = subprocess.run(
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
            return _wifi_ssid_from_helper(interface)
        lines = output.splitlines()
        if len(lines) == 1 and lines[0].startswith(_NETWORKSETUP_PREFIX):
            value = lines[0][len(_NETWORKSETUP_PREFIX):]
            if value.startswith(" "):
                value = value[1:]
            return _validate_ssid_line(value)
        if len(lines) != 1 or not _NETWORKSETUP_UNAVAILABLE.fullmatch(lines[0]):
            return ""
    return _wifi_ssid_from_helper(interface)


def _validate_ssid_line(value: str) -> str:
    if not value or len(value.encode("utf-8")) > _MAX_SSID_UTF8_BYTES:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        return ""
    return value


def _wifi_ssid_from_helper(interface: str) -> str:
    helper = os.environ.get("JARVIS_COMPANION_WIFI_SSID_HELPER", "").strip()
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
        result = subprocess.run(
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


def _network_matches_allowlist(interface: str, allowed_ssid: str) -> bool:
    actual_ssid = _wifi_ssid(interface)
    return bool(actual_ssid) and actual_ssid == allowed_ssid


def _dns_sd_registration_args(executable: str, hostname: str, bind_host: str, port: int) -> list[str]:
    """Advertise the real HTTPS host/address instead of dns-sd's localhost SRV target."""
    return [
        executable,
        "-P",
        "Jarvis Companion",
        "_jarvis-companion._tcp.",
        "local.",
        str(port),
        hostname,
        bind_host,
        f"tls-host={hostname}",
        "protocol=1",
    ]


def _tls_frontend_python() -> str:
    configured = os.environ.get("JARVIS_COMPANION_TLS_PROXY_PYTHON", "").strip()
    candidates = [configured] if configured else [shutil.which("python3.14"), "/opt/homebrew/bin/python3.14"]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        result = subprocess.run(
            [candidate, "-c", "import ssl,sys; print(f'{sys.version_info.major}.{sys.version_info.minor} {ssl.OPENSSL_VERSION_INFO[0]}.{ssl.OPENSSL_VERSION_INFO[1]}')"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            continue
        match = re.fullmatch(r"(\d+)\.(\d+) (\d+)\.(\d+)", result.stdout.strip())
        if match and (int(match[1]), int(match[2])) >= (3, 14) and (int(match[3]), int(match[4])) >= (3, 6):
            return str(Path(candidate).resolve())
    raise RuntimeError("A Python 3.14/OpenSSL 3.6 TLS frontend is required for Android local Wi-Fi")


def _tls_frontend_child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Pass runtime-only values to the stdlib TLS proxy, never OpenJarvis credentials."""
    source = source or os.environ
    return {key: source[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if source.get(key)}


def serve() -> None:
    if not wifi_gateway_enabled():
        raise RuntimeError("Local Wi-Fi gateway is disabled")
    interface = os.environ.get("JARVIS_COMPANION_WIFI_INTERFACE", "en0").strip()
    allowed_ssid = os.environ.get("JARVIS_COMPANION_WIFI_ALLOWED_SSID", "").strip()
    configured_bind = os.environ.get("JARVIS_COMPANION_WIFI_BIND_HOST", "").strip()
    if not interface or not allowed_ssid or not configured_bind:
        raise RuntimeError("Local Wi-Fi gateway requires an explicit interface, SSID allowlist, and bind IP")
    bind_host = validate_wifi_bind_host(configured_bind)
    if _wifi_address(interface) != bind_host:
        raise RuntimeError("Configured Wi-Fi bind IP is not assigned to the selected interface")
    if not _network_matches_allowlist(interface, allowed_ssid):
        raise RuntimeError("Current Wi-Fi network does not match the configured allowlist")

    port = int(os.environ.get("JARVIS_COMPANION_WIFI_PORT", "8443"))
    if port != 8443:
        raise RuntimeError("Local Wi-Fi gateway uses fixed port 8443")
    hostname = local_gateway_hostname()
    tls_identity = ensure_local_gateway_tls_identity(
        get_config_dir() / "companion-wifi", hostname, port=port,
    )

    # Keep the normal gateway and its device authorization on Python 3.13, but
    # terminate LAN TLS with the tested system Python 3.14/OpenSSL 3.6 runtime.
    # The proxy forwards only to this fixed loopback gateway, never to the raw API.
    try:
        with socket.create_connection((_LOOPBACK_GATEWAY_HOST, _LOOPBACK_GATEWAY_PORT), timeout=0.2):
            raise RuntimeError("Loopback companion gateway port is already in use")
    except (ConnectionRefusedError, TimeoutError, OSError):
        pass
    loopback_server = uvicorn.Server(uvicorn.Config(
        create_gateway_app(),
        host=_LOOPBACK_GATEWAY_HOST,
        port=_LOOPBACK_GATEWAY_PORT,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
    ))
    loopback_thread = threading.Thread(target=loopback_server.run, name="jarvis-companion-loopback", daemon=True)
    loopback_thread.start()
    stop = threading.Event()
    tls_proxy: subprocess.Popen | None = None
    advertiser: subprocess.Popen | None = None
    try:
        deadline = time.monotonic() + 5
        while not loopback_server.started and loopback_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        if not loopback_server.started:
            raise RuntimeError("Loopback companion gateway did not start")

        tls_frontend_python = _tls_frontend_python()
        tls_frontend_script = Path(__file__).with_name("companion_wifi_tls_proxy.py")
        if not tls_frontend_script.is_file():
            raise RuntimeError("Local TLS frontend script is unavailable")
        proxy_args = [
            tls_frontend_python,
            str(tls_frontend_script),
            "--host", bind_host,
            "--port", str(port),
            "--certificate", str(tls_identity.certificate_path),
            "--private-key", str(tls_identity.private_key_path),
        ]
        if os.environ.get("JARVIS_COMPANION_WIFI_DEBUG", "").strip() == "1":
            proxy_args.append("--debug")
        tls_proxy = subprocess.Popen(
            proxy_args,
            stdin=subprocess.DEVNULL,
            env=_tls_frontend_child_environment(),
            start_new_session=True,
        )
        time.sleep(0.2)
        if tls_proxy.poll() is not None:
            raise RuntimeError("Local TLS frontend failed to start")

        dns_sd = shutil.which("dns-sd") or "/usr/bin/dns-sd"
        if not Path(dns_sd).is_file():
            raise RuntimeError("macOS dns-sd is unavailable; gateway was not started")
        advertiser = subprocess.Popen(
            _dns_sd_registration_args(dns_sd, hostname, bind_host, port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def stop_if_network_changes() -> None:
            while not stop.wait(10):
                if not _network_matches_allowlist(interface, allowed_ssid) or _wifi_address(interface) != bind_host:
                    if tls_proxy is not None:
                        tls_proxy.terminate()
                    loopback_server.should_exit = True
                    return

        monitor = threading.Thread(target=stop_if_network_changes, name="jarvis-wifi-policy", daemon=True)
        monitor.start()
        while tls_proxy.poll() is None and loopback_thread.is_alive():
            time.sleep(0.2)
    finally:
        stop.set()
        if tls_proxy is not None and tls_proxy.poll() is None:
            tls_proxy.terminate()
            try:
                tls_proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tls_proxy.kill()
                tls_proxy.wait(timeout=2)
        loopback_server.should_exit = True
        loopback_thread.join(timeout=5)
        if advertiser is not None:
            advertiser.terminate()
            try:
                advertiser.wait(timeout=2)
            except subprocess.TimeoutExpired:
                advertiser.kill()
                advertiser.wait(timeout=2)


def main() -> int:
    try:
        serve()
    except Exception as exc:  # noqa: BLE001 - never expose network/config values in logs
        print(f"Jarvis Wi-Fi gateway stopped ({type(exc).__name__}).", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - runner integration is opt-in
    raise SystemExit(main())
