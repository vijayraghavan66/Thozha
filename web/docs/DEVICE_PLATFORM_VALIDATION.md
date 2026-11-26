# Device platform validation

All commands were run from `web/` in the isolated `sahaa/web-device-platform` worktree. Browser suites used temporary HOME/OpenJarvis storage and the committed backend snapshot at `/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-codex-core`; no live gateway, supervisor, speech worker, companion identity, certificate, credential or production database was used.

## Latest passing evidence

```sh
npm run typecheck
```

Passed.

```sh
npm run lint
```

Passed.

```sh
npm test -- --run
```

Passed: 16 files, 74 tests.

```sh
npm run build
```

Passed; production assets generated under ignored `web/dist/`.

```sh
THOZHA_E2E_BACKEND=/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-codex-core \
THOZHA_E2E_PYTHON=/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-web-device-platform/openjarvis/.venv/bin/python \
npm run test:e2e -- --project=workspace --project=control --reporter=line
```

Passed: 44 tests. Covers existing Chat, conversations, memory, attachments, task timeline, supervisor events, approvals, schedules, mapped devices, QR pairing, responsive shell, screenshots, keyboard flows and accessibility checks.

```sh
THOZHA_E2E_BACKEND=/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-codex-core \
THOZHA_E2E_PYTHON=/Users/vijay-25589/Documents/Jarvis-Lab/worktrees/thozha-web-device-platform/openjarvis/.venv/bin/python \
npm run test:e2e -- --project=device-platform --reporter=line
```

Passed: 6 tests. Covers inventory adapter behavior, agent install unavailable states, responsive Devices/Agents layouts, accessibility, offline/reconnect handling, QR lifecycle, owner approval/rejection, expiry/cancellation and failed QR creation.

## Investigated 500 / endpoint failures

The first browser run was against the branch-local backend snapshot, which did not contain the newer Web device/supervisor/attachment routes required by this Web baseline. The visible failures were 404/405 and readiness unavailability rather than a production 500 root cause. Retesting against the compatible `codex/core@b162d409` worktree resolved the API failures and all workspace/control/browser suites passed.

The remaining unsupported endpoint is intentionally `GET /v1/web/device-platform/inventory`; 404/501 for that route are handled as honest unavailable states and are covered by tests. No backend service was modified to make that endpoint appear present.

## Limits

- Fixture inventory and QR tests are browser-contract validation only, not physical Android/Mac LAN verification.
- Voice candidate code was retained, but speech workers and physical microphone/speaker acceptance were not started in this pass.
- Windows/Linux installers and lifecycle mutations remain unavailable until signed artifacts and backend contracts exist.
