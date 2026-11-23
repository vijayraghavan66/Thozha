package com.jarvis.companion.link

import com.jarvis.companion.presentation.ConversationPresentation
import com.jarvis.companion.presentation.Speaker
import com.jarvis.link.CompanionEvent
import com.jarvis.link.decodeCompanionEvent
import java.security.MessageDigest

/**
 * Regression checks: a completed Mac answer reaches the chat whole, is verified by SHA-256 before use, and is never
 * replaced by a card preview. Runs on the JVM with no device: `./gradlew verifyFullAnswers`.
 */
private var assertions = 0

private fun check(condition: Boolean, message: String) {
    assertions++
    if (!condition) throw AssertionError("FAILED: $message")
}

private fun sha256Hex(text: String): String =
    MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

/** Builds the flat JSON a host task.result payload uses. Values are JSON-escaped here, as the host encodes them. */
private fun payload(vararg pairs: Pair<String, Any?>): String = pairs.joinToString(",", "{", "}") { (key, value) ->
    val encoded = when (value) {
        null -> "null"
        is Boolean -> value.toString()
        is Number -> value.toString()
        else -> "\"" + value.toString().replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n") + "\""
    }
    "\"$key\":$encoded"
}

private val answer = "Java is a programming language and software platform. ".repeat(40) +
    "\n\n```java\nSystem.out.println(\"hi\");\n```\n\nதமிழ் உரை ✓ — end."

private val fmt: (Long) -> String = { "t$it" }

fun main() {
    // --- decode: a verified body is accepted exactly -------------------------------------------------------------
    val verified = decodeCompanionEvent(
        "task.result",
        payload("task_id" to "a".repeat(32), "state" to "COMPLETED", "summary" to answer.take(200),
            "in_reply_to" to "m1", "body" to answer, "body_sha256" to sha256Hex(answer), "body_complete" to true),
    ) as CompanionEvent.TaskUpdate
    check(verified.body == answer, "a body whose SHA-256 matches is delivered byte-for-byte")
    check(!verified.integrityFailed && verified.bodyComplete, "a verified complete body is not flagged")

    // --- decode: a body that does not verify is never shown -------------------------------------------------------
    val tampered = decodeCompanionEvent(
        "task.result",
        payload("task_id" to "a".repeat(32), "state" to "COMPLETED", "summary" to "preview",
            "body" to answer + "x", "body_sha256" to sha256Hex(answer)),
    ) as CompanionEvent.TaskUpdate
    check(tampered.body == null && tampered.integrityFailed, "a body that fails its hash is dropped and flagged")

    val missingHash = decodeCompanionEvent(
        "task.result",
        payload("task_id" to "a".repeat(32), "state" to "COMPLETED", "summary" to "preview", "body" to answer),
    ) as CompanionEvent.TaskUpdate
    check(missingHash.body == null && missingHash.integrityFailed, "a body without a hash is not trusted")

    // --- decode: a stated cut stays stated -----------------------------------------------------------------------
    val cut = "partial answer\n\n[The full answer is longer than this phone delivery limit. The rest stays on the Mac.]"
    val cutEvent = decodeCompanionEvent(
        "task.result",
        payload("task_id" to "a".repeat(32), "state" to "COMPLETED", "summary" to "partial", "body" to cut,
            "body_sha256" to sha256Hex(cut), "body_complete" to false),
    ) as CompanionEvent.TaskUpdate
    check(cutEvent.body == cut && !cutEvent.bodyComplete, "a cut answer is delivered with its cut notice and marked incomplete")

    // --- decode: progress without a body keeps working ------------------------------------------------------------
    val progress = decodeCompanionEvent(
        "task.progress",
        payload("task_id" to "a".repeat(32), "state" to "RUNNING", "summary" to "Working on it."),
    ) as CompanionEvent.TaskUpdate
    check(progress.body == null && !progress.integrityFailed, "progress without a body is not an integrity failure")

    // --- bubbles: the verified body is the message; the summary is never substituted ------------------------------
    val bubble = ConversationPresentation.items(listOf(mapOf(
        "message_id" to "m1", "text" to "what is Java", "state" to "COMPLETED", "summary" to answer.take(200),
        "body" to answer, "created_at_unix_ms" to 1000L, "updated_at_unix_ms" to 2000L,
    )), fmt).first { it.speaker == Speaker.JARVIS }
    check(bubble.text == answer, "the chat bubble shows the full verified answer, not the 200-character preview")
    check(bubble.text.endsWith("end."), "the final characters of a long answer are present")

    val cutBubble = ConversationPresentation.items(listOf(mapOf(
        "message_id" to "m2", "text" to "q", "state" to "COMPLETED", "summary" to "partial",
        "body" to cut, "body_complete" to false, "created_at_unix_ms" to 1000L, "updated_at_unix_ms" to 2000L,
    )), fmt).first { it.speaker == Speaker.JARVIS }
    check(cutBubble.text == cut, "a stated cut is shown as stated")

    val failedBubble = ConversationPresentation.items(listOf(mapOf(
        "message_id" to "m3", "text" to "q", "state" to "COMPLETED", "summary" to "preview",
        "body_integrity_failed" to true, "created_at_unix_ms" to 1000L, "updated_at_unix_ms" to 2000L,
    )), fmt).first { it.speaker == Speaker.JARVIS }
    check(failedBubble.text.contains("integrity") && !failedBubble.text.contains("preview"), "an unverified answer is not shown as an answer")

    val legacy = ConversationPresentation.items(listOf(mapOf(
        "message_id" to "m4", "text" to "q", "state" to "COMPLETED", "summary" to "older record",
        "created_at_unix_ms" to 1000L, "updated_at_unix_ms" to 2000L,
    )), fmt).first { it.speaker == Speaker.JARVIS }
    check(legacy.text == "older record", "records saved before bodies existed still show their summary")

    println("Full answer checks passed: $assertions assertions")
}
