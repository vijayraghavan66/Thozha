# Thozha Web Alpha 0.1

The Web Workspace is a React, TypeScript, Vite, and Tailwind application in the Copilot-owned `web/` directory. It integrates the approved MagicPath export `sunnily-current-6815` (project `475494667987865600`, component `475494671267811328`). The original graphite shell, sidebar, panel layouts, Trading Lab interactions, Inter typography, and water keyframes were carried forward. The export contained no separate image assets. Its ZIP SHA-256 was `714a01526bc5ba8365aa81ddac11ff687bbbe373d3745006c9b6c1c9a78df111`.

## Run locally

1. Use Node.js 22.12+ (or 20.19+) and run `npm ci` from `web/`.
2. Have the backend owner run the compatible OpenJarvis API on loopback, normally `127.0.0.1:8000`, with a configured model. See the [Web auth contract](../contracts/web-auth/v1.md). Set `THOZHA_WEB_OWNER_ID`, a PBKDF2 `THOZHA_WEB_PASSWORD_HASH` generated with the backend helper, and the origin **in the backend process only**. The backend's SQLite directories must be owner-only, and its conversation database currently requires one API process.
3. For loopback development only, configure `THOZHA_WEB_ORIGIN=http://127.0.0.1:5173` and `THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV=1` on the backend. Start `npm run dev` in `web/` and open `http://127.0.0.1:5173` (not `localhost`). If the backend uses a different loopback port, set `THOZHA_WEB_UPSTREAM=http://127.0.0.1:<port>` on the **Vite process**.
4. Sign in with the browser owner ID and password. There is no default owner or frontend authentication bypass. `npm run build` produces `dist/`. For local production preview, use `npm run preview` and configure the backend origin to `http://127.0.0.1:4173` instead.

Use `Ctrl+K` / `⌘K` to jump to pages, `Escape` to close dialogs, and `Enter` / `Shift+Enter` to send / insert a newline. The workspace respects system and in-app reduced-motion preferences. On desktop the 237px navigation rail and top bar stay in place; only the active page scrolls. At narrow widths the rail becomes a focus-managed navigation drawer. Settings groups browser session, visual preferences, and unavailable integrations; the sign-out control remains reachable when zoomed or on short screens.

### Run alongside an existing OpenJarvis service

If `GET /v1/web/session` returns `200 text/html`, Vite may be proxying to an older OpenJarvis dashboard instead of the Web API. Don't restart the existing service or reuse its configuration. With `qwen3.5:4b` already installed in Ollama's default model directory, start these commands in three terminals from `web/`:

```sh
# Terminal 1: separate Ollama process; reuse downloaded model weights.
umask 077
mkdir -p "$HOME/.local/state/thozha-web/ollama"
OLLAMA_HOST=127.0.0.1:45841 OLLAMA_MODELS="$HOME/.ollama/models" OLLAMA_NO_CLOUD=1 \
  HOME="$HOME/.local/state/thozha-web/ollama" ollama serve

# Terminal 2: use a Python interpreter with the OpenJarvis server extras installed.
/path/to/venv/bin/python -B scripts/serve-local.py "$HOME/.local/state/thozha-web" 5174 45840 45841

# Terminal 3: point Vite at the isolated Web backend, not the default port 8000.
THOZHA_WEB_UPSTREAM=http://127.0.0.1:45840 npm run dev -- --port 5174
```

Open `http://127.0.0.1:5174`. Sign in as `thozha-local` with the password in `~/.local/state/thozha-web/owner-password` (generated once, mode `0600`). An unauthenticated `curl -i http://127.0.0.1:5174/v1/web/session` should return JSON with HTTP `401`. This profile has its own Web history, memory, and sessions; it does not import existing OpenJarvis data or Android pairing.

### Supervisor prerequisites

The Ollama setup above is for chat and memory, not supervisor execution. On `codex/core` at `b47142d5`, the durable Link worker starts only with a configured Codex SDK engine, model, and agent. Use a separately authorized SDK login for that profile. Do not copy an existing account's authentication files or change machine permissions for a demo.

