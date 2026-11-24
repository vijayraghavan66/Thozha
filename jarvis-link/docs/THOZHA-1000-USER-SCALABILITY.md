# Thozha Telegram and 1,000-user readiness

Status: architecture proposal plus an in-process relay simulation. No Worker was deployed, no account was changed, and no real Telegram message was sent.

## Current Telegram diagnosis

- The existing adapter uses Telegram Bot API `getUpdates` long polling, with a persisted offset, one nonblocking `adapter.lock`, a single configured numeric user ID, and a PAC-aware HTTPS client. It does not use a webhook.
- A read-only `getMe` call through the adapter's existing Keychain/PAC code succeeded. `getWebhookInfo` reported no webhook, no pending webhook updates, and no last error. The current process is the only adapter consumer observed.
- The adapter and `jarvis-link/host` both target `http://127.0.0.1:8787`. Nothing is listening there. In the most recent hour the adapter polled successfully, received two tunnel frames, and returned HTTP 504 for both because the relay connection was refused. This is a local Jarvis Link relay failure, not evidence of Telegram token or Bot API failure.
- Port 8787 is the loopback development endpoint for the existing Cloudflare Worker at `jarvis-link/relay`. The Worker is not deployed. The current Wrangler state contains Durable Object metadata but no application storage rows; the test host key file exists separately and is owner-only. Starting an empty Worker would not restore the companion certificate records. Bootstrapping/enrolling would change the relay's trust state, so it remains stopped until the intended test identity is explicitly confirmed and the Samsung is connected.
- A plain `JARVIS-PING` could produce a notification on Telegram. It was not sent under the silent-test constraint. No `getUpdates`, webhook mutation, token rotation, adapter restart, or second poller was used.

## QR pairing inventory

The OpenJarvis pair API already creates a five-minute signed invitation; the Android parser verifies canonical JSON, signature, public-key fingerprint, expiry and strict fields. Android then signs the challenge from its Keystore key, the Mac owner confirms the pending device, and Android claims the short-lived token with a second proof of possession. Device revocation is separate. The Mac OpenJarvis Settings source includes a QR/pending-device panel, and the Android AdvancedActivity flow includes scan, identity review, owner-confirmation wait, and token claim. The Compose app exposes that flow through Settings → Setup → “Mac pairing and advanced setup.”

Samsung/Vivo OpenJarvis pair records were preserved. QR pairing does not establish mobile-data transport. Jarvis Link tenant enrollment is a separate identity system; the Link relay state is currently uninitialized and must not be silently bootstrapped from the legacy OpenJarvis pairing.

Known gap: invitation expiry is enforced, and Android can clear an unused scanned invitation locally, but there is no server-side cancellation route for a Mac owner to revoke a still-live invitation before expiry. Physical QR scan/approval testing is deferred until the Samsung is connected; Vivo remains untouched.

## Telegram account linking for multiple owners

The existing adapter is a single-owner test adapter and must remain unchanged. It accepts one allowlisted Telegram numeric user ID, harmless `JARVIS-PING` messages, and encrypted `JLT1` relay frames. It does not implement `/start` account linking. The current Keychain token authenticates a bot but does not prove that it is the centrally managed Thozha bot intended for public enrollment.

Recommended v1 flow:

1. An authenticated Thozha session creates a cryptographically random, one-use link token with a short expiry. Store only its hash, owner ID, creation/expiry, and attempt state.
2. The bot's **single** webhook/poller consumer accepts `/start <token>` only in a private chat, validates the token atomically, and captures numeric `from.id` and `chat.id`. Never use `username` for identity.
3. The bot acknowledges only that a confirmation is waiting; it does not reveal owner data. The authenticated Thozha session explicitly confirms the exact account/chat before binding.
4. Link/unlink, retries, duplicate Telegram update IDs, token expiry and owner isolation are persisted and auditable. Unlink revokes the mapping. Linking never grants Mac capabilities.
5. A single durable Bot API outbox owns delivery and its global/per-chat rate limits. Do not create multiple bots to evade Telegram limits.

This requires an owner-confirmed central bot identity and a controlled migration from the current `getUpdates` consumer. Do not set a webhook or start a second consumer until that migration has an explicit rollback plan.

## Proposed multi-tenant service

