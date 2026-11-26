# Copilot Web Workspace integration guide

Status: integration contract and ownership guide only. The owner-approved MagicPath Web Workspace concept exists, but its React/TypeScript frontend is not yet in this repository.

## Current source-of-truth routes

Use Companion HTTP API v1 as the route inventory. It distinguishes direct loopback routes from the subset exposed by the strict companion gateway.

Implemented backend operations include read-only Mac status, task listing/detail, async Link task submission and status, cancellation, pending approval review/decision, and direct-API artifact listing/download. Approval decisions remain bound to the task, exact operation digest, paired-device owner, expiry and single-use authorization.

The Android client implements authenticated request shapes in android-companion/app/src/main/kotlin/com/jarvis/companion/data/JarvisApi.kt. Backend routes live in openjarvis/src/openjarvis/server/supervisor_routes.py; the restricted transport allowlist lives in openjarvis/src/openjarvis/server/companion_gateway.py. The local source commit 13ddffc5 also includes the encrypted, optional FCM token store/sender; its credentials are external to Git and real delivery is unverified. These are source references, not a claim that every route is available remotely or on the public Internet.

### Browser-owned Mac pairing (Web v1)

Use only the authenticated `/v1/web/devices/pairing-*` routes in the browser UI. Mutations require the Web session cookie, `X-Thozha-CSRF`, same-origin `Origin`, and JSON. `POST /v1/web/devices/pairing-invitations` accepts exactly `{ "transport": "wifi" }` and returns the same signed v2 QR payload as the Mac pairing implementation. It fails closed unless the configured private Wi-Fi address/SSID, TLS pin, and restricted gateway listener are live; it never returns a loopback-only QR for a phone to scan. The browser must render the signed nested `qr_payload` and its expiry, not synthesize a QR invitation.

The phone uses the existing Android parser and pinned companion gateway: invitation-scoped `POST /v1/jarvis/pair/{id}/complete`, `GET .../status`, and `POST .../claim`. The Mac owner reviews `GET /v1/web/devices/pairing-requests` and sends one explicit approve/reject decision to `POST /v1/web/devices/pairing-requests/{id}/decision`. The Web owner can cancel an unclaimed invitation with `POST /v1/web/devices/pairing-invitations/{id}/cancel` and inspect status with `GET /v1/web/devices/pairing-invitations/{id}`. Web responses never include the phone claim challenge or issued device token. Device records are not deleted by cancellation; an already issued credential requires the separate revoke flow.

Keep the connected-device list backed by `GET /v1/web/devices`. That endpoint continues to return only IDs explicitly mapped by `THOZHA_WEB_ALLOWED_DEVICE_IDS`. QR pairing does **not** automatically add a new phone to the browser's task-actor allowlist; do not imply that pairing alone grants Web task, approval, or Mac execution authority. The approved UI should present pairing completion separately from Web-task mapping. Contracts are in `contracts/web-api/v1.ts` and the versioned OpenAPI branch; tests are in `tests/server/test_thozha_web_pairing.py`.

OpenJarvis also has general chat routes: POST /v1/chat/completions (including SSE where the selected engine supports streaming), WebSocket /v1/chat/stream, GET /v1/sessions, and several /v1/memory routes. They are not in the companion-gateway allowlist and are not a browser-safe authorization model. Separate Web session, owner-scoped conversation, and owner-isolated memory routes are implemented under `openjarvis/src/openjarvis/server/thozha_web_*.py`; see `contracts/web-auth/v1.md`. They are mounted only when separate browser owner credentials, password hash, and exact origin are configured. There is no live Web credential configured or browser deployment verified. Web conversation `POST .../messages/stream` uses `engine.stream()` with tools disabled. Native-stream engines produce incremental deltas; Codex SDK currently buffers full `generate()` output and is labeled `buffered`. Cancellation suppresses late results but cannot stop an already-running blocking Codex call. Web memory search is deterministic substring matching, not semantic retrieval.

### Authenticated question screenshot/document input

