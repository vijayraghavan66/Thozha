package com.jarvis.companion.telegram

/**
 * The only surface Jarvis messaging uses. TDLib-specific classes stay behind an implementation of this
 * interface, so the UI, the controller and the tests never touch TDLib types.
 */
interface TelegramTransport {
    /** Starts the transport. Events arrive on [sink]; implementations may call back from any thread. */
    fun start(credentials: ApiCredentials, sink: TelegramEvents)

    fun submitPhone(phone: String)
    fun submitCode(code: String)
    fun submitPassword(password: String)

    /** Sends a harmless Jarvis ping. The reply arrives later through [TelegramEvents.onPingAck]. */
    fun sendPing(nonce: String)

    fun stop()
}

/** Callbacks from a transport. Implementations must not block these calls. */
interface TelegramEvents {
    fun onAuthStep(step: AuthStep)
    fun onConnectionChanged(online: Boolean)
    fun onPingAck(nonce: String)
    fun onError(message: String)
}
