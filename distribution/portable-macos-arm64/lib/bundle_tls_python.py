#!/usr/bin/env python3
"""Bundle a self-contained Python 3.14/OpenSSL 3.6 runtime for TLS proxy use.

OpenJarvis runs under a managed CPython 3.13 runtime. The existing LAN TLS
proxy has a stricter 3.14/OpenSSL 3.6 gate. This builder copies the matching
Homebrew framework and all non-system dylibs it needs, rewrites load paths on
the copies, and verifies the result after relocation. It never modifies the
source Homebrew installation.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def run(args: list[str], *, check: bool = True, env=None) -> str:
    result = subprocess.run(args, check=check, capture_output=True, text=True, env=env)
    return result.stdout.strip()


def deps(binary: Path) -> list[str]:
    result = subprocess.run(["otool", "-L", str(binary)], check=False, capture_output=True, text=True)
    if result.returncode or not result.stdout:
        return []
    return [line.strip().split(" (", 1)[0] for line in result.stdout.splitlines()[1:] if line.strip()]


def rpaths(binary: Path) -> list[str]:
    result = subprocess.run(["otool", "-l", str(binary)], check=False, capture_output=True, text=True)
    lines = result.stdout.splitlines()
    values = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for item in lines[index + 1 : index + 5]:
            value = item.strip()
            if value.startswith("path "):
                values.append(value.split(" (offset", 1)[0][5:])
                break
    return values


def macho_candidates(root: Path) -> list[Path]:
    suffixes = {".so", ".dylib", ".bundle"}
    exact_names = {"Python", "python3.14"}
    return [p for p in root.rglob("*") if p.is_file() and (p.suffix in suffixes or p.name in exact_names)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-framework-version", type=Path, required=True)
    parser.add_argument("--openssl-prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source_version = args.python_framework_version.resolve()
    openssl = args.openssl_prefix
    if source_version.name != "3.14":
        raise SystemExit("The TLS frontend requires Python 3.14.")
    source_python = source_version / "bin/python3.14"
    source_python_app = source_version / "Resources/Python.app/Contents/MacOS/Python"
    if not source_python.is_file() or not source_python_app.is_file():
        raise SystemExit("The Homebrew Python 3.14 framework executables are incomplete.")
    version_text = run([str(source_python), "-c", "import sys; print('.'.join(map(str,sys.version_info[:3])))"])
    ssl_text = run([str(openssl / "bin/openssl"), "version"])
    if not version_text.startswith("3.14.") or not ssl_text.startswith("OpenSSL 3.6."):
        raise SystemExit("The source TLS runtime must be Python 3.14/OpenSSL 3.6 or newer.")

    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Refusing to overwrite a non-empty TLS runtime output.")
    output.mkdir(parents=True, exist_ok=True)
    source_framework = source_version.parents[1]
    target_framework = output / "Python.framework"
    if target_framework.exists():
        raise SystemExit("Refusing to overwrite an existing Python framework.")
    subprocess.run(["ditto", str(source_framework), str(target_framework)], check=True)

    source_binaries = macho_candidates(source_framework)
    source_root = source_framework.resolve()
    target_root = target_framework.resolve()
    mapping: dict[Path, Path] = {}
    binary_map: dict[Path, Path] = {}
    for source in source_binaries:
        resolved = source.resolve()
        try:
            target = target_root / resolved.relative_to(source_root)
        except ValueError:
            continue
        mapping[resolved] = target
        binary_map[resolved] = source

    external_root = target_framework / "Versions/3.14/lib/python3.14/lib-dynload/thozha-libs"
    external_root.mkdir(parents=True, exist_ok=True)
    pending = list(mapping)
    while pending:
        source_binary = pending.pop()
        for dependency in deps(binary_map[source_binary]):
            if dependency.startswith(("/System/", "/usr/lib/", "@rpath/", "@loader_path/", "@executable_path/")):
                continue
            candidate = Path(dependency)
            if not candidate.is_absolute() or not candidate.is_file():
                raise RuntimeError(f"Unresolved TLS runtime dependency: {candidate.name}")
            resolved = candidate.resolve()
            if resolved in mapping:
                continue
            try:
                target = target_root / resolved.relative_to(source_root)
            except ValueError:
                target = external_root / resolved.name
                if target.exists():
                    raise RuntimeError(f"TLS runtime dependency name collision: {resolved.name}")
                shutil.copyfile(resolved, target)
                os.chmod(target, resolved.stat().st_mode & 0o777)
                subprocess.run(["codesign", "--remove-signature", str(target)], check=False, capture_output=True)
            mapping[resolved] = target
            binary_map[resolved] = candidate
            pending.append(resolved)

    modified: set[Path] = set()
    for source_binary, source in binary_map.items():
        target_binary = mapping[source_binary]
        for dependency in deps(source):
            if dependency.startswith(("/System/", "/usr/lib/", "@rpath/", "@loader_path/", "@executable_path/")):
                continue
            resolved = Path(dependency).resolve()
            target_dependency = mapping.get(resolved)
            if target_dependency is None:
                raise RuntimeError(f"Unmapped TLS runtime dependency: {Path(dependency).name}")
            replacement = "@loader_path/" + os.path.relpath(target_dependency, target_binary.parent)
            subprocess.run(["install_name_tool", "-change", dependency, replacement, str(target_binary)], check=True, capture_output=True)
            modified.add(target_binary)
        for search_path in rpaths(source):
            if search_path.startswith(("/opt/homebrew/", "/usr/local/")):
                subprocess.run(["install_name_tool", "-delete_rpath", search_path, str(target_binary)], check=True, capture_output=True)
                modified.add(target_binary)

    # Dylib IDs are also load paths and must not retain Homebrew locations.
    for source_binary, source in binary_map.items():
        target_binary = mapping[source_binary]
        if (
            target_binary.suffix == ".dylib"
            or target_binary == target_framework / "Versions/3.14/Python"
        ):
            install_id = "@rpath/" + target_binary.name
            subprocess.run(["install_name_tool", "-id", install_id, str(target_binary)], check=True, capture_output=True)
            modified.add(target_binary)
    for binary in modified:
        subprocess.run(["codesign", "--remove-signature", str(binary)], check=False, capture_output=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(binary)], check=True, capture_output=True)

    python_app = target_framework / "Versions/3.14/Resources/Python.app"
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(python_app)], check=True, capture_output=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(python_app)], check=True, capture_output=True)

    launcher = output / "bin/python3.14"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        '#!/bin/sh\n'
        'ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"\n'
        'exec "$ROOT/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python" "$@"\n',
        encoding="utf-8",
    )
    os.chmod(launcher, 0o755)
    check = run([str(launcher), "-c", "import asyncio,hashlib,ssl,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} {ssl.OPENSSL_VERSION_INFO[0]}.{ssl.OPENSSL_VERSION_INFO[1]}')"])
    py_text, ssl_version_text = check.split()
    if tuple(int(x) for x in py_text.split("."))[:2] != (3, 14) or tuple(int(x) for x in ssl_version_text.split(".")) < (3, 6):
        raise SystemExit("Bundled TLS runtime failed its Python/OpenSSL minimum check.")
    # Audit every copied Mach-O load command and rpath for build-host paths.
    for binary in macho_candidates(target_framework) + list(external_root.glob("*.dylib")):
        if any(path.startswith(("/opt/homebrew/", "/usr/local/")) for path in deps(binary)):
            raise SystemExit(f"Bundled TLS runtime contains a Homebrew dependency: {binary.name}")
        if any(path.startswith(("/opt/homebrew/", "/usr/local/")) for path in rpaths(binary)):
            raise SystemExit(f"Bundled TLS runtime contains a Homebrew rpath: {binary.name}")
    print(f"TLS runtime verified: Python {py_text}, OpenSSL {ssl_version_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
