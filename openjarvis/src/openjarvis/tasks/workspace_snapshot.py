"""Fail-closed snapshot primitives for the supervisor workspace bridge.

Authorization and task binding are enforced by :mod:`workspace_bridge`.
"""

from __future__ import annotations

import hashlib
import os
import stat

from openjarvis.tasks.dev_capability import (
    CapabilityError,
    _check_relative,
    canonical_workspace,
)


def snapshot_files(
    root: str, paths: list[str], *, max_bytes: int = 4 * 1024 * 1024
) -> dict[str, bytes]:
    """Read explicitly selected regular files without following links.

    Return an in-memory snapshot so a failed validation leaves no partial staging
    directory. A caller must authenticate the resulting manifest before transfer.
    """
    root = canonical_workspace(root)
    if not paths or len(paths) > 2000 or len(set(paths)) != len(paths):
        raise CapabilityError(
            "INVALID_SNAPSHOT", "file selection must be nonempty and unique"
        )
    if not 0 < max_bytes <= 4 * 1024 * 1024:
        raise CapabilityError("INVALID_SNAPSHOT", "invalid snapshot size limit")
    result: dict[str, bytes] = {}
    total = 0
    for rel in sorted(paths):
        parts = _check_relative(rel)
        if rel == "preflight-sentinel.txt" or any(
            p == ".git" or p.startswith(".") for p in parts
        ):
            raise CapabilityError(
                "PATH_DENIED", "hidden paths, Git metadata, and VM marker are excluded"
            )
        fds: list[int] = []
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fds.append(fd)
            for component in parts[:-1]:
                fd = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                fds.append(fd)
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            fds.append(fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise CapabilityError(
                    "PATH_DENIED", "only unlinked regular files are permitted"
                )
            if before.st_size > max_bytes - total:
                raise CapabilityError("SNAPSHOT_LIMIT", "snapshot exceeds size limit")
            chunks = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    raise CapabilityError(
                        "SNAPSHOT_CHANGED", "file changed during snapshot"
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise CapabilityError(
                    "SNAPSHOT_CHANGED", "file changed during snapshot"
                )
            result[rel] = b"".join(chunks)
            total += before.st_size
        except OSError as exc:
            raise CapabilityError(
                "PATH_DENIED", "snapshot path cannot be opened safely"
            ) from exc
        finally:
            for opened in reversed(fds):
                os.close(opened)
    return result


def snapshot_manifest(snapshot: dict[str, bytes]) -> dict[str, str]:
    """Return per-file SHA-256 hashes for a validated snapshot."""
    return {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(snapshot.items())
    }
