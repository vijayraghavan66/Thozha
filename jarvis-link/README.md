# Jarvis Link v1

A self-hostable, end-to-end encrypted relay prototype connecting Jarvis hosts and companion clients. This workspace is separate from the existing OpenJarvis checkout and Android app.

## Safety boundary

- The relay stores public device keys, opaque delivery metadata, and encrypted payloads. It never receives the Mac API key, Codex credentials, tool arguments in plaintext, or a shell endpoint.
- The host makes outbound-only relay connections. It dispatches decrypted task envelopes only through a narrow supervisor adapter; it has no remote shell command handler.
- The Worker is configured for local simulation by default. It has no account ID, route, custom domain, `workers.dev` URL, production secret, or deploy step.
- No Worker was deployed and no Cloudflare account was modified.

## Workspace

- `protocol/v1.md` — versioned wire protocol and cryptographic inputs.
- `relay/` — Cloudflare Worker and SQLite-backed Durable Object.
- `host/` — outbound host client and supervisor adapter.
- `companion/` — client transport and simulation.
- `docs/` — deployment, costs, security, and recovery notes.
- `fixtures/` — public test keys and deterministic envelopes; no production private keys.

Run all components locally first. The cloud deployment requires an explicit Cloudflare account and deployment step; this checkpoint does not configure one.

## Running the tests

`scripts/run-all-tests.sh` runs the relay, host/integration (real local workerd), and companion suites. Set `KOTLINC` and `JAVA17_HOME` to use a standalone Kotlin compiler instead of Gradle. Nothing is deployed.
