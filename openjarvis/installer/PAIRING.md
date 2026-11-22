# Mac pairing UI contract

## Local authorization

The Connect Phone screen first performs a read-only `GET http://127.0.0.1:8000/health`. Only after the user selects **Create signed Wi-Fi QR** or **Create signed USB QR** does it call `POST http://127.0.0.1:8000/v1/jarvis/pair/invitations`.

The current backend handler uses its loopback-peer, loopback-Host and allowlisted-local-Origin checks. The client sends `Origin: http://tauri.localhost`; the route does not require an API-key bearer. The app never reads the LaunchAgent environment, OpenJarvis config, `supervisor_api_key`, Keychain, or any pairing identity. No credential is included in requests. The invitation signing private key remains in OpenJarvis.

Request bodies are explicit and distinct:

- Wi-Fi (default): exact UTF-8 JSON `{"transport":"wifi"}` with `Content-Type: application/json`.
- USB / ADB fallback: POST with no body and no content type, preserving the legacy v1 invitation.

Both use the same loopback endpoint. HTTP 409 for Wi-Fi is shown as “secure Wi-Fi pairing is disabled or not ready”; the UI displays no QR and offers a user-selected USB fallback. Other HTTP errors are not converted into success or retried automatically.

## Signed response validation

Before QR rendering, the Mac client requires the response wrapper and nested `qr_payload` to have exactly matching fields. It checks the exact v1 or v2 field set, verifies the server-key SHA-256 fingerprint, verifies the P-256 ECDSA/SHA-256 signature over Python-compatible canonical UTF-8 JSON, rejects expired or overlong lifetimes, and enforces an 8 KiB QR size limit. The invitation challenge is never logged or persisted.

V1 USB invitations retain `openjarvis-pairing-v1` and the loopback server URL. The screen explains that they require a working USB/ADB reverse route.

V2 Wi-Fi invitations require:

- `protocol`: `openjarvis-pairing-v2`;
- `server_url`: exactly `https://<single-label>.local:8443`;
- `gateway_address`: RFC1918 private IPv4;
- `gateway_port`: string `8443`;
- `gateway_certificate_sha256`: `sha256:` plus 64 lowercase hexadecimal characters;
- a valid signature and matching server-key fingerprint covering all fields.

The UI displays the signed private IPv4 address, `.local` TLS host, certificate pin and server-key fingerprint. The Android device must connect to that signed IP while validating TLS/SNI against the `.local` hostname and the certificate pin. A malformed or unsigned Wi-Fi payload never produces a QR.

Refresh asks before creating another invitation while the current one is valid, because the older invitation remains valid until expiry. Closing only hides the QR; it does not revoke the invitation. Failed calls are not retried automatically because the Mac may have created an invite before a response was lost.

## Local pending-device confirmation

The **Pending devices** screen performs a read-only GET to `/v1/jarvis/pair/pending` every five seconds while open. It displays only the backend's safe review fields: device name, device public-key fingerprint, request time and expiry. It does not display phone public keys, pairing challenges or credentials.

Only a direct owner tap on **Confirm** or **Reject** begins a decision. After that tap, the UI re-reads the pending row and checks that the ID, name, fingerprint and expiry still match before POSTing exactly `{"decision":"approve"}` or `{"decision":"reject"}` to `/v1/jarvis/pair/{id}/decision`. The backend's loopback Host and local UI Origin guard is used; no bearer key is read or sent. Expired, changed, missing and already-decided rows are reported without treating a UI state as a successful decision. The list refreshes after the result. Confirming the phone only allows it to complete the one-time token claim; it does not grant development allowlist membership or approve any task.

Tests use a fake transport and verify there is no decision POST from listing/polling, exact request method/path/body/Origin, expiry/replay errors, and safe device fields. No production endpoint or device decision was exercised.

## Paired-phone management

The **Paired phones** screen calls the loopback `GET /v1/jarvis/pair/devices` endpoint and displays only device ID, sanitized name, public-key fingerprint, created/expiry timestamps and server-reported status. It never retrieves or displays a raw public key or bearer token. Revocation is available only for a server-reported active row. Clicking **Revoke** opens a confirmation dialog; only its explicit confirmation starts a fresh GET and compares ID, name, fingerprint, creation/expiry and status before POSTing exactly `{"confirm":true}` to `/v1/jarvis/pair/devices/{id}/revoke`.

HTTP 404/409 are reported as missing/conflicting state. HTTP 503 on the revoke endpoint is reported distinctly: the backend may already have revoked device access while FCM cleanup remains pending. The screen refreshes afterward and does not represent the 503 as rollback or successful cleanup.

Fake-transport tests verify request shapes, no revoke POST from a list read, changed/expired/revoked row refusal, and the post-revocation 503 outcome. No production device list or revocation request was made.

## Verification boundary

Swift tests use fixed v1 and v2 signed fixtures, plus fake transports; they do not call the production invitation endpoint, enable the gateway, enroll a device, or mutate pairing state. The test-only v2 signer was ephemeral and its private key was not retained.

The real gateway remains disabled until separately authorized/configured and verified. No live phone pairing or Wi-Fi request has been performed by this UI workstream.
