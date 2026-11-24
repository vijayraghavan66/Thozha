# Copilot Web Workspace integration guide

Status: integration contract and ownership guide only. The owner-approved MagicPath Web Workspace concept exists, but its React/TypeScript frontend is not yet in this repository.

## Current source-of-truth routes

Use Companion HTTP API v1 as the route inventory. It distinguishes direct loopback routes from the subset exposed by the strict companion gateway.

Implemented backend operations include read-only Mac status, task listing/detail, async Link task submission and status, cancellation, pending approval review/decision, and direct-API artifact listing/download. Approval decisions remain bound to the task, exact operation digest, paired-device owner, expiry and single-use authorization.

The Android client implements authenticated request shapes in android-companion/app/src/main/kotlin/com/jarvis/companion/data/JarvisApi.kt. Backend routes live in openjarvis/src/openjarvis/server/supervisor_routes.py; the restricted transport allowlist lives in openjarvis/src/openjarvis/server/companion_gateway.py. The local source commit 13ddffc5 also includes the encrypted, optional FCM token store/sender; its credentials are external to Git and real delivery is unverified. These are source references, not a claim that every route is available remotely or on the public Internet.

OpenJarvis also has general chat routes: POST /v1/chat/completions (including SSE where the selected engine supports streaming), WebSocket /v1/chat/stream, GET /v1/sessions, and several /v1/memory routes. They are not in the companion-gateway allowlist and are not a browser-safe authorization model. SessionStore/trace and memory-backend records should not be treated as a per-device conversation history without a separate user/device binding contract. The Codex SDK path is not certified for sub-3-second first-token latency.

## Web backend updates on `codex/core`

The owner-authenticated Web API is implemented on the backend branch, but the running LaunchAgent on port 8000 does not mount these routes. The loopback Web integration test uses port 45840 and a separate Web profile; do not infer production readiness from that test process. Web session cookies are independent of Android bearer credentials and every Web task/approval route requires an explicit owner-to-active-device mapping.

- Conversation messages can stream final-answer deltas when the engine supports streaming. Internal reasoning and tool calls are excluded. The `stream_mode` event distinguishes `engine_stream` from `buffered`; completed responses are persisted and digest-checked by the client.
- `PATCH /v1/web/conversations/{conversation_id}` renames an owner-scoped conversation. It requires the Web session, CSRF header and same-origin request; title is trimmed, nonblank and limited to 160 UTF-8 bytes.
- Web memory storage/search remains owner-scoped. Only a record explicitly tagged `chat_context` is considered for prompt context; `sensitive`, `private`, `no_chat`, and `never_inject` tags override the opt-in. Retrieval is deterministic token overlap, at most three records and 4,000 characters. The model receives the memories as untrusted data, never as instructions. Completed conversation-event details include only the count and opaque memory IDs, not memory content.

The browser UI must use the generated/versioned contract. Do not wire this to the currently running port 8000 unless that service is separately configured and restarted through an approved deployment. No frontend files were changed by this backend update.

## 2026-11-24 backend contract delta

The `codex/openapi-contracts` revision adds `PATCH /v1/web/conversations/{conversation_id}` with request type `WebConversationRename` (`title`, trimmed nonblank, at most 160 UTF-8 bytes). It is a Web-session mutation and therefore requires same-origin CSRF; it returns the updated owner-scoped `WebConversation`, or 404 when the conversation is not owned by the session.

Web memory remains deliberately opt-in for chat context. A saved memory participates only when its tags contain `chat_context`; any of `sensitive`, `private`, `no_chat`, or `never_inject` excludes it. Retrieval is owner-scoped and deterministic token-overlap (not semantic search), bounded to three records/4,000 characters. Completed conversation-event details expose only `memory_context_count` and opaque `memory_ids`, never memory content. A client may render this as provenance, but must not surface the memory text as a new user message.

Backend verification: conversation rename and memory retrieval are covered by isolated API/store tests. The currently running LaunchAgent on port 8000 remains an older local service without Web routes; the verified Web runtime used a separate loopback-only test profile on port 45840. Do not treat the test runtime as production deployment.

## Frontend rules before live backend wiring

- Implement from the approved MagicPath Web Workspace concept. Keep preview fixtures labeled while wiring is pending; do not independently recreate or replace the design.
- Any fixture must visibly say Preview or Sample; fixture values must never be labeled as live Mac state, real tasks, real approvals, or generated artifacts.
- Build a typed API adapter that consumes only versioned contract types. Do not add hard-coded backend paths in screen components.
- Do not put a paired-device bearer token, API key, certificate private key, or pairing secret in browser local/session storage, URLs, source maps, or mock data.
- The proposed browser session interface is in contracts/web-auth/v1.md, but no Web authentication/token exchange exists in the implemented source. Do not call loopback, the companion gateway, or the relay from a browser. Browser-to-backend wiring requires a separately reviewed auth and deployment design.
- Keep actions disabled in preview mode. Do not simulate approval decisions, task execution, push notifications, or artifact delivery.
- After a future authenticated integration is approved, show truthful states: accepted is not completed; approval recorded is not execution completed; artifact bytes are not verified until size/hash checks pass.

## Implemented versus planned

| Capability | Source status | Web readiness |
|---|---|---|
| Mac status / battery | Implemented fixed read-only route | Contracted; no browser auth or live wiring |
| Task list and detail | Implemented persisted state/events | Contracted; polling only via available routes |
| Async task submit/status/cancel | Implemented supervisor path and Android client | Contracted; route availability depends on local gateway; no Web client |
| Approval review and exact-digest decision | Implemented supervisor boundary and Android client | Contracted; Web controls stay preview-only until auth/design approval |
| ZIP artifact manifest/download | Implemented in direct loopback API and Android client | Not in gateway allowlist; no Web delivery |
| FCM token registration | Registration/revoke route implemented | End-to-end delivery not verified; not a Web feature |
| Generic OpenJarvis chat/SSE/WebSocket | Implemented in the general server | Not companion-gateway allowlisted; no browser session/auth contract |
| OpenJarvis sessions and memory | General server routes exist | Not bound to a Thozha per-device conversation history/profile; memory may be unconfigured |
| Jarvis Link encrypted relay | Versioned prototype protocol and local tests | No production deployment; not the current Android transport |
| Live audio | Development protocol on loopback port 8766 through ADB reverse | Not available to Web and not USB-free |
| Browser auth, task event stream, remote artifact gateway, public/Tailscale remote access | Not implemented in the published contract | Requires separate design/security review |

## File ownership

Copilot may prepare files under web/ only after the owner approves the mockup. Codex owns all contract changes, Android and backend integration, and final security review. See OWNERSHIP.md.
