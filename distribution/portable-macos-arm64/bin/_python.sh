#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
CONFIG="$ROOT/runtime/server-venv/pyvenv.cfg"
BASE="$ROOT/runtime/managed-python/cpython-3.13.15-macos-aarch64-none/bin"
if [ ! -x "$BASE/python3.13" ] || [ ! -f "$CONFIG" ]; then
  echo "Thozha portable Python runtime is incomplete; run ./bin/doctor.sh." >&2
  exit 2
fi
CURRENT="$(awk -F ' = ' '$1 == "home" { print $2; exit }' "$CONFIG")"
if [ "$CURRENT" != "$BASE" ]; then
  TEMP="$CONFIG.tmp.$$"
  umask 077
  if ! awk -v home="$BASE" 'BEGIN { updated = 0 } /^home = / { print "home = " home; updated = 1; next } { print } END { if (!updated) exit 2 }' "$CONFIG" > "$TEMP"; then
    rm -f "$TEMP"
    echo "Thozha could not repair the extracted Python environment path." >&2
    exit 2
  fi
  chmod 600 "$TEMP"
  mv -f "$TEMP" "$CONFIG"
fi
exec "$ROOT/runtime/server-venv/bin/python" "$@"
