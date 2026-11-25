package com.jarvis.companion.ui

/**
 * Pure policy for foreground Link delivery. The durable Link conversation is authoritative when a
 * Compose snapshot is stale; process/activity restoration must not make an open task look idle.
 */
internal object LinkSyncPolicy {
    val activeStates = setOf(
        "CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL",
        "WAITING_CONNECTION", "PAUSED", "SENT", "QUEUED", "UNCONFIRMED",
    )

    const val ACTIVE_INTERVAL_MS = 10_000L
    const val TELEGRAM_ACTIVE_INTERVAL_MS = 30_000L
    const val IDLE_INTERVAL_MS = 30_000L

    /** LAN WSS is authorized by the paired API identity; Telegram/Link identities stay on their own transport. */
    fun taskEventWatchCandidates(
        conversation: List<Map<String, Any>>,
        limit: Int,
    ): List<Pair<String, String>> {
        require(limit >= 0)
        return conversation.asSequence()
            .filter { it["transport"] == "api" && (it["state"] as? String)?.uppercase() in activeStates }
            .mapNotNull { row ->
                val requestId = row["message_id"] as? String ?: return@mapNotNull null
                val taskId = row["task_id"] as? String ?: return@mapNotNull null
                if (!requestId.matches(Regex("[A-Za-z0-9._~-]{16,128}")) || !taskId.matches(Regex("[0-9a-f]{32}"))) {
                    return@mapNotNull null
                }
                val updatedAt = (row["updated_at_unix_ms"] as? Number)?.toLong() ?: 0L
                Triple(requestId, taskId, updatedAt)
            }
            .sortedByDescending { it.third }
            .take(limit)
            .map { it.first to it.second }
            .toList()
    }

    fun isTerminalDeliveryState(state: String?): Boolean = state?.uppercase() in setOf(
        "COMPLETED", "FAILED", "CANCELLED", "REJECTED", "EXPIRED", "INDETERMINATE",
    )

    fun hasActiveWork(
        visibleEntries: List<Map<String, Any>>,
        durableConversation: List<Map<String, Any>>?,
    ): Boolean {
        val effective = if (durableConversation == null) {
            visibleEntries
        } else {
            // Keep UI-only rows (for example a locally-created task), but let the disk-backed Link
            // row replace a possibly stale Compose row with the same request ID.
            val durableById = durableConversation.mapNotNull { row ->
                (row["message_id"] as? String)?.let { it to row }
            }.toMap()
            visibleEntries.map { row ->
                val id = row["message_id"] as? String
                if (id == null) row else durableById[id] ?: row
            } + durableById.filterKeys { id -> visibleEntries.none { it["message_id"] == id } }.values
        }
        return effective.any { row ->
            row["transport"] != "local" && (row["state"] as? String)?.uppercase() in activeStates
        }
    }

    fun shouldRunLinkSync(
        lanHealthy: Boolean,
        pendingTelegramWork: Boolean,
        queuedOutboxMessages: Int,
    ): Boolean = !lanHealthy || pendingTelegramWork || queuedOutboxMessages > 0

    fun intervalMs(
        hasActiveWork: Boolean,
        telegramRetryAfterMs: Long = 0L,
        telegramTunnelActive: Boolean = false,
    ): Long {
        val normalDelay = when {
            hasActiveWork && telegramTunnelActive -> TELEGRAM_ACTIVE_INTERVAL_MS
            hasActiveWork -> ACTIVE_INTERVAL_MS
            else -> IDLE_INTERVAL_MS
        }
        return if (telegramRetryAfterMs > 0L) maxOf(normalDelay, telegramRetryAfterMs.coerceAtMost(300_000L) + 250L)
        else normalDelay
    }
}

/**
 * Single-flight gate with a one-bit catch-up. Repeated foreground/resume/timer requests cannot start
 * concurrent Telegram exchanges, but one request arriving during an exchange is not silently lost.
 */
internal class CoalescingSyncGate {
    private var running = false
    private var catchUpRequested = false

    @Synchronized
    fun request(): Boolean {
        if (running) {
            catchUpRequested = true
            return false
        }
        running = true
        return true
    }

    /** Returns true when the caller should run one coalesced catch-up; otherwise releases the gate. */
    @Synchronized
    fun complete(): Boolean {
        if (!running) return false
        if (catchUpRequested) {
            catchUpRequested = false
            return true
        }
        running = false
        return false
    }
}
