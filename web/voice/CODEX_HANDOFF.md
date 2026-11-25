# Voice 2 backend coordination boundary

No active backend/runtime/LAN changes were made. Voice stays on durable POST /messages plus owner-authenticated polling. Do not enable the old disconnect-cancelling POST /messages/stream for latency.

Inspected reference: openjarvis/src/openjarvis/server/thozha_web_conversations.py:977-1006 cancels on CancelledError and unfinished generator close. Codex streaming worktree HEAD observed: 86e09d3a.

Requested independently testable backend contract:
1. Submission persists idempotency key/content binding and creates one disconnect-independent generation worker.
2. Separate owner-authenticated read-only SSE subscription to that response, with durable monotonically numbered public final-answer deltas and Last-Event-ID replay. No reasoning/tool output.
3. Subscription disconnect never cancels inference; explicit authorized cancel does.
4. Return capability/version only when this contract is active. Never infer durability from engine_stream.
5. Same key/content replay across refresh produces one user message and one assistant response. Reject conflicting content and foreign owners.
6. Tests: disconnect before acceptance response; disconnect midway through first sentence; reconnect replay without duplicate speech; worker restart; explicit cancellation; authorization/CSRF.

Then the client can feed complete phrases to neural synthesis before completion without sacrificing recovery. Until that deployment is reviewed, the 1–3 second full conversational target is not demonstrated. Reducing frontend polling alone cannot remove multi-second model generation.
