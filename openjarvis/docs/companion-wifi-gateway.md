# Opt-in local Wi-Fi companion gateway

This separate runner exposes a narrow, authenticated Android companion API over local HTTPS. The primary OpenJarvis API remains bound to `127.0.0.1:8000`; the Wi-Fi gateway proxies to that loopback API and serves only the explicitly declared companion routes. It does not enable Tailscale, a public relay, firewall changes, or remote shell access.

## State and trust

- The runner exits unless `JARVIS_COMPANION_WIFI_ENABLED=1` is explicitly set.
- It also requires an explicit Wi-Fi interface, exact SSID allowlist, and private interface bind address. It checks the live interface address and SSID before binding and stops if either changes.
- Some macOS privacy/runtime combinations report that no SSID is available to an unprivileged process even while the Wi-Fi card is connected. In that case, a **manual, owner-confirmed session** is supported only when `JARVIS_COMPANION_WIFI_OWNER_CONFIRMED_SSID` exactly matches the allowlist, `JARVIS_COMPANION_WIFI_OWNER_CONFIRMATION_UNTIL` is a Unix expiry no more than 10 minutes ahead, and macOS reports Wi-Fi power on plus link connected. This is an explicit attestation, not an automatic SSID match; the monitor also requires the same private interface IP and the session expires. Never persist these variables in a LaunchAgent.
- It listens on HTTPS TCP 8443 at only that private interface address. It does not bind wildcard or public addresses.
- A self-signed TLS certificate/private key are stored in the OpenJarvis config directory with private filesystem permissions. The Android device must first obtain the certificate fingerprint over its existing paired route and verify the Mac’s signed identity before persisting the pin. The bearer device token is sent only after the TLS pin verifies.
- The gateway validates active enrolled-device bearer tokens and forwards only allowlisted task, approval, and device-registration endpoints to the fixed loopback backend. The supervisor remains responsible for authorization and operation execution.
- DNS-SD advertises the local gateway. Discovery is a hint only; Android still requires the pinned hostname, certificate fingerprint, and authenticated API token.
- The Bonjour advertisement uses the proxy-registration form with the actual pinned `.local` hostname and configured interface IP as its SRV/A target. This is important: advertising the default host as `localhost` cannot be accepted by Android's pinned-host address check.
- The loopback-only `/v1/jarvis/local-gateway/address` endpoint signs the current private interface IP with the already-paired Mac key and a fresh nonce. Android stores this IP alongside the TLS pin in the same encrypted record and may use it only as a fallback when mDNS is unavailable. It still connects with HTTPS, verifies the pinned certificate and exact DNS SAN, and sends the device token only after TLS succeeds. If DHCP changes the IP and mDNS is blocked, refresh the signed address over the paired route.

## Manual start (not enabled by default)

After confirming that the Mac and paired phone are on the intended authorized home network, verify the interface and SSID from macOS Network settings. Then set the variables for a one-off process and run:

```sh
JARVIS_COMPANION_WIFI_ENABLED=1 \
JARVIS_COMPANION_WIFI_INTERFACE=en0 \
JARVIS_COMPANION_WIFI_ALLOWED_SSID='<exact home SSID>' \
JARVIS_COMPANION_WIFI_OWNER_CONFIRMED_SSID='<same exact home SSID>' \
JARVIS_COMPANION_WIFI_OWNER_CONFIRMATION_UNTIL="$(($(date +%s)+600))" \
JARVIS_COMPANION_WIFI_BIND_HOST='<private IP assigned to that interface>' \
uv run python -m openjarvis.server.companion_wifi_gateway
```

Stop the process with Ctrl-C after the test. Do not use `0.0.0.0`, a public address, or an unverified/managed network. The gateway is not installed as a LaunchAgent and was not started during code verification. There is no consumer setup flow yet.

## Verification state

The offline gateway and Wi-Fi identity test suites are in `tests/server/test_companion_gateway.py` and `tests/server/test_companion_wifi.py`. These validate token authorization, route/method allowlisting, request bounds, identity bootstrap/signature, TLS certificate generation and private-key modes, bind restrictions, and refusal of public/wildcard addresses. Passing these tests does not verify a real network path; that requires the Mac and Samsung on the same approved Wi-Fi, a verified certificate pin, and a request with ADB reverse absent.
