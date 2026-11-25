# Final validation update

This update supersedes earlier experimental SSE behavior described in README.

## Durable generation selected by default

Two refresh tests against real Web SSE FAILED: the deployed server explicitly
cancels stream-owned generation when the browser disconnects (see
`thozha_web_conversations.py:1673–1705` in codex/core). Merely retaining a request
ID cannot turn a cancelled stream into an automatically resumable answer.

Default call turns now use existing POST /messages plus saved-result reconciliation
at 600 ms. This gives up early model text in exchange for genuine refresh recovery.
Sentence synthesis still overlaps playback. The retained experimental streaming path
is opt-in via `thozha.voice.experimental-stream=1` and is NOT recommended until the
backend owns generation independently of connection lifetime.

Final real-backend synthetic-media recovery test PASSED after this change:
- one durable submission;
- temporarily offline, then browser refresh;
- a new explicit Voice click (no automatic mic reacquisition);
- same response ID recovered and spoken;
- history contains exactly one user and one assistant message;
- no page errors.

Owner-spoken physical turns and their 9.3–9.6 second scheduled-audio measurements
were on the earlier SSE path. Do not attribute those exact timings to the final
buffered/polling configuration; final default acoustic latency is unmeasured.

No production restart, merge or deployment is performed. Preview remains on loopback
5176. Existing 5174 frontend, 8000 backend, LAN gateway and Android remain unchanged.

## Completion assessment

The integrated browser call implementation works for real microphone turns and
local neural playback. It is NOT yet fully accepted against the requested finish
line: owner-confirmed natural voice quality, robust acoustic barge-in and actual
first-audible latency remain open. Energy VAD is a practical baseline, not a claim
of production-grade duplex echo separation. The final default deliberately favors
durable history and no duplicate execution over unsupported token-streaming claims.
