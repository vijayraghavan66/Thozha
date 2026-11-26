#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
exec "$ROOT/bin/_python.sh" "$ROOT/lib/portable_manager.py" doctor "$@"
