#!/bin/bash
set -euo pipefail

SOURCE_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)"
DIST_SOURCE="$SOURCE_ROOT/distribution/portable-macos-arm64"
OUTPUT_DIR="${1:-}"
if [[ -z "$OUTPUT_DIR" ]]; then
  echo "Usage: $0 /absolute/output/directory" >&2
  exit 2
fi
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
  echo "Build this archive on macOS Apple Silicon." >&2
  exit 2
fi
if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain)" ]]; then
  echo "Refusing to package uncommitted source. Commit and review changes first." >&2
  exit 2
fi
command -v uv >/dev/null || { echo "uv is required on the build host." >&2; exit 2; }
command -v npm >/dev/null || { echo "Node/npm is required only to build the Web assets." >&2; exit 2; }
command -v zip >/dev/null || { echo "zip is required on the build host." >&2; exit 2; }
command -v rg >/dev/null || { echo "ripgrep is required for the release secret-pattern scan." >&2; exit 2; }

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
ARCHIVE="$OUTPUT_DIR/thozha-server-macos-arm64.zip"
if [[ -e "$ARCHIVE" ]]; then
  echo "Refusing to overwrite existing archive: $ARCHIVE" >&2
  exit 2
fi
WORK="$(mktemp -d "${TMPDIR:-/tmp}/thozha-portable-build.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/thozha-server-macos-arm64"
mkdir -p "$STAGE/bin" "$STAGE/lib" "$STAGE/runtime" "$STAGE/app" "$STAGE/data" "$STAGE/wheels"

VERSION="$(git -C "$SOURCE_ROOT" rev-parse --short=12 HEAD)"
cp "$DIST_SOURCE/README.md" "$STAGE/README.md"
cp "$DIST_SOURCE/lib/portable_manager.py" "$STAGE/lib/portable_manager.py"
cp "$DIST_SOURCE/bin/"*.sh "$STAGE/bin/"
chmod 755 "$STAGE/bin/"*.sh

# Build Web assets once and embed them in the Python package before wheel build.
(cd "$SOURCE_ROOT/web" && npm run build)
mkdir -p "$WORK/source/openjarvis/src/openjarvis/server/static"
git -C "$SOURCE_ROOT" archive HEAD openjarvis | tar -x -C "$WORK/source"
cp -R "$SOURCE_ROOT/web/dist/." "$WORK/source/openjarvis/src/openjarvis/server/static/"

export UV_PYTHON_INSTALL_DIR="$STAGE/runtime/managed-python"
export UV_NO_PROGRESS=1
uv python install --install-dir "$UV_PYTHON_INSTALL_DIR" 3.13.15
SERVER_PYTHON="$(find "$UV_PYTHON_INSTALL_DIR" -type f -path '*/bin/python3.13' -print -quit)"
if [[ -z "$SERVER_PYTHON" ]]; then
  echo "uv did not install the expected managed Python 3.13 runtime." >&2
  exit 2
fi
uv venv --relocatable --python "$SERVER_PYTHON" "$STAGE/runtime/server-venv"
export VIRTUAL_ENV="$STAGE/runtime/server-venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"
uv sync --project "$WORK/source/openjarvis" --active --no-install-project --no-dev --extra server --extra codex-sdk --frozen
SETUPTOOLS_SCM_PRETEND_VERSION="0.1.0+portable.$VERSION" uv build --project "$WORK/source/openjarvis" --wheel --out-dir "$STAGE/wheels"
WHEEL="$(find "$STAGE/wheels" -maxdepth 1 -name '*.whl' -print -quit)"
if [[ -z "$WHEEL" ]]; then
  echo "OpenJarvis wheel build produced no wheel." >&2
  exit 2
fi
uv pip install --python "$STAGE/runtime/server-venv/bin/python" "$WHEEL"
rm -rf "$STAGE/wheels"

