"""Bounded development workspace for Jarvis's `development` capability.

Every file operation and every execution is confined to one workspace root. The checks here are the enforcement
boundary: they run before any file is touched or any process is started, and they never trust the caller's path.

- Paths are relative, contain no dot segments, no NUL, and no symlink at any level (checked with lstat).
- The resolved target must stay inside the workspace root (canonical containment).
- Writes are atomic within the workspace, bounded in size, and never follow a symlink.
- Execution accepts exactly one fixed command (unittest discovery of the workspace's tests), runs with the
  workspace as cwd, a minimal environment, a wall-clock timeout, and POSIX resource limits. No shell is used.
- Every operation is recorded in an audit log that stores hashes and sizes, never file contents or secrets.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE = Path.home() / "Documents" / "Jarvis-Lab" / "agent-evaluation" / "taskflow"
MAX_FILE_BYTES = 256 * 1024
MAX_PATH_CHARS = 240
MAX_OUTPUT_CHARS = 8000
TEST_TIMEOUT_S = 60.0
CPU_LIMIT_S = 30
MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
FILE_SIZE_LIMIT_BYTES = 20 * 1024 * 1024
PROCESS_LIMIT = 64
FD_LIMIT = 256
# The interpreter that runs the host is pinned: a minimal child PATH must not resolve a different python3.
TEST_COMMAND = (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}$")


class WorkspaceError(Exception):
    """A request was refused by the boundary. The message is safe to show; it never includes file contents."""


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def open(cls, root: Path | str | None = None) -> "Workspace":
        candidate = Path(root or DEFAULT_WORKSPACE).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceError("workspace must be an absolute path")
        if candidate.is_symlink():
            raise WorkspaceError("workspace root must not be a symbolic link")
        if not candidate.is_dir():
            raise WorkspaceError("workspace does not exist")
        return cls(root=candidate.resolve(strict=True))

    # --- containment -------------------------------------------------------------------------

    def resolve(self, relative: str, *, for_write: bool = False, must_exist: bool = False) -> Path:
        """Returns the canonical path for a workspace-relative name, or raises WorkspaceError."""
        if not isinstance(relative, str) or not relative or len(relative) > MAX_PATH_CHARS:
            raise WorkspaceError("path is empty or too long")
        if "\x00" in relative or "\\" in relative or relative.startswith(("/", "~")):
            raise WorkspaceError("path must be a relative name inside the workspace")
        segments = relative.split("/")
        for segment in segments:
            if segment in ("", ".", "..") or not _SEGMENT_RE.fullmatch(segment):
                raise WorkspaceError("path contains a disallowed segment")
        current = self.root
        for index, segment in enumerate(segments):
            current = current / segment
            if os.path.islink(current):  # lstat-based: a symlink at any level is refused, even a dangling one
                raise WorkspaceError("symbolic links are not allowed in workspace paths")
            if not os.path.lexists(current):
                break  # the rest of the path does not exist yet (a write target); nothing below can be a link
            if index < len(segments) - 1 and not current.is_dir():
                raise WorkspaceError("a path segment is not a directory")
        # The loop may stop early at a segment that does not exist yet; that part cannot contain links, so the
        # full joined path is the canonical target.
        resolved = self.root.joinpath(*segments).resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceError("path escapes the workspace")
        if must_exist and not resolved.is_file():
            raise WorkspaceError("file does not exist in the workspace")
        if for_write and resolved.exists() and not resolved.is_file():
            raise WorkspaceError("target exists and is not a regular file")
        return resolved

    # --- file operations ----------------------------------------------------------------------

    def read_text(self, relative: str) -> str:
        path = self.resolve(relative, must_exist=True)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError("file is larger than the read limit")
        return path.read_text(encoding="utf-8", errors="strict")

    def list_dir(self, relative: str = ".") -> list[str]:
        if relative in ("", "."):
            base = self.root
        else:
            base = self.resolve(relative)
            if not base.is_dir():
                raise WorkspaceError("not a directory inside the workspace")
        entries = []
        for entry in sorted(base.iterdir(), key=lambda p: p.name):
            if entry.is_symlink():
                continue  # symbolic links are never listed or followed
            entries.append(entry.name + ("/" if entry.is_dir() else ""))
        return entries[:500]

    def write_text(self, relative: str, content: str) -> dict[str, Any]:
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES or "\x00" in content:
            raise WorkspaceError("content is too large or contains NUL")
        target = self.resolve(relative, for_write=True)
        parent = target.parent
        self._ensure_directories(parent)
        fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=".jarvis-write-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.islink(target):
                raise WorkspaceError("target became a symbolic link")
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return {"path": str(target.relative_to(self.root)), "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def _ensure_directories(self, directory: Path) -> None:
        relative = directory.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if os.path.islink(current):
                raise WorkspaceError("symbolic links are not allowed in workspace paths")
            if not current.exists():
                os.mkdir(current, 0o700)
            elif not current.is_dir():
                raise WorkspaceError("a path segment is not a directory")

    # --- execution ------------------------------------------------------------------------------

    def run_tests(self) -> dict[str, Any]:
        """Runs the workspace's own unittest suite (stdlib only). Nothing else can be executed."""
        tests_dir = self.root / "tests"
        if not tests_dir.is_dir() or os.path.islink(tests_dir):
            raise WorkspaceError("the workspace has no tests directory")
        result = run_bounded(list(TEST_COMMAND), cwd=self.root, timeout_s=TEST_TIMEOUT_S)
        result.update(parse_unittest_summary(result["output"]))
        return result


