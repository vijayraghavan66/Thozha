"""Launch the loopback Thozha Web API with owner-only configuration.

This wrapper is intended for an explicitly reviewed LaunchAgent candidate. It
reads the existing owner password from a mode-0600 file, derives the Web
password hash in memory, and execs the existing OpenJarvis CLI. It never prints
or persists the password or hash.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Mapping


def _private_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    info = path.stat()
    return info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600


def _private_directory(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    info = path.stat()
    return info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700


def prepare_web_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Validate the local-only owner configuration and produce child env."""
    env = dict(environ)
    core_source = Path(env.get("THOZHA_CORE_SOURCE", "")).expanduser().resolve()
    if not core_source.is_dir() or not (core_source / "openjarvis/server/app.py").is_file():
        raise RuntimeError("reviewed Thozha core source is unavailable")

    password_file = Path(env.get("THOZHA_WEB_PASSWORD_FILE", "")).expanduser()
    if not _private_file(password_file) or not _private_directory(password_file.parent):
        raise RuntimeError("owner password file must be in an owner-only directory and mode 0600")
    password = password_file.read_text(encoding="utf-8").strip()
    if len(password) < 12 or len(password) > 1024:
        raise RuntimeError("owner password file contains an invalid password")

    owner_id = env.get("THOZHA_WEB_OWNER_ID", "").strip()
    origin = env.get("THOZHA_WEB_ORIGIN", "").strip()
    if not owner_id or origin != "http://127.0.0.1:5174":
        raise RuntimeError("Web owner and exact loopback origin are required")
    if env.get("THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV") != "1":
        raise RuntimeError("HTTP is allowed only for the explicitly configured loopback dev origin")

    device_ids = [item.strip() for item in env.get("THOZHA_WEB_ALLOWED_DEVICE_IDS", "").split(",")]
    if not device_ids or any(len(item) != 32 or any(c not in "0123456789abcdef" for c in item) for item in device_ids):
        raise RuntimeError("an explicit lowercase paired-device allowlist is required")
    if len(set(device_ids)) != len(device_ids):
        raise RuntimeError("duplicate paired-device IDs are not allowed")

    state_root = Path(env.get("THOZHA_WEB_STATE_ROOT", "")).expanduser()
    if not _private_directory(state_root):
        raise RuntimeError("Web state directory must exist and be owner-only")
    for key in ("THOZHA_WEB_SESSION_DB", "THOZHA_WEB_MEMORY_DB", "THOZHA_WEB_CONVERSATION_DB"):
        value = env.get(key, "").strip()
        if not value:
            raise RuntimeError(f"{key} is required")
        path = Path(value).expanduser()
        if path.parent.resolve() != state_root.resolve():
            raise RuntimeError(f"{key} must stay inside the owner-only Web state directory")
        if path.exists() and not _private_file(path):
            raise RuntimeError(f"{key} must be an owner-owned mode-0600 regular file")

    worker_config = env.get("JARVIS_DEV_WORKER_CONFIG", "").strip()
    if worker_config and not Path(worker_config).expanduser().is_file():
        raise RuntimeError("configured development worker file is unavailable")

    # Import after the candidate source path is selected so the same reviewed
    # WebAuthConfig/hash implementation is used by runtime and tests.
    sys.path.insert(0, str(core_source))
    from openjarvis.server.thozha_web_auth import WebAuthConfig, hash_password

    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(core_source), env.get("PYTHONPATH", "")) if part
    )
    env["THOZHA_WEB_PASSWORD_HASH"] = hash_password(password)
    env.pop("THOZHA_WEB_PASSWORD_FILE", None)
    env["JARVIS_MCP_OPEN_APP_ENABLED"] = "0"
    env["THOZHA_WEB_AUTOMATION_ENABLED"] = "0"
    WebAuthConfig.from_environment(env)
    return env


def server_command(home: Path | None = None) -> list[str]:
    """Return the only supported API launch command: loopback port 8000."""
    root = (home or Path.home()).expanduser()
    cli = root / ".openjarvis/src/.venv/bin/jarvis"
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise RuntimeError("OpenJarvis CLI executable is unavailable")
    return [str(cli), "serve", "--host", "127.0.0.1", "--port", "8000"]


def main() -> None:
    env = prepare_web_environment(os.environ)
    command = server_command()
    os.execve(command[0], command, env)


if __name__ == "__main__":
    main()
