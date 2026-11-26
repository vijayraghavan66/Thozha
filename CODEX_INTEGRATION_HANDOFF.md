# Devices & Agents integration handoff

## Scope and isolation

Repository: https://github.com/vijayraghavan66/Thozha
Branch: `sahaa/web-device-platform`
Worktree: `/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-web-device-platform`

This extends the single existing React Web application, not a second frontend. No Android/backend source, shared working tree, service, pairing identity, credential, certificate, persistent database, production configuration or release branch was changed. No merge or deployment was performed.

## Source review / compatibility

- `main` and `origin/main`: `12282858c9fb5930dffccff855cec16dad1e49e6` at inspection; not the complete Beta 1 source.
- Chosen Web baseline: `sahaa/web-quality-pass@a0ee95f5`, including `copilot/web@16c03db1`, attachment/pairing UI `7b0a8e9c`, and earlier existing workspace implementation. All approved quality changes retained.
- Voice candidate reviewed and incorporated by two clean cherry-picks: source `a40fc0b7` → local `169bf091`; source `8c63cf2e` → local `aeca3665`. No shared speech worker started. Existing opt-in `THOZHA_VOICE_DEV=1` behavior preserved. Voice remains an integration candidate with its existing physical acceptance gates, not newly physically verified here. See `web/voice/FINAL_HANDOFF.md`.
- Backend API review: `codex/web-devices@839bd6d8` is older than integrated `codex/core@b162d409`; the latter includes authenticated QR APIs in `thozha_web_devices.py`. `thozha_web_pairing.py` is not a separate module at that revision.
- Android actual source tip at inspection: `codex/samsung-usb-api@3cbf6b8b`, not reference `2c9f4b5c`. Read-only; no Android changes incorporated or required.
- Other independent tips recorded: `codex/local-discovery@f2a3c769`, `codex/qr-gateway-ssid@dfa126c5`, `codex/beta1-result-delivery@752e79a9`, `codex/openapi-contracts@0f0541d9`, `codex/web-supervisor@0984bbc7`.
- OpenJarvis checkout `/Users/vijay-25589/.openjarvis/src` actually points at `f4a49df116985547dc33c3d3ea8d6854b2fdffa8`; it has uncommitted work. It was inspected read-only, not copied, edited or restarted. Its interpreter was reused only for temporary-HOME test processes against a committed backend archive.
- Read `Jarvis-Lab/PROGRESS.md`; examined auth, routes, task/approval adapters, QR protocol, component primitives and plugin/tool registries. No previous unified architecture report was found in the searched Sahaa / Jarvis-Lab architecture, docs and report locations. Runtime `AgentRegistry`/`ToolRegistry` entries describe implementations, not enrolled execution devices; they must not become fabricated installed-agent cards.

## Implementation versus backend-blocked matrix

| Feature | Implementation / boundary |
| --- | --- |
| Device inventory/details | Existing real owner-mapped active pairing API, creation/expiry and stable ID preserved |
| QR pairing | Existing signed nested payload, countdown, pending identity review, approve/reject, claim distinction, cancellation, terminal/error states retained; QR hidden on status failure/session disconnect |
| Already-paired recovery | Added non-destructive transport/mapping guidance; no reset or forced re-enrollment |
| Device revocation | Existing confirmed CSRF mutation, cache invalidation and notification cleanup retained; removes active access, not history |
| Presence/last seen/versions | Typed additive adapter and UI ready; unavailable/unknown on current backend, stale/offline safeguards; no guessed facts |
| Device capabilities/permissions | Displays host-reported supported/authorization states only; editing unavailable until backend authority exists |
| Agents | Added lazy-loaded `#/agents` route, sidebar/mobile navigation, installed inventory/details, presence/capabilities, install/recovery interface |
| macOS | Reuses OpenJarvis; no duplicate runtime or desktop UI |
| Windows/Linux | Honest future-agent enrollment/compatibility screens; no verified download, shell command or fake installer |
| iOS | Future native companion constraints documented; no application or unrestricted background promises |
| Task timeline/approvals | Existing APIs, IDs, scope checks, exact digest and idempotency preserved; links to same histories |
| Target routing | Blocked: existing requester identity is not execution target; no dishonest target picker or weakened authorization |
| Agent lifecycle/enrollment | Versioned proposal documented; no speculative production mutations |
| Permanent device deletion | Backend-blocked; never silently substitutes destructive removal for revocation |
| Themes/accessibility | Existing silver styling and shell retained; scoped system-light/dark management styles, keyboard-native controls and shared focus-trapped modal |

