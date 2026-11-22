# Local Wi-Fi pairing protocol v2 (opt-in only)

This protocol adds first-pair onboarding over the explicitly configured private Wi-Fi gateway. It does not replace the legacy loopback/USB QR (`openjarvis-pairing-v1`) or the authenticated existing-device Wi-Fi route. It is not enabled or deployed by this change.

## Owner-side QR creation

The Mac UI calls `POST /v1/jarvis/pair/invitations` on the loopback OpenJarvis API. The existing loopback peer, Host and same-origin UI checks remain the authorization boundary; the UI does not need a bearer/API key. An empty body retains the legacy v1 QR. A body must be exactly `{"transport":"wifi"}` to request v2. The request fails closed unless the gateway opt-in is enabled, the configured interface and private bind address match the live interface, the live SSID exactly matches the configured SSID, the port is 8443, a matching TLS certificate identity exists, and a listener accepts a connection on that interface/port.

The v2 QR payload is canonical JSON: UTF-8, sorted object keys, compact separators, no ASCII escaping. Its ECDSA P-256/SHA-256 `server_signature` covers every field except the signature itself:

- `protocol`: `openjarvis-pairing-v2`
- `server_url`: `https://<single-label>.local:8443` (TLS/SNI hostname; the Android client dials the separate signed address)
- `gateway_address`: validated private IPv4 address assigned to the configured interface
- `gateway_port`: string `8443`
- `gateway_certificate_sha256`: `sha256:` plus 64 lowercase hex characters
- `server_fingerprint`, `server_public_key`, `invitation_id`, `challenge`, `expires_at`
- `server_signature`

The invitation remains short-lived and single-use (the existing store creates a five-minute invitation). A QR is a temporary enrollment request, not a persistent device token. Mac-side local confirmation still approves or rejects the device’s submitted public-key fingerprint. Only after approval can the device prove possession of its key over a fresh, short-lived claim challenge and receive its expiring, revocable token.

## LAN gateway routes

The allowlisted gateway exposes only these invitation-ID-scoped paths without a device bearer token:

- `POST /v1/jarvis/pair/{invitation_id}/complete` — exact JSON fields `device_name`, `public_key`, `signature`; the path supplies the invitation ID.
- `GET /v1/jarvis/pair/{invitation_id}/status` — status for that one invitation; no device enumeration.
- `POST /v1/jarvis/pair/{invitation_id}/claim` — exact JSON field `signature`; the path supplies the invitation ID.

The gateway injects the path ID into fixed loopback calls to the existing pairing handlers. The supervisor store remains authoritative for invitation expiry, single-use completion, owner decision, claim expiry, signature checks and one-time token issuance. These routes are not general proxy routes and forward no client authorization header.

The LAN gateway does **not** expose invitation creation, pending-device listing, Mac approve/reject decisions, raw `/pair/complete`, raw `/pair/status`, raw `/pair/claim`, API docs, or arbitrary OpenJarvis routes. The pairing API remains behind the TLS gateway’s pinned certificate and network interface restrictions; the raw OpenJarvis API remains loopback-only.

## Test and deployment status

Automated tests cover v1 field compatibility and master/loopback authorization; explicit v2 request validation and URL/address/certificate/listener failure; full signature coverage; invitation-scoped route allowlisting; body schemas/size; pending-without-token; expiration; wrong invitation proof; duplicate completion; unapproved claim; owner-approved one-time claim and replay rejection. These are local tests only. The production opt-in, Wi-Fi gateway, first-pair Android v2 flow and physical device operation must be reviewed and tested separately before enabling.
