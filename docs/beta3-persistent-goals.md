# Beta 3: persistent personal goals

## Objective

Extend Thozha's existing supervisor and Link task boundary into a durable goal runner. A goal is a sequence of short, authorized work sessions with saved state and explicit wake conditions. It is not a continuously running model conversation.

The first goal fixture is movie-ticket discovery for *Avengers: Doomsday* in Chennai. Acceptance stops before payment and must not hold or purchase real seats.

## Current implementation and verified limits

The branch currently contains a private SQLite goal store, bounded finite-step engine, event log, lease recovery, owner-scoped polling deadlines, conservative retry scheduling, cancellation, exact supervisor approval bindings, an isolated persistent browser-profile manager, and a District MCP adapter. Goal-store and District-boundary regression tests run under `openjarvis/tests/goals/`.

The District adapter uses only the official `https://mcp-server.district.in/v2/mcp` endpoint. Guest OAuth and MCP initialization were tested successfully; the server exposed 10 top-level tools. Thozha now caches only operation schemas returned by the MCP `search` tool with `call_with: execute`, validates nested arguments, and refuses the generic `execute` wrapper for unknown, mutating, or incomplete schemas. One reserved poll cycle allows a small bounded burst of related read calls, while a default 15-minute interval is enforced between monitoring cycles and can be restored from the saved poll checkpoint. The guest grant is read-only; hold, booking, and payment remain disabled.

The live read-only test discovered Chennai, Avengers: Doomsday, EGA Cinemas Kilpauk, an English MS Infinity Vision 3D show at 09:30 on 18 December 2026, and fetched the provider's seat map. District search results did not expose customer-facing seat labels in the returned map; a prior observation exposed grid coordinates and a ticket subtotal, not an all-in amount. No seat was held, no booking/payment tool was called, and temporary access and refresh tokens were revoked (HTTP 200). See the sanitized live evidence and the un-sent enablement request in the Beta 3 output folder.

The goal engine and provider adapter are implemented as reusable components, but they are not yet mounted as authenticated Android goal routes or started by the application service. Android does not yet render structured booking approval cards. District has not enabled a personal non-guest client or exposed a manageable client-registration handle for the first test registration. Therefore persistent live monitoring, Android goal approvals, and any hold/booking action are **not available** in this branch yet; guest discovery is the only live District capability verified. Do not treat this as completed end-to-end booking integration.

## Execution model

1. Persist a goal with an owner, human-readable title, objective, preferences, deadline, status, retry policy, checkpoint, next wake time, and idempotency key.
2. A scheduler claims only due goals through a transaction and lease. It wakes a bounded worker for one finite step, then records its checkpoint, events, and next wake time before returning.
3. Resume on a timer, approved supervisor event, user response, recovered connection, or explicit user action. Do not keep an LLM or browser worker alive while sleeping.
4. On restart, reconcile expired leases. Replay safe reads with the same idempotency key. If a prior booking/hold action may have reached the platform but its result is unknown, stop in `needs_review`; never repeat it automatically.
5. Enforce a goal deadline, attempt budget, per-domain rate budget, cancellation, exponential backoff with jitter, and a maximum live-step duration.

Suggested goal states: `active`, `waiting_until`, `waiting_approval`, `needs_user`, `needs_review`, `completed`, `failed`, `cancelled`, and `expired`. Each run and transition is append-only audit history. Every wake uses the existing supervisor owner identity and produces a normal Link task; goal execution never gains a separate route to the host or payment system.

## Browser and provider boundary

Use a provider adapter interface for `discover`, `show_detail`, `seat_map`, `prepare_action`, and `revalidate`. Each adapter declares its approved origins, supported operations, minimum polling interval, session requirements, and any partner authorization. Reject unexpected origins and redirects. Do not expose cookies, OTPs, UPI PINs, passwords, provider transaction/session identifiers, or payment tokens to model context, logs, task summaries, or approval payloads.

