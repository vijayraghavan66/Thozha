package com.jarvis.companion.presentation

private var assertions = 0

private fun check(condition: Boolean, message: String) {
    assertions++
    if (!condition) throw AssertionError("FAILED: $message")
}

private val fmt: (Long) -> String = { "t$it" }

private fun record(id: String, state: String?, text: String = "", summary: String = "", created: Long = 1000L, updated: Long = 2000L) =
    mapOf<String, Any?>(
        "message_id" to id, "text" to text, "state" to state, "summary" to summary,
        "created_at_unix_ms" to created, "updated_at_unix_ms" to updated,
    )

fun main() {
    // --- states -----------------------------------------------------------------------------------------------
    check(TaskStates.toneFor("completed") == TaskTone.COMPLETED, "state names are case-insensitive")
    check(TaskStates.toneFor("WAITING_APPROVAL") == TaskTone.WAITING, "approval wait is WAITING")
    check(TaskStates.toneFor("REFUSED") == TaskTone.FAILED, "refused counts as failed")
    check(TaskStates.toneFor("SOMETHING_NEW") == TaskTone.UNKNOWN && TaskStates.toneFor(null) == TaskTone.UNKNOWN, "unknown states are never success")
    check(TaskStates.label(TaskTone.UNKNOWN) == "Status unknown", "unknown label is honest")

    // --- conversation: no fabricated answers ----------------------------------------------------------------------
    val items = ConversationPresentation.items(listOf(
        record("m1", "COMPLETED", text = "Explain Java.", summary = "Java is a programming language."),
        record("m2", "COMPLETED", text = "Silent task"),
        record("m3", "RUNNING", text = "Still going"),
        record("m4", "QUEUED", text = "Queued one"),
        record("m5", "FAILED", text = "Broken"),
        record("m6", "CANCELLED", text = "Stopped"),
        record("m7", "WAITING_APPROVAL", text = "Needs approval"),
        record("m8", "SOMETHING_ODD", text = "Odd, no summary"),
        record("m1", "COMPLETED", text = "Explain Java.", summary = "Duplicate must not appear"),
        record("", "COMPLETED", text = "No id, skipped"),
    ), fmt)
    val byId = items.associateBy { it.id }
    check(byId["m1:jarvis"]?.text == "Java is a programming language.", "a genuine answer is shown exactly")
    check(byId["m1:user"]?.text == "Explain Java.", "the user's message is kept")
    check(byId["m2:jarvis"]?.text == "Completed. The assistant returned no text for this request.", "completed without text is not invented")
    check(byId["m3:jarvis"]?.isProgress == true && byId["m3:jarvis"]?.text == "Working on it.", "running shows honest progress")
    check(byId["m4:jarvis"]?.text == "Sent. Waiting for Jarvis to pick it up.", "queued shows a sent status, not a result")
    check(byId["m5:jarvis"]?.text == "This request failed. No answer was returned.", "failure is explicit")
    check(byId["m6:jarvis"]?.text == "Cancelled. Jarvis will not continue this request.", "cancellation is explicit")
    check(byId["m7:jarvis"]?.isProgress == true, "approval wait is progress, not success")
    check(byId["m8:jarvis"] == null, "unknown state without a summary shows no bubble")
    check(items.count { it.id == "m1:jarvis" } == 1 && items.count { it.id == "m1:user" } == 1, "duplicate message ids are shown once")
    check(items.none { it.text.contains("Duplicate") }, "the duplicate's content is discarded")
    check(items.first().id == "m1:user" && items.first().timeLabel == "t1000", "order and timestamps are preserved")
    check(items.none { it.text.contains("No id") }, "records without a message id are skipped")

    // --- timeline ---------------------------------------------------------------------------------------------
    val sections = TimelinePresentation.sections(listOf(
        mapOf("message_id" to "a", "text" to "Done one", "state" to "COMPLETED", "summary" to "ok", "updated_at_unix_ms" to 5L, "capability_scope" to "development"),
        mapOf("message_id" to "b", "text" to "Running", "state" to "RUNNING", "updated_at_unix_ms" to 6L),
        mapOf("message_id" to "c", "text" to "Approve me", "state" to "WAITING_APPROVAL", "updated_at_unix_ms" to 7L),
        mapOf("message_id" to "d", "text" to "Cancelled", "state" to "CANCELLED", "updated_at_unix_ms" to 8L),
        mapOf("message_id" to "e", "text" to "Failed", "state" to "FAILED", "updated_at_unix_ms" to 9L),
        mapOf("message_id" to "a", "text" to "Duplicate", "state" to "COMPLETED", "updated_at_unix_ms" to 10L),
        mapOf("text" to "No message id"),
        mapOf("message_id" to "f", "text" to "Sent only", "state" to "SENT", "updated_at_unix_ms" to 11L),
    ), fmt)
    check(sections.map { it.title } == listOf("Needs you", "In progress", "Completed", "Failed", "Cancelled"), "sections appear in a fixed order and empties are omitted")
    val rows = sections.flatMap { it.rows }.associateBy { it.requestId }
    check(rows.getValue("b").canCancel && rows.getValue("c").canCancel, "active and waiting tasks offer cancellation")
    check(!rows.getValue("a").canCancel && !rows.getValue("d").canCancel && !rows.getValue("e").canCancel, "finished tasks do not offer cancellation")
    check(rows.getValue("f").tone == TaskTone.RUNNING && !rows.getValue("f").canCancel, "sent-only requests are shown but not cancellable (matches the existing cancel rule)")
    check(sections.flatMap { it.rows }.size == 6, "duplicate and id-less records are dropped")
    check(rows.getValue("b").requestId == "b", "rows carry the request id the cancel path uses")
    check(rows.getValue("a").capabilityScope == "development" && rows.getValue("b").capabilityScope.isBlank(), "artifact action scope comes only from the locally recorded task scope")
    check(TimelinePresentation.sections(emptyList(), fmt).isEmpty(), "no tasks yields the empty state")
    check(TaskStates.toneFor("UNCONFIRMED") == TaskTone.UNKNOWN, "unconfirmed delivery is not presented as success or active execution")
    check(ConversationPresentation.replyFor(
        "UNCONFIRMED", TaskTone.UNKNOWN, "No Mac delivery receipt was confirmed. Check task status before retrying.",
    )?.first?.contains("No Mac delivery receipt") == true, "unconfirmed delivery explains the uncertainty instead of hanging")

    val progress = TaskProgressPresentation.eventsFor(
        taskId = "task-1",
        updates = listOf(
            mapOf("task_id" to "task-1", "state" to "PLANNING", "summary" to "Plan accepted", "event_seq" to 1L, "received_at_unix_ms" to 11L),
            mapOf("task_id" to "other", "state" to "COMPLETED", "summary" to "Unrelated", "event_seq" to 2L, "received_at_unix_ms" to 12L),
            mapOf("task_id" to "task-1", "state" to "RUNNING", "summary" to "Worker started", "event_seq" to 3L, "received_at_unix_ms" to 13L),
            mapOf("task_id" to "task-1", "state" to "RUNNING", "summary" to "Duplicate", "event_seq" to 3L, "received_at_unix_ms" to 14L),
        ),
        formatTime = fmt,
    )
    check(progress.map { it.label } == listOf("In progress", "In progress"), "only the task's authenticated progress events are shown in sequence")
    check(progress.last().detail == "Worker started" && progress.last().timeLabel == "t13", "duplicate progress events are discarded in favor of the original event")
    check(TaskProgressPresentation.eventsFor("missing", emptyList(), fmt).isEmpty(), "no progress is fabricated when the Mac has sent no event")

    // --- approvals -----------------------------------------------------------------------------------------------
    val now = 10_000_000L
    fun approval(status: String?, expiresIn: Long?) = mapOf<String, Any?>(
        "approval_id" to "ap1", "operation" to "open_app", "target" to "Calculator",
        "canonical_arguments_digest" to "0123456789abcdef", "status" to status,
        "expires_at_unix_ms" to expiresIn?.let { now + it },
    )
    val pending = ApprovalPresentation.card(approval("PENDING", 60_000), now) { "1 min" }!!
    check(pending.canDecide && pending.phase == ApprovalPhase.PENDING, "a pending approval with a future expiry can be decided")
    check(pending.operation == "open_app" && pending.target == "Calculator" && pending.digestPrefix == "0123456789ab", "exact operation, target and digest prefix are shown")
    check(pending.expiryLabel == "Expires in 1 min", "expiry is shown while pending")
    val expired = ApprovalPresentation.card(approval("PENDING", -1), now) { "x" }!!
    check(expired.phase == ApprovalPhase.EXPIRED && !expired.canDecide, "an expired pending approval cannot be decided")
    check(!ApprovalPresentation.card(approval("PENDING", null), now) { "x" }!!.canDecide, "no stated expiry fails closed")
    check(ApprovalPresentation.card(approval("PENDING", 0), now) { "x" }!!.phase == ApprovalPhase.EXPIRED, "an approval expires at its exact deadline")
    check(!ApprovalPresentation.card(approval("APPROVED", 60_000), now) { "x" }!!.canDecide, "an approved item is not decidable again")
    check(!ApprovalPresentation.card(approval("ODD", 60_000), now) { "x" }!!.canDecide, "unknown approval status is not decidable")
    check(ApprovalPresentation.card(mapOf("operation" to "x"), now) { "x" } == null, "an approval without an id is ignored")

    // --- approve -> supervisor -> execution: transport receipt is never treated as authorization -----------------
    val queuedDecision = ApprovalDecisionPresentation.submitted(
        approvalId = "ap1", taskId = "task1", decision = "approve", queuedOffline = true,
        messageId = "msg1", nowUnixMs = now, operation = "open_app", target = "Calculator",
    )!!
    check(queuedDecision.phase == ApprovalDecisionPhase.QUEUED, "offline approval decision stays queued")
    check(ApprovalDecisionPresentation.label(queuedDecision).contains("not confirmed"), "queued decision is not called approved")
    check(ApprovalDecisionPresentation.blocksAnotherTap(queuedDecision), "queued decision prevents a duplicate tap")
    val deliveryFailed = queuedDecision.copy(phase = ApprovalDecisionPhase.DELIVERY_FAILED)
    check(ApprovalDecisionPresentation.canExplicitlyRetry(deliveryFailed, "approve"), "a permanent delivery refusal permits only an explicit same-decision retry")
    check(!ApprovalDecisionPresentation.canExplicitlyRetry(deliveryFailed, "reject"), "a failed approval cannot be silently changed into a rejection")
    check(ApprovalDecisionPresentation.blocksAnotherTap(deliveryFailed), "ordinary duplicate taps remain blocked after a delivery failure")
    val interrupted = ApprovalDecisionPresentation.submitting(
        approvalId = "ap2", taskId = "task2", decision = "approve", operation = "open_app", target = "Calculator", nowUnixMs = now,
    )!!
    val afterRestart = ApprovalDecisionPresentation.afterRestart(interrupted)
    check(afterRestart.phase == ApprovalDecisionPhase.UNKNOWN, "process restart during send is reconciled as uncertain")
    check(ApprovalDecisionPresentation.blocksAnotherTap(afterRestart), "uncertain decision is not blindly replayed")
    val restoredQueued = ApprovalDecisionPresentation.fromRuntimeState(null, mapOf(
        "approval_id" to "ap3", "task_id" to "task3", "decision" to "approve", "state" to "QUEUED",
    ), now + 1)
    check(restoredQueued?.phase == ApprovalDecisionPhase.QUEUED, "durable runtime restores queued decision after app restart")
    val restoredDelivered = ApprovalDecisionPresentation.fromRuntimeState(restoredQueued, mapOf(
        "approval_id" to "ap3", "task_id" to "task3", "decision" to "approve",
        "state" to "DELIVERED_WAITING_SUPERVISOR", "authoritative" to false,
    ), now + 2)
    check(restoredDelivered?.phase == ApprovalDecisionPhase.DELIVERED, "delivery recovery still waits for supervisor authority")
    val relayAccepted = ApprovalDecisionPresentation.submitted(
        approvalId = "ap1", taskId = "task1", decision = "approve", queuedOffline = false,
        messageId = "msg2", nowUnixMs = now,
    )!!
    check(relayAccepted.phase == ApprovalDecisionPhase.DELIVERED, "relay receipt is distinct from supervisor acknowledgement")
    check(ApprovalDecisionPresentation.label(relayAccepted).contains("Waiting for the Mac supervisor"), "relay receipt does not claim execution")
    check(ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "other", "task_id" to "task1",
        "decision" to "approve", "status" to "APPROVED", "execution_state" to "COMPLETED",
        "executed" to true, "success" to true,
    ), now + 1) == null, "mismatched approval result cannot advance this card")
    val approved = ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "ap1", "task_id" to "task1",
        "decision" to "approve", "status" to "APPROVED", "execution_state" to "EXECUTION_PENDING",
        "executed" to false, "success" to false,
    ), now + 2)!!
    check(approved.phase == ApprovalDecisionPhase.APPROVED, "supervisor approval is shown as pending execution")
    check(ApprovalDecisionPresentation.taskState(approved, "RUNNING", now + 3)?.phase == ApprovalDecisionPhase.EXECUTING, "fresh Mac task state shows execution")
    check(ApprovalDecisionPresentation.taskState(approved, "COMPLETED", now + 4)?.phase == ApprovalDecisionPhase.COMPLETED, "terminal Mac task state completes an approved operation")
    val completed = ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "ap1", "task_id" to "task1",
        "decision" to "approve", "status" to "APPROVED", "execution_state" to "COMPLETED",
        "executed" to true, "success" to true,
    ), now + 5)!!
    check(completed.phase == ApprovalDecisionPhase.COMPLETED, "only executed, persisted success can show completed")
    check(ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "ap1", "task_id" to "task1",
        "decision" to "approve", "status" to "APPROVED", "execution_state" to "COMPLETED",
        "executed" to true, "success" to true, "simulated" to true,
    ), now + 5)?.phase == ApprovalDecisionPhase.INDETERMINATE, "simulated completion never becomes physical success")
    check(ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "ap1", "task_id" to "task1",
        "decision" to "approve", "status" to "APPROVED", "execution_state" to "COMPLETED",
        "executed" to false, "success" to true,
    ), now + 6)?.phase == ApprovalDecisionPhase.INDETERMINATE, "success without execution evidence fails closed")
    check(ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "ap1", "task_id" to "task1",
        "decision" to "approve", "status" to "APPROVED", "execution_state" to "INDETERMINATE",
        "executed" to false, "success" to false,
    ), now + 7)?.phase == ApprovalDecisionPhase.INDETERMINATE, "consumed approval without terminal evidence is not replayed")
    check(ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "ap1", "task_id" to "task1",
        "decision" to "approve", "status" to "APPROVED", "execution_state" to "FAILED",
        "executed" to true, "success" to false,
    ), now + 8)?.phase == ApprovalDecisionPhase.FAILED, "execution failure is not presented as approval success")
    check(ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "ap1", "task_id" to "task1",
        "decision" to "approve", "status" to "REJECTED", "execution_state" to "REJECTED",
        "executed" to false, "success" to false,
    ), now + 9)?.phase == ApprovalDecisionPhase.REJECTED, "rejection is explicit and not execution")
    check(ApprovalDecisionPresentation.authoritative(relayAccepted, mapOf(
        "kind" to "approval.decision_result", "approval_id" to "ap1", "task_id" to "task1",
        "decision" to "approve", "status" to "EXPIRED", "execution_state" to "EXPIRED",
        "executed" to false, "success" to false,
    ), now + 10)?.phase == ApprovalDecisionPhase.EXPIRED, "expiration is explicit")
    check(!ApprovalDecisionPresentation.blocksAnotherTap(null), "fresh pending approval remains decidable")
    check(ApprovalDecisionPresentation.blocksAnotherTap(completed), "terminal approval cannot be decided again")
    val completedPlanApproval = completed.copy(operation = "mcp:jarvis_dev:approve_plan")
    check(
        ApprovalDecisionPresentation.label(completedPlanApproval).contains("Review its test and artifact result"),
        "plan approval completion directs the owner to the actual project result",
    )

    // --- connection: three separate facts, no false "Mac unavailable" ---------------------------------------------
    val connected = ConnectionPresentation.summary(TransportState.CONNECTED, now - 60_000, AiEvidence.ANSWERED, now) { "$it ms ago" }
    check(connected.headline == "Connected", "connected with an answer reads Connected")
    check(connected.rows.map { it.label } == listOf("Telegram", "Mac", "AI"), "transport, Mac and AI are reported separately")
    check(connected.rows[1].value == "Replied 60000 ms ago", "a recent Mac reply is shown as observed")
    val noReply = ConnectionPresentation.summary(TransportState.CONNECTED, null, AiEvidence.NOT_VERIFIED, now) { "x" }
    check(noReply.rows[2].value == "Not verified yet", "the AI is not claimed available without an answer")
    check(noReply.headline == "Connected to Telegram", "connected without a reply is not called Mac-connected")
    val offline = ConnectionPresentation.summary(TransportState.RECONNECTING, now - 9_000_000, AiEvidence.NO_RECENT_ANSWER, now) { "old" }
    check(offline.headline == "Reconnecting", "a reconnecting transport is named")
    check(offline.rows[1].value.startsWith("Last reply"), "a stale Mac reply is labelled as last seen")
    check((connected.rows + noReply.rows + offline.rows).none { it.value.contains("Mac unavailable") }, "never reports Mac unavailable as a transport fact")
    check(ConnectionPresentation.summary(null, null, AiEvidence.NOT_VERIFIED, now) { "x" }.headline == "Telegram is not set up", "no transport state means not set up")

    // --- paired Mac status is distinct from Telegram and phone internet ---------------------------------------
    val unpairedMac = MacLinkPresentation.state(
        paired = false, enrollmentPending = false, syncing = false,
        lastSyncMs = 0L, lastSyncFailed = false, nowMs = now, formatAgo = fmt,
    )
    check(unpairedMac.label == "Mac not paired" && !unpairedMac.paired, "a fresh phone never claims Mac connectivity")
    check(unpairedMac.actionLabel == "Set up this phone", "fresh device points to the signed pairing flow")
    val pendingMac = MacLinkPresentation.state(
        paired = false, enrollmentPending = true, syncing = false,
        lastSyncMs = 0L, lastSyncFailed = false, nowMs = now, formatAgo = fmt,
    )
    check(pendingMac.tone == TaskTone.WAITING && pendingMac.pendingEnrollment, "pending enrollment waits for Mac confirmation")
    val pairedUnseen = MacLinkPresentation.state(
        paired = true, enrollmentPending = false, syncing = false,
        lastSyncMs = 0L, lastSyncFailed = false, nowMs = now, formatAgo = fmt,
    )
    check(pairedUnseen.label == "Paired · not checked yet" && pairedUnseen.tone == TaskTone.UNKNOWN, "pairing alone is not reachability")
    val reachableMac = MacLinkPresentation.state(
        paired = true, enrollmentPending = false, syncing = false,
        lastSyncMs = now - 30_000L, lastSyncFailed = false, nowMs = now, formatAgo = fmt,
    )
    check(reachableMac.label == "Mac reachable" && reachableMac.tone == TaskTone.COMPLETED, "recent successful sync confirms reachability")
    val staleMac = MacLinkPresentation.state(
        paired = true, enrollmentPending = false, syncing = false,
        lastSyncMs = now - 300_000L, lastSyncFailed = false, nowMs = now, formatAgo = fmt,
    )
    check(staleMac.tone == TaskTone.UNKNOWN && staleMac.label.startsWith("Last seen"), "stale sync is not shown as connected")
    val failedMac = MacLinkPresentation.state(
        paired = true, enrollmentPending = false, syncing = false,
        lastSyncMs = now, lastSyncFailed = true, nowMs = now, formatAgo = fmt,
    )
    check(failedMac.tone == TaskTone.FAILED && failedMac.actionLabel == "Sync now", "failed sync offers explicit recovery")

    // --- voice ---------------------------------------------------------------------------------------------------
    check(!VoicePresentation.view(VoicePhase.PERMISSION_DENIED).pushToTalkEnabled, "denied microphone disables push-to-talk")
    check(VoicePresentation.view(VoicePhase.LISTENING).showWaveform, "listening shows the waveform")
    check(!VoicePresentation.view(VoicePhase.THINKING).pushToTalkEnabled, "thinking does not accept a new recording")
    check(VoicePresentation.view(VoicePhase.ERROR, "x".repeat(500)).hint.length == 200, "error hints are bounded")
    check(VoicePhase.values().all { VoicePresentation.view(it).title.isNotBlank() }, "every voice phase has a title")

    // --- motion --------------------------------------------------------------------------------------------------
    check(MotionPolicy.reduced(0f, false) && MotionPolicy.reduced(1f, true), "reduced motion from system or user")
    check(!MotionPolicy.reduced(1f, false), "full motion by default")
    check(MotionPolicy.durationMs(320L, true) == 0L && MotionPolicy.durationMs(320L, false) == 320L, "reduced motion removes durations")

    println("Presentation checks passed: $assertions assertions")
}
