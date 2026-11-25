package com.jarvis.companion.telegram

import com.jarvis.link.LinkJson
import java.io.File
import java.io.IOException
import kotlin.concurrent.thread

private var assertions = 0

private fun check(condition: Boolean, message: String) {
    assertions++
    if (!condition) throw AssertionError("FAILED: $message")
}

/** args[0]: path to telegram_adapter/tunnel-v1.fixture.json produced by the Python (Mac) encoder. */
fun main(args: Array<String>) {
    var fakeNow = 0L
    val waits = mutableListOf<Long>()
    val rateGate = TelegramSendRateGate(100L, { fakeNow }, { wait -> waits += wait; fakeNow += wait })
    rateGate.awaitSlot()
    rateGate.awaitSlot()
    rateGate.awaitSlot()
    check(waits == listOf(0L, 100L, 100L), "concurrent tunnel frames reserve paced send slots")
    rateGate.deferForNanos(500L)
    rateGate.awaitSlot()
    check(waits.last() == 500L, "Telegram flood wait defers queued tunnel frames")

    var delayedNow = 0L
    val delayedWaits = mutableListOf<Long>()
    val delayedGate = TelegramSendRateGate(1_000_000_000L, { delayedNow }, { wait -> delayedWaits += wait; delayedNow += wait })
    delayedGate.deferForNanos(120_000_000_000L)
    check(!delayedGate.tryAwaitSlot(30_000_000_000L), "long Retry-After defers the signed request without waiting or sending stale auth")
    check(delayedWaits.isEmpty(), "long flood wait does not block the send executor")
    delayedNow = 90_000_000_000L
    check(delayedGate.tryAwaitSlot(30_000_000_000L), "request can reserve a safe slot once the signed freshness budget is met")
    check(delayedWaits == listOf(30_000_000_000L), "remaining safe delay is respected")

    var wallNow = 1_000L
    val retryFile = File.createTempFile("telegram-retry-state", ".txt").apply { delete() }
    val retryStore = TelegramRetryStateStore(retryFile, { wallNow }, { 1.0 })
    val serverRetry = retryStore.deferForServerRetryAfter(210L)
    check(serverRetry.retryUntilUnixMs == 211_000L, "server Retry-After is stored as the exact absolute deadline")
    check(retryStore.remainingMs() == 210_000L, "persisted Retry-After remaining time is readable")
    val restartedStore = TelegramRetryStateStore(retryFile, { wallNow }, { 1.0 })
    check(restartedStore.remainingMs() == 210_000L, "cooldown survives transport/process recreation")
    wallNow += 100_000L
    check(restartedStore.remainingMs() == 110_000L, "cooldown decreases against wall time after restart")
    check(telegramRetryDisposition(restartedStore.state(), wallNow) == TelegramRetryDisposition.RATE_LIMITED, "sync remains deferred until Retry-After expires")
    wallNow += 110_000L
    check(restartedStore.remainingMs() == 0L, "expired server deadline no longer blocks queued messages")

    val transientState = TelegramRetryStateStore(retryFile, { wallNow }, { 1.0 })
    check(transientState.deferTransientFailure().retryUntilUnixMs == wallNow + 1_000L, "first transient failure uses bounded exponential backoff")
    wallNow += 1_000L
    check(transientState.deferTransientFailure().retryUntilUnixMs == wallNow + 2_000L, "next transient failure increases its delay")
    val blocked = transientState.blockAutomaticRetry("AUTH_REQUIRED")
    check(telegramRetryDisposition(blocked, wallNow) == TelegramRetryDisposition.PERMANENTLY_BLOCKED, "permanent auth failure stops automatic sync")
    check(TelegramRetryStateStore(retryFile, { wallNow }, { 1.0 }).state().blockedCode == "AUTH_REQUIRED", "permanent block survives process recreation")
    check(transientState.clearPermanentBlock().blockedCode == null, "explicit owner retry can clear only the permanent failure block")
    retryFile.delete()

    check(classifyTdlibSendFailure(429) == TdlibSendFailureKind.RATE_LIMITED, "TDLib 429 is treated as server Retry-After")
    check(classifyTdlibSendFailure(401) == TdlibSendFailureKind.AUTHORIZATION, "TDLib authentication rejection is not auto-retried")
    check(classifyTdlibSendFailure(503) == TdlibSendFailureKind.TRANSIENT, "TDLib server errors use bounded backoff")
    check(classifyTdlibSendFailure(400) == TdlibSendFailureKind.PERMANENT, "TDLib permanent request rejection is blocked")
    check(classifyRelayFailure(401, "AUTH_FAILED") == RelayFailureDisposition.PERMANENT, "relay authorization failures stop automatic retries")
    check(classifyRelayFailure(429, "RATE_LIMITED") == RelayFailureDisposition.TRANSIENT, "relay rate limits use bounded cooldown")
    check(classifyRelayFailure(503, "INTERNAL_ERROR") == RelayFailureDisposition.TRANSIENT, "relay service failures use backoff")
    check(classifyRelayFailure(404, "NOT_FOUND") == RelayFailureDisposition.PERMANENT, "permanent relay rejection does not loop")

    val fixture = LinkJson.parseObject(File(args[0]).readText(Charsets.UTF_8))
    val requestFrames = (fixture["request_frames"] as List<*>).map { it as String }
    val responseFrames = (fixture["response_frames"] as List<*>).map { it as String }
    @Suppress("UNCHECKED_CAST")
    val expectedRequest = fixture["request"] as Map<String, Any>
    @Suppress("UNCHECKED_CAST")
    val expectedResponse = fixture["response"] as Map<String, Any>

    // --- cross-language parity: the Mac encoder's frames decode to the same messages on the phone -------------
    check(requestFrames.size == 2 && responseFrames.size == 1, "fixture has the expected frame counts")
    check(requestFrames.all { it.length <= TunnelFrames.MAX_FRAME_CHARS + 200 && it.all { c -> c.code < 0x80 } }, "Mac frames are ASCII and small")
    val reassembler = TunnelFrames.Reassembler()
    val decodedRequest = requestFrames.mapNotNull { reassembler.add(it) }
    check(decodedRequest.size == 1, "Mac request frames reassemble exactly once")
    check(decodedRequest[0]["id"] == expectedRequest["id"] && decodedRequest[0]["m"] == "POST", "request id and method survive")
    check(decodedRequest[0]["b"] == expectedRequest["b"], "request body (with non-ASCII and 5 KB of data) is byte-identical")
    check(decodedRequest[0]["h"] == expectedRequest["h"], "request headers survive")
    val decodedResponse = responseFrames.mapNotNull { TunnelFrames.Reassembler().add(it) }
    check(decodedResponse.size == 1 && (decodedResponse[0]["s"] as Number).toInt() == 202, "Mac response decodes with status 202")
    check(decodedResponse[0]["b"] == expectedResponse["b"], "response body with non-ASCII text decodes identically")

    // --- phone encoder: frames are ASCII, bounded, and reassemble out of order and with duplicates ----------
    val request = linkedMapOf<String, Any>(
        "t" to "req", "id" to "phone-req-0001", "m" to "POST", "p" to "/v1/messages",
        "h" to mapOf("Content-Type" to "application/json"),
        "b" to "{\"ciphertext\":\"" + "B".repeat(7000) + "\",\"note\":\"ünïcödé ✓\"}",
    )
    val phoneFrames = TunnelFrames.encode(request, "phone-req-0001")
    check(phoneFrames.size >= 3, "a 7 KB body is split into several frames")
    check(phoneFrames.all { it.length <= 4096 && it.all { c -> c.code < 0x80 } }, "phone frames are ASCII and under 4096 characters")
    val escapeHeavy = linkedMapOf<String, Any>("id" to "escape-test-0001", "b" to "😀\\\"".repeat(3000))
    val escapedFrames = TunnelFrames.encode(escapeHeavy, "escape-test-0001")
    check(escapedFrames.all { it.length <= 4096 }, "escape-heavy Unicode frames fit Telegram's wire limit")
    val escapedDecoder = TunnelFrames.Reassembler()
    check(escapedFrames.mapNotNull { escapedDecoder.add(it) }.single()["b"] == escapeHeavy["b"], "escape-heavy message round-trips")
    val mismatch = TunnelFrames.encode(mapOf("id" to "different-id-001", "t" to "res"), "outer-id-000001")
    check(TunnelFrames.Reassembler().add(mismatch.single()) == null, "frame id must match message id")
    val conflicting = TunnelFrames.Reassembler()
    val firstFrame = LinkJson.parseObject(escapedFrames.first().removePrefix(TunnelFrames.PREFIX)).toMutableMap()
    check(conflicting.add(escapedFrames.first()) == null, "partial frame held")
    firstFrame["d"] = "tampered"
    check(conflicting.add(TunnelFrames.PREFIX + LinkJson.encode(firstFrame)) == null, "conflicting duplicate frame rejected")
    val concurrentDecoder = TunnelFrames.Reassembler()
    val results = java.util.Collections.synchronizedList(mutableListOf<Map<String, Any>>())
    val workers = escapedFrames.map { frame -> thread { concurrentDecoder.add(frame)?.let { results.add(it) } } }
    workers.forEach { it.join() }
    check(results.size == 1 && results.single()["b"] == escapeHeavy["b"], "concurrent frame callbacks reassemble once")
    val phoneReassembler = TunnelFrames.Reassembler()
    val shuffled = phoneFrames.reversed() + phoneFrames.first()
    val completed = shuffled.mapNotNull { phoneReassembler.add(it) }
    check(completed.size == 1 && completed[0]["b"] == request["b"], "reversed frames with a duplicate reassemble once, identically")

    // --- malformed frames are ignored -------------------------------------------------------------------------
    check(TunnelFrames.Reassembler().add("not a tunnel frame") == null, "plain text is ignored")
    check(TunnelFrames.Reassembler().add(TunnelFrames.PREFIX + "{bad") == null, "broken JSON is ignored")

    // --- full exchange against a simulated Mac ----------------------------------------------------------------
    val sent = mutableListOf<String>()
    val diagnostics = mutableListOf<String>()
    val tunnel = TelegramRelayTunnel(sendFrame = { sent += it }, timeoutMs = 5_000, idSource = { "exchange-id-0001" }, log = { diagnostics += it })
    val macResponse = linkedMapOf<String, Any>("t" to "res", "id" to "exchange-id-0001", "s" to 200L, "b" to "{\"messages\":[]}")
    val worker = thread(start = true) {
        while (sent.isEmpty()) Thread.sleep(5)
        // The simulated Mac sees exactly the frames the phone sent, then answers with its response frames.
        val macView = TunnelFrames.Reassembler()
        sent.forEach { macView.add(it) }
        TunnelFrames.encode(macResponse, "exchange-id-0001").forEach { tunnel.onText(it) }
    }
    val response = tunnel.exchange("GET", "/v1/devices/d1/inbox?limit=5", mapOf("X-Jarvis-Link-Device" to "d1", "Authorization" to "leak"), ByteArray(0))
    worker.join()
    check(response.status == 200 && response.text == "{\"messages\":[]}", "exchange returns the relay response through the tunnel")
    check(sent.isNotEmpty() && !sent.joinToString("").contains("leak"), "only allowlisted headers leave the phone")
    check(diagnostics.size == 3 && diagnostics.all { !it.contains("exchange-id-0001") && !it.contains("/v1/") && !it.contains("leak") }, "diagnostics avoid raw ids, paths and headers")
    check(diagnostics.any { it.contains("tunnel_finish") && it.contains("outcome=response") }, "successful exchange records timing outcome")

    val authFrames = mutableListOf<String>()
    val authDiagnostics = mutableListOf<String>()
    val authTunnel = TelegramRelayTunnel(
        sendFrame = { authFrames += it }, timeoutMs = 5_000, idSource = { "auth-401-req-001" },
        log = { authDiagnostics += it },
    )
    val authRequestId = "01234567-89ab-cdef-0123-456789abcdef"
    val authWorker = thread(start = true) {
        while (authFrames.isEmpty()) Thread.sleep(1)
        val requestId = LinkJson.parseObject(authFrames.single().removePrefix(TunnelFrames.PREFIX))["id"] as String
        val errorBody = LinkJson.encode(mapOf("error" to mapOf(
            "code" to "TIMESTAMP_SKEW", "request_id" to authRequestId, "message" to "not logged",
        )))
        authTunnel.onText(TunnelFrames.encode(mapOf("t" to "res", "id" to requestId, "s" to 401L, "b" to errorBody), requestId).single())
    }
    val authResponse = authTunnel.exchange("GET", "/v1/devices/d1/inbox", emptyMap(), ByteArray(0))
    authWorker.join()
    val authDiagnostic = authDiagnostics.single { it.startsWith("relay_rejected") }
    check(authResponse.status == 401 && authDiagnostic.contains("code=TIMESTAMP_SKEW"), "401 rejection code is traced through the tunnel")
    check(authDiagnostic.contains("request_id=01234567") && "not logged" !in authDiagnostic, "auth diagnostics include only a truncated correlation ID, never response text")

    // --- timeout is an IOException, not a silent success -------------------------------------------------------
    val slow = TelegramRelayTunnel(sendFrame = { }, timeoutMs = 50, idSource = { "timeout-id-0001" })
    val timedOut = try { slow.exchange("GET", "/v1/x", emptyMap(), ByteArray(0)); false } catch (_: IOException) { true }
    check(timedOut, "no response within the timeout raises IOException")

    // --- late or unknown responses are ignored ------------------------------------------------------------------
    tunnel.onText(TunnelFrames.encode(macResponse, "exchange-id-0001")[0])  // second copy after completion
    check(true, "late duplicate response does not throw")

    println("Telegram tunnel checks passed: $assertions assertions")
}