Persist an isolated browser profile per owner/provider, protected by OS file permissions and excluded from logs and artifacts. A browser step runs only while actively needed. CAPTCHA, queue challenges, OTP, UPI PIN, and any other authentication checkpoint transition the goal to `needs_user`; the user completes it in the isolated browser. The worker resumes only after explicit user return and fresh state validation.

No adapter may bypass queues, CAPTCHA, provider rate limits, or other access controls. A source must be an official API/MCP integration or have explicit authorization for automation. This branch does not automate BookMyShow. District is integrated through its official MCP guest discovery path; additional personal-client capabilities depend on District's enablement and authorization flow. [District MCP and integration guidance](https://studio.district.in/mcp/) · [District Terms of Service](https://www.district.in/policies/terms-of-service/).

## Booking approval contract

Every consequential action uses the existing one-time supervisor approval store. A movie-seat approval card must bind a canonical digest to:

- Provider and operation.
- Movie, date, venue, screen, showtime, format, and language.
- Exact adjacent seat IDs and seat class.
- Ticket subtotal, fees, tax, discount, final amount, and currency.
- Offer/payment method only if it changes the amount and is user-authorized.
- Quote timestamp, seat/quote expiry, goal deadline, and bounded alternatives.

After approval, reacquire the provider state and compare the exact seats, showtime, price, fees, and digest. If any field changes or the hold expires, invalidate the approval and ask again. Duplicate approval/booking attempts must be rejected by a goal/action idempotency key. In all acceptance tests, the action policy forbids payment and purchase; test fixtures assert that no payment endpoint or final purchase control is invoked.

### Two-stage amount disclosure

The read-only District seat-map response exposes availability and category pricing, but the verified response did not include customer-facing seat labels or the complete all-in fee quote. Do not claim an all-in total before the provider quotes fees. A future hold approval must clearly say “hold only, no payment,” show labels only if District returns them, state which fees and expiry remain unknown, and enforce an owner-approved maximum total if supplied. Any provider confirmation workflow must be tested and approved before a hold is attempted. Payment remains a direct user handoff and is disabled in acceptance. If the product policy requires an exact all-in amount before a temporary hold, stop and request a provider quote capability instead of holding seats.

## Ticket-discovery fixture

Use deterministic provider fixtures for multiple cinemas, languages, formats, showtimes, prices, fees, seat layouts, unavailable adjacent pairs, expiring quotes, and changing availability. Verify the selector chooses only adjacent available seats matching the user's stated preferences and budget. The live read-only test may inspect public listings and a seat map through permitted access, then perform seat selection locally against the observed read-only map. Do not click controls that hold seats or advance to payment during acceptance.

The live observation is time-bounded and must record the provider, source URL, observation time, and stale-data warning. It is not a promise that a show or seat remains available.

## Android and supervisor integration

Android receives append-only goal progress and structured pending approvals through the existing authenticated task/approval sync. Notifications contain only generic status text on lock screens. The task detail shows human-readable goal status, last verified observation, next scheduled check, checkpoint, and artifacts without raw supervisor JSON. Approval decisions remain one-time, digest-bound, replay-protected, and tied to the paired device.

Remote notification delivery must use a supported provider/configuration that satisfies current MDM policy. Do not sideload a push client, weaken managed-app controls, or add another network path to work around MDM.

## Required verification

- Scheduler restart recovers due goals and stale leases without duplicate actions.
- Cancellation removes future wakes and stops an active bounded step.
- Approval expiry, replay, changed seat/price, unavailable seats, and duplicate booking key fail closed.
- Browser storage is isolated, credentials are absent from prompts/logs, and disallowed origins/redirects are blocked.
- A user handoff for OTP/CAPTCHA/UPI PIN pauses the goal and resumes only after explicit user action.
- Approval notifications arrive on a supported Android path; locked-screen content reveals no movie, seats, account, or price details unless the user opted in.
- Real platform validation is read-only and uses only an officially supported/authorized connector. No purchase, seat hold, queue bypass, CAPTCHA bypass, or payment attempt occurs.
