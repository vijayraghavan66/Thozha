# Independent Beta 1 QA handoff

Scope: targeted source review and isolated classification tests, not full release
certification. Codex is integration/release owner. No production runtime edits,
physical Android reruns or service restarts. PROGRESS.md latest inspected transport
checkpoint: 2026-11-25; Android 2c9f4b5c, backend source f4a49df1.

## P0: unsafe shell approval classification — CONFIRMED

`~/.openjarvis/src/src/openjarvis/tasks/supervisor.py:111–150`,
`requires_approval`, still allows all `diskutil` subcommands and `git branch`
without mutation-flag inspection, and matches executable basename rather than
trusted absolute executable identity. Isolated AST invocation returns False with
`declared=True` for eraseDisk, branch -D, and an arbitrary path ending in df.
No command was executed. Reproducer: `web/voice/audit_policy.py`.

The live checkout has uncommitted ToolExecutor durable authorization around
`tools/_stubs.py:405–520`, and calls this classifier. The later legacy-confirmation
fallback may block some declared generic tools but is not a substitute for correct
policy; arbitrary caller/tool metadata must not determine whether a destructive
operation needs durable authorization. This review does not prove a currently
reachable end-to-end Mac MCP exploit. Treat classifier failure as a release blocker.

Proposed Codex change: default shell operations to approval, allow only reviewed
complete operations with validated executable and argv; never override explicit
confirmation requirements. Add inert executor tests plus exact classifier cases.
Do not run destructive payloads against a live system.

## P0: source != deployed security implementation — CONFIRMED

Monorepo main remains 12282858. Backend source is f4a49df1 with dirty security-
relevant `_stubs.py`, auth middleware, CLI and other files. Gateway deployment is
recorded as f7f23844; raw API is explicitly recorded as an older loaded revision.
Source HEAD cannot certify the running process. Existing dirty work was preserved.
Acceptance: reviewed component manifest, committed authorization changes, loaded
revision diagnostics and matched release artifacts. No independent restart.

## P1: Telegram fallback is blocked by identity mapping — CONFIRMED DOCUMENTARY

Latest PROGRESS records a retained UNCONFIRMED fallback, no supervisor task, and
separate API/Link owners with no authorized binding. f4a49df1 adds a cross-owner
idempotency rejection test; this is a necessary security success, not proof that
fallback works. Android 2c9f4b5c persists rate limits and bounds stale polling.
Do not repeat prior cellular submission solely to raise a test count.
Acceptance: explicit owner-authorized identity binding or a clear unsupported
fallback UI, then one matched-key failover test proving one task/action. Never
make device IDs equivalent merely because request keys match.

## P1: QR source promotion != physical onboarding — CONFIRMED EVIDENCE GAP

PROGRESS records automated disposable pairing tests, preserved Vivo identity and
real pinned LAN/WSS observations. It also explicitly leaves physical QR claim
unpassed and raw API promotion pending. Preserve these distinctions.
Acceptance: Codex-reviewed safe deployment plus disposable phone identity and
genuine owner confirmation. Do not reset the existing Vivo identity to test QR.

## P1: discovery is not physically automatic — CONFIRMED DOCUMENTARY

Mac Bonjour browse sees advertisement; Vivo starts NSD but sees no service callback.
Signed private-endpoint/pinned TLS fallback works historically. Root cause is NOT
established as router filtering. Do not change MDM/firewall/router policy without
authorization. UI must distinguish authenticated direct connectivity from discovery.

## P1: voice contracts and packaging — CONFIRMED

Current installed `engine/codex_sdk.py:724–734` awaits full generate before yielding.
Experimental Web conversations stream at 86e09d3a:1030–1058 cancels on disconnect.
Voice correctly retains durable submit/poll instead. No actual early Luna text
or 1–3s response evidence. Heard-prefix history semantics are absent. See
CODEX_HANDOFF.md and FINAL_HANDOFF.md for exact proposed contract/tests.

The voice HTTP handlers and VAD assets are Vite-development middleware, not
production artifacts. Static build passing does not deploy those handlers.
Require Codex-owned authenticated handlers, bounded admission, supervised workers,
offline weights and approved packaging. Preserve origin/CSRF checks.

## P1: sleep/wake, memory and approvals — EVIDENCE GAPS

PROGRESS contains meaningful physical pinned TLS/WSS and identity-preservation
results; do not invalidate them just because this pass did not repeat them.
DHCP gateway rebind is fixture-tested, not physically induced in that checkpoint.
No full cold-start/sleep-wake acceptance matrix was located in the targeted paths;
latest log is not a substitute for a versioned matrix. Request Codex's release matrix.
Mac ~16.24 GB swap was in use during inspection. This is a contention risk, not
proof of a leak or permission to stop other workloads. GPU utilization unmeasured.
Physical approve/reject/action execution and reboot packaging need artifact-bound
acceptance references before release clearance; not independently rerun here.

## Positive findings preserved

- Owner-bound idempotency rejects cross-device replay rather than weakening ownership.
- TLS/SAN pinning remains enforced; no certificate regeneration proposed.
- Vivo signer/data/first-install identity preserved by documented in-place updates.
- Cooldown prevents Retry-After bypass and unnecessary Telegram resends.
- Gateway async forwarding/shared policy and bounded listener recovery now exist in
  newer source; earlier audit findings must not be repeated as unchanged defects.

## Independent verdict

No release clearance. Confirmed security classification defect plus deployment
provenance and physical acceptance gaps remain. This report does not claim an
exhaustive vulnerability scan, credential scan or independent re-execution of
Codex's physical tests. Integrate reviewed fixes, not unsupported success claims.
