#!/usr/bin/env bash
# Reproduces every Jarvis Link check. Nothing is deployed: relay runs in local workerd (wrangler dev --local).
# Requirements: node, python3.11+, JDK 17 (JAVA_HOME or /opt/homebrew/opt/openjdk@17), the Android project
# (../android-companion) buildable offline, and the local Maven mirror (scripts/build-local-maven-mirror.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ANDROID="${ANDROID_COMPANION:-$ROOT/../android-companion}"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home}"
export PATH="$JAVA_HOME/bin:$PATH"
cd "$ROOT"

echo "== relay: node:test against the in-memory Durable Object harness =="
(cd relay && npm test)

echo "== android: build Kotlin transport + CLI jar + JVM protocol checks (offline Gradle) =="
(cd "$ANDROID" && ./gradlew --console=plain --offline :app:linkCompanionCliJar :app:verifyLinkProtocol)

echo "== host: simulator, Python/Kotlin interop, and real-workerd integration =="
if [ -n "${JARVIS_LINK_HOST_PYTHON:-}" ]; then
  HOST_PYTHON="$JARVIS_LINK_HOST_PYTHON"
else
  (cd host && { [ -x .venv/bin/python ] || python3 -m venv .venv; } && .venv/bin/pip install -q -e '.[test]')
  HOST_PYTHON="$ROOT/host/.venv/bin/python"
fi
if [ ! -x "$HOST_PYTHON" ]; then
  echo "JARVIS_LINK_HOST_PYTHON is not an executable Python interpreter" >&2
  exit 2
fi
(cd host && \
  PYTHONPATH="$ROOT/host/src${PYTHONPATH:+:$PYTHONPATH}" \
  JARVIS_LINK_CLI_JAR="$ANDROID/app/build/libs/jarvis-link-cli.jar" JARVIS_LINK_JAVA="$JAVA_HOME/bin/java" \
  "$HOST_PYTHON" -m pytest -q)
