# Mac pairing UI contract

## Existing endpoint used by Connect Phone

The screen calls `POST http://127.0.0.1:8000/pair/invitations` only after the user selects **Create signed QR invitation**. Before that mutation, it performs a read-only `GET http://127.0.0.1:8000/health`; it never calls the invitation endpoint from app launch or test code.

The backend handler in `src/openjarvis/server/supervisor_routes.py` uses `_authorize_local_pair_ui(state_changing=True)`. Its existing boundary requires a loopback peer, loopback `Host`, and an allowlisted local UI `Origin`. The Swift client sends `Origin: http://tauri.localhost`, which the backend explicitly accepts. This route does not require `OPENJARVIS_API_KEY`; the client does not read the LaunchAgent environment, config file, `supervisor_api_key`, Keychain, or any pairing identity. The `Authorization` header is intentionally absent. The app does not access or export the backend signing private key.

The returned payload is checked locally before QR display:

- exact v1 field set and matching outer/nested payload;
- `openjarvis-pairing-v1` protocol;
- loopback-only server URL;
- SHA-256 fingerprint over the DER public key;
- P-256 ECDSA signature over Python-compatible canonical UTF-8 JSON;
- current expiry, capped at ten minutes;
- 8 KiB QR size bound.

The QR includes a short-lived challenge. The UI never logs or persists it and warns users not to share the QR. Closing the view hides the QR; there is no local revoke endpoint in this flow. Refreshing before expiry asks the user to confirm because the old invitation remains valid until its original expiry. A failed response is not retried automatically because the server may have created an invite before the response was lost.

Tests use the shared fixed Python/Android signature vector and a fake transport. They do not contact the live Mac API or create pairing rows.

## Wi-Fi pairing blocker

The current invitation route constructs `server_url` from the loopback request, so the v1 QR advertises `127.0.0.1`/`localhost`. This is usable only when the phone has a corresponding local forward (for example, the existing ADB reverse route); the UI explicitly says it is not a Wi-Fi invitation. No phone or network pairing was run by these tests.

The existing `/local-gateway/address` endpoint returns a signed private address and TLS certificate pin, but it requires an already-enrolled device bearer, so it cannot bootstrap an unpaired phone. A Wi-Fi first-pair flow needs the separately reviewed v2 extension: signed `.local` HTTPS gateway URL/address, port and certificate SHA-256 in the invitation, with the correct local authorization rule. The Connect Phone code must not construct or infer these fields. Until the backend exposes that endpoint, this screen will not claim Wi-Fi is ready.
