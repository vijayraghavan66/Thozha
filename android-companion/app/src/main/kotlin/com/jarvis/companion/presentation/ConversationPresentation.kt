package com.jarvis.companion.presentation

enum class Speaker { USER, JARVIS }

/** One rendered bubble. [id] is stable so list diffing and animations do not duplicate or drop messages. */
data class ConversationItem(
    val id: String,
    val speaker: Speaker,
    val text: String,
    val tone: TaskTone?,
    val statusLabel: String?,
    val timeLabel: String,
    val isProgress: Boolean,
)

object ConversationPresentation {
    private const val COMPLETED_NO_TEXT = "Completed. The assistant returned no text for this request."
    private const val FAILED_NO_TEXT = "This request failed. No answer was returned."
    private const val CANCELLED_TEXT = "Cancelled. Jarvis will not continue this request."
    private const val WAITING_TEXT = "Waiting for your approval before Jarvis continues."
    private const val RUNNING_NO_TEXT = "Working on it."
    private const val SENT_TEXT = "Sent. Waiting for Jarvis to pick it up."

    /**
     * Projects conversation records (as returned by LinkRuntime.conversation()) into bubbles, oldest first.
     * Records are read, never changed. Duplicate message ids are shown once.
     */
    fun items(records: List<Map<String, Any?>>, formatTime: (Long) -> String): List<ConversationItem> {
        val seen = HashSet<String>()
        val out = ArrayList<ConversationItem>()
        for (record in records) {
            val messageId = (record["message_id"] as? String)?.takeIf { it.isNotBlank() } ?: continue
            if (!seen.add(messageId)) continue
            val text = (record["text"] as? String).orEmpty()
            val created = (record["created_at_unix_ms"] as? Number)?.toLong()
            val updated = (record["updated_at_unix_ms"] as? Number)?.toLong() ?: created
            val state = record["state"] as? String
            val summary = (record["summary"] as? String).orEmpty().trim()
            val tone = TaskStates.toneFor(state)

            if (text.isNotBlank()) {
                out += ConversationItem(
                    id = "$messageId:user",
                    speaker = Speaker.USER,
                    text = text,
                    tone = null,
                    statusLabel = null,
                    timeLabel = created?.let(formatTime).orEmpty(),
                    isProgress = false,
                )
            }
            val reply = replyFor(state, tone, summary) ?: continue
            out += ConversationItem(
                id = "$messageId:jarvis",
                speaker = Speaker.JARVIS,
                text = reply.first,
                tone = tone,
                statusLabel = if (tone == TaskTone.UNKNOWN) TaskStates.label(tone) else null,
                timeLabel = updated?.let(formatTime).orEmpty(),
                isProgress = reply.second,
            )
        }
        return out
    }

    /** Returns (text, isProgress) for the assistant side, or null when there is nothing honest to show. */
    internal fun replyFor(state: String?, tone: TaskTone, summary: String): Pair<String, Boolean>? = when (tone) {
        TaskTone.COMPLETED -> (summary.ifBlank { COMPLETED_NO_TEXT }) to false
        TaskTone.FAILED -> (summary.ifBlank { FAILED_NO_TEXT }) to false
        TaskTone.CANCELLED -> CANCELLED_TEXT to false
        TaskTone.WAITING -> WAITING_TEXT to true
        TaskTone.RUNNING -> when {
            state?.trim()?.uppercase() == "QUEUED" || state?.trim()?.uppercase() == "SENT" -> SENT_TEXT to true
            summary.isNotBlank() -> summary to true
            else -> RUNNING_NO_TEXT to true
        }
        TaskTone.UNKNOWN -> if (summary.isBlank()) null else summary to false
    }
}
