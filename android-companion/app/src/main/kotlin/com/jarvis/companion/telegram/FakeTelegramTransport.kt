package com.jarvis.companion.telegram

/**
 * Scripted transport for UI previews and automated tests. It never touches the network. It exercises the
 * same authorization and ping paths as the real transport, so the UI and controller are covered without TDLib.
 */
class FakeTelegramTransport(
    private val requirePassword: Boolean = false,
    private val deliverAcks: Boolean = true,
    private val acceptedCode: String = "12345",
) : TelegramTransport {
    @Volatile private var sink: TelegramEvents? = null
    val pingsSent = mutableListOf<String>()
    @Volatile var started = false
        private set

    override fun start(credentials: ApiCredentials, sink: TelegramEvents) {
        this.sink = sink
        started = true
        sink.onAuthStep(AuthStep.WaitPhone)
    }

    override fun submitPhone(phone: String) {
        sink?.onAuthStep(AuthStep.WaitCode)
    }

    override fun submitCode(code: String) {
        val s = sink ?: return
        if (code != acceptedCode) {
            s.onError("The code was not accepted. Check the Telegram app and try again.")
            return
        }
        if (requirePassword) {
            s.onAuthStep(AuthStep.WaitPassword(hint = "your two-step password"))
        } else {
            s.onAuthStep(AuthStep.Ready)
            s.onConnectionChanged(true)
        }
    }

    override fun submitPassword(password: String) {
        val s = sink ?: return
        s.onAuthStep(AuthStep.Ready)
        s.onConnectionChanged(true)
    }

    override fun sendPing(nonce: String) {
        pingsSent += nonce
        if (deliverAcks) sink?.onPingAck(nonce)
    }

    override fun stop() {
        sink?.onAuthStep(AuthStep.Closed)
        sink = null
        started = false
    }

    /** Simulates the network dropping while the account is still authorized. */
    fun simulateDrop() {
        sink?.onConnectionChanged(false)
    }

    /** Simulates the network returning. */
    fun simulateRestore() {
        sink?.onConnectionChanged(true)
    }
}
