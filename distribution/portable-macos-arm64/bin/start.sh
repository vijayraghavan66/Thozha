#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
exec "$ROOT/runtime/server-venv/bin/python" "$ROOT/lib/portable_manager.py" start "$@"
