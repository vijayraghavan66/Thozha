#!/usr/bin/env python3
"""Foreground process supervisor for the portable Thozha Server bundle."""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("THOZHA_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
RUN = DATA / "run"
LOGS = DATA / "logs"
STATE = RUN / "server.json"
LOCK = RUN / "server.lock"
WEB_AUTH = DATA / "config" / "web-auth.json"
GATEWAY_POLICY = DATA / "config" / "gateway-policy.json"
SERVER_IDENTITY = DATA / "config" / "server-identity.json"
PORTABLE_TLS_PORT = 8444
PORTABLE_LOOPBACK_GATEWAY_PORT = 8002


class PortableError(RuntimeError):
    pass


def _atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise PortableError(f"Configuration file is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise PortableError(f"Configuration file has an invalid shape: {path.name}")
    return value


def _server_identity() -> dict[str, str]:
    current = _read_json(SERVER_IDENTITY)
    if current is not None:
        if _server_identity_is_valid(current):
            return {"instance_id": current["instance_id"], "hostname": current["hostname"]}
        raise PortableError("Stored server identity is invalid; refusing to replace it automatically.")
    instance_id = secrets.token_hex(16)
    identity = {"instance_id": instance_id, "hostname": f"thozha-{instance_id[:12]}.local"}
    _atomic_private_json(
        SERVER_IDENTITY,
        {**identity, "created_at": datetime.now(timezone.utc).isoformat()},
    )
    return identity


def _server_identity_is_valid(value: dict[str, Any]) -> bool:
    instance_id = value.get("instance_id")
    hostname = value.get("hostname")
    return (
        isinstance(instance_id, str)
        and len(instance_id) == 32
        and all(char in "0123456789abcdef" for char in instance_id)
        and hostname == f"thozha-{instance_id[:12]}.local"
    )


def _base_environment() -> dict[str, str]:
    allow = ("HOME", "TMPDIR", "LANG", "LC_ALL", "TERM")
    env = {key: os.environ[key] for key in allow if os.environ.get(key)}
    env["PATH"] = os.pathsep.join(
        str(item)
        for item in (
            ROOT / "runtime" / "server-venv" / "bin",
            ROOT / "runtime" / "tls-python" / "bin",
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/sbin"),
            Path("/sbin"),
        )
    )
    env.update(
        {
            "OPENJARVIS_HOME": str(DATA / "openjarvis"),
            "OPENJARVIS_CODEX_HOME": str(DATA / "codex-home"),
            "CODEX_HOME": str(DATA / "codex-home"),
            "OPENJARVIS_PYTHON": str(ROOT / "runtime" / "server-venv" / "bin" / "python"),
            "JARVIS_DEV_WORKSPACE": str(DATA / "agent-evaluation" / "taskflow"),
            "JARVIS_MCP_OPEN_APP_ENABLED": "0",
            "THOZHA_WEB_SESSION_DB": str(DATA / "openjarvis" / "thozha_web_sessions.db"),
            "THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV": "1",
        }
    )
    return env


def _codex_auth_file() -> Path:
    return DATA / "codex-home" / "auth.json"


def _prepare_codex_file_auth(codex_home: Path) -> None:
    config = codex_home / "config.toml"
    if config.exists():
        try:
            text = config.read_text(encoding="utf-8")
        except OSError as exc:
            raise PortableError("Portable Codex configuration cannot be read.") from exc
        import re

        match = re.search(r"(?m)^\s*cli_auth_credentials_store\s*=\s*['\"]([^'\"]+)['\"]\s*(?:#.*)?$", text)
        if match and match.group(1) != "file":
            raise PortableError("Portable Codex config selects non-file credential storage; set cli_auth_credentials_store = \"file\" to keep sign-in inside data/.")
        if not match:
            if text.strip():
                raise PortableError("Portable Codex config must explicitly set cli_auth_credentials_store = \"file\"; refusing to rewrite existing settings.")
            text = 'cli_auth_credentials_store = "file"\n'
            temporary = config.with_name(".config.toml.tmp")
            temporary.write_text(text, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, config)
    else:
        temporary = config.with_name(".config.toml.tmp")
        temporary.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, config)
    os.chmod(config, 0o600)


def _ensure_codex_login() -> None:
    if _codex_auth_file().is_file():
        return
    if not sys.stdin.isatty():
        raise PortableError("Codex sign-in is required. Run ./bin/login.sh in an interactive Terminal.")
    answer = input("Sign in to Codex for this portable server now? [Y/n] ").strip().lower()
    if answer not in {"", "y", "yes"}:
        raise PortableError("Server startup stopped; Codex credentials were not created.")
    login(args=None)
    if not _codex_auth_file().is_file():
        raise PortableError("Codex sign-in did not complete; no server was started.")
    os.chmod(_codex_auth_file(), 0o600)


def _web_auth() -> tuple[str, str, str | None]:
    existing = _read_json(WEB_AUTH)
    if existing:
        owner = existing.get("owner_id")
        password_hash = existing.get("password_hash")
        if not isinstance(owner, str) or not isinstance(password_hash, str):
            raise PortableError("Stored Web login configuration is invalid.")
        return owner, password_hash, None
    if not sys.stdin.isatty():
        raise PortableError("First start must run in Terminal to create the Web login safely.")
    from openjarvis.server.thozha_web_auth import hash_password

    owner = "owner"
    password = secrets.token_urlsafe(27)
    password_hash = hash_password(password)
    _atomic_private_json(
        WEB_AUTH,
        {"owner_id": owner, "password_hash": password_hash, "created_at": datetime.now(timezone.utc).isoformat()},
    )
    return owner, password_hash, password


def _wifi_interfaces(env: dict[str, str]) -> list[tuple[str, str, str]]:
    try:
        raw = subprocess.run(
            ["/usr/sbin/networksetup", "-listallhardwareports"],
            check=True,
            capture_output=True,
            text=True,
            timeout=4,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    interfaces: list[str] = []
    current_is_wifi = False
    for line in raw.splitlines():
        if line.startswith("Hardware Port:"):
            current_is_wifi = line.split(":", 1)[1].strip().lower() in {"wi-fi", "wifi", "airport"}
        elif current_is_wifi and line.startswith("Device:"):
            interface = line.split(":", 1)[1].strip()
            if interface:
                interfaces.append(interface)
            current_is_wifi = False
    helper = Path("/Applications/Thozha Setup.app/Contents/MacOS/ThozhaSetup")
    if helper.is_file() and os.access(helper, os.X_OK):
        env = {**env, "JARVIS_COMPANION_WIFI_SSID_HELPER": str(helper)}
    from openjarvis.server.companion_wifi import read_wifi_ssid, wifi_interface_ipv4_address

    found = []
    for interface in interfaces:
        ssid = read_wifi_ssid(interface, environment=env)
        try:
            address = wifi_interface_ipv4_address(interface)
        except (OSError, RuntimeError, ValueError):
            continue
        if ssid and address:
            found.append((interface, ssid, address))
    return found


def _prepare_gateway(
    env: dict[str, str], *, local_only: bool, hostname: str | None = None,
) -> dict[str, Any] | None:
    if local_only:
        return None
    policy = _read_json(GATEWAY_POLICY)
    candidates = _wifi_interfaces(env)
    if len(candidates) != 1:
        if policy:
            raise PortableError("The allowlisted Wi-Fi network cannot be verified; LAN gateway remains disabled.")
        raise PortableError("SSID and private Wi-Fi address could not be verified; no LAN gateway was started.")
    interface, ssid, address = candidates[0]
    if policy is None:
        if not sys.stdin.isatty():
            raise PortableError("Owner confirmation is required before the private LAN gateway can start.")
        print(f"Private pairing gateway will bind only to this verified network ({ssid}) on {interface}.")
        answer = input("Allow paired Android access on this exact Wi-Fi network? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise PortableError("LAN gateway was not enabled; no network listener was started.")
        hostname = hostname or f"thozha-{secrets.token_hex(16)[:12]}.local"
        from openjarvis.server.companion_wifi import ensure_local_gateway_tls_identity

        identity = ensure_local_gateway_tls_identity(
            DATA / "openjarvis" / "companion-wifi", hostname, port=PORTABLE_TLS_PORT
        )
        policy = {
            "interface": interface,
            "allowed_ssid": ssid,
            "bind_ipv4": address,
            "hostname": identity.hostname,
            "certificate_sha256": identity.certificate_sha256,
            "gateway_port": PORTABLE_TLS_PORT,
            "loopback_port": PORTABLE_LOOPBACK_GATEWAY_PORT,
        }
        _atomic_private_json(GATEWAY_POLICY, policy)
    else:
        if policy.get("interface") != interface or policy.get("allowed_ssid") != ssid:
            raise PortableError("Current network differs from the saved allowlist; gateway remains disabled.")
        if (
            policy.get("gateway_port") != PORTABLE_TLS_PORT
            or policy.get("loopback_port") != PORTABLE_LOOPBACK_GATEWAY_PORT
        ):
            raise PortableError("Saved gateway port pair is unsupported; refusing to rewrite its policy.")
        policy["bind_ipv4"] = address  # DHCP change on the same verified interface/SSID.
        _atomic_private_json(GATEWAY_POLICY, policy)
    return policy


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def _bind_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _process_matches(pid: int) -> bool:
    try:
        import psutil

        process = psutil.Process(pid)
        command = " ".join(process.cmdline())
        return str(Path(__file__).resolve()) in command
    except Exception:
        return False


def _managed_child_matches(pid: int) -> bool:
    """Match only processes launched from this extracted bundle's venv."""
    try:
        import psutil

        process = psutil.Process(pid)
        expected = str((ROOT / "runtime" / "server-venv" / "bin" / "python").resolve())
        command = process.cmdline()
        return bool(command) and str(Path(command[0]).resolve()) == expected
    except Exception:
        return False


def _stop_stale_children(state: dict[str, Any]) -> bool:
    pids = state.get("managed_children", {})
    if not isinstance(pids, dict):
        return False
    matching = [pid for pid in pids.values() if isinstance(pid, int) and _managed_child_matches(pid)]
    for pid in matching:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + 10
    while matching and time.monotonic() < deadline:
        matching = [pid for pid in matching if _managed_child_matches(pid)]
        if matching:
            time.sleep(0.15)
    for pid in matching:
        if _managed_child_matches(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return not any(_managed_child_matches(pid) for pid in pids.values() if isinstance(pid, int))


def _write_state(value: dict[str, Any]) -> None:
    _atomic_private_json(STATE, value)


def doctor(args) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("macOS Apple Silicon", sys.platform == "darwin" and platform.machine() == "arm64", "target must be Darwin/arm64"))
    server_python = ROOT / "runtime/server-venv/bin/python"
    tls_python = ROOT / "runtime/tls-python/bin/python3.14"
    checks.append(("bundled server Python", server_python.is_file(), "Python 3.13 runtime missing"))
    checks.append(("bundled TLS Python", tls_python.is_file(), "Python 3.14/OpenSSL 3.6 runtime missing"))
    if tls_python.is_file():
        try:
            tls_result = subprocess.run(
                [str(tls_python), "-c", "import ssl,sys; print(f'{sys.version_info.major}.{sys.version_info.minor} {ssl.OPENSSL_VERSION_INFO[0]}.{ssl.OPENSSL_VERSION_INFO[1]}')"],
                capture_output=True, text=True, timeout=8,
            )
            tls_parts = tls_result.stdout.strip().split()
            tls_ready = tls_result.returncode == 0 and len(tls_parts) == 2 and tuple(map(int, tls_parts[0].split(".")))[:2] == (3, 14) and tuple(map(int, tls_parts[1].split("."))) >= (3, 6)
        except (OSError, subprocess.SubprocessError, ValueError):
            tls_ready = False
        checks.append(("TLS runtime version", tls_ready, "requires Python 3.14 and OpenSSL 3.6 or newer"))
    if server_python.is_file():
        static_probe = (
            "from importlib.resources import files; "
            "print(files('openjarvis.server').joinpath('static/index.html').is_file())"
        )
        try:
            static_result = subprocess.run(
                [str(server_python), "-c", static_probe],
                env=_base_environment(), capture_output=True, text=True, timeout=8,
            )
            static_ready = static_result.returncode == 0 and static_result.stdout.strip() == "True"
        except (OSError, subprocess.SubprocessError):
            static_ready = False
        checks.append(("production Web assets", static_ready, "Web bundle missing from installed server package"))
    else:
        checks.append(("production Web assets", False, "server runtime missing"))
    checks.append(("isolated data root", DATA != ROOT and not DATA.is_symlink(), "data root must be separate from executable files"))
    identity = _read_json(SERVER_IDENTITY)
    checks.append((
        "persistent server identity",
        identity is None or _server_identity_is_valid(identity),
        "stored identity is invalid; refusing automatic replacement",
    ))
    if identity is None:
        print("INFO First start will create a private server identity under data/config.")
    if server_python.is_file():
        env = _base_environment()
        env["OPENJARVIS_HOME"] = str(DATA / "openjarvis")
        env["OPENJARVIS_CODEX_HOME"] = str(DATA / "codex-home")
        env["OPENJARVIS_PYTHON"] = str(server_python)
        code = "from openjarvis.engine.codex_sdk import CodexSDKEngine; print(CodexSDKEngine().readiness())"
        try:
            result = subprocess.run([str(server_python), "-c", code], env=env, capture_output=True, text=True, timeout=8)
            ready = result.returncode == 0 and "'sdk_package_present': True" in result.stdout and "'cli_binary_present': True" in result.stdout
            checks.append(("Codex SDK and bundled CLI", ready, "SDK/CLI dependency missing"))
            auth = result.returncode == 0 and "'credential_file_present': True" in result.stdout
            checks.append(("Codex account sign-in", auth, "owner sign-in required; credentials are not bundled"))
        except (OSError, subprocess.SubprocessError):
            checks.append(("Codex SDK and bundled CLI", False, "runtime health probe failed"))
    for label, passed, reason in checks:
        print(f"{'PASS' if passed else 'BLOCKED'} {label}" + (f" — {reason}" if not passed else ""))
    return 0 if all(passed for _, passed, _ in checks) else 2


def status(args) -> int:
    state = _read_json(STATE)
    if not state:
        print("STOPPED" if not LOCK.exists() else "STOPPED (stale lock)")
        return 0 if not LOCK.exists() else 1
    pid = state.get("manager_pid")
    if not isinstance(pid, int) or not _process_matches(pid):
        pids = state.get("managed_children", {})
        orphaned = isinstance(pids, dict) and any(
            isinstance(child_pid, int) and _managed_child_matches(child_pid)
            for child_pid in pids.values()
        )
        print("ORPHANED (run stop.sh to clean up this bundle)" if orphaned else "STOPPED (stale state)")
        return 1
    api_port = int(state.get("api_port", 8000))
    print("RUNNING" if _health(api_port) else "STARTING/DEGRADED")
    print(f"Web URL: http://127.0.0.1:{api_port}")
    print(f"Gateway enabled: {bool(state.get('gateway_enabled'))}")
    pids = state.get("managed_children", {})
    gateway_ok = True
    if state.get("gateway_enabled"):
        gateway_pid = pids.get("gateway") if isinstance(pids, dict) else None
        gateway_ok = isinstance(gateway_pid, int) and _managed_child_matches(gateway_pid)
        print(f"Gateway process: {'RUNNING' if gateway_ok else 'FAILED'}")
    healthy = _health(api_port) and gateway_ok
    print(f"API health: {'healthy' if _health(api_port) else 'unhealthy'}")
    return 0 if healthy else 1


def stop(args) -> int:
    state = _read_json(STATE)
    if not state:
        print("STOPPED")
        return 0
    pid = state.get("manager_pid")
    if not isinstance(pid, int) or not _process_matches(pid):
        if not _stop_stale_children(state):
            raise PortableError("Portable manager is stale and a child could not be safely identified; inspect its PID before retrying.")
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass
        try:
            LOCK.rmdir()
        except OSError:
            pass
        print("STOPPED (stale portable children cleaned up)")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _process_matches(pid):
            print("STOPPED")
            return 0
        time.sleep(0.2)
    raise PortableError("Portable server did not stop gracefully within 15 seconds.")


def start(args) -> int:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise PortableError("This archive supports macOS Apple Silicon only.")
    if not (ROOT / "runtime/server-venv/bin/python").is_file():
        raise PortableError("Bundled server runtime is missing; extract a complete ZIP first.")
    RUN.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = _server_identity()
    if STATE.is_file():
        previous = _read_json(STATE) or {}
        pid = previous.get("manager_pid")
        if isinstance(pid, int) and _process_matches(pid):
            raise PortableError("This portable server is already running.")
        if not _stop_stale_children(previous):
            raise PortableError("A child from a previous run could not be safely identified; run ./bin/stop.sh and inspect the PID.")
        STATE.unlink()
        try:
            LOCK.rmdir()
        except OSError:
            pass
    elif LOCK.exists():
        try:
            LOCK.rmdir()
        except OSError as exc:
            raise PortableError("A stale or active lock exists without state; inspect data/run before retrying.") from exc

    api_port = int(os.environ.get("THOZHA_PORTABLE_API_PORT", "8000"))
    local_only = args.local_only or os.environ.get("THOZHA_PORTABLE_LOCAL_ONLY") == "1"
    if not 1024 <= api_port <= 65535 or not _port_available("127.0.0.1", api_port):
        raise PortableError(f"Loopback API port {api_port} is unavailable; no service was started.")
    if not local_only and (
        not _bind_available("127.0.0.1", PORTABLE_LOOPBACK_GATEWAY_PORT)
        or not _bind_available("0.0.0.0", PORTABLE_TLS_PORT)
    ):
        raise PortableError("A gateway port is already in use; refusing to create a duplicate service.")

    # Authentication is an explicit first-run owner action. Run it before
    # persisting the Web password or gateway policy so a declined login leaves
    # no partially enabled installation behind.
    _ensure_codex_login()
    gateway_policy = _prepare_gateway(_base_environment(), local_only=local_only, hostname=identity["hostname"])
    owner, password_hash, first_password = _web_auth()
    child_env = _base_environment()
    child_env.update(
        {
            "OPENJARVIS_HOME": str(DATA / "openjarvis"),
            "OPENJARVIS_CODEX_HOME": str(DATA / "codex-home"),
            "CODEX_HOME": str(DATA / "codex-home"),
            "OPENJARVIS_PYTHON": str(ROOT / "runtime/server-venv/bin/python"),
            "JARVIS_DEV_WORKSPACE": str(DATA / "agent-evaluation/taskflow"),
            "JARVIS_MCP_OPEN_APP_ENABLED": "0",
            "THOZHA_WEB_OWNER_ID": owner,
            "THOZHA_WEB_PASSWORD_HASH": password_hash,
            "THOZHA_WEB_ORIGIN": f"http://127.0.0.1:{api_port}",
            "THOZHA_WEB_SESSION_DB": str(DATA / "openjarvis/thozha_web_sessions.db"),
            "THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV": "1",
            "JARVIS_COMPANION_BACKEND_URL": f"http://127.0.0.1:{api_port}",
        }
    )
    if gateway_policy:
        child_env.update(
            {
                "JARVIS_COMPANION_WIFI_ENABLED": "1",
                "JARVIS_COMPANION_WIFI_INTERFACE": gateway_policy["interface"],
                "JARVIS_COMPANION_WIFI_ALLOWED_SSID": gateway_policy["allowed_ssid"],
                "JARVIS_COMPANION_WIFI_BIND_HOST": gateway_policy["bind_ipv4"],
                "JARVIS_COMPANION_WIFI_PORT": str(gateway_policy["gateway_port"]),
                "JARVIS_COMPANION_WIFI_LOOPBACK_PORT": str(gateway_policy["loopback_port"]),
                "JARVIS_COMPANION_LOOPBACK_API_BASE_URL": f"http://127.0.0.1:{api_port}",
                "JARVIS_COMPANION_WIFI_HOSTNAME": gateway_policy["hostname"],
                "JARVIS_COMPANION_WIFI_TLS_CERT_SHA256": gateway_policy["certificate_sha256"],
                "JARVIS_COMPANION_TLS_PROXY_PYTHON": str(ROOT / "runtime/tls-python/bin/python3.14"),
            }
        )
        helper = Path("/Applications/Thozha Setup.app/Contents/MacOS/ThozhaSetup")
        if helper.is_file() and os.access(helper, os.X_OK):
            child_env["JARVIS_COMPANION_WIFI_SSID_HELPER"] = str(helper)

    web_password = first_password
    if web_password:
        print("First-run Web login — owner ID: owner")
        print(f"One-time Web password (not saved in plaintext): {web_password}")

    # A lock directory prevents duplicate manager processes without using a
    # system service or touching LaunchAgents.
    try:
        LOCK.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise PortableError("Portable server lock already exists; run doctor.sh before retrying.") from exc
    state_doc = {
            "manager_pid": os.getpid(),
            "api_port": api_port,
            "gateway_enabled": bool(gateway_policy),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "root": str(ROOT),
            "managed_children": {},
    }
    _write_state(state_doc)
    python = str(ROOT / "runtime/server-venv/bin/python")
    api_script = (
        "import sys; from openjarvis.cli import cli; "
        "cli.main(args=['--quiet','serve',*sys.argv[1:]],prog_name='jarvis')"
    )
    api_command = [python, "-c", api_script, "--host", "127.0.0.1", "--port", str(api_port), "--engine", "codex_sdk", "--model", "gpt-6-luna"]
    api_log = open(LOGS / "server.log", "ab", buffering=0)
    gateway_log = open(LOGS / "gateway.log", "ab", buffering=0)
    children: dict[str, subprocess.Popen] = {}
    restart_counts = {"api": 0, "gateway": 0}
    stopping = False

    def handle_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    try:
        api = subprocess.Popen(api_command, env=child_env, stdout=api_log, stderr=api_log)
        children["api"] = api
        state_doc["managed_children"]["api"] = api.pid
        _write_state(state_doc)
        if gateway_policy:
            gateway = subprocess.Popen(
                [python, "-m", "openjarvis.server.companion_wifi_gateway"],
                env=child_env,
                stdout=gateway_log,
                stderr=gateway_log,
            )
            children["gateway"] = gateway
            state_doc["managed_children"]["gateway"] = gateway.pid
            _write_state(state_doc)
        _wait_api(api_port, api, timeout=40)
        print(f"Thozha Web: http://127.0.0.1:{api_port}")
        print("Raw supervisor API: loopback only")
        print(f"Pairing gateway: {'enabled on the approved SSID' if gateway_policy else 'disabled/local-only'}")
        print("Press Ctrl-C to stop Thozha.")
        while not stopping:
            for name, child in tuple(children.items()):
                if child.poll() is None:
                    continue
                if restart_counts[name] >= 3:
                    raise PortableError(
                        f"The {name} component exited repeatedly; inspect ./bin/doctor.sh and data/logs."
                    )
                restart_counts[name] += 1
                delay = min(2 ** restart_counts[name], 8)
                print(f"{name} exited; restarting once in {delay}s ({restart_counts[name]}/3).")
                deadline = time.monotonic() + delay
                while not stopping and time.monotonic() < deadline:
                    time.sleep(0.1)
                if stopping:
                    break
                if name == "api":
                    if not _port_available("127.0.0.1", api_port):
                        raise PortableError("API port was claimed after a crash; refusing to attach to an unknown service.")
                    children[name] = subprocess.Popen(api_command, env=child_env, stdout=api_log, stderr=api_log)
                    state_doc["managed_children"][name] = children[name].pid
                    _write_state(state_doc)
                    _wait_api(api_port, children[name], timeout=40)
                else:
                    if (
                        not _bind_available("127.0.0.1", PORTABLE_LOOPBACK_GATEWAY_PORT)
                        or not _bind_available("0.0.0.0", PORTABLE_TLS_PORT)
                    ):
                        raise PortableError("Gateway ports were claimed after a crash; refusing to attach to an unknown service.")
                    children[name] = subprocess.Popen(
                        [python, "-m", "openjarvis.server.companion_wifi_gateway"],
                        env=child_env, stdout=gateway_log, stderr=gateway_log,
                    )
                    state_doc["managed_children"][name] = children[name].pid
                    _write_state(state_doc)
            time.sleep(0.25)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            if child.poll() is None:
                try:
                    child.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
        api_log.close()
        gateway_log.close()
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass
        try:
            LOCK.rmdir()
        except OSError:
            pass
    return 0


def _wait_api(port: int, process: subprocess.Popen, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PortableError("OpenJarvis exited during startup; inspect data/logs/server.log.")
        if _health(port):
            return
        time.sleep(0.25)
    raise PortableError("OpenJarvis did not become healthy within 40 seconds.")


def _build_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start_parser = sub.add_parser("start")
    start_parser.add_argument(
        "--local-only",
        action="store_true",
        help="serve only on loopback; do not start LAN pairing/discovery",
    )
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("login")
    return parser


def login(_args) -> int:
    """Run the bundled Codex CLI login in the portable credential directory."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise PortableError("This archive supports macOS Apple Silicon only.")
    if not sys.stdin.isatty():
        raise PortableError("Run ./bin/login.sh from an interactive Terminal window.")
    codex_home = DATA / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(codex_home, 0o700)
    _prepare_codex_file_auth(codex_home)
    cli = _codex_cli_path()
    if cli is None:
        raise PortableError("The bundled Codex CLI is missing; run ./bin/doctor.sh.")
    env = _base_environment()
    env["CODEX_HOME"] = str(codex_home)
    # Do not accidentally reuse a globally injected API key. OAuth state is
    # created only by the owner's interactive Codex CLI login.
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run([str(cli), "login"], env=env, check=False)
    if result.returncode != 0:
        raise PortableError("Codex sign-in did not complete; no server was started.")
    auth = _codex_auth_file()
    if not auth.is_file():
        raise PortableError("Codex CLI finished without creating portable auth state.")
    os.chmod(auth, 0o600)
    print("Codex account sign-in saved in the portable data directory.")
    return 0


def _codex_cli_path() -> Path | None:
    try:
        from codex_cli_bin import bundled_codex_path

        result = Path(bundled_codex_path()).resolve()
        if result.is_file() and os.access(result, os.X_OK):
            return result
    except (ImportError, OSError, FileNotFoundError):
        return None
    return None


def main() -> int:
    args = _build_parser().parse_args()
    try:
        return {"start": start, "stop": stop, "status": status, "doctor": doctor, "login": login}[args.command](args)
    except PortableError as exc:
        print(f"Thozha: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