def run_bounded(argv: list[str], *, cwd: Path, timeout_s: float) -> dict[str, Any]:
    """Runs argv (never a shell) with a minimal environment, a timeout and POSIX resource limits."""
    if not argv or any(not isinstance(a, str) or "\x00" in a for a in argv):
        raise WorkspaceError("invalid command")
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONNOUSERSITE": "1", "HOME": str(cwd)}

    def limit_resources() -> None:
        os.setsid()
        # Each limit is best-effort: some (RLIMIT_AS) are not settable on macOS. The child reports which applied.
        for limit, value in (
            (resource.RLIMIT_CPU, CPU_LIMIT_S),
            (resource.RLIMIT_DATA, MEMORY_LIMIT_BYTES),
            (resource.RLIMIT_FSIZE, FILE_SIZE_LIMIT_BYTES),
            (resource.RLIMIT_NPROC, PROCESS_LIMIT),
            (resource.RLIMIT_NOFILE, FD_LIMIT),
            (resource.RLIMIT_CORE, 0),
        ):
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=limit_resources,
        )
    except OSError as exc:
        raise WorkspaceError(f"command could not start: {type(exc).__name__}") from exc
    try:
        out, _ = process.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
        out, _ = process.communicate()
    text = out.decode("utf-8", "replace")
    return {
        "returncode": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - started, 3),
        "output": text[-MAX_OUTPUT_CHARS:],
    }


def parse_unittest_summary(output: str) -> dict[str, Any]:
    ran = re.search(r"^Ran (\d+) tests?", output, re.MULTILINE)
    failed = re.search(r"^FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?", output, re.MULTILINE)
    ok = re.search(r"^OK\b", output, re.MULTILINE)
    failures = int(failed.group(1) or 0) if failed else 0
    errors = int(failed.group(2) or 0) if failed else 0
    return {
        "tests_ran": int(ran.group(1)) if ran else 0,
        "failures": failures,
        "errors": errors,
        "passed": bool(ok) and not failed,
    }


class AuditLog:
    """Append-only JSON lines. Stores operation names, relative paths, hashes and sizes; never contents."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields: Any) -> None:
        safe = {k: v for k, v in fields.items() if k not in {"content", "output", "token", "secret"}}
        line = json.dumps({"ts": int(time.time() * 1000), "event": event, **safe}, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