Set `THOZHA_WEB_ALLOWED_DEVICE_IDS` **on the backend** to the explicit comma-separated IDs of active paired companions authorized for the configured `THOZHA_WEB_OWNER_ID`. The supervisor uses the pairing records from that backend's store. A browser session does not create or authorize a pairing. Enable the separate read-only scheduler with `THOZHA_WEB_AUTOMATION_ENABLED=1` only if wanted. Isolate `HOME`, `OPENJARVIS_HOME`, the session/conversation/memory/scheduler stores, credentials, and ports from any active service. Do not import or automatically map an existing Android pairing. `GET /v1/web/automations/capabilities` reports supported operations; a mounted device or task list does not prove that the worker is running.

### Deployment boundary

For an actual deployment, serve `dist/` and proxy **only the contracted browser Web v1 routes** under the same trusted HTTPS origin. Configure the backend with that exact external origin and leave insecure-loopback cookies disabled. A trusted proxy must preserve the original `Origin` and host, communicate the external HTTPS scheme through trusted proxy headers, leave cookies intact, prevent caching of all private API responses, and pass `text/event-stream` through without buffering. Do not log password bodies, cookies, CSRF headers, chat bodies, or memory search queries. Do not expose the raw OpenJarvis server, companion gateway, or general OpenJarvis APIs to the browser. Vite's proxy is for local development and test, not a production gateway.

The backend sets and validates the opaque `HttpOnly`, `SameSite=Strict` cookie. Only the in-memory CSRF value accompanies writes, including DELETE. Private UI and caches are scoped to the verified session, removed on expiry or HTTP 401, and hidden while sign-out is unconfirmed. Offline reads, when already cached, are labeled. No session keys, passwords, conversation bodies, memory entries, or device credentials go into browser storage. Only the motion preference and explicitly simulated paper fills are stored locally.

## Capability status

| Workspace area        | Alpha behavior                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Overview and Settings | Live owner-scoped task, mapped-pairing and pending-approval snapshots where available. Browser session verification, expiry and sign-out. No claim about Mac reachability or model health.                                                                                                                                                                                           |
| AI Workspace          | Owner-scoped conversations, persisted history, archival/deletion, Markdown, submission, replay and cancellation. **Review as supervisor task** prefills a separate form from the current draft. It does not send a chat message, submit a task, or authorize a tool.                                                                                                                 |
| Conversation delivery | Incremental SSE only when the engine reports actual deltas; Codex SDK is buffered. A missing SSE route falls back to the verified `202` API and persisted status. Cancellation suppresses late output but cannot forcibly interrupt a blocking model call.                                                                                                                           |
| Memory Center         | Owner-scoped create, list, detail, substring search and confirmed delete. No update, automatic retention or semantic retrieval in Web v1.                                                                                                                                                                                                                                            |
| Trading Lab           | **Browser-local paper simulation** at fictional quotes. No market feed, brokerage connection or real orders.                                                                                                                                                                                                                                                                         |
| Device Hub            | Active, explicitly mapped pairings, with dates and confirmed revocation. An active pairing is not online presence. No browser enrollment, discovery, raw pairing credentials, or privilege editing. Sample device types remain labeled when there is no usable mapping.                                                                                                              |
| Automations           | Capability-gated read-only scheduling, editing, pause/resume, manual runs, soft deletion, and persisted run history with links to supervisor tasks. Unmounted scheduler: explicitly labeled, page-local drafts only.                                                                                                                                                                 |
| Developer Studio      | Real supervisor submission, history filters and per-task links. Follows replayable SSE with `Last-Event-ID`, reconnects with bounded backoff, and can recover persisted events without SSE. Server-reported cancellation and failure are separate from completion. The UI compares full saved result text to its SHA-256 when supplied; that proves integrity, not factual accuracy. |
| Approval review       | Explicit approve/reject from a fresh exact-operation review. The backend enforces device ownership, digest, expiry, and a single dispatch claim. No approval is implied by task submission.                                                                                                                                                                                          |
| Artifacts             | Authorized backend ZIP listing/download for completed development tasks. The client verifies length and SHA-256 against the manifest list and the response header before creating a file download.                                                                                                                                                                                   |
| Other integrations    | No GitHub/CI provider, notification history, unread counts, or standard browser route for live host/device presence.                                                                                                                                                                                                                                                                 |

Demo chats, approvals, and fake device/task results from the export are deliberately not migrated into the live workspace.

## Verification