For owner-authenticated conversations, the browser uploads raw bytes to `POST /v1/web/conversations/{conversation_id}/attachments` with `Content-Type` (`image/png`, `image/jpeg`, or `application/pdf`), `X-File-Name`, `Idempotency-Key`, Web session cookie, same-origin Origin, and `X-Thozha-CSRF`. The API verifies file signatures, caps each file at 8 MiB, allows at most ten files/30 MiB per conversation and 100 MiB per owner, limits OCR to two concurrent documents, stores content owner-scoped in the secured conversation SQLite database, and returns metadata/hash only. `GET .../attachments/{attachment_id}` serves the original file only to that same owner. Include returned attachment IDs in the next message's `attachment_ids` list; retries should reuse the same idempotency key only for identical bytes.

On macOS, local Vision/PDFKit extracts text from PNG/JPEG and PDF pages (maximum 10 pages, 100,000 extracted characters/file). The extraction is performed by a fixed Swift helper in a bounded subprocess; the original file and extracted text are persisted, while logs contain only safe error codes. The model receives only bounded OCR/PDF text, clearly marked as untrusted document content, with tools disabled. This does **not** send image pixels to a multimodal model and cannot reliably interpret diagrams, handwriting, color/layout, or unreadable scans. Empty or malformed documents fail visibly. Linux runtimes report extraction unavailable until an equivalent reviewed backend is supplied. The course checklist screenshot was OCR-checked but contained no question to answer; no course answer or live model answer was tested.

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
- Current browser type definitions are in `contracts/web-api/v1.ts`; OpenAPI source is on `codex/openapi-contracts`. Types cover session, conversation, memory, task, approval, artifact, device, and automation routes. Live use requires separate browser credentials, an exact HTTPS origin, an active Link runner, and an explicit `THOZHA_WEB_ALLOWED_DEVICE_IDS` mapping; never populate these from fixtures or Android tokens.
- Do not put a paired-device bearer token, API key, certificate private key, or pairing secret in browser local/session storage, URLs, source maps, or mock data.
- The browser session and conversation interfaces are implemented in source and documented in contracts/web-auth/v1.md. The Web UI is not integrated. Do not use companion bearer tokens; an owner must configure separate browser credentials and an exact HTTPS origin before browser login is available. Do not expose the raw API or relay from a browser.
- Keep actions disabled in preview mode. Do not simulate approval decisions, task execution, push notifications, or artifact delivery.
- After a future authenticated integration is approved, show truthful states: accepted is not completed; approval recorded is not execution completed; artifact bytes are not verified until size/hash checks pass.

### Supervisor readiness and contract details

- `GET /v1/web/supervisor/readiness` is authenticated and owner-scoped. It returns redacted component states/reason codes for the Codex preflight, browser session, mapped devices, durable Link worker and optional automation scheduler. A present Codex auth file is explicitly `credential_present_unverified`; only a successful model turn in the current process reports `verified_this_process`. The endpoint never returns credential paths, tokens, device keys or private worker settings.
- `POST /v1/web/tasks` returns `202` for a new task or a nonterminal replay, and `200` for a completed idempotent replay. Do not assume every replay is `202`.
- Task cancellation returns a cancellation receipt (`task_id`, `state`, `cancelled`, `action_may_have_started`), not a `WebTaskResult`.
- Approval detail/pending items include `requester_device_id` only after the route has verified that the task belongs to an active device explicitly mapped to the authenticated Web owner. This supports deliberate device selection; clients must still send and revalidate it on a decision.
- `mac_read` is a narrow server-validated Link scope for the fixed `get_system_info` and `get_disk_usage` tools. It disables other configured MCP servers and shell surfaces. It does not grant screen observation, app launch, or control.
- These contract changes are additive except for correcting the documented response shape. The Web type and OpenAPI definitions live in `contracts/web-api/v1.ts` and the separate `codex/openapi-contracts` branch. The Copilot-owned `web/README.md` still contains its original handoff notes and should be refreshed by Copilot when the UI branch is next updated; no Copilot frontend files were changed by this pass.

