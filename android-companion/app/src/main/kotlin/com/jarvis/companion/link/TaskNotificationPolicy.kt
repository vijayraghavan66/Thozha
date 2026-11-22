package com.jarvis.companion.link

/** Pure notification gate: only real persisted lifecycle events qualify, keyed for deduplication. */
object TaskNotificationPolicy {
    private val notifyStates = setOf("RUNNING", "FAILED", "COMPLETED", "CANCELLED")
    fun eventKey(event: Map<String, Any>): String? {
        val id = event["task_id"] as? String ?: return null
        val state = ((event["state"] ?: event["status"]) as? String)?.uppercase() ?: return null
        if (state !in notifyStates) return null
        val sequence = event["event_seq"]?.toString() ?: event["received_at_unix_ms"]?.toString()
            ?: event["timestamp_unix_ms"]?.toString() ?: event["event_id"]?.toString() ?: return null
        return "$id:$state:$sequence"
    }
}
