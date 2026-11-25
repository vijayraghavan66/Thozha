# Opt-in local Wi-Fi companion gateway

This separate runner exposes a narrow, authenticated Android companion API over local HTTPS. The primary OpenJarvis API remains bound to `127.0.0.1:8000`; the Wi-Fi gateway proxies to that loopback API and serves only the explicitly declared companion routes. It does not enable Tailscale, a public relay, firewall changes, or remote shell access.

## State and trust

- The runner exits unless `JARVIS_COMPANION_WIFI_ENABLED=1` is explicitly set.
- It also requires an explicit Wi-Fi interface, exact SSID allowlist, and private interface bind address. It checks the live interface address and SSID before binding and stops if either changes.
- The runner and signed Wi-Fi QR issuer share one SSID reader. It reads the current network with `networksetup` first. If that utility reports that the Mac is not associated with an AirPort/Wi-Fi network, it may use `JARVIS_COMPANION_WIFI_SSID_HELPER` only when the variable names an absolute, regular, executable file. The helper receives exactly `--ssid <interface>` in a minimal environment, has a 3-second timeout, and must exit zero with exactly one valid SSID line (at most 32 UTF-8 bytes). The gateway fails closed on a missing/invalid helper, nonzero exit, timeout, malformed/multiline output, or an unknown `networksetup` response. SSIDs and command output are never logged. Manual or persistent owner-confirmation SSID overrides are not supported.
- It listens on HTTPS TCP 8443 at only that private interface address. It does not bind wildcard or public addresses.
- TLS terminates in a small standard-library proxy launched with system Python 3.14/OpenSSL 3.6 or newer. The OpenJarvis Python 3.13 process serves only the restricted companion ASGI gateway on `127.0.0.1:8001`; the TLS proxy forwards only to that fixed loopback port. The raw API remains on `127.0.0.1:8000`.
- The TLS frontend forwards WebSocket Upgrade as an ordinary encrypted TCP stream. The gateway’s task-event WSS route is `.../v1/jarvis/link/tasks/{idempotency_key}/events/ws`; it accepts only the paired-device Bearer header and negotiated `thozha.task-events.v1` subprotocol. The supervisor owner-checks the task on initial catch-up and each event page. The client resumes with its persisted `after_seq`, persists frames before ACK, and can reconnect after a bounded backpressure close; no long-lived event cursor is treated as server-side proof of delivery.
- A self-signed TLS certificate/private key are stored in the OpenJarvis config directory with private filesystem permissions. The Android device must first obtain the certificate fingerprint over its existing paired route and verify the Mac’s signed identity before persisting the pin. The bearer device token is sent only after the TLS pin verifies.
- A Wi-Fi QR is created only after the signed-invitation route verifies the current interface address, exact allowlisted SSID, expected hostname/port, and a bounded local TLS handshake that presents the exact persisted certificate fingerprint. A TCP listener with the wrong certificate cannot produce a QR or consume an invitation.
- The gateway validates active enrolled-device bearer tokens and forwards only allowlisted read-only Mac status, task, approval, and device-registration endpoints to the fixed loopback backend. The supervisor remains responsible for authorization and operation execution.
- DNS-SD advertises the local gateway. Discovery is a hint only; Android still requires the pinned hostname, certificate fingerprint, and authenticated API token.
- The Bonjour advertisement uses the proxy-registration form with the actual pinned `.local` hostname and configured interface IP as its SRV/A target. This is important: advertising the default host as `localhost` cannot be accepted by Android's pinned-host address check.
- The loopback-only `/v1/jarvis/local-gateway/address` endpoint signs the current private interface IP with the already-paired Mac key and a fresh nonce. Android stores this IP alongside the TLS pin in the same encrypted record and may use it only as a fallback when mDNS is unavailable. It still connects with HTTPS, verifies the pinned certificate and exact DNS SAN, and sends the device token only after TLS succeeds. If DHCP changes the IP and mDNS is blocked, refresh the signed address over the paired route.

## Manual start (not enabled by default)

After confirming that the Mac and paired phone are on the intended authorized home network, verify the interface and SSID from macOS Network settings. Then set the variables for a one-off process and run:

```sh
JARVIS_COMPANION_WIFI_ENABLED=1 \
JARVIS_COMPANION_WIFI_INTERFACE=en0 \
JARVIS_COMPANION_WIFI_ALLOWED_SSID='<exact home SSID>' \
JARVIS_COMPANION_WIFI_BIND_HOST='<private IP assigned to that interface>' \
JARVIS_COMPANION_WIFI_SSID_HELPER='/Applications/Thozha Setup.app/Contents/Helpers/ThozhaGatewaySupervisor' \
uv run python -m openjarvis.server.companion_wifi_gateway
```

Stop the process with Ctrl-C after the test. Do not use `0.0.0.0`, a public address, or an unverified/managed network. The gateway is not installed as a LaunchAgent and was not started during code verification. There is no consumer setup flow yet.

For a short diagnostic run only, `JARVIS_COMPANION_WIFI_DEBUG=1` makes the TLS frontend report exception classes for closed connections. It does not log peer addresses, paths, request bodies, authorization headers, tokens, or keys. Leave this unset in any persistent service.

## Verification state

The offline gateway and Wi-Fi identity test suites are in `tests/server/test_companion_gateway.py` and `tests/server/test_companion_wifi.py`. These validate token authorization, route/method allowlisting, request bounds, identity bootstrap/signature, TLS certificate generation and private-key modes, bind restrictions, and refusal of public/wildcard addresses. Passing these tests does not verify a real network path; that requires the Mac and Samsung on the same approved Wi-Fi, a verified certificate pin, and a request with ADB reverse absent.
