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
        mapOf("task_id" to "a", "text" to "Done one", "state" to "COMPLETED", "summary" to "ok", "updated_at_unix_ms" to 5L),
        mapOf("task_id" to "b", "text" to "Running", "state" to "RUNNING", "updated_at_unix_ms" to 6L),
        mapOf("task_id" to "c", "text" to "Approve me", "state" to "WAITING_APPROVAL", "updated_at_unix_ms" to 7L),
        mapOf("task_id" to "d", "text" to "Cancelled", "state" to "CANCELLED", "updated_at_unix_ms" to 8L),
        mapOf("task_id" to "e", "text" to "Failed", "state" to "FAILED", "updated_at_unix_ms" to 9L),
        mapOf("task_id" to "a", "text" to "Duplicate", "state" to "COMPLETED", "updated_at_unix_ms" to 10L),
        mapOf("text" to "No task id"),
    ), fmt)
    check(sections.map { it.title } == listOf("Needs you", "In progress", "Completed", "Failed", "Cancelled"), "sections appear in a fixed order and empties are omitted")
    val rows = sections.flatMap { it.rows }.associateBy { it.taskId }
    check(rows.getValue("b").canCancel && rows.getValue("c").canCancel, "active and waiting tasks offer cancellation")
    check(!rows.getValue("a").canCancel && !rows.getValue("d").canCancel && !rows.getValue("e").canCancel, "finished tasks do not offer cancellation")
    check(sections.flatMap { it.rows }.size == 5, "duplicate and task-less records are dropped")
    check(TimelinePresentation.sections(emptyList(), fmt).isEmpty(), "no tasks yields the empty state")

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
    check(!ApprovalPresentation.card(approval("APPROVED", 60_000), now) { "x" }!!.canDecide, "an approved item is not decidable again")
    check(!ApprovalPresentation.card(approval("ODD", 60_000), now) { "x" }!!.canDecide, "unknown approval status is not decidable")
    check(ApprovalPresentation.card(mapOf("operation" to "x"), now) { "x" } == null, "an approval without an id is ignored")

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
