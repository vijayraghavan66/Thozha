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

_MANUAL_SSID_CONFIRMATION_MAX_SECONDS = 600


def _wifi_address(interface: str) -> str:
    return wifi_interface_ipv4_address(interface)


def _wifi_ssid(interface: str) -> str:
    result = subprocess.run(
        ["/usr/sbin/networksetup", "-getairportnetwork", interface],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    if result.returncode != 0:
        return ""
    prefix = "Current Wi-Fi Network:"
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _manual_ssid_confirmation_expiry(allowed_ssid: str, *, now: float | None = None) -> float | None:
    """Return a short-lived owner attestation only when it matches the allowlist.

    Some macOS versions withhold the associated SSID from the non-privileged
    networksetup/CoreWLAN interfaces. In that case an operator may attest the
    exact SSID for one manual run. The attestation expires within ten minutes
    and is never written to the LaunchAgent or persistent configuration.
    """
    confirmed = os.environ.get("JARVIS_COMPANION_WIFI_OWNER_CONFIRMED_SSID", "")
    raw_expiry = os.environ.get("JARVIS_COMPANION_WIFI_OWNER_CONFIRMATION_UNTIL", "")
    if not confirmed or confirmed != allowed_ssid or not re.fullmatch(r"[0-9]{10,12}", raw_expiry):
        return None
    try:
        expiry = float(int(raw_expiry))
    except ValueError:
        return None
    current = time.time() if now is None else now
    if not current < expiry <= current + _MANUAL_SSID_CONFIRMATION_MAX_SECONDS:
        return None
    return expiry


def _wifi_interface_connected(interface: str) -> bool:
    """Confirm the selected Wi-Fi radio is on and macOS reports a live link."""
    power = subprocess.run(
        ["/usr/sbin/networksetup", "-getairportpower", interface],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    if power.returncode != 0 or not re.search(r":\s*on\s*$", power.stdout.strip(), re.IGNORECASE):
        return False
    status = subprocess.run(
        ["/usr/sbin/system_profiler", "SPAirPortDataType"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return status.returncode == 0 and re.search(r"^\s*Status:\s*Connected\s*$", status.stdout, re.MULTILINE | re.IGNORECASE) is not None


def _network_matches_allowlist(interface: str, allowed_ssid: str, manual_expiry: float | None) -> bool:
    actual_ssid = _wifi_ssid(interface)
    if actual_ssid:
        return actual_ssid == allowed_ssid
    if manual_expiry is None or time.time() >= manual_expiry:
        return False
    return _wifi_interface_connected(interface)


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
    manual_expiry = _manual_ssid_confirmation_expiry(allowed_ssid)
    if not _network_matches_allowlist(interface, allowed_ssid, manual_expiry):
        raise RuntimeError("Current Wi-Fi network does not match the configured allowlist")

    port = int(os.environ.get("JARVIS_COMPANION_WIFI_PORT", "8443"))
    if not 1024 <= port <= 65535:
        raise RuntimeError("Local Wi-Fi gateway port must be between 1024 and 65535")
    hostname = local_gateway_hostname()
    tls_identity = ensure_local_gateway_tls_identity(
        get_config_dir() / "companion-wifi", hostname, port=port,
    )

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
    server = uvicorn.Server(uvicorn.Config(
        create_gateway_app(),
        host=bind_host,
        port=port,
        ssl_certfile=str(tls_identity.certificate_path),
        ssl_keyfile=str(tls_identity.private_key_path),
        log_level="warning",
        access_log=False,
        proxy_headers=False,
    ))
    stop = threading.Event()

    def stop_if_network_changes() -> None:
        while not stop.wait(10):
            if not _network_matches_allowlist(interface, allowed_ssid, manual_expiry) or _wifi_address(interface) != bind_host:
                server.should_exit = True
                return

    monitor = threading.Thread(target=stop_if_network_changes, name="jarvis-wifi-policy", daemon=True)
    monitor.start()
    try:
        server.run()
    finally:
        stop.set()
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
