package com.jarvis.companion.telegram

private var assertions = 0

private fun check(condition: Boolean, message: String) {
    assertions++
    if (!condition) throw AssertionError("FAILED: $message")
}

private const val HASH = "0123456789abcdef0123456789abcdef"

fun main() {
    // --- explicit remote transport preference ---------------------------------
    check(!TelegramRemoteModePolicy.resolve(null, previouslyConfigured = false), "fresh install defaults to local-only")
    check(TelegramRemoteModePolicy.resolve(null, previouslyConfigured = true), "existing Telegram sign-in is preserved as prior opt-in")
    check(!TelegramRemoteModePolicy.resolve(false, previouslyConfigured = true), "explicit local-only choice overrides saved account")
    check(TelegramRemoteModePolicy.resolve(true, previouslyConfigured = false), "explicit remote enable is preserved while account setup is pending")

    // --- credentials ---------------------------------------------------------
    val creds = ApiCredentials.parse("12345678", HASH)
    check(creds != null, "valid id and hash are accepted")
    check(ApiCredentials.parse(" 12345678 ", HASH.uppercase())?.apiHash == HASH, "hash is trimmed and lowercased")
    check(ApiCredentials.parse("0", HASH) == null, "zero id rejected")
    check(ApiCredentials.parse("-5", HASH) == null, "negative id rejected")
    check(ApiCredentials.parse("12x45", HASH) == null, "non-numeric id rejected")
    check(ApiCredentials.parse("12345678", HASH.dropLast(1)) == null, "short hash rejected")
    check(ApiCredentials.parse("12345678", HASH.replace('a', 'g')) == null, "non-hex hash rejected")
    check(ApiCredentials.parse("", "") == null, "empty input rejected")
    check(!creds!!.toString().contains(HASH) && !creds.toString().contains("12345678"), "credentials are redacted in toString")

    // --- connection status table ----------------------------------------------
    check(TelegramStatus.connection(false, AuthStep.Ready, true, null) == TelegramConnection.NOT_CONFIGURED, "no credentials -> NOT_CONFIGURED")
    check(TelegramStatus.connection(true, AuthStep.WaitParameters, false, null) == TelegramConnection.CONNECTING, "waiting for parameters -> CONNECTING")
    check(TelegramStatus.connection(true, AuthStep.WaitPhone, false, null) == TelegramConnection.LOGIN_REQUIRED, "phone prompt -> LOGIN_REQUIRED")
    check(TelegramStatus.connection(true, AuthStep.WaitCode, false, null) == TelegramConnection.LOGIN_REQUIRED, "code prompt -> LOGIN_REQUIRED")
    check(TelegramStatus.connection(true, AuthStep.WaitPassword("h"), false, null) == TelegramConnection.LOGIN_REQUIRED, "password prompt -> LOGIN_REQUIRED")
    check(TelegramStatus.connection(true, AuthStep.Ready, true, null) == TelegramConnection.CONNECTED, "ready and online -> CONNECTED")
    check(TelegramStatus.connection(true, AuthStep.Ready, false, null) == TelegramConnection.RECONNECTING, "ready and offline -> RECONNECTING")
    check(TelegramStatus.connection(true, AuthStep.Closed, false, null) == TelegramConnection.RECONNECTING, "closed -> RECONNECTING")
    check(TelegramStatus.connection(true, AuthStep.Ready, true, "boom") == TelegramConnection.ERROR, "error message -> ERROR")

    // --- input validators -----------------------------------------------------
    check(TelegramStatus.validPhone("+1 555-123-4567"), "international phone with separators accepted")
    check(!TelegramStatus.validPhone("12"), "too-short phone rejected")
    check(!TelegramStatus.validPhone("+1555abc4567"), "phone with letters rejected")
    check(TelegramStatus.validCode("12345"), "5-digit code accepted")
    check(!TelegramStatus.validCode("1234a"), "code with letters rejected")
    check(TelegramStatus.validNonce("abcdefgh1234"), "valid nonce accepted")
    check(!TelegramStatus.validNonce("short"), "too-short nonce rejected")
    check(!TelegramStatus.validNonce("has spaces 123"), "nonce with spaces rejected")
    check(!TelegramStatus.validNonce("a".repeat(65)), "over-long nonce rejected")

    // --- reconnect backoff ----------------------------------------------------
    val policy = ReconnectPolicy()
    check(policy.delayMs(0) == 1_000L && policy.delayMs(1) == 2_000L && policy.delayMs(2) == 4_000L, "backoff doubles")
    check(policy.delayMs(6) == 60_000L && policy.delayMs(40) == 60_000L, "backoff is capped at 60 s")

    // --- controller: login, ping timing, correlation -------------------------------
    var now = 1_000L
    val nonces = ArrayDeque(listOf("abcdefgh1234", "zyxwvuts9876", "qrstuvwx5555"))
    val snapshots = mutableListOf<TelegramSnapshot>()
    val fake = FakeTelegramTransport(deliverAcks = false)
    val controller = TelegramController(fake, clock = { now }, nonceSource = { nonces.removeFirst() }, onChange = { snapshots += it })

    check(controller.snapshot().connection == TelegramConnection.NOT_CONFIGURED, "starts as NOT_CONFIGURED")
    check(controller.ping() == null, "ping refused before start")

    controller.start(creds!!)
    check(controller.snapshot().connection == TelegramConnection.LOGIN_REQUIRED, "after start, login is required")
    check(controller.submitPhone("+15551234567"), "valid phone accepted")
    check(controller.snapshot().auth == AuthStep.WaitCode, "phone submitted -> waiting for code")
    check(!controller.submitCode("12"), "malformed code refused locally")
    check(controller.snapshot().notice == "Enter the login code from Telegram", "validation message shown as a notice")
    check(controller.snapshot().connection == TelegramConnection.LOGIN_REQUIRED, "validation notice does not change connection state")
    check(controller.submitCode("12345"), "code accepted")
    check(controller.snapshot().connection == TelegramConnection.CONNECTED, "login completes -> CONNECTED")
    check(controller.snapshot().error == null, "error cleared after login")

    now = 2_000L
    val first = controller.ping()
    check(first == "abcdefgh1234", "ping returns the generated nonce")
    check(fake.pingsSent == listOf("abcdefgh1234"), "transport receives exactly the nonce")
    check(controller.snapshot().pendingPings == 1, "ping is pending")
    now = 2_137L
    controller.onPingAck(first!!)
    check(controller.recentResults().last().outcome == PingOutcome.ACK, "ack recorded")
    check(controller.recentResults().last().rttMs == 137L, "round-trip time is measured from the sent time")
    check(controller.snapshot().lastRttMs == 137L && controller.snapshot().pendingPings == 0, "snapshot reflects the ack")

    controller.onPingAck(first)  // duplicate
    controller.onPingAck("unknownnonce1")  // never sent
    check(controller.ignoredAckCount() == 2 && controller.recentResults().size == 1, "duplicate and unknown acks are never counted as success")

    // --- drop, refusal while offline, restore ----------------------------------
    fake.simulateDrop()
    check(controller.snapshot().connection == TelegramConnection.RECONNECTING, "drop -> RECONNECTING")
    check(controller.ping() == null, "ping refused while reconnecting")
    check(controller.snapshot().error == null && controller.snapshot().notice != null, "refused ping sets a notice, not an error")
    fake.simulateRestore()
    check(controller.snapshot().connection == TelegramConnection.CONNECTED, "restore -> CONNECTED")

    // --- timeout of an unanswered ping ----------------------------------------
    now = 10_000L
    val second = controller.ping()
    check(second == "zyxwvuts9876", "second ping uses the next nonce")
    now = 10_500L
    check(controller.expirePings(20_000L) == 0, "ping is not expired early")
    now = 40_000L
    check(controller.expirePings(20_000L) == 1, "unanswered ping expires")
    check(controller.recentResults().last().outcome == PingOutcome.TIMEOUT, "expired ping recorded as TIMEOUT")
    controller.onPingAck(second!!)  // late ack after timeout
    check(controller.recentResults().count { it.outcome == PingOutcome.ACK } == 1, "late ack after timeout is not counted")

    // --- error path and recovery ----------------------------------------------
    controller.onError("network unavailable")
    check(controller.snapshot().connection == TelegramConnection.ERROR, "transport error -> ERROR")
    controller.onAuthStep(AuthStep.Ready)
    check(controller.snapshot().connection == TelegramConnection.CONNECTED, "error clears once authorization is ready again")

    controller.onRateLimitUntil(now + 210_000L)
    check(controller.snapshot().retryAfterUntilUnixMs == now + 210_000L, "server Retry-After is exposed as a delivery cooldown")
    controller.onSendBlocked("AUTH_REQUIRED")
    check(controller.snapshot().sendBlockedReasonCode == "AUTH_REQUIRED", "permanent send authorization failures are surfaced separately")
    controller.clearSendBlock()
    check(controller.snapshot().sendBlockedReasonCode == null, "an explicit manual retry clears only the permanent block")

    // --- two-step verification ---------------------------------------------------
    val fake2 = FakeTelegramTransport(requirePassword = true)
    val controller2 = TelegramController(fake2, clock = { 0L }, nonceSource = { "nonce-aaaa-1111" })
    controller2.start(creds!!)
    controller2.submitPhone("+15551234567")
    controller2.submitCode("12345")
    check(controller2.snapshot().connection == TelegramConnection.LOGIN_REQUIRED, "password step keeps LOGIN_REQUIRED")
    check(!controller2.submitPassword(""), "empty password refused")
    check(controller2.submitPassword("secret"), "password submitted")
    check(controller2.snapshot().connection == TelegramConnection.CONNECTED, "password completes login")

    // --- secrets never appear in snapshots --------------------------------------
    check(snapshots.none { it.toString().contains(HASH) }, "no snapshot exposes the API hash")

    println("Telegram model checks passed: $assertions assertions")
}
