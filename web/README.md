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

### Deployment boundary

For an actual deployment, serve `dist/` and proxy **only the contracted browser Web v1 routes** under the same trusted HTTPS origin. Configure the backend with that exact external origin and leave insecure-loopback cookies disabled. A trusted proxy must preserve the original `Origin` and host, communicate the external HTTPS scheme through trusted proxy headers, leave cookies intact, prevent caching of all private API responses, and pass `text/event-stream` through without buffering. Do not log password bodies, cookies, CSRF headers, chat bodies, or memory search queries. Do not expose the raw OpenJarvis server, companion gateway, or general OpenJarvis APIs to the browser. Vite's proxy is for local development and test, not a production gateway.

The backend sets and validates the opaque `HttpOnly`, `SameSite=Strict` cookie. Only the in-memory CSRF value accompanies writes, including DELETE. Private UI and caches are scoped to the verified session, removed on expiry or HTTP 401, and hidden while sign-out is unconfirmed. Offline reads, when already cached, are labeled. No session keys, passwords, conversation bodies, memory entries, or device credentials go into browser storage. Only the motion preference and explicitly simulated paper fills are stored locally.

## Capability status

| Workspace area                             | Alpha behavior                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Overview and Settings                      | Actual browser session verification, expiry and sign-out; quick navigation and a persisted motion preference. Session verification is not Mac, service, or model health.                                                                                                                                                                                                                                                                                                                                                                                                               |
| AI Workspace                               | Real conversation creation, selection, persisted history and lifecycle events, archiving, permanent deletion, Markdown, code copying, submission, cancellation, and safe message replay after an uncertain send. The browser chat is conversational and cannot invoke Mac tools.                                                                                                                                                                                                                                                                                                       |
| Response delivery                          | Uses the contracted SSE `POST` and shows only actual deltas from engines reported as incremental. Buffered mode (including the current Codex SDK contract) shows progress and then the saved complete response. If SSE is unavailable with `404`/`405`, uses the contracted `202` endpoint with the same idempotency key, following persisted status for the active, visible conversation. A disrupted accepted stream recovers from saved status, not a fake stream or an automatic resend. Cancellation suppresses late output, but cannot forcibly interrupt a blocking model call. |
| Memory Center                              | Real owner-scoped create, list, detail, substring search, and confirmed deletion. Saving is explicit; no semantic search, automatic chat injection, shared Android memories, expiration settings, or update endpoint. Corrections require creating a replacement and deleting the original.                                                                                                                                                                                                                                                                                            |
| Trading Lab                                | Functional **browser-local simulation** at fixed fictional quotes: order ticket, cash ledger, positions, P&L and equity chart. No live/delayed/historical feed, Paytm Money, real orders, strategy runs, watchlists, risk model or server journal. The sandbox can be reset separately; its records are not account data.                                                                                                                                                                                                                                                              |
| Device Hub                                 | Labeled examples only. No network discovery, pairing, presence, permission or revocation endpoints wired.                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Automations                                | Local form drafts while the page is open. No server save, scheduler, enable/disable, execution or history.                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Developer Studio, approvals, notifications | Explicit unavailable states. No simulated jobs, test results, approvals, artifacts, or unread counts.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

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
THOZHA_E2E_PYTHON=/path/to/venv/bin/python npm run test:e2e
THOZHA_E2E_PYTHON=/path/to/venv/bin/python THOZHA_E2E_MODE=buffered \
  npm run test:e2e -- --grep 'renders the reported SSE mode' --output test-results/buffered
