# Copilot Web Workspace integration guide

Status: integration contract and ownership guide only. The owner-approved MagicPath Web Workspace concept exists, but its React/TypeScript frontend is not yet in this repository.

## Current source-of-truth routes

Use Companion HTTP API v1 as the route inventory. It distinguishes direct loopback routes from the subset exposed by the strict companion gateway.

Implemented backend operations include read-only Mac status, task listing/detail, async Link task submission and status, cancellation, pending approval review/decision, and direct-API artifact listing/download. Approval decisions remain bound to the task, exact operation digest, paired-device owner, expiry and single-use authorization.

The Android client implements authenticated request shapes in android-companion/app/src/main/kotlin/com/jarvis/companion/data/JarvisApi.kt. Backend routes live in openjarvis/src/openjarvis/server/supervisor_routes.py; the restricted transport allowlist lives in openjarvis/src/openjarvis/server/companion_gateway.py. The local source commit 13ddffc5 also includes the encrypted, optional FCM token store/sender; its credentials are external to Git and real delivery is unverified. These are source references, not a claim that every route is available remotely or on the public Internet.

OpenJarvis also has general chat routes: POST /v1/chat/completions (including SSE where the selected engine supports streaming), WebSocket /v1/chat/stream, GET /v1/sessions, and several /v1/memory routes. They are not in the companion-gateway allowlist and are not a browser-safe authorization model. The separate Web session routes and owner-scoped conversation store are implemented in `openjarvis/src/openjarvis/server/thozha_web_auth.py` and `thozha_web_conversations.py`; see `contracts/web-auth/v1.md`. They are mounted only when the separate browser owner credential, password hash, and exact origin are configured. There is no live Web credential configured or physical-browser deployment verified. Web conversation generation uses a configured engine with tools disabled, persists history/events, but returns only after full `generate()` completion; it is not token streaming. Cancellation suppresses late results but cannot stop an already-running blocking model call. The Codex SDK path is not certified for sub-3-second first-token latency.

## Frontend rules before live backend wiring

- Implement from the approved MagicPath Web Workspace concept. Keep preview fixtures labeled while wiring is pending; do not independently recreate or replace the design.
- Any fixture must visibly say Preview or Sample; fixture values must never be labeled as live Mac state, real tasks, real approvals, or generated artifacts.
- Build a typed API adapter that consumes only versioned contract types. Do not add hard-coded backend paths in screen components.
- Do not put a paired-device bearer token, API key, certificate private key, or pairing secret in browser local/session storage, URLs, source maps, or mock data.
- The browser session and conversation interfaces are implemented in source and documented in contracts/web-auth/v1.md. The Web UI is not integrated. Do not use companion bearer tokens; an owner must configure separate browser credentials and an exact HTTPS origin before browser login is available. Do not expose the raw API or relay from a browser.
- Keep actions disabled in preview mode. Do not simulate approval decisions, task execution, push notifications, or artifact delivery.
- After a future authenticated integration is approved, show truthful states: accepted is not completed; approval recorded is not execution completed; artifact bytes are not verified until size/hash checks pass.

## Implemented versus planned

| Capability | Source status | Web readiness |
|---|---|---|
| Mac status / battery | Implemented fixed read-only route | Contracted; no browser auth or live wiring |
| Task list and detail | Implemented persisted state/events | Contracted; polling only via available routes |
| Async task submit/status/cancel | Implemented supervisor path and Android client | Contracted; route availability depends on local gateway; no Web client |
| Approval review and exact-digest decision | Implemented supervisor boundary and Android client | Contracted; Web controls stay preview-only until auth/design approval |
| ZIP artifact manifest/download | Implemented in direct API, Android client, and restricted paired-device gateway | Not available to Web; no Web artifact routes |
| FCM token registration | Registration/revoke route implemented | End-to-end delivery not verified; not a Web feature |
| Generic OpenJarvis chat/SSE/WebSocket | Implemented in the general server | Not companion-gateway allowlisted; no browser session/auth contract |
| OpenJarvis sessions and memory | General server routes exist | Not bound to a Thozha per-device conversation history/profile; memory may be unconfigured |
| Web session authentication | Implemented in source; disabled without separate external owner configuration | Contracted; no credentialed Web deployment verified |
| Owner-scoped Web conversation history | Implemented in source; tools disabled; persistent results/events; no token streaming | Contracted; not yet consumed by Web UI. Cancellation suppresses late output; one server process per DB is required |
| Web automation route adapter | Isolated on `codex/web-automations`; not mounted; scheduler mutations unavailable without a supervisor adapter | Do not consume as a live API |
| Jarvis Link encrypted relay | Versioned prototype protocol and local tests | No production deployment; not the current Android transport |
| Live audio | Development protocol on loopback port 8766 through ADB reverse | Not available to Web and not USB-free |
| Browser task event stream, Web approval actions, remote artifact gateway, public/Tailscale remote access | Not implemented/available | Requires separate implementation and security review |

## File ownership

Copilot may prepare files under web/ only after the owner approves the mockup. Codex owns all contract changes, Android and backend integration, and final security review. See OWNERSHIP.md.
