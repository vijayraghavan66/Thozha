package com.jarvis.companion.presentation

/*
 * Presentation-only models. Each function is a read-only projection of records the app already holds
 * (conversation entries, task states, approval records, transport status). Nothing here sends, decides,
 * approves, cancels, or stores anything, and nothing here infers success that the records do not state.
 * Pure Kotlin so the rules are testable on the JVM without Android.
 */

/** Visual tone for a task state. Mapped only from the state names the runtime reports. */
enum class TaskTone { RUNNING, WAITING, COMPLETED, FAILED, CANCELLED, UNKNOWN }

object TaskStates {
    private val running = setOf("CREATED", "PLANNING", "RUNNING", "VERIFYING", "QUEUED", "SENT")
    private val waiting = setOf("WAITING_APPROVAL", "WAITING_CONNECTION", "PAUSED")
    private val completed = setOf("COMPLETED")
    private val failed = setOf("FAILED", "REFUSED", "EXPIRED", "REJECTED")
    private val cancelled = setOf("CANCELLED")

    fun toneFor(state: String?): TaskTone {
        val s = state?.trim()?.uppercase().orEmpty()
        return when (s) {
            in running -> TaskTone.RUNNING
            in waiting -> TaskTone.WAITING
            in completed -> TaskTone.COMPLETED
            in failed -> TaskTone.FAILED
            in cancelled -> TaskTone.CANCELLED
            else -> TaskTone.UNKNOWN
        }
    }

    fun statusLabel(state: String?): String = when (state?.trim()?.uppercase()) {
        "CREATED" -> "Created"
        "QUEUED", "SENT" -> "Queued"
        "RUNNING", "PLANNING", "VERIFYING" -> "Running"
        "WAITING_APPROVAL" -> "Waiting for approval"
        "WAITING_CONNECTION", "PAUSED" -> "Waiting for connection"
        "COMPLETED" -> "Completed"
        "FAILED", "EXPIRED" -> "Failed"
        "REJECTED" -> "Rejected"
        "CANCELLED" -> "Cancelled"
        else -> "Status unknown"
    }

    /** Short, sentence-case label. Unknown states are shown as "Status unknown", never as success. */
    fun label(tone: TaskTone): String = when (tone) {
        TaskTone.RUNNING -> "In progress"
        TaskTone.WAITING -> "Needs your approval"
        TaskTone.COMPLETED -> "Completed"
        TaskTone.FAILED -> "Failed"
        TaskTone.CANCELLED -> "Cancelled"
        TaskTone.UNKNOWN -> "Status unknown"
    }
}
