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

## Remote host integration

`host/scripts/serve_openjarvis.py` is the outbound-only host runner for a configured relay. It connects to the Worker over HTTPS/WSS, then forwards decrypted task and approval data only to the fixed OpenJarvis loopback API at `127.0.0.1:8000`. The supervisor API key is read locally from `~/.openjarvis/supervisor_api_key` or the local OpenJarvis config; it is never included in a relay request or envelope.

The runner is not started automatically and the Cloudflare Worker is not deployed. See `docs/REMOTE-CONNECTIVITY.md` for the local wiring and the exact external configuration still required.

Run all components locally first. The cloud deployment requires an explicit Cloudflare account and deployment step; this checkpoint does not configure one.

## Running the tests

`scripts/run-all-tests.sh` runs the relay, host/integration (real local workerd), and companion suites. In an isolated Jarvis Lab worktree, point `ANDROID_COMPANION` at the shared app checkout and `JARVIS_LINK_HOST_PYTHON` at an existing host test interpreter; `JAVA_HOME` selects the JDK. The script is local-only and never deploys.
