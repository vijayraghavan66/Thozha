package com.jarvis.companion.ui

private var assertions = 0

private fun checkPolicy(condition: Boolean, message: String) {
    assertions++
    if (!condition) throw AssertionError("FAILED: $message")
}

private fun row(id: String, state: String, transport: String = "telegram") = mapOf<String, Any>(
    "message_id" to id,
    "state" to state,
    "transport" to transport,
)

object LinkSyncPolicyChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        // Cold start / background-to-foreground: the durable SENT request selects active catch-up
        // before asynchronous Compose hydration has populated the visible list.
        checkPolicy(
            LinkSyncPolicy.hasActiveWork(emptyList(), listOf(row("request-a", "SENT"))),
            "persisted SENT request is active before UI hydration",
        )
        checkPolicy(
            LinkSyncPolicy.intervalMs(LinkSyncPolicy.hasActiveWork(emptyList(), listOf(row("request-a", "QUEUED")))) == 10_000L,
            "persisted queued work uses the existing bounded 10-second cadence",
        )

        // Durable terminal state wins over an old Compose snapshot. Completed/cancelled work falls
        // back to the existing idle cadence rather than keeping an aggressive poll loop alive.
        checkPolicy(
            !LinkSyncPolicy.hasActiveWork(listOf(row("request-b", "RUNNING")), listOf(row("request-b", "COMPLETED"))),
            "durable completion overrides stale visible RUNNING state",
        )
        checkPolicy(
            LinkSyncPolicy.intervalMs(false) == 30_000L,
            "terminal task returns to existing 30-second idle cadence",
        )
        for (terminal in listOf("COMPLETED", "FAILED", "CANCELLED", "REJECTED", "EXPIRED", "INDETERMINATE")) {
            checkPolicy(
                !LinkSyncPolicy.hasActiveWork(emptyList(), listOf(row("request-$terminal", terminal))),
                "$terminal does not keep foreground delivery polling active",
            )
        }
        checkPolicy(
            !LinkSyncPolicy.hasActiveWork(emptyList(), listOf(row("local-task", "RUNNING", "local"))),
            "phone-local work does not schedule Mac/Link polling",
        )
        checkPolicy(
            LinkSyncPolicy.hasActiveWork(listOf(row("ui-only", "WAITING_APPROVAL")), null),
            "visible state remains a fallback if the durable snapshot is temporarily unreadable",
        )

        val oldApiTask = mapOf<String, Any>("message_id" to "request-old-api-0001", "task_id" to "a".repeat(32), "transport" to "api", "state" to "RUNNING", "updated_at_unix_ms" to 1L)
        val telegramTask = mapOf<String, Any>("message_id" to "request-telegram-01", "task_id" to "b".repeat(32), "transport" to "telegram", "state" to "RUNNING", "updated_at_unix_ms" to 3L)
        val newApiTask = mapOf<String, Any>("message_id" to "request-new-api-0001", "task_id" to "c".repeat(32), "transport" to "api", "state" to "CREATED", "updated_at_unix_ms" to 5L)
        val candidates = LinkSyncPolicy.taskEventWatchCandidates(listOf(oldApiTask, telegramTask, newApiTask), 1)
        checkPolicy(candidates == listOf("request-new-api-0001" to "c".repeat(32)), "WSS selects the newest valid API-owned task and excludes Telegram identities")
        checkPolicy(
            LinkSyncPolicy.taskEventWatchCandidates(listOf(oldApiTask + ("task_id" to "")), 1).isEmpty(),
            "local outbox rows without a confirmed supervisor task do not consume WSS capacity",
        )

        // Reconnect/onResume while a relay exchange is in flight must not start a competing
        // consumer. Duplicate triggers coalesce to one immediate catch-up after the exchange.
        val gate = CoalescingSyncGate()
        checkPolicy(gate.request(), "first lifecycle sync starts")
        checkPolicy(!gate.request(), "overlapping foreground/reconnect sync does not start concurrently")
        checkPolicy(!gate.request(), "duplicate timer trigger is coalesced")
        checkPolicy(gate.complete(), "one missed catch-up is retained")
        checkPolicy(!gate.complete(), "catch-up completes and releases the gate")
        checkPolicy(gate.request(), "a later fresh reconnect can start a new sync")
        checkPolicy(!gate.complete(), "no redundant pass is scheduled when no request overlapped")

        // Same request ID appears once in UI and once on disk: the durable cancellation state wins
        // and the duplicate must not keep polling or resurrect the request.
        checkPolicy(
            !LinkSyncPolicy.hasActiveWork(
                listOf(row("duplicate-id", "RUNNING")),
                listOf(row("duplicate-id", "CANCELLED"), row("another-id", "COMPLETED")),
            ),
            "duplicate request IDs are reconciled by durable state without resurrecting cancelled work",
        )
        checkPolicy(assertions == 22, "all expected policy assertions executed")
        println("Link sync delivery policy passed: $assertions assertions.")
    }
}
