# Thozha Server for macOS Apple Silicon

This is a portable ZIP distribution. It is not a Thozha macOS application installer.
It contains a managed Python 3.13 server runtime, its production dependencies,
the Web UI, the Codex CLI runtime, and a separate Python 3.14/OpenSSL 3.6 TLS
runtime for the restricted companion gateway. No global Python or Node
installation is needed to run the extracted bundle.

The upstream CPython TLS runtime is distributed as a Python.framework and
includes its standard `Python.app` executable launcher internally. This is a
runtime component, not a Thozha application or installer; the ZIP has no
Thozha `.app`, `.pkg`, or Store package.

## Start

1. Extract the ZIP to a folder you own, such as `~/Applications/Thozha Server`.
2. Open Terminal and change into that folder.
3. Run `./bin/doctor.sh`.
4. Run `./bin/login.sh` and complete the official Codex sign-in in the browser.
   The sign-in is stored only in this bundle's `data/codex-home` directory.
5. Run `./bin/start.sh` and keep that Terminal window open.
6. Open the loopback URL printed by the server, normally
   `http://127.0.0.1:8000`.

The first start prints a one-time Web password. Save it in a password manager;
the server stores only its password hash. Logs, tasks, configuration,
certificates, and credentials are created under `data/` with owner-only
permissions. Do not share or publish the `data/` directory.

## Local-only and gateway

For a loopback-only session, run `./bin/start.sh --local-only`. The raw API and
Web UI bind only to `127.0.0.1`; no LAN listener or discovery is started.

LAN pairing is disabled by default. The portable instance uses its own
restricted gateway pair: loopback `127.0.0.1:8002` and TLS `8444`. This lets a
previous Thozha gateway on `8001/8443` remain available during a portable test.
To enable it, use the interactive first-run prompt while on the approved
Wi-Fi network. The gateway verifies the exact SSID and interface, creates a
server TLS identity in the private data folder, and binds only to that
interface's current private IPv4 address. It preserves the existing enforced
SSID policy and never exposes the raw supervisor API.
If macOS cannot verify the SSID or the policy changes, the gateway fails closed.
Do not bypass that check. QR pairing remains subject to device proof and the
normal owner approval flow.

The ZIP does not include credentials, a device pairing, TLS keys, task databases,
or user data. A first-run server identity is created under `data/config/` and
preserved on later starts. The TLS certificate is created only when the owner
enables the restricted gateway. Keep backups encrypted and private.

## Stop, status, and diagnostics

- `./bin/status.sh` reports whether this bundle is healthy.
- `./bin/stop.sh` requests a graceful shutdown and waits for the managed
  processes to exit.
- `./bin/doctor.sh` checks the bundled runtime, Web package, Codex SDK/CLI,
  platform, data-root separation, and account sign-in without printing secrets.
- `./bin/login.sh` can be rerun to manage Codex authentication through the
  bundled CLI.

The server runs in the foreground; Ctrl-C performs graceful shutdown. If a
child exits unexpectedly, the manager attempts up to three bounded restarts,
then exits with a diagnostic. Logs are under `data/logs/`. A forced `kill -9`
of the manager cannot run cleanup; `status.sh` reports stale/orphaned state and
`stop.sh` cleans up only child processes whose executable matches this bundle.
Do not delete `data/run` while a process is active.

## macOS security

This archive is not notarized or signed as a Developer ID application. macOS
may show its standard downloaded-file warning. Do not disable Gatekeeper or
remove quarantine attributes to work around it. If local policy blocks a
component, stop and ask the administrator to approve a supported distribution
path. No root privileges, LaunchAgent, firewall change, public bind, or MDM
change is required by this bundle.

## Compatibility and limitations

- Target: macOS on Apple Silicon (arm64).
- The raw API/Web server is loopback-only.
- The paired-device HTTPS/WSS gateway is a separate restricted listener and
  requires verified SSID policy, the existing supervisor authorization, and
  available ports 8001/8443.
- This ZIP is isolated from other Thozha installations. It does not import or
  alter an existing OpenJarvis home, Android pairing, task history, or VM.
- Windows and Linux archives are not included.
- `./bin/update.sh` is intentionally not included: a safe update and rollback
  protocol that migrates this bundle's databases has not been implemented.

## Build provenance

The release builder is maintained in `distribution/portable-macos-arm64` in
the source repository. The published ZIP should be accompanied by a SHA-256
checksum and the full source commit used to build it.

Third-party Python package license files and their distribution metadata are
copied into `THIRD-PARTY-LICENSES/` by the release builder.

Third-party Python package license files and their distribution metadata are
copied into `THIRD-PARTY-LICENSES/` by the release builder.
