"""SSH-framed MCP worker client and isolated guest worker for Jarvis development tasks.

The Mac-side MCP remains the supervisor/capability authority. This module only
forwards fixed MCP operations to a disposable Linux guest; its SSH command is
constant and all tool arguments travel over stdin as JSON-RPC data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import resource
import shlex
import stat
import subprocess
import sys
import errno
import time
import uuid
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from threading import Event
from openjarvis.server.performance import PERF_LOGGER

PROTOCOL = "2025-03-26"
DEFAULT_WORKSPACE = "/Users/REDACTED/Documents/Jarvis-Lab/agent-evaluation/taskflow"
GUEST_WORKSPACE_ROOT = "/workspace/taskflow"
GUEST_HOST_SENTINEL_PATH = "/Users/REDACTED/Documents/Jarvis-Lab/agent-evaluation/vm-run-20261121/host-only-canary.txt"
DEFAULT_GUEST_WORKER = "/usr/local/libexec/jarvis-dev-worker.py"
DEFAULT_SSH_TARGET = "vmadmin@127.0.0.1"
DEFAULT_SSH_PORT = 2222
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_FILES = 2000
MAX_OUTPUT_CHARS = 8000
MAX_MESSAGE_BYTES = 1024 * 1024
_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class RemoteWorkerError(RuntimeError):
    """A sanitized worker transport or protocol failure."""


def _no_symlink_components(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return False
        except FileNotFoundError:
            continue
        except OSError:
            return False
    return True


@dataclass(frozen=True)
class RemoteWorkerConfig:
    identity_file: Path
    known_hosts_file: Path
    workspace_root: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_SSH_PORT
    ssh_user: str = "vmadmin"
    worker_path: str = DEFAULT_GUEST_WORKER

    def __post_init__(self) -> None:
        if (self.host, self.port, self.ssh_user, self.worker_path) != (
            "127.0.0.1",
            DEFAULT_SSH_PORT,
            "vmadmin",
            DEFAULT_GUEST_WORKER,
        ):
            raise RemoteWorkerError(
                "worker endpoint differs from the pinned loopback guest"
            )
        if self.workspace_root != Path(DEFAULT_WORKSPACE):
            raise RemoteWorkerError(
                "remote worker workspace differs from the fixed taskflow root"
            )

    @classmethod
    def load(cls, config_path: str | os.PathLike[str]) -> "RemoteWorkerConfig":
        path = Path(config_path).expanduser()
        if not path.is_absolute() or not _no_symlink_components(path):
            raise RemoteWorkerError("worker configuration path is invalid")
        if path.stat().st_size > 4096 or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RemoteWorkerError("worker configuration is too large")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RemoteWorkerError("worker configuration could not be loaded") from exc
        required = {"identity_file", "known_hosts_file", "workspace_root"}
        if not isinstance(raw, dict) or set(raw) != required:
            raise RemoteWorkerError("worker configuration fields are invalid")
        identity = Path(raw["identity_file"]).expanduser()
        known_hosts = Path(raw["known_hosts_file"]).expanduser()
        workspace = Path(raw["workspace_root"]).expanduser()
        if not all(
            item.is_absolute() and _no_symlink_components(item)
            for item in (identity, known_hosts, workspace)
        ):
            raise RemoteWorkerError("worker paths must be absolute and non-symlinked")
        if workspace != Path(DEFAULT_WORKSPACE):
            raise RemoteWorkerError(
                "remote worker workspace differs from the fixed taskflow root"
            )
        if not identity.is_file() or stat.S_IMODE(identity.stat().st_mode) & 0o077:
            raise RemoteWorkerError(
                "test SSH identity must be a private, owner-only file"
            )
        if identity.stat().st_uid != os.getuid():
            raise RemoteWorkerError(
                "test SSH identity must be owned by the current user"
            )
        if (
            not known_hosts.is_file()
            or known_hosts.stat().st_size == 0
            or stat.S_IMODE(known_hosts.stat().st_mode) & 0o022
        ):
            raise RemoteWorkerError("pinned guest host key is missing")
        return cls(
            identity_file=identity,
            known_hosts_file=known_hosts,
            workspace_root=workspace,
        )


class RemoteMcpWorkerClient:
    """One-shot MCP JSON-RPC over loopback SSH; never places arguments in a shell command."""

    def __init__(
        self,
        config: RemoteWorkerConfig,
        *,
        runner: Callable[..., Any] | None = None,
        process_factory: Callable[..., Any] = subprocess.Popen,
        task_id: str,
        trace_id: str | None = None,
    ):
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise RemoteWorkerError("VM worker requires a supervisor-bound task ID")
        self.config = config
        self.runner = runner
        self.process_factory = process_factory
        self.task_id = task_id
        self.trace_id = trace_id if isinstance(trace_id, str) and re.fullmatch(r"[0-9a-f]{12}", trace_id) else hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_s: int = 60,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        call_started_ns = time.perf_counter_ns()
        safe_name = name if isinstance(name, str) and re.fullmatch(r"[a-z_]{1,40}", name) else "invalid"
        PERF_LOGGER.info(
            "perf trace=%s event=vm_rpc_started wall_ms=%d mono_ns=%d task_id=%s operation=%s",
            self.trace_id, int(time.time() * 1000), call_started_ns, self.task_id[:8], safe_name,
        )
        if name not in {
            "list_project_files",
            "read_project_file",
            "write_project_file",
            "run_project_tests",
            "workspace_manifest",
            "read_artifact",
            "execution_context",
        }:
            raise RemoteWorkerError("remote worker operation is not allowlisted")
        if not isinstance(arguments, dict):
            raise RemoteWorkerError("remote worker arguments must be an object")
        if not isinstance(timeout_s, int) or timeout_s < 1 or timeout_s > 600:
            raise RemoteWorkerError(
                "remote worker timeout is outside the allowed range"
            )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "jarvis-host-bridge", "version": "1"},
            },
        }
        initialized = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, "task_id": self.task_id},
        }
        payload = (
            "\n".join(
                json.dumps(item, separators=(",", ":"), ensure_ascii=False)
                for item in (initialize, initialized, call)
            )
            + "\n"
        )
        if len(payload.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise RemoteWorkerError("remote worker request exceeds the size limit")

        unit = "jarvis-dev-" + uuid.uuid4().hex[:16]
        workspace = GUEST_WORKSPACE_ROOT
        remote_args = [
            "sudo",
            "-n",
            "systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            f"--unit={unit}",
            "--property=User=codex",
            "--property=Group=codex",
            f"--property=WorkingDirectory={workspace}",
            f"--property=Environment=HOME={workspace}",
            f"--property=Environment=TMPDIR={workspace}/.tmp",
            "--property=Environment=CODEX_HOME=/var/empty",
            f"--property=Environment=JARVIS_DEV_WORKSPACE_ROOT={workspace}",
            "--property=Environment=JARVIS_DEV_WORKER_USER=codex",
            f"--property=Environment=JARVIS_DEV_TASK_ID={self.task_id}",
            "--property=Environment=PYTHONDONTWRITEBYTECODE=1",
            "--property=UMask=0022",
            "--property=ProtectSystem=strict",
            f"--property=ReadWritePaths={workspace}",
            "--property=ProtectHome=true",
            "--property=NoNewPrivileges=true",
            "--property=CapabilityBoundingSet=",
            "--property=AmbientCapabilities=",
            "--property=PrivateDevices=true",
            "--property=ProtectKernelTunables=true",
            "--property=ProtectKernelModules=true",
            "--property=ProtectControlGroups=true",
            "--property=ProtectKernelLogs=true",
            "--property=ProtectClock=true",
            "--property=RestrictSUIDSGID=true",
            "--property=LockPersonality=true",
            "--property=MemoryDenyWriteExecute=true",
            "--property=ProtectProc=invisible",
            "--property=ProcSubset=pid",
            "--property=TasksMax=128",
            "--property=MemoryMax=2G",
            "--property=CPUQuota=200%",
            f"--property=RuntimeMaxSec={max(30, min(600, timeout_s))}",
            "--property=PrivateNetwork=true",
            "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=InaccessiblePaths=/Users /etc /home /root /var/log /var/lib/private /tmp /var/tmp /mnt /media /workspace/sibling-probe",
            "/usr/bin/python3",
            self.config.worker_path,
        ]
        remote_command = shlex.join(remote_args)
        argv = [
            "/usr/bin/ssh",
            "-T",
            "-F",
            "/dev/null",
            "-p",
            str(self.config.port),
            "-i",
            str(self.config.identity_file),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.config.known_hosts_file}",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            f"{self.config.ssh_user}@{self.config.host}",
            remote_command,
        ]
        if self.runner is not None:
            try:
                proc = self.runner(
                    argv,
                    input=payload,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s + 15,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self._stop_guest_unit(unit)
                raise RemoteWorkerError("isolated worker operation timed out") from exc
            except OSError as exc:
                raise RemoteWorkerError(
                    "isolated worker transport could not start"
                ) from exc
            if proc.returncode != 0:
                raise RemoteWorkerError("isolated worker transport or operation failed")
            stdout = proc.stdout
        else:
            try:
                proc = self.process_factory(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise RemoteWorkerError(
                    "isolated worker transport could not start"
                ) from exc
            deadline = time.monotonic() + timeout_s + 15
            try:
                try:
                    stdout, _stderr = proc.communicate(
                        payload.encode("utf-8"), timeout=min(0.25, timeout_s + 15)
                    )
                except subprocess.TimeoutExpired:
                    stdout = b""
                    while proc.poll() is None:
                        if cancel_event is not None and cancel_event.is_set():
                            self._stop_guest_unit(unit)
                            proc.terminate()
                            try:
                                proc.communicate(timeout=2)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.communicate()
                            raise RemoteWorkerError(
                                "isolated worker operation was cancelled"
                            )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._stop_guest_unit(unit)
                            proc.terminate()
                            try:
                                proc.communicate(timeout=2)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.communicate()
                            raise RemoteWorkerError(
                                "isolated worker operation timed out"
                            )
                        try:
                            stdout, _stderr = proc.communicate(
                                timeout=min(0.25, remaining)
                            )
                        except subprocess.TimeoutExpired as exc:
                            if exc.output:
                                stdout = exc.output
                    if proc.returncode not in {0, None}:
                        raise RemoteWorkerError(
                            "isolated worker transport or operation failed"
                        )
            except RemoteWorkerError:
                raise
            except (BrokenPipeError, OSError) as exc:
                self._stop_guest_unit(unit)
                raise RemoteWorkerError("isolated worker transport failed") from exc
            if len(stdout or b"") > MAX_MESSAGE_BYTES:
                self._stop_guest_unit(unit)
                raise RemoteWorkerError(
                    "isolated worker response exceeds the size limit"
                )
            stdout = (stdout or b"").decode("utf-8", "replace")
        messages: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                messages.append(value)
        result = next(
            (message.get("result") for message in messages if message.get("id") == 2),
            None,
        )
        if not isinstance(result, dict):
            raise RemoteWorkerError("isolated MCP worker returned no tool result")
        if result.get("isError"):
            text = next(
                (
                    item.get("text")
                    for item in result.get("content", [])
                    if isinstance(item, dict)
                ),
                "",
            )
            raise RemoteWorkerError(
                str(text or "isolated worker refused the operation")[:300]
            )
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise RemoteWorkerError("isolated worker response was malformed")
        PERF_LOGGER.info(
            "perf trace=%s event=vm_rpc_completed wall_ms=%d mono_ns=%d elapsed_ms=%.3f task_id=%s operation=%s",
            self.trace_id,
            int(time.time() * 1000),
            time.perf_counter_ns(),
            (time.perf_counter_ns() - call_started_ns) / 1_000_000,
            self.task_id[:8],
            safe_name,
        )
        return structured

    def _stop_guest_unit(self, unit: str) -> None:
        if not re.fullmatch(r"jarvis-dev-[0-9a-f]{16}", unit):
            return
        argv = [
            "/usr/bin/ssh",
            "-T",
            "-F",
            "/dev/null",
            "-p",
            str(self.config.port),
            "-i",
            str(self.config.identity_file),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.config.known_hosts_file}",
            "-o",
            "ConnectTimeout=5",
            f"{self.config.ssh_user}@{self.config.host}",
            shlex.join(["sudo", "-n", "systemctl", "stop", unit]),
        ]
        try:
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


class RemoteWorkspace:
    """Workspace-compatible facade whose data operations execute only in the guest."""

    is_remote = True

    def __init__(self, root: Path, client: RemoteMcpWorkerClient):
        self.root = root
        self.client = client

    def resolve(
        self, relative: str, *, for_write: bool = False, must_exist: bool = False
    ) -> Path:
        if not isinstance(relative, str) or not relative or len(relative) > 240:
            raise RemoteWorkerError("workspace path is empty or too long")
        if "\x00" in relative or "\\" in relative or relative.startswith(("/", "~")):
            raise RemoteWorkerError("workspace path must be relative")
        parts = relative.split("/")
        if any(
            part in {"", ".", ".."} or not _SEGMENT.fullmatch(part) for part in parts
        ):
            raise RemoteWorkerError("workspace path has a disallowed segment")
        result = self.root.joinpath(*parts)
        return result

    def list_dir(self, relative: str = ".") -> list[str]:
        response = self.client.call_tool("list_project_files", {"dir": relative})
        entries = response.get("entries")
        if not isinstance(entries, list) or any(
            not isinstance(item, str) for item in entries
        ):
            raise RemoteWorkerError(
                "isolated worker returned an invalid directory listing"
            )
        return entries

    def read_text(self, relative: str) -> str:
        response = self.client.call_tool("read_project_file", {"path": relative})
        value = response.get("text")
        if not isinstance(value, str):
            raise RemoteWorkerError("isolated worker returned invalid text")
        return value

    def read_bytes(self, relative: str, *, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        response = self.client.call_tool(
            "read_artifact", {"path": relative, "max_bytes": max_bytes}
        )
        encoded = response.get("base64")
        if not isinstance(encoded, str):
            raise RemoteWorkerError("isolated worker returned invalid artifact bytes")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise RemoteWorkerError("isolated worker returned invalid base64") from exc
        if (
            len(data) > max_bytes
            or response.get("bytes") != len(data)
            or response.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise RemoteWorkerError(
                "isolated artifact response failed integrity or size validation"
            )
        return data

    def write_text(
        self, relative: str, content: str, *, expected_sha256: str | None = None
    ) -> dict[str, Any]:
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise RemoteWorkerError("workspace write digest changed before dispatch")
        response = self.client.call_tool(
            "write_project_file",
            {
                "path": relative,
                "content": content,
                "content_sha256": digest,
                "size": len(data),
            },
            timeout_s=60,
        )
        if (
            response.get("sha256") != digest
            or response.get("bytes") != len(data)
            or response.get("path") != relative
        ):
            raise RemoteWorkerError(
                "isolated worker write receipt did not match the request"
            )
        return response

    def run_tests(self) -> dict[str, Any]:
        result = self.client.call_tool(
            "run_project_tests", {"command": "unittest-tests"}, timeout_s=300
        )
        output = str(result.get("output") or "")
        if (
            not isinstance(result.get("returncode"), int)
            and result.get("returncode") is not None
            or not isinstance(result.get("timed_out"), bool)
            or not isinstance(result.get("duration_s"), (int, float))
            or len(output) > MAX_OUTPUT_CHARS
        ):
            raise RemoteWorkerError("isolated test result was malformed")
        ran = re.search(r"^Ran (\d+) tests?", output, re.MULTILINE)
        failed = re.search(
            r"^FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?",
            output,
            re.MULTILINE,
        )
        result.update(
            {
                "tests_ran": int(ran.group(1)) if ran else 0,
                "failures": int(failed.group(1) or 0) if failed else 0,
                "errors": int(failed.group(2) or 0) if failed else 0,
                "passed": bool(re.search(r"^OK\b", output, re.MULTILINE))
                and failed is None,
            }
        )
        return result

    def manifest(self, paths: list[str] | None = None) -> dict[str, Any]:
        return self.client.call_tool(
            "workspace_manifest", {"paths": paths or []}, timeout_s=60
        )

    def manifest_sha256(self) -> str:
        value = self.manifest()
        digest = value.get("manifest_sha256")
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise RemoteWorkerError("isolated worker manifest digest is invalid")
        return digest

    def execution_context(self) -> dict[str, Any]:
        response = self.client.call_tool("execution_context", {}, timeout_s=30)
        if response.get("os") != "Linux" or response.get("architecture") not in {
            "aarch64",
            "arm64",
        }:
            raise RemoteWorkerError(
                "worker execution context is not the approved ARM64 Linux guest"
            )
        if response.get("cwd") != GUEST_WORKSPACE_ROOT:
            raise RemoteWorkerError(
                "worker current directory is outside the approved workspace"
            )
        if response.get("task_id") != self.client.task_id:
            raise RemoteWorkerError(
                "worker execution context returned a different task identity"
            )
        if response.get("username") != "codex" or response.get("uid") in {0, None}:
            raise RemoteWorkerError(
                "worker process is not running as the restricted guest user"
            )
        if response.get("host_only_marker_visible") is not False:
            raise RemoteWorkerError(
                "synthetic host-only marker is visible inside the guest"
            )
        if response.get("host_only_marker_access") != "denied":
            raise RemoteWorkerError(
                "synthetic host-only marker path was not denied by the guest sandbox"
            )
        return response


def _relative_parts(path: Any) -> list[str]:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise ValueError("workspace path is empty or too long")
    if "\x00" in path or "\\" in path or path.startswith(("/", "~")):
        raise ValueError("workspace path must be relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or not _SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("workspace path has a disallowed segment")
    return parts


class GuestWorkspace:
    def __init__(self, root: Path):
        if not root.is_absolute() or not root.is_dir() or _has_symlink_component(root):
            raise ValueError("fixed task workspace is unavailable")
        self.root = root.resolve(strict=True)
        self._dir_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )

    def _open_directory(self, parts: list[str], *, create: bool = False) -> int:
        """Walk from the pinned root using dirfds so symlink swaps cannot escape the workspace."""
        fd = os.open(self.root, self._dir_flags)
        try:
            for part in parts:
                try:
                    child = os.open(part, self._dir_flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(part, self._dir_flags, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise ValueError(
                            "workspace path contains a symlink or non-directory"
                        ) from exc
                    raise
                os.close(fd)
                fd = child
            return fd
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _read_fd(fd: int, max_bytes: int) -> bytes:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError("workspace file exceeds the read limit")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ValueError("workspace file exceeds the read limit")
        return data

    def resolve(self, relative: Any, *, for_write: bool = False) -> Path:
        parts = _relative_parts(relative)
        parent_fd = self._open_directory(parts[:-1], create=for_write)
        try:
            try:
                info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if for_write:
                    return self.root.joinpath(*parts)
                raise FileNotFoundError("workspace file was not found")
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("symbolic links are not allowed")
            if for_write and not stat.S_ISREG(info.st_mode):
                raise ValueError("write target is not a regular file")
            if not for_write and not stat.S_ISREG(info.st_mode):
                raise FileNotFoundError("workspace file was not found")
            return self.root.joinpath(*parts)
        finally:
            os.close(parent_fd)

    def list_dir(self, relative: str = ".") -> list[str]:
        parts = [] if relative in {"", "."} else _relative_parts(relative)
        fd = self._open_directory(parts)
        try:
            entries = []
            for name in sorted(os.listdir(fd)):
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue
                entries.append(name + ("/" if stat.S_ISDIR(info.st_mode) else ""))
            return entries[:500]
        finally:
            os.close(fd)

    def read_bytes(self, relative: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        if (
            not isinstance(max_bytes, int)
            or max_bytes < 0
            or max_bytes > MAX_FILE_BYTES
        ):
            raise ValueError("invalid read size limit")
        parts = _relative_parts(relative)
        parent_fd = self._open_directory(parts[:-1])
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        try:
            return self._read_fd(fd, max_bytes)
        finally:
            os.close(fd)
            os.close(parent_fd)

    def read_text(self, relative: str) -> str:
        return self.read_bytes(relative).decode("utf-8", "strict")

    def write_text(
        self, relative: str, content: str, expected_sha256: str, size: int
    ) -> dict[str, Any]:
        data = content.encode("utf-8")
        if (
            "\x00" in content
            or len(data) > MAX_FILE_BYTES
            or size != len(data)
            or hashlib.sha256(data).hexdigest() != expected_sha256
        ):
            raise ValueError("workspace write payload failed validation")
        parts = _relative_parts(relative)
        parent_fd = self._open_directory(parts[:-1], create=True)
        target_name = parts[-1]
        try:
            try:
                existing = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                    raise ValueError("write target is not a regular file")
            except FileNotFoundError:
                pass
            temp_name = f".jarvis-write-{uuid.uuid4().hex}.tmp"
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
        except Exception:
            os.close(parent_fd)
            raise
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                existing = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                    raise ValueError("write target is not a regular file")
            except FileNotFoundError:
                pass
            os.replace(
                temp_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
        return {"path": relative, "bytes": len(data), "sha256": expected_sha256}

    def manifest(self, paths: list[str] | None = None) -> dict[str, Any]:
        if paths:
            selected = sorted(set(paths))
        else:
            selected = []

            def collect(relative_parts: list[str]) -> None:
                fd = self._open_directory(relative_parts)
                try:
                    for name in sorted(os.listdir(fd)):
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raise ValueError(
                                "workspace manifests refuse symbolic links"
                            )
                        child = relative_parts + [name]
                        if stat.S_ISDIR(info.st_mode):
                            collect(child)
                        elif stat.S_ISREG(info.st_mode):
                            selected.append("/".join(child))
                        if len(selected) > MAX_FILES:
                            raise ValueError("workspace has too many files")
                finally:
                    os.close(fd)

            collect([])
        if len(selected) > MAX_FILES:
            raise ValueError("workspace has too many files")
        files = []
        total = 0
        digest = hashlib.sha256()
        for relative in selected:
            data = self.read_bytes(relative)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("workspace manifest exceeds the size limit")
            file_hash = hashlib.sha256(data).hexdigest()
            files.append({"path": relative, "bytes": len(data), "sha256": file_hash})
            digest.update(relative.encode("utf-8") + b"\x00" + bytes.fromhex(file_hash))
        return {
            "files": files,
            "total_bytes": total,
            "manifest_sha256": digest.hexdigest(),
        }

    def execution_context(self) -> dict[str, Any]:
        marker = self.read_text("preflight-sentinel.txt")
        try:
            Path(GUEST_HOST_SENTINEL_PATH).stat()
            host_marker_visible = True
            host_marker_access = "visible"
        except PermissionError:
            host_marker_visible = False
            host_marker_access = "denied"
        except FileNotFoundError:
            host_marker_visible = False
            host_marker_access = "not_present"
        return {
            "os": platform.system(),
            "architecture": platform.machine(),
            "cwd": os.getcwd(),
            "uid": os.geteuid(),
            "pid": os.getpid(),
            "username": os.environ.get("JARVIS_DEV_WORKER_USER", ""),
            "vm_marker": marker[:128],
            "host_only_marker_visible": host_marker_visible,
            "host_only_marker_access": host_marker_access,
        }

    def run_tests(self) -> dict[str, Any]:
        tests_fd = self._open_directory(["tests"])
        os.close(tests_fd)
        tmp_fd = self._open_directory([".tmp"], create=True)
        output_name = f".jarvis-tests-{uuid.uuid4().hex}.log"
        output_fd = os.open(
            output_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=tmp_fd,
        )
        output_limit = MAX_OUTPUT_CHARS
        started = time.monotonic()

        def limit():
            os.setsid()
            for key, val in (
                (resource.RLIMIT_CPU, 120),
                (resource.RLIMIT_FSIZE, 20 * 1024 * 1024),
                (resource.RLIMIT_NOFILE, 128),
                (resource.RLIMIT_NPROC, 128),
                (resource.RLIMIT_CORE, 0),
            ):
                try:
                    resource.setrlimit(key, (val, val))
                except (ValueError, OSError):
                    pass

        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HOME": str(self.root),
            "TMPDIR": str(self.root / ".tmp"),
            "CODEX_HOME": "/var/empty",
        }
        process = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_*.py",
                    "-q",
                ],
                cwd=self.root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output_fd,
                stderr=subprocess.STDOUT,
                preexec_fn=limit,
            )
            try:
                process.wait(timeout=120)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, 9)
                except OSError:
                    pass
                process.wait()
            os.fsync(output_fd)
            size = os.fstat(output_fd).st_size
            os.lseek(output_fd, max(0, size - output_limit), os.SEEK_SET)
            text = os.read(output_fd, output_limit).decode("utf-8", "replace")
            return {
                "returncode": None if timed_out else process.returncode,
                "timed_out": timed_out,
                "duration_s": round(time.monotonic() - started, 3),
                "output": text,
            }
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, 9)
                except OSError:
                    pass
                process.wait()
            os.close(output_fd)
            try:
                os.unlink(output_name, dir_fd=tmp_fd)
            except FileNotFoundError:
                pass
            os.close(tmp_fd)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for segment in path.parts[1:]:
        current /= segment
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            return True
        except OSError:
            return True
    return False


def _worker_dispatch(
    name: str, args: dict[str, Any], workspace: GuestWorkspace
) -> dict[str, Any]:
    if name == "execution_context":
        if args:
            raise ValueError("execution context accepts no arguments")
        return workspace.execution_context()
    if name == "list_project_files":
        relative = args.get("dir", ".")
        if not isinstance(relative, str) or set(args) - {"dir"}:
            raise ValueError("invalid list arguments")
        return {"entries": workspace.list_dir(relative)}
    if name == "read_project_file":
        if set(args) != {"path"} or not isinstance(args.get("path"), str):
            raise ValueError("invalid read arguments")
        return {"text": workspace.read_text(args["path"])}
    if name == "write_project_file":
        if (
            set(args) != {"path", "content", "content_sha256", "size"}
            or not isinstance(args.get("content"), str)
            or not isinstance(args.get("size"), int)
            or not isinstance(args.get("content_sha256"), str)
        ):
            raise ValueError("invalid write arguments")
        if not _HASH.fullmatch(args["content_sha256"]):
            raise ValueError("invalid content digest")
        return workspace.write_text(
            args["path"], args["content"], args["content_sha256"], args["size"]
        )
    if name == "run_project_tests":
        if set(args) != {"command"} or args.get("command") != "unittest-tests":
            raise ValueError("test command is not allowlisted")
        return workspace.run_tests()
    if name == "workspace_manifest":
        if set(args) - {"paths"}:
            raise ValueError("invalid manifest arguments")
        paths = args.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("invalid manifest paths")
        return workspace.manifest(paths or None)
    if name == "read_artifact":
        if (
            set(args) != {"path", "max_bytes"}
            or not isinstance(args.get("path"), str)
            or not isinstance(args.get("max_bytes"), int)
        ):
            raise ValueError("invalid artifact arguments")
        data = workspace.read_bytes(
            args["path"], min(MAX_FILE_BYTES, max(0, args["max_bytes"]))
        )
        return {
            "base64": base64.b64encode(data).decode("ascii"),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    raise ValueError("tool is not allowlisted")


def worker_handle(
    message: dict[str, Any], workspace: GuestWorkspace, *, expected_task_id: str
) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis-dev-vm-worker", "version": "1"},
            },
        }
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            task_id = str(params.get("task_id", ""))
            if (
                not re.fullmatch(r"[0-9a-f]{32}", expected_task_id)
                or task_id != expected_task_id
            ):
                raise ValueError("task identity does not match this worker")
            if name not in {
                "list_project_files",
                "read_project_file",
                "write_project_file",
                "run_project_tests",
                "workspace_manifest",
                "read_artifact",
                "execution_context",
            }:
                raise ValueError("tool is not allowlisted")
            value = _worker_dispatch(name, args, workspace)
            if name == "execution_context":
                value["task_id"] = task_id
            text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            result = {
                "content": [{"type": "text", "text": text}],
                "structuredContent": value,
                "isError": False,
            }
        except Exception as exc:
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": f"VM worker refused or failed ({type(exc).__name__}).",
                    }
                ],
                "isError": True,
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "method not found"},
        }
    return None


def worker_main() -> int:
    root = Path(os.environ.get("JARVIS_DEV_WORKSPACE_ROOT", GUEST_WORKSPACE_ROOT))
    expected_task_id = os.environ.get("JARVIS_DEV_TASK_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", expected_task_id):
        return 2
    try:
        workspace = GuestWorkspace(root)
    except Exception:
        return 2
    for line in sys.stdin:
        if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
            return 2
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError
            result = worker_handle(
                message, workspace, expected_task_id=expected_task_id
            )
        except Exception as exc:
            result = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32602,
                    "message": f"invalid request ({type(exc).__name__})",
                },
            }
        if result is not None:
            sys.stdout.write(
                json.dumps(result, separators=(",", ":"), ensure_ascii=False) + "\n"
            )
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(worker_main())
