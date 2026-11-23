# Thozha Web Workspace

The Web Workspace is a React, TypeScript, Vite, and Tailwind application in the Copilot-owned `web/` directory. The 4.0 interface replaces the original MagicPath-derived Overview-first dashboard with a conversation-first desktop workspace. The earlier export `sunnily-current-6815` (project `475494667987865600`, component `475494671267811328`) remains provenance, not the current layout. Its ZIP SHA-256 was `714a01526bc5ba8365aa81ddac11ff687bbbe373d3745006c9b6c1c9a78df111`.

## Run locally

1. Use Node.js 22.12+ (or 20.19+) and run `npm ci` from `web/`.
2. Have the backend owner run the compatible OpenJarvis API on loopback, normally `127.0.0.1:8000`, with a configured model. See the [Web auth contract](../contracts/web-auth/v1.md). Set `THOZHA_WEB_OWNER_ID`, a PBKDF2 `THOZHA_WEB_PASSWORD_HASH` generated with the backend helper, and the origin **in the backend process only**. The backend's SQLite directories must be owner-only, and its conversation database currently requires one API process.
3. For loopback development only, configure `THOZHA_WEB_ORIGIN=http://127.0.0.1:5173` and `THOZHA_WEB_ALLOW_INSECURE_LOOPBACK_DEV=1` on the backend. Start `npm run dev` in `web/` and open `http://127.0.0.1:5173` (not `localhost`). If the backend uses a different loopback port, set `THOZHA_WEB_UPSTREAM=http://127.0.0.1:<port>` on the **Vite process**.
4. Sign in with the browser owner ID and password. There is no default owner or frontend authentication bypass. `npm run build` produces `dist/`. For local production preview, use `npm run preview` and configure the backend origin to `http://127.0.0.1:4173` instead.

Use `Ctrl+K` / `⌘K` to jump to pages, `Escape` to close dialogs and the mobile context sheet, and `Enter` / `Shift+Enter` to send / insert a newline. The workspace respects system and in-app reduced-motion preferences. AI Workspace opens first. On desktop the 252px rail can contract to 68px; the inspector can close to return space to the conversation. The header stays fixed while only the active view or message log scrolls. On smaller screens the rail becomes a focus-managed navigation drawer and the inspector a dismissible sheet. Settings and sign-out remain accessible when zoomed or on short screens.

### Conversation history and startup

The three-dot button swaps the saved conversation title for icon-only actions inside the same row. Use Tab to move between actions and Escape or the close icon to restore the title. **Archive** keeps the conversation readable under **Show archived conversations**. **Delete** requires a separate confirmation and removes server-saved history. If a delete is interrupted or returns `404`, the frontend checks the owner's history before treating the record as gone. Starting a conversation still needs no title: the first ordinary message supplies it automatically.

Living Pulse draws while the browser verifies its session. Once session verification succeeds, the workspace renders immediately beneath a non-interactive overlay. The centered mark travels to the measured sidebar or mobile header slot, then hands off to the same SVG. A signed-out response lands at the sign-in card; errors bypass the transition. Escape, interaction, a resize, or hiding the tab dismisses it, and reduced motion disables it. **Settings → Visual preferences → Replay introduction** plays the same handoff into the current page without changing routes, session state, or data.

The chat toolbar shows only the conversation title and workspace controls. Find saved and archived chats in the sidebar or mobile navigation drawer, and connection and reply details in **Context**. The empty chat opens with the Living Pulse and the real message composer. The starters only fill an empty draft: **Start a task** inserts `/task` and still requires an objective and explicit review. Sending the first ordinary message creates the conversation on the host. The same composer moves to the transcript footer without remounting; reduced-motion mode moves it immediately.