PYTHON314="$(brew --prefix python@3.14 2>/dev/null)/Frameworks/Python.framework/Versions/3.14"
OPENSSL36="$(brew --prefix openssl@3.6 2>/dev/null)"
if [[ ! -x "$PYTHON314/bin/python3.14" || ! -x "$OPENSSL36/bin/openssl" ]]; then
  echo "Homebrew Python 3.14 and OpenSSL 3.6 are required to build the restricted TLS runtime." >&2
  exit 2
fi
python3 "$DIST_SOURCE/lib/bundle_tls_python.py" \
  --python-framework-version "$PYTHON314" \
  --openssl-prefix "$OPENSSL36" \
  --output "$STAGE/runtime/tls-python"

mkdir -p "$STAGE/data/config" "$STAGE/data/openjarvis" "$STAGE/data/codex-home" \
  "$STAGE/data/agent-evaluation/taskflow" "$STAGE/data/logs" "$STAGE/data/run"
chmod 700 "$STAGE/data" "$STAGE/data/"* "$STAGE/data/"*/ 2>/dev/null || true
mkdir -p "$STAGE/THIRD-PARTY-LICENSES"
cp "$WORK/source/openjarvis/LICENSE" "$STAGE/THIRD-PARTY-LICENSES/OpenJarvis-LICENSE.txt"
"$STAGE/runtime/server-venv/bin/python" - "$STAGE" <<'PY'
from importlib.metadata import distributions
from pathlib import Path
import re
import shutil
import sys

root = Path(sys.argv[1])
licenses = root / "THIRD-PARTY-LICENSES"
rows = ["# Bundled Python distributions", "", "License files are copied from the installed wheel metadata where available.", ""]
for dist in sorted(distributions(), key=lambda item: (item.metadata.get("Name") or "").lower()):
    name = dist.metadata.get("Name") or "unknown"
    version = dist.version
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{name}-{version}")
    license_text = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See the bundled files or the package project metadata."
    rows.append(f"- {name} {version}: {license_text}")
    for item in dist.files or ():
        if item.parts and (item.parts[0].lower() == "licenses" or re.fullmatch(r"(?i)(license|copying|notice)(\..*)?", item.name)):
            source = Path(dist.locate_file(item))
            if source.is_file():
                target = licenses / safe / Path(*item.parts[-2:])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
(licenses / "DISTRIBUTIONS.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
PY
SOURCE_SHA="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
printf 'source_commit=%s\nplatform=macos-arm64\n' "$SOURCE_SHA" > "$STAGE/BUILD-MANIFEST.txt"
if find "$STAGE" -type f \( -name 'auth.json' -o -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \
  -o -name '*.key' -o -name '*.p12' -o -name '*.pfx' -o -name '*.jks' -o -name '*.keystore' \
  -o -name '.env' -o -name '.env.*' -o -name '*.log' \) -print -quit | grep -q .; then
  echo "Refusing to package credentials, private keys, databases, or logs." >&2
  exit 2
fi
SECRET_MATCHES="$(rg -n --hidden --glob '!*.so' --glob '!*.dylib' --glob '!*.onnx' \
  '(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----)' \
  "$STAGE" 2>/dev/null || true)"
# OpenJarvis ships a documented synthetic credential string in its security
# scanner benchmark fixture. Exempt only that exact source line; all other
# matches remain fail-closed and are printed for investigation.
SECRET_MATCHES="$(printf '%s\n' "$SECRET_MATCHES" | rg -v 'security_scanner\.py:.*sk-proj-abc123def456ghi789' || true)"
if [[ -n "$SECRET_MATCHES" ]]; then
  echo "Refusing to package a file that matches a credential/private-key pattern:" >&2
  printf '%s\n' "$SECRET_MATCHES" >&2
  exit 2
fi
(
  cd "$WORK"
  COPYFILE_DISABLE=1 zip -q -r "$ARCHIVE" thozha-server-macos-arm64
)
shasum -a 256 "$ARCHIVE" > "$ARCHIVE.sha256"
echo "Built: $ARCHIVE"
echo "SHA-256: $(awk '{print $1}' "$ARCHIVE.sha256")"
echo "Source commit: $VERSION"
