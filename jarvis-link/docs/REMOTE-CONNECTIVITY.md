# Jarvis Link remote connectivity checkpoint

## Current remote path

```text
Vivo on mobile data
  -- device-signed HTTPS + E2E encrypted envelope --> Cloudflare Worker
  -- tenant-isolated opaque queue --> Durable Object
  <-- outbound HTTPS/WSS, host-signed -- Mac Link host
  -- paired Mac API key on loopback only --> OpenJarvis / Codex SDK
```

The Worker routes by tenant and the Durable Object stores ciphertext plus delivery metadata. It does not receive the Mac API key, signing private keys, plaintext task objective, approval arguments, or a shell endpoint. The host makes outbound-only HTTPS/WSS connections; it has no inbound listener. The host's API adapter accepts only the fixed OpenJarvis loopback origin and reads the Mac's supervisor key locally.

The Android protocol uses the existing ECDSA P-256 device identity, ECDH P-256, HKDF-SHA256, and AES-256-GCM envelope format. Unique IDs, timestamps, signatures, per-device request nonces, queue expiry, ACKs, device revocation, and task/idempotency keys are enforced. An `assistant` task may request a Mac MCP action, but Codex's handler persists an exact-operation approval and does not call the Mac MCP tool until the supervisor atomically consumes the approved decision. The remote host then forwards the phone's continuation objective over loopback; OpenJarvis checks its digest against the stored task before continuing.

## Code and verification

- `relay/` contains the Worker and per-tenant Durable Object. It is configured for local simulation only.
- `host/src/jarvis_link_host/openjarvis_api_adapter.py` forwards structured requests to OpenJarvis over 127.0.0.1 and keeps the supervisor API key out of relay traffic.
- `host/src/jarvis_link_host/response_outbox.py` persists only encrypted response envelopes before relay delivery. A retry after network loss or host restart resends the same replies; the idempotent supervisor prevents duplicate task/decision execution.
- `host/scripts/serve_openjarvis.py` is a manual outbound-only host runner. It is not installed or started as a launch agent.
- The Python relay/host tests and Kotlin protocol checks are run by `scripts/run-all-tests.sh`. The latest root-agent results must be read from `PROGRESS.md` before any release claim.

### Isolated worktree verification (2026-11-16)

- Host and integration suite: **68 passed** using local `wrangler dev --local`; this includes message replay, revoked devices, expired messages, cross-tenant isolation, and lost reply delivery recovered after a host restart.
- Relay Worker/Durable Object suite: **2 passed**.
- Companion Kotlin protocol harness: compiled from the isolated `companion/` source and **passed** against the shared deterministic envelope fixture.
- Host sources and scripts passed `compileall`; `serve_openjarvis.py --help` was checked without starting the host runner.
- No Cloudflare deployment, Firebase send, Android UI build, ADB/Vivo operation, VPN install, firewall change, or MDM/network change occurred in this workstream.

## Not configured or verified

- No Cloudflare account, Worker deployment, custom domain, production route, secret, or billing plan is configured. Mobile-data operation has not been tested.
- The host runner requires an owner-approved HTTPS Worker origin and a separately bootstrapped production host identity/certificate. No production bootstrap secret or identity was created here.
- The current host state under `~/.jarvis-link-test/` is explicitly test-only. A production tenant and host identity must be provisioned separately; never repurpose or overwrite the test/paired identities silently.
- The main Android Home UI currently uses the authenticated Mac API directly when USB/local gateway is reachable. Wiring that UI to a production relay URL is a separate integration step; no relay selector or production endpoint is configured in the app.
- Firebase project ID is present in `android-companion/app/google-services.json`, but this Mac has no ADC file, service-account file, `gcloud`, or available Google credentials. Push delivery and real mobile-data notification receipt are not verified.
- The Mac is MDM-managed and the Vivo previously reported `no_config_vpn`. No VPN software, firewall rule, Tailscale, Funnel, port-forward, or MDM change was performed. Do not test across networks until the organization's outbound-cloud policy and the required endpoint are authorized.

## Minimum authorized setup for a later remote test

1. Obtain organizational authorization for outbound Cloudflare Worker/WSS traffic from the managed Mac and mobile app traffic to the relay. No VPN is required by this transport; do not install one or alter firewall policy.
2. In the owner's Cloudflare account, create a Workers Free project, bind the SQLite Durable Object namespace, configure a high-entropy one-time host bootstrap secret as a Worker secret, and deploy only after the owner explicitly authorizes that external deployment. Keep free-tier billing limits in place.
3. Create a new production tenant/host identity outside the repo. Never commit relay secrets or private keys. Enroll/certify the Vivo through the existing host approval step without clearing the Mac API pairing.
4. On the Mac, start `serve_openjarvis.py` with the authorized HTTPS Worker origin, the new host state path, and a local supervisor key file. The API key must stay on the Mac and be sent only to `127.0.0.1:8000`.
5. On the Android app, use the configured HTTPS relay origin and the existing Keystore device identity. The app sends an encrypted envelope; the host's reply and approvals are independently encrypted to the companion.
6. For live FCM, the only currently missing Google step is ADC authorization: install/obtain the approved Google Cloud CLI and run `gcloud auth application-default login --scopes=https://www.googleapis.com/auth/firebase.messaging,https://www.googleapis.com/auth/cloud-platform` as the project owner. Do not create or place a service-account key in the APK or repository.
7. Validate on genuinely separate networks: host Wi-Fi/ethernet and Vivo mobile data with USB disconnected. Verify a real assistant result, approval notification/tap, task continuation, rejection, duplicate replay, expiry, revocation, network interruption/reconnect, and app/host restart. No step above is claimed complete until this run succeeds.
