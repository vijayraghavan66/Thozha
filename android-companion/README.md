# Jarvis Companion

Native Android client for the Mac-side Jarvis supervisor. It displays tasks and exact-operation approval requests. Pairing uses a signed QR invitation and requires confirmation in the Mac's local pending-device UI before the phone can claim an expiring token.

**Current status:** QR pairing and task/approval screens compile. The Android client does not expose a server, send a permanent API key, or enable a public relay. Firebase push and local approval notifications are not implemented.

## Build and install

Build the debug APK:

    ./scripts/build_debug.sh

APK output:

    app/build/outputs/apk/debug/app-debug.apk

Install with an Android device connected by USB:

    adb install -r app/build/outputs/apk/debug/app-debug.apk
    adb shell monkey -p com.jarvis.companion.debug 1

Do not clear app data during an in-place update. Pairing has not been exercised against the live Mac API in this build.

## Screens

- **Tasks:** active task list and current step.
- **Approvals:** pending operations that require a user decision.
- **Activity:** recent task events and task timelines.
- **Done:** completed tasks.
- **Device:** scan a signed Mac invitation QR, inspect server and phone-key fingerprints, request enrollment, and watch for Mac confirmation.
- **Details:** task event timeline or approval review with exact operation, target, canonical arguments, expiry, server risk label, and full operation digest.

## QR pairing API contract

The Mac's local UI creates the invitation. Android never calls the invitation-creation endpoint and never receives the Mac master API key.

- The Mac local UI calls POST /v1/jarvis/pair/invitations over loopback with its local authorization.
- The QR JSON contains protocol, server_url, server_fingerprint, server_public_key, invitation_id, challenge, expires_at, and server_signature.
- server_public_key is Base64 DER P-256 SubjectPublicKeyInfo. server_fingerprint is sha256 plus the lowercase SHA-256 hex digest of those DER bytes.
- server_signature is ECDSA P-256/SHA-256 over compact UTF-8 JSON with lexicographically sorted keys, including protocol and server_public_key and excluding server_signature. The app verifies the signature and fingerprint before showing the invitation.
- POST /v1/jarvis/pair/complete sends invitation_id, device_name, public_key, and an ECDSA/SHA-256 signature over the raw UTF-8 invitation challenge. It returns pending confirmation, not a token.
- GET /v1/jarvis/pair/status/{invitation_id} returns pending, rejected, expired, or approved with a fresh claim_challenge. It never returns the token.
- Only after the user confirms the device in the Mac's local pending-device UI, Android signs claim_challenge and sends POST /v1/jarvis/pair/claim with invitation_id and signature. The one-time expiring device token is encrypted with Android Keystore.

The Mac pending-device screen should show the Android device name and key fingerprint and require a local Confirm or Reject action. No invitation, status, decision, or claim endpoint should be exposed on a LAN or public interface by this client.

## Future pinned Tailscale migration

The current saved USB URL is not changed automatically. On Device, the user can first bootstrap the Mac identity over the existing loopback connection. The app authenticates that request with its already stored device bearer; it verifies the nonce, device ID, P-256 public-key fingerprint, and HMAC-SHA256 proof before encrypting the server pin with Android Keystore.

For a user-requested host switch, Android accepts only an HTTPS *.ts.net URL that resolves entirely to Tailscale's assigned 100.64.0.0/10 or fd7a:115c:a1e0::/48 addresses. It contacts the candidate without the device bearer, verifies standard TLS and the nonce-bound P-256 identity signature against the stored pin, then changes the saved URL while preserving the encrypted token. Any mismatch, TLS failure, public/LAN DNS answer, or rejected challenge leaves the current URL and token untouched.

This client does not install or configure Tailscale. The phone and Mac must already have authorized Tailscale connectivity. No migration attempt has been run as part of this build.

## USB-only network path

- Pairing invitations accept only loopback URLs. The current paired URL stays on USB loopback until the user explicitly performs the pinned Tailscale migration above.
- For a USB-connected phone, the Mac operator runs the port-forward command shown in the scanned invitation preview, for example: adb reverse tcp:8000 tcp:8000.
- The QR server URL should use http://127.0.0.1:PORT for that USB reverse path. The Mac service remains bound to loopback.
- The app uses Google Play services Code Scanner, configured for QR codes. It does not declare CAMERA permission; Google Play services runs the scanner UI and returns QR text only. The first scan may require Google Play services to download its scanner module.
- The manifest requests Internet access only. No Accessibility, camera, SMS, contacts, location, Firebase, or notification permission is requested.
- No FCM project is configured. Local Android notifications are not implemented.

## Other API routes

- GET /v1/jarvis/tasks?state=active|completed returns { "tasks": [...] }.
- GET /v1/jarvis/tasks/{id} returns task details and events.
- GET /v1/jarvis/approvals/pending returns { "approvals": [...] }.
- POST /v1/jarvis/approvals/{id}/decision sends decision, expected_operation_digest, and idempotency_key.

The Mac server remains authoritative for device authentication, approval expiry, replay prevention, digest matching, and authorization at the privileged tool boundary.