```

Playwright opens the production bundle on loopback ports `45837`/`45838` using the **real** repository `create_app`, auth/CSRF, conversations, and memory stores. It creates temporary owner-only storage and a randomized test credential, supplies a deterministic engine to exercise streamed/buffered delivery, and uses no provider account, existing store, or production credential. The fallback and replay tests deliberately interrupt delivery at the browser boundary. This verifies local browser-to-backend integration, not a deployment or live inference service. This suite uses Chromium and checks WCAG 2.x A/AA rules with axe on sign-in, Overview and chat.

### Alpha verification results

- Strict TypeScript, ESLint, formatting, and production build passed; 9 Vitest unit/component tests passed.
- 8 Chromium integration tests passed against the real Web API with the streaming fixture, plus a separate buffered-mode browser test. The scanned screens had no axe violations.
- Production entry assets measured 116.46 kB JavaScript and 7.68 kB CSS gzipped. One warm Chromium loopback navigation measured 6 ms to `DOMContentLoaded`; none of the AI Workspace, Trading Lab, or Memory Center chunks loaded on Overview. This is not a deployment or model latency benchmark.

Playwright screenshots (the conversation uses the labeled test engine): [Sign-in](./docs/alpha-visuals/sign-in.png) · [Desktop Overview](./docs/alpha-visuals/desktop-overview.png) · [Desktop conversation](./docs/alpha-visuals/desktop-conversation.png) · [Mobile conversation](./docs/alpha-visuals/mobile-conversation.png).

### Workspace layout and motion refinement

The approved monochrome surfaces and navigation are preserved. The Overview liquid scene now uses lightweight layered SVG wave contours, pearl/silver shading and slow transform/opacity animation, with a small pointer parallax on fine-pointer devices. It does not create simulated data, fetch motion assets, or use WebGL. System and in-app reduced-motion settings disable ambient movement and dialog/page transitions.

Chromium layout checks cover all eight routes at 1440×900, 1280×800, 390×844, and a 720×450 CSS-pixel viewport at 2× device scale (the effective viewport of a 1440×900 window at 200% zoom). They verify fixed shell and header geometry, 237px desktop rail, scrollable content/chat, reachable Settings controls, no document or horizontal overflow, drawer focus restoration, and mobile Settings accessibility. Before refinement, Settings made the document 1,131px high at 1280×800; afterward the document stays 800px, with 829px of Settings content scrolling inside its 704px main pane. A 1.2-second local Chromium requestAnimationFrame sample observed 73 frames (median and p95 interval 16.7ms); this measures browser pacing only, not GPU cost or low-end-device performance. Two hero captures 2 seconds apart changed 24,102 pixels above a 5-level RGB threshold; computed water transforms changed while both reduced-motion modes set animation to `none`.

[Before Overview](./docs/alpha-visuals/refinement/before-1440-overview.png) · [After Overview](./docs/alpha-visuals/refinement/after-1440-overview.png) · [Before Settings](./docs/alpha-visuals/refinement/before-1280-settings.png) · [After Settings](./docs/alpha-visuals/refinement/after-1280-settings.png) · [Before mobile AI](./docs/alpha-visuals/refinement/before-390-ai-workspace.png) · [After mobile AI](./docs/alpha-visuals/refinement/after-390-ai-workspace.png) · [Motion frame 0](./docs/alpha-visuals/refinement/motion-frame-0.png) · [Motion frame +2s](./docs/alpha-visuals/refinement/motion-frame-2s.png) · [Measurements](./docs/alpha-visuals/refinement/motion-measurement.json).

## Contract handoff

Runtime wiring is based on `codex/core` at `90927d50` and the OpenAPI v1 on `codex/openapi-contracts` at `e7ae0cd2`. See the [official Web types](../contracts/web-api/v1.ts) and [integration guide](../docs/COPILOT_INTEGRATION.md). `npm run contract:generate` generates browser-only types from that pinned Git object; fetch `codex/openapi-contracts` if needed, or pass a reviewed local spec as an argument. The published spec references an undefined generic `Error` schema; generation excludes just that error payload shape and the client uses HTTP status messages. There are no changes to the shared specs.

Open items for Codex: credentialed HTTPS deployment verification, provider-specific streaming latency and behavior, owner-safe tasks and approvals, browser device permissions, artifact verification/delivery, scheduler integration, notification API, memory correction/retention policy, and read-only market/broker data contracts. No companion or generic backend routes are substituted for these gaps.
