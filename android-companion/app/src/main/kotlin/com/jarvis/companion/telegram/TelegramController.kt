package com.jarvis.companion.telegram

enum class PingOutcome { ACK, TIMEOUT }

data class PingResult(val nonce: String, val outcome: PingOutcome, val rttMs: Long?)

data class TelegramSnapshot(
    val connection: TelegramConnection,
    val auth: AuthStep,
    val online: Boolean,
    val error: String?,
    /** User-facing hint for an action that was refused (validation, not-connected). Does not change [connection]. */
    val notice: String?,
    val pendingPings: Int,
    val lastRttMs: Long?,
    val retryAfterUntilUnixMs: Long? = null,
    val sendBlockedReasonCode: String? = null,
)

/**
 * Orchestrates one Telegram transport: authorization, connection status, ping correlation and timing.
 * Pure Kotlin with an injected clock and nonce source, so every rule is testable on the JVM.
 */
class TelegramController(
    private val transport: TelegramTransport,
    private val clock: () -> Long,
    private val nonceSource: () -> String,
    private val onChange: (TelegramSnapshot) -> Unit = {},
) : TelegramEvents {
    private val lock = Any()
    private var credentials: ApiCredentials? = null
    private var auth: AuthStep = AuthStep.WaitParameters
    private var online = false
    private var error: String? = null
    private var notice: String? = null
    private val pending = LinkedHashMap<String, Long>()
    private val history = ArrayDeque<PingResult>()
    private var lastRtt: Long? = null
    private var ignoredAcks = 0
    private var retryAfterUntilMs = 0L
    private var sendBlockedReasonCode: String? = null

    fun start(creds: ApiCredentials) {
        synchronized(lock) {
            credentials = creds
            error = null
            notice = null
            auth = AuthStep.WaitParameters
        }
        transport.start(creds, this)
        notifyChange()
    }

    fun submitPhone(raw: String): Boolean {
        if (!TelegramStatus.validPhone(raw)) return fail("Enter the phone number in international format")
        transport.submitPhone(raw.trim().replace(" ", "").replace("-", ""))
        return true
    }

    fun submitCode(raw: String): Boolean {
        if (!TelegramStatus.validCode(raw)) return fail("Enter the login code from Telegram")
        transport.submitCode(raw.trim())
        return true
    }

    fun submitPassword(password: String): Boolean {
        if (password.isEmpty()) return fail("Enter your two-step verification password")
        transport.submitPassword(password)
        return true
    }

    /** Returns the nonce if a ping was sent, or null when the connection is not ready. */
    fun ping(): String? {
        val nonce: String = synchronized(lock) {
            if (connectionLocked() != TelegramConnection.CONNECTED) {
                notice = "Connect to Telegram before sending a ping"
                null
            } else {
                val fresh = nonceSource()
                require(TelegramStatus.validNonce(fresh)) { "nonce source produced an invalid nonce" }
                pending[fresh] = clock()
                fresh
            }
        } ?: run {
            notifyChange()
            return null
        }
        transport.sendPing(nonce)
        notifyChange()
        return nonce
    }

    /** Marks pings older than [timeoutMs] as timed out. Returns how many expired. */
    fun expirePings(timeoutMs: Long): Int {
        var expired = 0
        synchronized(lock) {
            val now = clock()
            val iterator = pending.entries.iterator()
            while (iterator.hasNext()) {
                val (nonce, sentAt) = iterator.next()
                if (now - sentAt >= timeoutMs) {
                    iterator.remove()
                    record(PingResult(nonce, PingOutcome.TIMEOUT, null))
                    expired++
                }
            }
        }
        if (expired > 0) notifyChange()
        return expired
    }

    fun stop() {
        transport.stop()
        synchronized(lock) { online = false; auth = AuthStep.Closed }
        notifyChange()
    }

    fun snapshot(): TelegramSnapshot = synchronized(lock) { snapshotLocked() }

    fun recentResults(): List<PingResult> = synchronized(lock) { history.toList() }

    fun ignoredAckCount(): Int = synchronized(lock) { ignoredAcks }

    // --- TelegramEvents (may be called from any thread) ---------------------

    override fun onAuthStep(step: AuthStep) {
        synchronized(lock) {
            auth = step
            notice = null
            if (step == AuthStep.Ready) error = null
        }
        notifyChange()
    }

    override fun onConnectionChanged(online: Boolean) {
        synchronized(lock) { this.online = online }
        notifyChange()
    }

    override fun onPingAck(nonce: String) {
        synchronized(lock) {
            val sentAt = pending.remove(nonce)
            if (sentAt == null || !TelegramStatus.validNonce(nonce)) {
                ignoredAcks++  // duplicate, late after timeout, or unknown: never counted as success
            } else {
                val rtt = clock() - sentAt
                lastRtt = rtt
                record(PingResult(nonce, PingOutcome.ACK, rtt))
            }
        }
        notifyChange()
    }

    override fun onError(message: String) {
        synchronized(lock) { error = message }
        notifyChange()
    }

    override fun onRateLimitUntil(retryUntilUnixMs: Long) {
        synchronized(lock) {
            retryAfterUntilMs = if (retryUntilUnixMs <= clock()) 0L else maxOf(retryAfterUntilMs, retryUntilUnixMs)
        }
        notifyChange()
    }

    override fun onSendBlocked(reasonCode: String) {
        val safe = reasonCode.takeIf { it.matches(Regex("[A-Z0-9_]{1,48}")) } ?: "SEND_BLOCKED"
        synchronized(lock) { sendBlockedReasonCode = safe }
        notifyChange()
    }

    fun clearSendBlock() {
        synchronized(lock) { sendBlockedReasonCode = null }
        notifyChange()
    }

    // --- internals -------------------------------------------------------------

    private fun fail(message: String): Boolean {
        synchronized(lock) { notice = message }
        notifyChange()
        return false
    }

    private fun record(result: PingResult) {
        history.addLast(result)
        while (history.size > 50) history.removeFirst()
    }

    private fun connectionLocked(): TelegramConnection =
        TelegramStatus.connection(credentials != null, auth, online, error)

    private fun snapshotLocked() = TelegramSnapshot(
        connection = connectionLocked(),
        auth = auth,
        online = online,
        error = error,
        notice = notice,
        pendingPings = pending.size,
        lastRttMs = lastRtt,
        retryAfterUntilUnixMs = retryAfterUntilMs.takeIf { it > clock() },
        sendBlockedReasonCode = sendBlockedReasonCode,
    )

    private fun notifyChange() {
        val snap = synchronized(lock) { snapshotLocked() }
        onChange(snap)
    }
}