## Required backend work (separate Codex review)

Implement `GET /v1/web/device-platform/inventory` using the executable schema and security semantics in `contracts/device-platform/v1.md`. Current host correctly shows unavailable; 404/501 are not turned into zero agents. This is the only new read endpoint invoked. No new backend or identity store was created.

Bind presence to authenticated host observations and owner-filtered enrollment. Bind any future routing target into durable task state, capability checks and exact approval digest; never reinterpret `requester_device_id`. Enrollment, permission editing, lifecycle actions, verified signed download artifacts and permanent removal require separate reviewed backend work. The contract document distinguishes deployed APIs, proposed reporting and future lifecycle proposals.

## Safe integration

1. Fetch this branch and review it in a new isolated worktree. Do not check it out over Codex's active worktrees, reset branches or copy `.env`/runtime data.
2. Compare latest release Web baseline against `a0ee95f5`. The two voice commits are cherry-picks, so avoid applying the originals again. If release already has voice or divergent Chat changes, merge carefully and keep only the devices feature delta after review.
3. This branch intentionally does not merge old backend source into the Web line. Compose the release with reviewed backend `b162d409` or a later compatible revision separately. Verify existing owner-mapped devices, QR and supervisor contracts before enabling controls.
4. Add the exact inventory path to the production Web reverse-proxy allowlist when the endpoint is implemented; the local Vite allowlist already includes it. Keep same-origin session, Origin/CSRF and no-store controls; never proxy all admin routes.
5. Run `npm ci`, `npm run typecheck`, `npm run lint`, `npm test`, `npm run build` from `web/`. Run the isolated browser suites as described in `web/docs/DEVICE_PLATFORM_VALIDATION.md`.
6. Production output is the existing `web/dist/` build, intentionally ignored in Git. No auto-deploy, gateway/supervisor restart, host pairing reset or data migration is needed for these UI changes.
7. Keep inventory unsupported until backend facts exist. Obtain actual owner-reviewed physical Android/Mac pairing, task routing and microphone/speaker acceptance in the release's authorized test window; fixture results are not physical verification.

See `web/docs/DEVICE_PLATFORM_VALIDATION.md` for actual command outcomes and remaining acceptance limits.

## Validation evidence

Latest local validation in the isolated worktree:

- `npm run typecheck` — passed.
- `npm run lint` — passed.
- `npm test -- --run` — passed, 16 files / 74 tests.
- `npm run build` — passed, production assets generated in ignored `web/dist/`.
- `THOZHA_E2E_BACKEND=/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-codex-core THOZHA_E2E_PYTHON=/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-web-device-platform/openjarvis/.venv/bin/python npm run test:e2e -- --project=workspace --project=control --reporter=line` — passed, 44 tests.
- `THOZHA_E2E_BACKEND=/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-codex-core THOZHA_E2E_PYTHON=/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-web-device-platform/openjarvis/.venv/bin/python npm run test:e2e -- --project=device-platform --reporter=line` — passed, 6 tests.

A first browser attempt against the branch-local backend snapshot produced endpoint failures because that snapshot lacks the newer Web devices/supervisor/attachment routes expected by this UI baseline. Retesting against compatible `codex/core@b162d409` resolved those failures. No live service, device identity or production data was touched.
