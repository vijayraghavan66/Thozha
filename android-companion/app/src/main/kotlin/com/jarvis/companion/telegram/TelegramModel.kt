package com.jarvis.companion.telegram

/** User-facing Telegram connection state. Android-independent so it can be tested on the JVM. */
enum class TelegramConnection { NOT_CONFIGURED, LOGIN_REQUIRED, CONNECTING, CONNECTED, RECONNECTING, ERROR }

/** Authorization step reported by the transport (mirrors TDLib's authorization states without depending on them). */
sealed interface AuthStep {
    data object WaitParameters : AuthStep
    data object WaitPhone : AuthStep
    data object WaitCode : AuthStep
    data class WaitPassword(val hint: String) : AuthStep
    data object Ready : AuthStep
    data object Closed : AuthStep
}

/** Telegram application credentials. Entered at runtime by the user; never embedded in the APK. */
class ApiCredentials private constructor(val apiId: Int, val apiHash: String) {
    override fun toString(): String = "ApiCredentials(apiId=<redacted>, apiHash=<redacted>)"

    companion object {
        private val HASH = Regex("[0-9a-f]{32}")

        /** Returns null when the input is not a positive integer id and a 32-character lowercase hex hash. */
        fun parse(rawId: String, rawHash: String): ApiCredentials? {
            val id = rawId.trim().toIntOrNull() ?: return null
            val hash = rawHash.trim().lowercase()
            if (id <= 0 || !HASH.matches(hash)) return null
            return ApiCredentials(id, hash)
        }
    }
}

object TelegramStatus {
    private val PHONE = Regex("\\+?[0-9]{7,15}")
    private val CODE = Regex("[0-9]{5,6}")

    fun connection(hasCredentials: Boolean, auth: AuthStep, online: Boolean, error: String?): TelegramConnection = when {
        !hasCredentials -> TelegramConnection.NOT_CONFIGURED
        error != null -> TelegramConnection.ERROR
        auth == AuthStep.WaitPhone || auth == AuthStep.WaitCode || auth is AuthStep.WaitPassword -> TelegramConnection.LOGIN_REQUIRED
        auth == AuthStep.WaitParameters -> TelegramConnection.CONNECTING
        auth == AuthStep.Closed -> TelegramConnection.RECONNECTING
        auth == AuthStep.Ready -> if (online) TelegramConnection.CONNECTED else TelegramConnection.RECONNECTING
        else -> TelegramConnection.CONNECTING
    }

    fun validPhone(raw: String): Boolean = PHONE.matches(raw.trim().replace(" ", "").replace("-", ""))
    fun validCode(raw: String): Boolean = CODE.matches(raw.trim())

    /** Jarvis ping nonce: letters, digits, underscore and hyphen, 8 to 64 characters. */
    private val NONCE = Regex("[A-Za-z0-9_-]{8,64}")
    fun validNonce(nonce: String): Boolean = NONCE.matches(nonce)

    const val PING_PREFIX = "JARVIS-PING "
    const val ACK_PREFIX = "JARVIS-ACK "
}

/** Bounded exponential backoff for transport restarts: 1s, 2s, 4s, ... capped at 60s. */
class ReconnectPolicy(private val baseMs: Long = 1_000L, private val capMs: Long = 60_000L) {
    fun delayMs(attempt: Int): Long {
        require(attempt >= 0) { "attempt must be non-negative" }
        if (attempt >= 31) return capMs
        return minOf(baseMs shl attempt, capMs)
    }
}