**Codex handoff — proposed, not implemented:** There is no title-update operation in Web v1. Add an owner-scoped `PATCH /v1/web/conversations/{conversation_id}` with `{ "title": "...", "expected_updated_at": "..." }`, using the existing browser session and mutation CSRF checks. Trim the title, reject empty or over-limit titles using the same limits as creation, and return updated `WebConversation` metadata (`200`) without changing `archived_at`, messages, or events. Return `404` for missing or other-owner IDs and `409` if the expected revision no longer matches; check the revision and apply the title in one transaction. No Rename action is exposed until this contract is versioned and implemented.

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
| Settings              | Browser session verification, expiry, sign-out, motion preference, and honestly labeled unavailable integrations. No claim about Mac reachability or model health.                                                                                                                                                                                                                   |
| AI Workspace          | Owner-scoped conversations, persisted history, archival/deletion, Markdown, submission, replay and cancellation. **Review as supervisor task** prefills a separate form from the current draft. It does not send a chat message, submit a task, or authorize a tool.                                                                                                                 |
| Conversation delivery | Incremental SSE only when the engine reports actual deltas; Codex SDK is buffered. A missing SSE route falls back to the verified `202` API and persisted status. Cancellation suppresses late output but cannot forcibly interrupt a blocking model call.                                                                                                                           |
| Memory Center         | Owner-scoped create, list, detail, substring search and confirmed delete. No update, automatic retention or semantic retrieval in Web v1.                                                                                                                                                                                                                                            |
| Trading Lab           | **Offline-first terminal** with instrument search, opt-in synthetic candlesticks/volume, tested indicators and signals, and a persistent virtual market/limit ledger. No versioned market feed, brokerage connection or real orders.                                                                                                                                                 |
| Device Hub            | Active, explicitly mapped pairings, with dates and confirmed revocation. An active pairing is not online presence. No browser enrollment, discovery, raw pairing credentials, or privilege editing. Sample device types remain labeled when there is no usable mapping.                                                                                                              |
| Automations           | Capability-gated read-only scheduling, editing, pause/resume, manual runs, soft deletion, and persisted run history with links to supervisor tasks. Unmounted scheduler: explicitly labeled, page-local drafts only.                                                                                                                                                                 |
| Developer Studio      | Real supervisor submission, history filters and per-task links. Follows replayable SSE with `Last-Event-ID`, reconnects with bounded backoff, and can recover persisted events without SSE. Server-reported cancellation and failure are separate from completion. The UI compares full saved result text to its SHA-256 when supplied; that proves integrity, not factual accuracy. |
| Approval review       | Explicit approve/reject from a fresh exact-operation review. The backend enforces device ownership, digest, expiry, and a single dispatch claim. No approval is implied by task submission.                                                                                                                                                                                          |
| Artifacts             | Authorized backend ZIP listing/download for completed development tasks. The client verifies length and SHA-256 against the manifest list and the response header before creating a file download.                                                                                                                                                                                   |
| Other integrations    | No GitHub/CI provider, notification history, unread counts, or standard browser route for live host/device presence.                                                                                                                                                                                                                                                                 |

Demo chats, approvals, and fake device/task results from the export are deliberately not migrated into the live workspace.

## 4.0 functional inventory and information architecture

The redesign kept the established request clients and query keys. [`SessionGate`](./src/auth/SessionGate.tsx) verifies the cookie before private UI or queries mount and owns sign-out and private-cache disposal. The shell reads the existing conversation list for recent history, and the center [`ConversationWorkspace`](./src/chat/ConversationWorkspace.tsx) still owns creation, saved messages, stream/buffer fallback, idempotent retry, cancellation, archive/delete, Markdown/code copying, and unsent drafts. Its contextual task-review action opens a **separate** form without submission. Conversation IDs remain server-issued; the hash only selects them.

Chat uses the existing authenticated Web v1 SSE endpoint. For `engine_stream`, ordered text deltas are painted as plain text at most once per animation frame; the saved completed message replaces the preview and renders as Markdown. `buffered` never replays the completed text character by character. If the connection drops after acceptance, the browser checks the saved response by ID without resending the prompt. The current endpoint does not resume a partially transmitted stream.

