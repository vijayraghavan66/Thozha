package com.jarvis.companion.link

/** Pure notification gate: only real persisted lifecycle events qualify, keyed for deduplication. */
object TaskNotificationPolicy {
    private val notifyStates = setOf("RUNNING", "FAILED", "COMPLETED", "CANCELLED")
    const val MAX_TRACKED_EVENTS = 500
    const val MAX_NOTIFICATIONS_PER_REFRESH = 8

    data class NotificationEvent(val taskId: String, val state: String, val eventKey: String)
    data class NotificationBatch(val eventsToNotify: List<NotificationEvent>, val seenKeys: Set<String>)

    fun eventKey(event: Map<String, Any>): String? {
        val id = event["task_id"] as? String ?: return null
        val state = ((event["state"] ?: event["status"]) as? String)?.uppercase() ?: return null
        if (state !in notifyStates) return null
        val sequence = event["event_seq"]?.toString() ?: event["received_at_unix_ms"]?.toString()
            ?: event["timestamp_unix_ms"]?.toString() ?: event["event_id"]?.toString() ?: return null
        return "$id:$state:$sequence"
    }

    /**
     * Collapses a refresh snapshot to its latest alert-worthy event per task. Mark every candidate
     * as seen, but enqueue at most a small notification burst. Large history snapshots must never
     * clear the dedupe set while iterating or re-notify old events on each refresh.
     */
    fun notificationBatch(
        updates: List<Map<String, Any>>,
        previousSeen: Set<String>,
        baselineInitialized: Boolean,
    ): NotificationBatch {
        val latestByTask = linkedMapOf<String, NotificationEvent>()
        updates.forEach { event ->
            val key = eventKey(event) ?: return@forEach
            val taskId = event["task_id"] as? String ?: return@forEach
            val state = ((event["state"] ?: event["status"]) as? String)?.uppercase() ?: return@forEach
            // Moving an existing task to the end preserves snapshot order when the inputs contain
            // both recent-update and full task-event histories.
            latestByTask.remove(taskId)
            latestByTask[taskId] = NotificationEvent(taskId, state, key)
        }
        val candidates = latestByTask.values.toList().takeLast(MAX_TRACKED_EVENTS)
        val candidateKeys = candidates.mapTo(linkedSetOf()) { it.eventKey }
        if (!baselineInitialized) return NotificationBatch(emptyList(), candidateKeys)

        val unseen = candidates.filter { it.eventKey !in previousSeen }
        val nextSeen = LinkedHashSet(previousSeen)
        candidates.forEach { candidate ->
            // Replace the older lifecycle key for this task so storage remains bounded by task count.
            nextSeen.removeAll { key -> key.startsWith("${candidate.taskId}:") }
            nextSeen.add(candidate.eventKey)
        }
        while (nextSeen.size > MAX_TRACKED_EVENTS) {
            val oldest = nextSeen.firstOrNull() ?: break
            nextSeen.remove(oldest)
        }
        return NotificationBatch(unseen.takeLast(MAX_NOTIFICATIONS_PER_REFRESH), nextSeen)
    }
}