```sh
npm ci
npm run typecheck
npm run lint
npm test
npm run build
npx playwright install chromium
# Point at an isolated-capable Python environment with the monorepo's OpenJarvis server extras.
# THOZHA_E2E_BACKEND can be omitted if this checkout already contains the reviewed backend.
export THOZHA_E2E_BACKEND=/path/to/thozha-codex-core
THOZHA_E2E_PYTHON=/path/to/venv/bin/python npm run test:e2e
THOZHA_E2E_PYTHON=/path/to/venv/bin/python THOZHA_E2E_MODE=buffered \
  npm run test:e2e -- --project workspace --grep 'renders the reported SSE mode' --output test-results/buffered
```

Playwright serves the production bundle on loopback. The `workspace` project (ports `45837`/`45838`) covers chat, memory, session, disabled capabilities, accessibility, and the approved layout and motion. The `control` project (ports `45933`/`45934`) uses the real backend's Web routes, Link worker, supervisor database and scheduler, with a deterministic agent and an inert approval executor. It provisions signed test pairings through the isolated store, maps their IDs explicitly, discards the issued companion credentials, and never uses any existing pairing. It covers success and rejection, persisted result verification on reload, SSE reconnection, cancellation, digest-bound decisions and replays, verified ZIP downloads, revocation, and schedule lifecycle/history. Both projects generate temporary browser credentials and owner-only storage and make no provider calls.

A separate Chromium attempt against the unmodified backend with its actual Ollama engine (no fixture worker) signed in, received one active isolated test pairing from `/v1/web/devices`, and submitted a read-only macOS version and uptime task. `POST /v1/web/tasks` returned **503: `Supervisor task worker is unavailable`** and history was still empty after reload. The clean test profile had no Codex SDK credentials. No Mac command ran and no result was fabricated.

### Functional integration verification

- Strict TypeScript, ESLint, and the production build passed, as did all 12 Vitest unit/component tests.
- All 20 Chromium tests passed (14 workspace, 6 supervisor control), plus a separate buffered-chat case. Axe found no violations on the scanned screens, including the schedule history view.
- The production build reported 125.08 kB of entry JavaScript and 9.07 kB of CSS gzipped. One warm Chromium loopback navigation measured 6 ms to `DOMContentLoaded`; none of the AI Workspace, Trading Lab, or Memory Center chunks loaded on Overview. This is not a deployment or model latency benchmark.

Playwright screenshots (the conversation uses the labeled test engine): [Sign-in](./docs/alpha-visuals/sign-in.png) · [Desktop Overview](./docs/alpha-visuals/desktop-overview.png) · [Desktop conversation](./docs/alpha-visuals/desktop-conversation.png) · [Mobile conversation](./docs/alpha-visuals/mobile-conversation.png).

### Workspace layout and motion refinement

The approved monochrome surfaces and navigation are preserved. The Overview liquid scene now uses lightweight layered SVG wave contours, pearl/silver shading and slow transform/opacity animation, with a small pointer parallax on fine-pointer devices. It does not create simulated data, fetch motion assets, or use WebGL. System and in-app reduced-motion settings disable ambient movement and dialog/page transitions. Header search expands left from its fixed magnifier/X button; the results are verified workspace destinations only. `Ctrl+K` / `⌘K`, Escape, arrows, Enter, clear, outside click, and focus restoration are supported. The mobile menu lines morph to a close state, while a native modal drawer handles focus, Escape and backdrop dismissal with a matching reverse close transition. Desktop navigation uses one sliding active surface. Shared CSS motion tokens govern press, icon, search, navigation, drawer, and page transitions.

Chromium layout checks cover all eight routes at 1440×900, 1280×800, 1024×768, 768×1024, 390×844, and a 720×450 CSS-pixel viewport at 2× device scale (the effective viewport of a 1440×900 window at 200% zoom). They verify fixed shell and header geometry, 237px desktop rail, scrollable content/chat, reachable Settings controls, no document or horizontal overflow, drawer focus restoration, and mobile Settings accessibility. Separate interaction tests verify the search anchor stays put as the field expands left, keyboard destination selection, focus return, hamburger state, active navigation highlight geometry, and outside-click/Escape drawer dismissal. Axe has no WCAG 2.x A/AA violations on Overview, search, mobile navigation, and chat. Before refinement, Settings made the document 1,131px high at 1280×800; afterward the document stays 800px, with 829px of Settings content scrolling inside its 704px main pane. A 1.2-second local Chromium requestAnimationFrame sample observed 73 frames (median and p95 interval 16.7ms); this measures browser pacing only, not GPU cost or low-end-device performance. Two hero captures 2 seconds apart changed 24,102 pixels above a 5-level RGB threshold; computed water transforms changed while both reduced-motion modes set animation to `none`.

