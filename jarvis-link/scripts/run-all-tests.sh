#!/usr/bin/env bash
# Runs every Jarvis Link suite locally. Nothing is deployed; relay runs in local workerd only.
# Optional: KOTLINC=/path/to/kotlinc and JAVA17_HOME=/path/to/jdk17 (Gradle is used if KOTLINC is unset).
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== relay (node:test, in-memory Durable Object harness) =="; (cd relay && npm test)
echo "== host + relay integration (pytest, real workerd via wrangler dev --local) =="
(cd host && { [ -d .venv ] || python3 -m venv .venv; } && .venv/bin/pip install -q -e '.[test]' && .venv/bin/python -m pytest -q)
echo "== companion (Kotlin, JDK 17) =="
if [ -n "${KOTLINC:-}" ]; then
  export JAVA_HOME="${JAVA17_HOME:?set JAVA17_HOME}" PATH="${JAVA17_HOME}/bin:$PATH"
  jar="$(mktemp -d)/companion-test.jar"
  (cd companion && "$KOTLINC" src/main/kotlin src/test/kotlin -include-runtime -d "$jar" && java -cp "$jar" com.jarvis.link.CompanionProtocolTest ../fixtures/envelope-v1.test.json)
else
  (cd companion && gradle verifyCompanionProtocol -Pjarvis.fixture=../fixtures/envelope-v1.test.json)
fi