| Destination            | Center workspace                                                                                                             | Context and authorized data                                                                                                                 |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| AI Workspace (landing) | Conversation reader, multiline composer, saved history and response states                                                   | Collapsible conversation events and draft-to-task review, active supervisor tasks and pending approval links; unavailable access is labeled |
| Tasks                  | Supervisor submission and persisted task timeline                                                                            | Collapsible active-task links; task detail follows replayable SSE, cancellation and verified result/artifact download                       |
| Approvals              | Exact-operation review queue and decision modal                                                                              | Mapped-device list used for backend-enforced identity, digest and expiry                                                                    |
| Automations            | Capability-gated schedule form, list, selected details and run history                                                       | No invented schedules when the backend feature is disabled                                                                                  |
| Memory Center          | Create/search form beside saved entries and selected detail                                                                  | Confirmed deletion and explicit retention limits                                                                                            |
| Device Hub             | Mapped-pairing list and selected-device details                                                                              | Confirmed revocation; no browser discovery or token exposure                                                                                |
| Trading Lab            | Offline feed state, explicitly opt-in synthetic candles, indicators/signals, virtual order ticket, positions, P&L and ledger | No versioned market feed; synthetic data is never presented as provider data; browser-local persistence                                     |
| Developer Studio       | Supervisor evidence timeline and approval queue                                                                              | GitHub/CI status explicitly unconnected; no fabricated build results                                                                        |
| Settings               | Session, visual preferences, integration limitations                                                                         | Backend-origin session check and sign-out                                                                                                   |

[`controlState`](./src/pages/controlState.ts) shares backend task, approval and device queries between the shell and pages. [`TaskDetail`](./src/pages/TaskDetail.tsx) keeps SSE replay and artifact integrity verification; [`api/`](./src/api) and generated contracts are unchanged. The inspector does not claim presence or model health. Compact AI empty states use the existing bounded Water Lab engine behind a small masked material; the shader pauses when chat is inactive and no other UI control creates a WebGL context. Anchored search/notifications and the traveling nav indicator keep the original keyboard and reduced-motion behavior.

The Chromium walkthrough in [`workspace.spec.ts`](./e2e/workspace.spec.ts) captures the actual desktop and mobile state of all nine destinations plus a browser screencast started **after** fixture sign-in. Test sessions are isolated; screenshots of unavailable capabilities report their true state. The live supervisor worker remains a separate backend prerequisite, not a feature supplied by this redesign.

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

### Latest verification

- Strict TypeScript, ESLint, the production build, and all 15 Vitest unit/component tests passed.
- The 4.0 suite covers 25 Chromium cases (19 workspace, 6 supervisor control) plus a separate buffered-chat case. It retains cookie-session, conversation, cancellation, memory, approval, device, automation, SSE, artifact and integrity checks while adding lazy chat entry, rail/inspector, all-route layout and Water Lab lifecycle checks. Axe found no violations on scanned desktop and mobile views.
- A production build measured 126.30 kB of entry JavaScript and 10.75 kB of CSS gzipped. One loopback navigation measured 6 ms to `DOMContentLoaded`; Trading Lab and Memory Center stayed lazy on the AI landing. In a short Chromium run on this Mac, navigation at 1440×900 without recording had a 16.7 ms p95 browser frame interval, with zero intervals over 25 ms among 146 samples. During the walkthrough recording it was 16.8 ms p95 with two intervals over 25 ms among 1,060 samples. These are browser frame intervals, not shader timings, battery metrics, or low-end hardware evidence.

Archival Alpha screenshots (the conversation uses the labeled test engine): [Sign-in](./docs/alpha-visuals/sign-in.png) · [Desktop Overview](./docs/alpha-visuals/desktop-overview.png) · [Desktop conversation](./docs/alpha-visuals/desktop-conversation.png) · [Mobile conversation](./docs/alpha-visuals/mobile-conversation.png).

## Water Lab integration