## Implemented versus planned

| Capability | Source status | Web readiness |
|---|---|---|
| Mac status / battery | Implemented fixed read-only companion route | No separate Web status route yet |
| Web task list/detail/submit/cancel/events | Implemented; mapped active device actors, persisted states, bounded resumable SSE | Contracted; no live browser credentials/device mapping configured |
| Async task submit/status/cancel | Implemented supervisor path and Android client | Contracted; route availability depends on local gateway; no Web client |
| Approval review and exact-digest decision | Implemented supervisor boundary and Android client | Web API reuses the same exact-digest/expiry/replay/dispatch path; no live decision was performed |
| ZIP artifact manifest/download | Implemented in direct API, Android client, and restricted paired-device gateway | Web routes verify completed development owner and archive SHA-256; no browser download test yet |
| FCM token registration | Registration/revoke route implemented | End-to-end delivery not verified; not a Web feature |
| Generic OpenJarvis chat/SSE/WebSocket | Implemented in the general server | Not companion-gateway allowlisted; no browser session/auth contract |
| OpenJarvis sessions and memory | General server routes exist | Not bound to a Thozha per-device conversation history/profile; memory may be unconfigured |
| Web session authentication | Implemented in source; disabled without separate external owner configuration | Contracted; no credentialed Web deployment verified |
| Owner-scoped Web conversation history | Implemented in source; tools disabled; persisted results/events; SSE uses engine stream; Codex SDK reports buffered output | Contracted; not yet consumed by Web UI. Cancellation suppresses late output; one server process per DB is required |
| Owner-scoped screenshot/PDF attachments | Implemented in source and tests; PNG/JPEG/PDF bytes and OCR/text are stored with the conversation; message passes extracted text only | Contracted for Copilot; local Mac Vision/PDFKit required. No multimodal image reasoning; no live model answer on the course checklist image |
| Owner-scoped Web memory | Implemented in source; separate SQLite store, quotas, idempotent writes, substring search | Contracted; no shared Android/OpenJarvis memory and no semantic retrieval |
| Owner-scoped PNG/JPEG/PDF attachments | Implemented in source and tests; validated bytes, OCR/PDF text extraction, owner-bound message references, download/hash verification, bounded context and no tool access | Contracted; macOS Vision/PDFKit only. Image-native visual interpretation and handwriting/diagram understanding are not implemented |
| Web task lifecycle | Implemented in source; task owner is a mapped paired-device actor; SSE replays sanitized persisted events | Contracted; not live until browser credentials and device mapping are configured |
| Web device status/revocation and QR pairing | Implemented in source; active-device metadata is explicitly mapped; revoke is confirmed; QR enrollment is signed, Wi-Fi-only, owner-decided, cancellable, and preserves separate Web task mapping | Contracted for Copilot. QR creation returns 409 until a verified private Wi-Fi gateway is configured; pairing does not auto-authorize Web task execution |
| Authenticated Web QR pairing | Implemented in backend source and isolated API tests; Wi-Fi only; same signed QR and existing phone gateway; explicit owner decision and cancellation | Contracted for Copilot; live Wi-Fi gateway and Samsung flow remain unverified; newly paired device is not automatically mapped for Web task execution |
| Web automations | Opt-in dedicated scheduler; read_only occurrences go through durable Link supervisor tasks; update/pause/resume/soft-delete/manual trigger/history | Disabled unless `THOZHA_WEB_AUTOMATION_ENABLED=1` and an active Link runner/device mapping are available; no production schedule was created |
| Jarvis Link encrypted relay | Versioned prototype protocol and local tests | No production deployment; not the current Android transport |
| Live audio | Development protocol on loopback port 8766 through ADB reverse | Not available to Web and not USB-free |
| Web automation scopes beyond `read_only`, notification history, public/Tailscale remote access | Not implemented/available | Requires separate security review and implementation |

## File ownership

Copilot may prepare files under web/ only after the owner approves the mockup. Codex owns all contract changes, Android and backend integration, and final security review. See OWNERSHIP.md.
