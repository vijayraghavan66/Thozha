#!/bin/bash
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/package-staging.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/thozha-package-safety.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

mkdir "$TMP/existing-output"
printf 'keep-me\n' > "$TMP/existing-output/user-file.txt"
if "$SCRIPT" "$TMP/existing-output" >"$TMP/stdout" 2>"$TMP/stderr"; then
  echo "expected the script to reject an existing output directory" >&2
  exit 1
fi
[[ "$(cat "$TMP/existing-output/user-file.txt")" == "keep-me" ]]
grep -q "Refusing to reuse an existing output path" "$TMP/stderr"

mkdir "$TMP/symlink-target"
printf 'keep-symlink-target\n' > "$TMP/symlink-target/marker"
ln -s "$TMP/symlink-target" "$TMP/output-link"
if "$SCRIPT" "$TMP/output-link" >"$TMP/stdout" 2>"$TMP/stderr"; then
  echo "expected the script to reject an existing output symlink" >&2
  exit 1
fi
[[ -L "$TMP/output-link" ]]
[[ "$(cat "$TMP/symlink-target/marker")" == "keep-symlink-target" ]]

echo "package staging refuses existing directories and symlinks without changing their contents"
