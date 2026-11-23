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

    fun newTransport(filesDir: java.io.File, databaseKey: ByteArray): TelegramTransport =
        TdlibTelegramTransport(filesDir, databaseKey)

    private val random = SecureRandom()
    private const val NONCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"

    /** 16 characters from a cryptographically secure source. Ping correlation depends on uniqueness. */
    fun newNonce(): String = buildString(16) {
        repeat(16) { append(NONCE_ALPHABET[random.nextInt(NONCE_ALPHABET.length)]) }
    }

    private inline fun buildString(capacity: Int, block: StringBuilder.() -> Unit): String =
        StringBuilder(capacity).apply(block).toString()
}