- **Identity:** authenticated Thozha `owner_id` is the tenant key. Each owner has independent conversations, memories, devices, Mac installation, task namespace, and artifacts. Telegram numeric IDs map to one owner only after confirmation. Every API, database query, queue message and artifact download rechecks the tenant.
- **Ingress:** one HTTPS Telegram webhook (or exactly one long-poller during migration) validates Telegram's webhook secret, enforces body limits, persists `update_id` idempotency, and quickly acknowledges after durable enqueue. Webhook and poller are never active together for the same bot.
- **Queue/scheduling:** durable per-tenant queues with bounded global workers, per-owner and per-Mac concurrency, fair scheduling, separate interactive/background priorities, cancellation, deduplication and dead letters. Approval-gated messages carry the exact task/operation digest and are revalidated at the Mac supervisor immediately before a single dispatch claim.
- **Mac host:** one outbound authenticated, encrypted session per owner's Mac through the relay. The relay contains no Mac credentials, approval authority, or shell endpoint. Offline Macs reconnect and receive only non-expired envelopes; stale approval cannot authorize execution.
- **Relay:** one Durable Object per tenant for replay state, encrypted bounded inboxes and delivery receipts. SQLite-backed DOs support local testing. Production should add a central index/account store, quotas, retention, backup/recovery and key-rotation procedures before onboarding users.
- **AI:** enforce per-owner request/token budgets and queue limits before model dispatch. Do not share a Codex CLI subscription across unrelated owners. Multi-tenant hosted inference requires an explicitly authorized provider/API billing account. Keep task execution behind the per-Mac supervisor; conversations alone never grant Mac capabilities.
- **Observability:** content-free counters for tenant queue depth, active workers, API/Telegram 429s, retries/dead letters, task latency, first token, approvals, relay delivery and resource usage. No prompts, credentials, raw update payloads or private tool results in logs.

## Offline scale simulation

Run `npm run simulate:scale` from `jarvis-link/relay`. It creates 1,600 synthetic tenant/device fixtures in memory, signs authenticated inbox requests with generated P-256 identities, and calls the production `TenantSessionCore` handler in-process. It performs no HTTP listener, Cloudflare request, Telegram operation, AI request, persistent state write, or user-credential access. Registration is seeded; the separate enrollment tests exercise the invitation/certification protocol.

Most recent run:

| Synthetic tenant set | Concurrent requests | Requests | p50 handler time | p95 | p99 | HTTP results |
|---:|---:|---:|---:|---:|---:|---|
| 100 | 50 | 100 | 4.07 ms | 6.59 ms | 6.63 ms | 100 × 200 |
| 500 | 100 | 500 | 6.90 ms | 7.69 ms | 7.84 ms | 500 × 200 |
| 1,000 | 250 | 1,000 | 14.91 ms | 15.97 ms | 16.24 ms | 1,000 × 200 |

Separate checks: a signed request replay returned 409; a tenant-A signature routed to tenant B returned 401; 120 requests in the rate window succeeded and the 121st returned 429. This core-worktree repeat run took 228.93 ms total wall time, used 455.97 ms CPU, added 74,547,200 bytes RSS and 21,787,832 bytes heap. These numbers measure an in-memory handler and crypto only. They do **not** demonstrate Cloudflare/Durable Object disk, network, Telegram, AI, webhook, queue-worker, or end-to-end capacity. Maximum demonstrated here is 1,000 seeded tenants and 250 concurrent in-process requests.

## Cost model (estimates, USD)

Current official rates are linked because provider pricing changes:

- [Cloudflare Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/) lists a $5/month Workers Paid base, 10 million requests and 30 million CPU-ms included, then $0.30 per additional million requests and $0.02 per additional million CPU-ms. Free Workers include 100,000 requests/day and 10 ms CPU per invocation.
- [Durable Objects pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/) lists Free limits of 100,000 requests/day, 13,000 GB-s/day, 5 million SQLite rows read/day, 100,000 rows written/day and 5 GB total storage. Paid metering includes 1 million DO requests/month and 400,000 GB-s/month, then $0.15/million requests and $12.50/million GB-s; SQLite storage beyond 5 GB-month is $0.20/GB-month. This estimate must include both Worker and DO usage rather than assume one fixed fee.
- [Telegram Bot FAQ](https://core.telegram.org/bots/faq) says normal broadcasts are free but rate-limited to about 30 messages/second bot-wide and one per second per chat; paid broadcast above 30/s costs 0.1 Stars/message and requires at least 100,000 monthly active users and 100,000 Stars. A 1,000-user Beta should queue/coalesce and stay below standard limits.
- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing) currently lists GPT-6 Luna at $0.10/1M input tokens and $0.50/1M output tokens (standard, short context). Example only: 1,000 users × 20 turns/day × 1,500 input tokens and 250 output tokens is 30M input + 5M output tokens/day, about **$5.50/day or $165 per 30 days** before cache, retries, tool usage, context growth and taxes. This is API pricing, not a claim that a Codex subscription includes API usage.

Free-tier capacity cannot be inferred from registered users alone. Measure actual messages, SQLite row writes, DO active duration, worker CPU, AI token use and retry rate, then set spending caps and per-tenant budgets. No paid plan, overage, Worker deployment or provider account was enabled.