**Source:** `~/Documents/Jarvis-Water-Lab`, standalone private local Git repository (`dev/water-material`, verified at `906ca1465c6fea01553b275fa4c4a2b063ceaf9d`, no configured remote). The reused `water.glsl`, `main.mjs`, and `simulation.mjs` originate at `2dcf244817368a2cf8b86e5f737fa40ddcc91678`. Run `npm start` in Water Lab and open `http://127.0.0.1:8947/`, or use a free loopback `PORT`. Its three original unit tests passed. The source checkout was left unchanged.

The lab uses a Node static server, JavaScript springs, WebGL 2 / GLSL ES 3.00, and a Canvas2D offscreen texture for editable scene text. Chromium verified actual lobe separation and settling as well as refraction of edited background text. The ray-marched volume has the strongest optical depth but takes more GPU time and visibly distorts text. GPU particles are cheaper to draw but break the surface into beads. The continuous height field was the practical fit for a shallow workspace surface. In contrast, the previous Thozha hero redrew SVG paths on the main thread and did not have optical shading.

- [`water.frag.glsl`](./src/water/water.frag.glsl) adapts the lab's height function, traveling damped ripple equation, water IOR, Schlick Fresnel, and thickness attenuation. Thozha adds crossing waves, slope-derived normals, and silver reflections in a single pass over a graphite scene; it does not refract the actual page. Foreground text is normal DOM over a fixed scrim.
- [`motion.ts`](./src/water/motion.ts) adapts the lab's damped 240 Hz spring substeps and frame-delta clamp for pointer response. Four packed ripple sources replace the lab's lobe and particle state. The production effect does not simulate detached droplets, volume conservation or physical caustics.
- [`renderer.ts`](./src/water/renderer.ts) uses the lab's fullscreen triangle, without ray marching, floating-point render targets, or a per-frame scene upload. [`engine.ts`](./src/water/engine.ts) owns resizing, scheduling, input and context loss/recovery. It creates one visible canvas on demand. Balanced mode targets 30 draws/s with at most 260,000 backing pixels; economy targets 24 with at most 96,000 pixels and no pointer ripples. DPR and buffer edge length are capped. Sustained missed frame deadlines step down to economy and then to the still. No package dependency or third-party reference code was added.
- Offscreen and hidden tabs stop scheduling draws. Either reduced-motion preference, lack of accelerated WebGL 2, or context loss shows the same locally rendered [still frame](./src/water/water-still.webp). On navigation the engine releases the program and vertex array and detaches observers and listeners. Sign-in uses the still without a canvas.
- The same damped easing guides the reversible search and notification surfaces, one traveling navigation highlight and tactile button feedback. Search reveals a fixed-width field from the stationary magnifier/X without animating width. Notifications deform only their background. The rail, header and scroll ownership stay fixed; keyboard focus and dismissal behavior are retained. No other surface runs a fluid simulation.

### Measured on this Mac

**Archival 3.0 comparison, before the 4.0 shell:** Headless Chromium 159 on Apple M5 / ANGLE Metal, 1280×800 at 1×. Recording was off during measurements. The Thozha rows use the then-current production builds with the same pointer sweep over approximately six seconds; lab rows are short ambient samples of the original fullscreen demos. The 4.0 chat material is much smaller; these GPU and memory values must not be presented as its benchmark.

| Case                              | Browser frame interval p95 | Browser intervals > 25 ms | Main-thread task time per elapsed second | GPU draw p95 |
| --------------------------------- | -------------------------: | ------------------------: | ---------------------------------------: | -----------: |
| Original Water Lab, 3D volume     |                    16.7 ms |                         0 |                                   8.1 ms |      8.87 ms |
| Original Water Lab, height field  |                    16.8 ms |                         0 |                                  12.1 ms |      5.48 ms |
| Original Water Lab, GPU particles |                    16.7 ms |                         0 |                                 112.2 ms |      0.97 ms |
| Thozha at `3d3bd43d`, SVG hero    |                    16.7 ms |                         2 |                                  73.8 ms |    Not timed |
| Thozha with Water Lab, WebGL hero |                    16.7 ms |                         0 |                                  18.7 ms |      0.35 ms |

