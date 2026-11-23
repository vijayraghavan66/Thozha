package com.jarvis.companion.presentation

/** One row of the Activity timeline. Read-only; cancelling is offered only when the runtime says it can be. */
data class TimelineRow(
    /** Identifier the existing cancel path uses (the conversation message id). */
    val requestId: String,
    val taskId: String,
    val title: String,
    val tone: TaskTone,
    val statusLabel: String,
    val detail: String,
    val timeLabel: String,
    val canCancel: Boolean,
    val capabilityScope: String = "",
    val transport: String = "",
)

data class TimelineSection(val title: String, val rows: List<TimelineRow>)

data class TaskProgressEvent(
    val id: String,
    val label: String,
    val detail: String,
    val timeLabel: String,
    val tone: TaskTone,
)

/** Only authenticated Mac task-update events become progress; no progress is inferred from elapsed time. */
object TaskProgressPresentation {
    fun eventsFor(
        taskId: String,
        updates: List<Map<String, Any?>>,
        formatTime: (Long) -> String,
    ): List<TaskProgressEvent> {
        val matching = updates.filter { event ->
            taskId.isNotBlank() && event["task_id"] == taskId
        }.distinctBy { event ->
            (event["event_seq"] as? Number)?.toLong()?.let { "seq:$it" }
                ?: "${event["task_id"]}:${event["state"]}:${event["received_at_unix_ms"]}"
        }.sortedWith(compareBy(
            { (it["event_seq"] as? Number)?.toLong() ?: Long.MAX_VALUE },
            { (it["received_at_unix_ms"] as? Number)?.toLong() ?: 0L },
        )).takeLast(5)

        return matching.mapIndexed { index, event ->
            val state = (event["state"] as? String)?.trim()?.uppercase()
            val tone = TaskStates.toneFor(state)
            val timestamp = (event["received_at_unix_ms"] as? Number)?.toLong()
            TaskProgressEvent(
                id = (event["event_seq"] as? Number)?.toLong()?.let { "seq:$it" }
                    ?: "event:$index:${event["received_at_unix_ms"]}",
                label = if (state.isNullOrBlank()) "Task updated" else TaskStates.statusLabel(state),
                detail = (event["summary"] as? String).orEmpty().trim().ifBlank { "The Mac reported a status update." }.take(240),
                timeLabel = timestamp?.let(formatTime).orEmpty(),
                tone = tone,
            )
        }
    }
}

object TimelinePresentation {
    // Must match the states for which the existing task detail offers cancellation; do not widen it.
    private val cancellableStates = setOf("CREATED", "RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED")

    /**
     * Groups task records into sections in a fixed order: needs you, in progress, completed, failed and cancelled.
     * Sections with no rows are omitted; an empty list means the caller should show the empty state.
     */
    fun sections(tasks: List<Map<String, Any?>>, formatTime: (Long) -> String): List<TimelineSection> {
        val rows = tasks.mapNotNull { row(it, formatTime) }.distinctBy { it.requestId }
        val byTone = rows.groupBy { it.tone }
        return listOf(
            TimelineSection("Needs you", byTone[TaskTone.WAITING].orEmpty()),
            TimelineSection("In progress", byTone[TaskTone.RUNNING].orEmpty()),
            TimelineSection("Completed", byTone[TaskTone.COMPLETED].orEmpty()),
            TimelineSection("Failed", byTone[TaskTone.FAILED].orEmpty()),
            TimelineSection("Cancelled", byTone[TaskTone.CANCELLED].orEmpty()),
            TimelineSection("Status unknown", byTone[TaskTone.UNKNOWN].orEmpty()),
        ).filter { it.rows.isNotEmpty() }
    }

    internal fun row(record: Map<String, Any?>, formatTime: (Long) -> String): TimelineRow? {
        val requestId = (record["message_id"] as? String)?.takeIf { it.isNotBlank() } ?: return null
        val taskId = (record["task_id"] as? String).orEmpty()
        val state = (record["state"] as? String)?.trim()?.uppercase()
        val tone = TaskStates.toneFor(state)
        val request = (record["text"] as? String).orEmpty().trim()
        val summary = (record["summary"] as? String).orEmpty().trim()
        val updated = (record["updated_at_unix_ms"] as? Number)?.toLong() ?: (record["created_at_unix_ms"] as? Number)?.toLong()
        val canCancel = state != null && state in cancellableStates
        return TimelineRow(
            requestId = requestId,
            taskId = taskId,
            title = request.ifBlank { "Request" }.take(120),
            tone = tone,
            statusLabel = TaskStates.statusLabel(state),
            detail = summary.take(400),
            timeLabel = updated?.let(formatTime).orEmpty(),
            canCancel = canCancel,
            capabilityScope = (record["capability_scope"] as? String).orEmpty(),
            transport = (record["transport"] as? String).orEmpty(),
        )
    }
}

