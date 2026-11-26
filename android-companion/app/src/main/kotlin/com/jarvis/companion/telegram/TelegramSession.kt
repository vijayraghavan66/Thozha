package com.jarvis.companion.telegram

import java.security.SecureRandom

/**
 * Process-wide Telegram session holder. The transport is chosen here, so the UI never names TDLib classes.
 * Until the official TDLib bindings are integrated, the simulated transport is used and labelled as such.
 */
object TelegramSession {
    /** Shown on-screen so the transport in use is always visible. */
    const val TRANSPORT_LABEL = "Telegram via TDLib (official client)"

    @Volatile var controller: TelegramController? = null

    /**
     * Current UI listener. Set by whichever screen is visible, so a recreated activity receives later updates.
     * The controller reports through this indirection instead of capturing one activity instance.
     */
    @Volatile var listener: ((TelegramSnapshot) -> Unit)? = null

    /** Writes one tunnel frame to the Jarvis bot chat. Set by the transport while it is started. */
    @Volatile var frameSender: ((String) -> Unit)? = null

    /** True only while the bot chat is resolved and Telegram reports the connection as online. */
    @Volatile var tunnelReady: Boolean = false

    @Volatile private var retryStateStore: TelegramRetryStateStore? = null

    /** Jarvis Link relay requests carried over Telegram. Fails with IOException when the connection is not up. */
    val tunnel: TelegramRelayTunnel = TelegramRelayTunnel(
        sendFrame = { frame ->
            val sender = frameSender ?: throw java.io.IOException("Telegram is not connected")
            sender(frame)
        },
        log = { line -> android.util.Log.i("JarvisTelegram", line) },
    )

    /** The tunnel when Telegram is ready for Home traffic; otherwise null, which keeps the USB/HTTPS path. */
    fun activeTunnel(): com.jarvis.link.RelayTransport? = if (tunnelReady && frameSender != null) tunnel else null

    internal fun retryState(): TelegramRetryState = retryStateStore?.state() ?: TelegramRetryState()

    internal fun retryRemainingMs(): Long = retryStateStore?.remainingMs() ?: 0L

    internal fun deferTransientRetry(): TelegramRetryState {
        val state = retryStateStore?.deferTransientFailure() ?: TelegramRetryState()
        controller?.onRateLimitUntil(state.retryUntilUnixMs)
        return state
    }

    internal fun blockAutomaticRetry(reasonCode: String): TelegramRetryState {
        val state = retryStateStore?.blockAutomaticRetry(reasonCode) ?: TelegramRetryState(blockedCode = reasonCode)
        state.blockedCode?.let { controller?.onSendBlocked(it) }
        return state
    }

    internal fun recordTransportSuccess(): TelegramRetryState {
        val state = retryStateStore?.recordSuccess() ?: TelegramRetryState()
        if (state.retryUntilUnixMs <= System.currentTimeMillis()) controller?.onRateLimitUntil(0L)
        return state
    }

    internal fun retryStoreFor(filesDir: java.io.File): TelegramRetryStateStore = synchronized(this) {
        retryStateStore ?: TelegramRetryStateStore(java.io.File(filesDir, "telegram_retry_state_v1.txt")).also {
            retryStateStore = it
        }
    }

    internal fun clearPermanentSendBlock(): TelegramRetryState {
        val state = retryStateStore?.clearPermanentBlock() ?: TelegramRetryState()
        controller?.clearSendBlock()
        return state
    }

    fun newTransport(filesDir: java.io.File, databaseKey: ByteArray): TelegramTransport =
        TdlibTelegramTransport(filesDir, databaseKey, retryStateStore = retryStoreFor(filesDir))

    private val random = SecureRandom()
    private const val NONCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"

    /** 16 characters from a cryptographically secure source. Ping correlation depends on uniqueness. */
    fun newNonce(): String = buildString(16) {
        repeat(16) { append(NONCE_ALPHABET[random.nextInt(NONCE_ALPHABET.length)]) }
    }

    private inline fun buildString(capacity: Int, block: StringBuilder.() -> Unit): String =
        StringBuilder(capacity).apply(block).toString()
}