The integrated hero rendered into an 883×294 buffer for a 943×314 CSS-pixel area. The separate GPU timer sample recorded 89 timings across 90 draws in 3 seconds. Browser frame intervals are not the hero draw rate; the SVG sample had one 103 ms long task and the final sample had none. In these runs, renderer-process RSS rose from 230.36 to 283.70 MiB and GPU-process RSS from 78.22 to 94.89 MiB. These are Chromium process totals, not memory attributed solely to the shader. JS heap snapshots were 6.97 and 6.31 MiB respectively. The lazy engine is 3.77 kB gzipped and the still is 15.42 kB. These short Mac samples do not establish mobile GPU timing, battery use, or 120 Hz behavior.

### Workspace layout and motion history

The prior Overview hero used a large Water Lab surface. The 4.0 workspace does not render that hero: its default route is AI Workspace, and only the compact conversation empty state invokes the GPU material. Header search still expands left from its fixed magnifier/X, the mobile drawer retains focus and dismissal, and desktop navigation uses one sliding active surface. The remaining measurements and screenshots below describe previous UI milestones, not the current layout.

Chromium layout checks cover all eight routes at 1440×900, 1280×800, 1024×768, 768×1024, 390×844, and a 720×450 CSS-pixel viewport at 2× device scale (the effective viewport of a 1440×900 window at 200% zoom). They verify fixed shell and header geometry, 237px desktop rail, scrollable content/chat, reachable Settings controls, no document or horizontal overflow, drawer focus restoration, and mobile Settings accessibility. Separate interaction tests verify the search anchor stays put as the field expands left, keyboard destination selection, focus return, hamburger state, active navigation highlight geometry, and outside-click/Escape drawer dismissal. Axe has no WCAG 2.x A/AA violations on Overview, search, mobile navigation, and chat. Before refinement, Settings made the document 1,131px high at 1280×800; afterward the document stays 800px, with 829px of Settings content scrolling inside its 704px main pane. A 1.2-second local Chromium requestAnimationFrame sample observed 73 frames (median and p95 interval 16.7ms); this measures browser pacing only, not GPU cost or low-end-device performance. Two hero captures 2 seconds apart changed 24,102 pixels above a 5-level RGB threshold; computed water transforms changed while both reduced-motion modes set animation to `none`.

[Before Overview](./docs/alpha-visuals/refinement/before-1440-overview.png) · [After Overview](./docs/alpha-visuals/refinement/after-1440-overview.png) · [Before Settings](./docs/alpha-visuals/refinement/before-1280-settings.png) · [After Settings](./docs/alpha-visuals/refinement/after-1280-settings.png) · [Before mobile AI](./docs/alpha-visuals/refinement/before-390-ai-workspace.png) · [After mobile AI](./docs/alpha-visuals/refinement/after-390-ai-workspace.png) · [Motion frame 0](./docs/alpha-visuals/refinement/motion-frame-0.png) · [Motion frame +2s](./docs/alpha-visuals/refinement/motion-frame-2s.png) · [Measurements](./docs/alpha-visuals/refinement/motion-measurement.json).

Search and drawer interaction captures: [Anchored search](./docs/alpha-visuals/refinement/anchored-search.png) · [Mobile navigation drawer](./docs/alpha-visuals/refinement/mobile-navigation-drawer.png).

### Previous liquid interaction milestone

The pre-change Playwright recording showed that the hero translated fixed paths, notifications opened a centered modal, and search results vanished immediately on close. The desktop rail already used a moving shared highlight; the drawer and page used entrance/exit or entrance-only motion. The device cards only changed fill at selection.