[Before Overview](./docs/alpha-visuals/refinement/before-1440-overview.png) · [After Overview](./docs/alpha-visuals/refinement/after-1440-overview.png) · [Before Settings](./docs/alpha-visuals/refinement/before-1280-settings.png) · [After Settings](./docs/alpha-visuals/refinement/after-1280-settings.png) · [Before mobile AI](./docs/alpha-visuals/refinement/before-390-ai-workspace.png) · [After mobile AI](./docs/alpha-visuals/refinement/after-390-ai-workspace.png) · [Motion frame 0](./docs/alpha-visuals/refinement/motion-frame-0.png) · [Motion frame +2s](./docs/alpha-visuals/refinement/motion-frame-2s.png) · [Measurements](./docs/alpha-visuals/refinement/motion-measurement.json).

Search and drawer interaction captures: [Anchored search](./docs/alpha-visuals/refinement/anchored-search.png) · [Mobile navigation drawer](./docs/alpha-visuals/refinement/mobile-navigation-drawer.png).

### Liquid interaction milestone

The pre-change Playwright recording showed that the hero translated fixed paths, notifications opened a centered modal, and search results vanished immediately on close. The desktop rail already used a moving shared highlight; the drawer and page used entrance/exit or entrance-only motion. The device cards only changed fill at selection.

Notifications and search results now share a reversible, trigger-aligned surface. A rounded clip reveals the panel while its background changes scale and elevation independently of the text. Notifications still report that Web v1 has no notification API. The same search handle stroke extends into the first diagonal of the X; the other diagonal emerges as the lens recedes. The rail retains a single traveling highlight. Tactile lighting is limited to actionable device, conversation, memory, and paper-trading controls; ordinary cards and unavailable developer actions remain static. Automation draft controls and Settings use the shared press treatment.

The hero redraws SVG contour geometry with counter-propagating waves and a decaying pointer ripple rather than translating fixed artwork. Drawing pauses offscreen, in hidden tabs, and under reduced motion. Slow-update or low-core devices use a lower update cadence without pointer ripples. The sign-in illustration is static. No graphics dependency or new API was added.

A 14.49-second local Chromium interaction sample without recording overhead observed 868 frame intervals (16.8 ms p95, none over 25 ms) and no Long Tasks API entries. Chromium reported 332 ms of main-thread task time, including 105 ms of script, 16 ms of layout, and 37 ms of style recalculation. This is a single local measurement; it does not isolate GPU time or certify lower-end hardware.

## Contract handoff

Runtime wiring is based on `codex/core` at `b47142d5` and OpenAPI v1 on `codex/openapi-contracts` at `a83d163d`. `npm run contract:generate` generates browser-only types from the pinned Git object; fetch `codex/openapi-contracts` if needed. The published document references an undefined generic `Error` schema, so generation omits that error payload shape. The older narrative guide on the contract branch still describes proposed Web auth; the pinned backend implementation and live route tests were the runtime reference. No backend or shared contract files changed.

For Codex to reconcile:

- Supervisor submission can return `200` for a completed idempotent replay as well as `202`. Cancellation returns `task_id`, `state`, `cancelled`, and `action_may_have_started`, not `WebTaskResult`.
- Approval details expose the reviewed arguments and digest but not the task's `requester_device_id`. When more than one active pairing is mapped, the browser asks the user to choose explicitly. Consider exposing that safe ID to already-authorized owners to remove the ambiguity. The browser never tries several devices or rewrites the digest to make a decision succeed.
- The automation update and lifecycle endpoints return action receipts in this runtime, not updated schedule objects. The UI refreshes the persisted detail/history after a mutation. When the feature flag is off the capabilities route itself is unmounted (`404`); when mounted, its operation flags are authoritative.
- Task detail contains neither objective, capability scope, nor requester metadata. The UI uses a task ID if no summary was returned and checks artifact eligibility using the actual artifact route. A result digest can cover pre-truncation text, so the browser labels the marked preview as unverified instead of claiming a match.
- A separate owner-scoped supervisor readiness signal would let the UI distinguish an available history API from an unavailable task worker before submitting. Production HTTPS, live SDK execution, notification history, memory correction/retention and read-only market data still need follow-up. No companion or generic backend routes are substituted.
