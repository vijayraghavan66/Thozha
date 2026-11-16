#!/usr/bin/env bash
# Rebuilds a Maven-layout repository from Gradle's local artifact store (~/.gradle/caches/modules-2/files-2.1).
#
# Why: on the build network repo.maven.apache.org returns 404 for every artifact (checked with
# junit:junit:4.13.2) and plugins.gradle.org redirects to it, so Gradle cannot re-fetch Kotlin/AGP
# artifacts that earlier builds downloaded. This copies only what Gradle already has, with no network.
# Output is outside the repository (default ~/.jarvis-link-maven-mirror) and is never committed.
set -euo pipefail
SRC="${GRADLE_USER_HOME:-$HOME/.gradle}/caches/modules-2/files-2.1"
DST="${1:-$HOME/.jarvis-link-maven-mirror}"
[ -d "$SRC" ] || { echo "no Gradle artifact store at $SRC" >&2; exit 1; }
rm -rf "$DST"; mkdir -p "$DST"
count=0
# files-2.1/<group>/<artifact>/<version>/<sha1>/<file>
find "$SRC" -mindepth 4 -type f | while IFS= read -r f; do
  rel="${f#"$SRC"/}"                       # group/artifact/version/sha1/file
  IFS=/ read -r group artifact version _sha file <<<"$rel"
  mkdir -p "$DST/$(echo "$group" | tr . /)/$artifact/$version"
  cp -n "$f" "$DST/$(echo "$group" | tr . /)/$artifact/$version/$file"
done
echo "mirror written to $DST ($(find "$DST" -type f | wc -l | tr -d ' ') files)"