Notifications and search results adopted a reversible, trigger-aligned surface. A rounded clip revealed the panel while its background changed scale and elevation independently of the text. The search handle stroke extended into the first diagonal of the X. Tactile lighting was limited to actionable device, conversation, memory and paper-trading controls.

The prior hero redrew SVG contour geometry with counter-propagating waves and a decaying pointer ripple rather than translating fixed artwork.

A 14.49-second local Chromium interaction sample without recording overhead observed 868 frame intervals (16.8 ms p95, none over 25 ms) and no Long Tasks API entries. Chromium reported 332 ms of main-thread task time, including 105 ms of script, 16 ms of layout, and 37 ms of style recalculation. This is a single local measurement; it does not isolate GPU time or certify lower-end hardware.

## Contract handoff

Runtime wiring targets `codex/core` at `f5e5bb45` and OpenAPI v1 at `0e4cd934`. `npm run contract:generate` generates browser-only types from the pinned Git object; fetch `codex/openapi-contracts` if needed. The error envelope is now included. Approval review uses the requester device returned by the authorized approval endpoint, while decisions still require an explicit choice and the server-provided digest.

In the composer, type `/` for commands. `/task <instruction>` opens the existing task form; only an explicit submit after a fresh readiness check sends a task, and its server-issued link opens the saved timeline and SSE progress. `/status` queries the authenticated supervisor readiness endpoint and distinguishes reported readiness from unverified model authentication. `/help` lists active and deferred commands. These actions do not write chat messages or claim a persisted task-to-conversation relationship. Normal text remains chat.

Tasks is a compact task-first workspace. The main input accepts an instruction; device and capability scope stay in the expandable review. Task counts, history, lifecycle milestones, output and artifacts come from the supervisor endpoints. Milestones are shown only when durable state events or the saved task state support them; the UI never calculates a progress percentage.

Trading Lab currently has no versioned Web market-data endpoint. Its offline screen therefore contains no fabricated quotes. Synthetic candle generation is opt-in and permanently labeled `SYNTHETIC DEMO`; it is not described as historical, delayed or live market data. The frontend adapter boundary is documented in `src/trading/marketData.ts`. A proposed Codex contract is `GET /v1/web/market-data/instruments` plus `GET /v1/web/market-data/candles?symbol=&exchange=&interval=` with source, delay/mode, timezone, `updated_at`, and complete OHLCV candles. Neither route should be added client-side until it exists in OpenAPI. The paper ledger remains virtual and browser-local with market/limit orders, cancellation, FIFO positions, realized/unrealized P&L, fees and persistence; no brokerage request is possible.

New Conversation opens an unsaved draft with focus in the composer. The first ordinary message supplies a short deterministic title to the conversation create API, so the title survives reload without a rename endpoint or an extra model call. Stop generating still invokes the conversation cancellation API; the host can continue running until cancellation takes effect.

For Codex to reconcile:

- Supervisor submission can return `200` for a completed idempotent replay as well as `202`. Cancellation returns `task_id`, `state`, `cancelled`, and `action_may_have_started`, not `WebTaskResult`.
- Approval details expose the reviewed arguments, digest, and the task's bound `requester_device_id` to the authorized owner. The browser displays that pairing and never tries a different device or rewrites the digest.
- The automation update and lifecycle endpoints return action receipts in this runtime, not updated schedule objects. The UI refreshes the persisted detail/history after a mutation. When the feature flag is off the capabilities route itself is unmounted (`404`); when mounted, its operation flags are authoritative.
- Task detail contains neither objective, capability scope, nor requester metadata. The UI uses a task ID if no summary was returned and checks artifact eligibility using the actual artifact route. A result digest can cover pre-truncation text, so the browser labels the marked preview as unverified instead of claiming a match.
- A separate owner-scoped supervisor readiness signal would let the UI distinguish an available history API from an unavailable task worker before submitting. Production HTTPS, live SDK execution, notification history, memory correction/retention and read-only market data still need follow-up. No companion or generic backend routes are substituted.