/** Refresh unfinished API requests and the one nonterminal approval response that contains only capability issuance. */
fun shouldReconcileApiTask(record: Map<String, Any?>, nowMs: Long = System.currentTimeMillis()): Boolean {
    if (record["transport"] != "api") return false
    if (record["lookup_link_status"] == false) return false
    val state = (record["state"] as? String)?.trim()?.uppercase().orEmpty()
    if (state in setOf("CREATED", "QUEUED", "RUNNING", "PLANNING", "VERIFYING", "WAITING_APPROVAL", "PAUSED", "SENT", "UNCONFIRMED")) return true
    val summary = (record["summary"] as? String).orEmpty().trim()
    if (state == "COMPLETED" && summary.startsWith("The exact approved plan is active. Use capability_id=")) return true
    val createdAt = (record["created_at_unix_ms"] as? Number)?.toLong() ?: return false
    return state == "FAILED" && record["task_id"].let { it == null || it == "" } &&
        summary == "The Mac no longer has this task record." && nowMs - createdAt in 0..STATUS_RECONCILIATION_WINDOW_MS
}

private const val STATUS_RECONCILIATION_WINDOW_MS = 30 * 60 * 1000L

enum class ApprovalPhase { PENDING, EXPIRED, APPROVED, REJECTED, CONSUMED, UNKNOWN }

/**
 * Presentation of one approval request. The caller shows the exact operation and target and enables a decision
 * control only when [canDecide] is true. The decision itself still goes through the existing supervisor path,
 * which re-validates digest, expiry and single use. Nothing here approves anything.
 */
data class ApprovalCard(
    val approvalId: String,
    val title: String,
    val operation: String,
    val target: String,
    val digestPrefix: String,
    val phase: ApprovalPhase,
    val expiryLabel: String,
    val canDecide: Boolean,
)

object ApprovalPresentation {
    fun card(record: Map<String, Any?>, nowMs: Long, formatRemaining: (Long) -> String): ApprovalCard? {
        val id = (record["approval_id"] as? String)?.takeIf { it.isNotBlank() } ?: return null
        val operation = (record["operation"] as? String).orEmpty().ifBlank { "Unnamed action" }
        val target = (record["target"] as? String).orEmpty().ifBlank { "No target stated" }
        val digest = (record["canonical_arguments_digest"] as? String).orEmpty().take(12)
        val status = (record["status"] as? String)?.trim()?.uppercase().orEmpty()
        val expiresAt = (record["expires_at_unix_ms"] as? Number)?.toLong()
        val remaining = expiresAt?.let { it - nowMs }
        val phase = when {
            status == "APPROVED" -> ApprovalPhase.APPROVED
            status == "REJECTED" -> ApprovalPhase.REJECTED
            status == "CONSUMED" -> ApprovalPhase.CONSUMED
            status == "EXPIRED" || (status == "PENDING" && remaining != null && remaining <= 0) -> ApprovalPhase.EXPIRED
            status == "PENDING" -> ApprovalPhase.PENDING
            else -> ApprovalPhase.UNKNOWN
        }
        val expiryLabel = when (phase) {
            ApprovalPhase.PENDING -> remaining?.let { "Expires in ${formatRemaining(it)}" } ?: "Expiry not stated"
            ApprovalPhase.EXPIRED -> "Expired. Request again if you still want this."
            ApprovalPhase.APPROVED -> "Approved. Jarvis continues on the Mac."
            ApprovalPhase.REJECTED -> "Rejected. Nothing was run."
            ApprovalPhase.CONSUMED -> "Used. This approval cannot be used again."
            ApprovalPhase.UNKNOWN -> "Status unknown. Check on the Mac before acting."
        }
        return ApprovalCard(
            approvalId = id,
            title = "Approve this action on your Mac?",
            operation = operation.take(160),
            target = target.take(400),
            digestPrefix = digest,
            phase = phase,
            expiryLabel = expiryLabel,
            // Fail closed: without a stated, future expiry the decision control stays disabled.
            canDecide = phase == ApprovalPhase.PENDING && remaining != null && remaining > 0,
        )
    }
}
