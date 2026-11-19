package com.jarvis.companion.presentation

/** User-visible lifecycle for one decision. Relay acceptance is deliberately separate from supervisor approval. */
enum class ApprovalDecisionPhase {
    SUBMITTING,
    QUEUED,
    DELIVERED,
    APPROVED,
    EXECUTING,
    COMPLETED,
    REJECTED,
    EXPIRED,
    CANCELLED,
    DELIVERY_FAILED,
    FAILED,
    INDETERMINATE,
    UNKNOWN,
}

data class ApprovalDecisionStatus(
    val approvalId: String,
    val taskId: String,
    val decision: String,
    val phase: ApprovalDecisionPhase,
    val updatedAtUnixMs: Long,
    val operation: String = "",
    val target: String = "",
    /** Transport message ID, if available, used only for reconciliation and never as authorization. */
    val messageId: String = "",
)

object ApprovalDecisionPresentation {
    /** A process death during an in-flight send leaves delivery uncertain until authoritative sync reconciles it. */
    fun afterRestart(previous: ApprovalDecisionStatus): ApprovalDecisionStatus =
        if (previous.phase == ApprovalDecisionPhase.SUBMITTING) previous.copy(phase = ApprovalDecisionPhase.UNKNOWN) else previous

    fun submitted(
        approvalId: String,
        taskId: String,
        decision: String,
        queuedOffline: Boolean,
        messageId: String,
        nowUnixMs: Long,
        operation: String = "",
        target: String = "",
    ): ApprovalDecisionStatus? {
        if (approvalId.isBlank() || taskId.isBlank() || decision !in setOf("approve", "reject")) return null
        return ApprovalDecisionStatus(
            approvalId = approvalId,
            taskId = taskId,
            decision = decision,
            phase = if (queuedOffline) ApprovalDecisionPhase.QUEUED else ApprovalDecisionPhase.DELIVERED,
            updatedAtUnixMs = nowUnixMs,
            operation = operation.take(160),
            target = target.take(400),
            messageId = messageId,
        )
    }

    fun submitting(
        approvalId: String,
        taskId: String,
        decision: String,
        operation: String,
        target: String,
        nowUnixMs: Long,
    ): ApprovalDecisionStatus? =
        submitted(
            approvalId = approvalId,
            taskId = taskId,
            decision = decision,
            queuedOffline = false,
            messageId = "",
            nowUnixMs = nowUnixMs,
            operation = operation,
            target = target,
        )
            ?.copy(phase = ApprovalDecisionPhase.SUBMITTING)

    /**
     * Interpret only the authenticated, host-originated `approval_decision_result` map. A relay receipt by itself
     * cannot make an item Approved or Completed. A result for another approval/task/decision is ignored.
     */
    fun authoritative(
        previous: ApprovalDecisionStatus,
        result: Map<String, Any?>,
        nowUnixMs: Long,
    ): ApprovalDecisionStatus? {
        // Callers pass this map only from authenticated TaskUpdate.approvalDecisionResult, whose decoder
        // checks the result kind. Retain a redundant check if the map also contains a kind discriminator.
        if (result["kind"] != null && result["kind"] != "approval.decision_result") return null
        if (result["approval_id"] != previous.approvalId || result["task_id"] != previous.taskId) return null
        val decision = (result["decision"] as? String)?.lowercase() ?: return null
        if (decision != previous.decision) return null
        if (result["simulated"] == true) {
            return previous.copy(phase = ApprovalDecisionPhase.INDETERMINATE, updatedAtUnixMs = nowUnixMs)
        }

        val status = (result["status"] as? String)?.uppercase().orEmpty()
        val execution = (result["execution_state"] as? String)?.uppercase().orEmpty()
        val executed = result["executed"] == true
        val success = result["success"] == true

        val phase = when {
            status == "EXPIRED" || execution == "EXPIRED" -> ApprovalDecisionPhase.EXPIRED
            status == "REJECTED" || execution == "REJECTED" -> ApprovalDecisionPhase.REJECTED
            execution == "CANCELLED" -> ApprovalDecisionPhase.CANCELLED
            execution == "DELIVERY_FAILED" -> ApprovalDecisionPhase.DELIVERY_FAILED
            execution == "INDETERMINATE" -> ApprovalDecisionPhase.INDETERMINATE
            execution == "FAILED" -> ApprovalDecisionPhase.FAILED
            execution == "COMPLETED" -> if (executed && success) ApprovalDecisionPhase.COMPLETED else ApprovalDecisionPhase.INDETERMINATE
            execution == "EXECUTION_PENDING" -> ApprovalDecisionPhase.APPROVED
            execution in setOf("RUNNING", "EXECUTING", "VERIFYING") -> ApprovalDecisionPhase.EXECUTING
            status == "APPROVED" && decision == "approve" -> ApprovalDecisionPhase.APPROVED
            status == "REJECTED" && decision == "reject" -> ApprovalDecisionPhase.REJECTED
            else -> ApprovalDecisionPhase.UNKNOWN
        }
        return previous.copy(phase = phase, updatedAtUnixMs = nowUnixMs)
    }

    /** Restore durable LinkRuntime decision state after an Activity/process restart. */
    fun fromRuntimeState(
        previous: ApprovalDecisionStatus?,
        runtime: Map<String, Any?>,
        nowUnixMs: Long,
        operation: String = "",
        target: String = "",
        allowSubmitting: Boolean = false,
    ): ApprovalDecisionStatus? {
        val approvalId = runtime["approval_id"] as? String ?: previous?.approvalId ?: return null
        val taskId = runtime["task_id"] as? String ?: previous?.taskId ?: return null
        val decision = (runtime["decision"] as? String)?.lowercase() ?: previous?.decision ?: return null
        if (decision !in setOf("approve", "reject")) return null
        if (previous != null && (previous.approvalId != approvalId || previous.taskId != taskId || previous.decision != decision)) return null
        val state = (runtime["state"] as? String)?.uppercase().orEmpty()
        val base = previous?.copy(
            operation = previous.operation.ifBlank { operation.take(160) },
            target = previous.target.ifBlank { target.take(400) },
            messageId = previous.messageId.ifBlank { runtime["message_id"] as? String ?: "" },
        ) ?: ApprovalDecisionStatus(
            approvalId = approvalId,
            taskId = taskId,
            decision = decision,
            phase = ApprovalDecisionPhase.UNKNOWN,
            updatedAtUnixMs = nowUnixMs,
            operation = operation.take(160),
            target = target.take(400),
            messageId = runtime["message_id"] as? String ?: "",
        )
        if (runtime["authoritative"] == true) return authoritative(base, runtime, nowUnixMs)
        val phase = when (state) {
            "SUBMITTING" -> if (allowSubmitting) ApprovalDecisionPhase.SUBMITTING else ApprovalDecisionPhase.UNKNOWN
            "QUEUED" -> ApprovalDecisionPhase.QUEUED
            "DELIVERED_WAITING_SUPERVISOR" -> ApprovalDecisionPhase.DELIVERED
            "DELIVERY_FAILED" -> ApprovalDecisionPhase.DELIVERY_FAILED
            else -> return null
        }
        return base.copy(phase = phase, updatedAtUnixMs = nowUnixMs)
    }

    /** A task-state event may advance an already-authoritatively-approved decision; it never grants approval. */
    fun taskState(previous: ApprovalDecisionStatus, state: String, nowUnixMs: Long): ApprovalDecisionStatus? {
        if (previous.decision != "approve" || previous.phase !in setOf(
                ApprovalDecisionPhase.APPROVED,
                ApprovalDecisionPhase.EXECUTING,
            )) return null
        val phase = when (state.trim().uppercase()) {
            "RUNNING", "VERIFYING" -> ApprovalDecisionPhase.EXECUTING
            "COMPLETED" -> ApprovalDecisionPhase.COMPLETED
            "FAILED", "REFUSED" -> ApprovalDecisionPhase.FAILED
            "CANCELLED" -> ApprovalDecisionPhase.CANCELLED
            "EXPIRED" -> ApprovalDecisionPhase.EXPIRED
            else -> return null
        }
        return previous.copy(phase = phase, updatedAtUnixMs = nowUnixMs)
    }

    fun label(status: ApprovalDecisionStatus): String = when (status.phase) {
        ApprovalDecisionPhase.SUBMITTING -> "Sending your decision…"
        ApprovalDecisionPhase.QUEUED -> "Decision queued on this phone. The Mac has not confirmed it yet."
        ApprovalDecisionPhase.DELIVERED -> "Relay received the decision. Waiting for the Mac supervisor to confirm it."
        ApprovalDecisionPhase.APPROVED -> "Approved by the Mac. Execution is pending."
        ApprovalDecisionPhase.EXECUTING -> "Approved operation is running on the Mac."
        ApprovalDecisionPhase.COMPLETED -> "Operation completed successfully on the Mac."
        ApprovalDecisionPhase.REJECTED -> "Rejected by the Mac. The operation was not run."
        ApprovalDecisionPhase.EXPIRED -> "Approval expired. The operation was not authorized."
        ApprovalDecisionPhase.CANCELLED -> "Operation was cancelled."
        ApprovalDecisionPhase.DELIVERY_FAILED -> "The relay refused this decision; the supervisor has not confirmed it. Retry only this same decision after checking the connection."
        ApprovalDecisionPhase.FAILED -> "The approved operation failed on the Mac."
        ApprovalDecisionPhase.INDETERMINATE -> "The decision was consumed, but execution outcome is unconfirmed. Check task status before retrying."
        ApprovalDecisionPhase.UNKNOWN -> "The Mac returned an unrecognized decision state. Check task status before acting."
    }

    fun blocksAnotherTap(status: ApprovalDecisionStatus?): Boolean = status != null && status.phase in setOf(
        ApprovalDecisionPhase.SUBMITTING,
        ApprovalDecisionPhase.QUEUED,
        ApprovalDecisionPhase.DELIVERED,
        ApprovalDecisionPhase.APPROVED,
        ApprovalDecisionPhase.EXECUTING,
        ApprovalDecisionPhase.COMPLETED,
        ApprovalDecisionPhase.REJECTED,
        ApprovalDecisionPhase.EXPIRED,
        ApprovalDecisionPhase.CANCELLED,
        ApprovalDecisionPhase.DELIVERY_FAILED,
        ApprovalDecisionPhase.FAILED,
        ApprovalDecisionPhase.INDETERMINATE,
        ApprovalDecisionPhase.UNKNOWN,
    )

    /** A permanent relay refusal can be retried explicitly with the same decision/idempotency key. */
    fun canExplicitlyRetry(status: ApprovalDecisionStatus?, decision: String): Boolean =
        status?.phase == ApprovalDecisionPhase.DELIVERY_FAILED && status.decision == decision
}
