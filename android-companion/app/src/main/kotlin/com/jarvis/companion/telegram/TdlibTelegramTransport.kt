package com.jarvis.companion.telegram

import org.drinkless.tdlib.Client
import org.drinkless.tdlib.TdApi
import java.io.File

/**
 * Real Telegram transport on the official TDLib client (org.drinkless.tdlib, native libtdjni, arm64-v8a).
 *
 * Carries only the harmless nonce ping: sends "JARVIS-PING <nonce>" to the Jarvis bot and accepts only the
 * exact "JARVIS-ACK <nonce>" reply from the bot's private chat. Telegram is untrusted; this does not grant
 * any capability. Database and session files live in this app's private storage.
 */
class TdlibTelegramTransport(
    private val filesDir: File,
    private val databaseKey: ByteArray,
    private val botUsername: String = BOT_USERNAME,
) : TelegramTransport {
    @Volatile private var sink: TelegramEvents? = null
    @Volatile private var credentials: ApiCredentials? = null
    @Volatile private var client: Client? = null
    @Volatile private var botChatId: Long = 0L
    @Volatile private var online: Boolean = false
    private val tunnelSendGate = TelegramSendRateGate()

    override fun start(credentials: ApiCredentials, sink: TelegramEvents) {
        this.credentials = credentials
        this.sink = sink
        TelegramSession.frameSender = { text -> sendText(text) }
        val handler = Client.ResultHandler { update -> onUpdate(update) }
        val exceptions = Client.ExceptionHandler { error -> sink.onError("TDLib: ${error.message ?: "error"}") }
        client = Client.create(handler, exceptions, exceptions)
    }

    override fun submitPhone(phone: String) {
        val request = TdApi.SetAuthenticationPhoneNumber().apply { phoneNumber = phone }
        request(request)
    }

    override fun submitCode(code: String) {
        request(TdApi.CheckAuthenticationCode().apply { this.code = code })
    }

    override fun submitPassword(password: String) {
        request(TdApi.CheckAuthenticationPassword().apply { this.password = password })
    }

    override fun sendPing(nonce: String) {
        val chatId = botChatId
        if (chatId == 0L) {
            sink?.onError("Bot chat is not ready yet")
            return
        }
        val content = TdApi.InputMessageText().apply {
            text = TdApi.FormattedText("${TelegramStatus.PING_PREFIX}$nonce", arrayOf())
            clearDraft = false
        }
        val send = TdApi.SendMessage().apply {
            this.chatId = chatId
            inputMessageContent = content
        }
        request(send)
    }

    override fun stop() {
        TelegramSession.frameSender = null
        TelegramSession.tunnelReady = false
        client?.send(TdApi.Close()) { }
        client = null
        sink?.onAuthStep(AuthStep.Closed)
        sink = null
        botChatId = 0L
        online = false
    }

    /** Sends one text message to the Jarvis bot chat. Used for Jarvis Link tunnel frames. */
    fun sendText(text: String) {
        val chatId = botChatId
        if (chatId == 0L) throw java.io.IOException("Bot chat is not ready")
        // Home submission and background inbox sync use separate executors; pace their shared bot chat.
        tunnelSendGate.awaitSlot()
        val trace = runCatching {
            val frame = com.jarvis.link.LinkJson.parseObject(text.removePrefix(TunnelFrames.PREFIX))
            val id = frame["id"] as? String ?: ""
            java.security.MessageDigest.getInstance("SHA-256").digest(id.toByteArray(Charsets.UTF_8))
                .take(6).joinToString("") { "%02x".format(it.toInt() and 0xff) }
        }.getOrDefault("unknown")
        val started = System.nanoTime()
        android.util.Log.i(TAG, "tdlib_send_start trace=$trace")
        val content = TdApi.InputMessageText().apply {
            this.text = TdApi.FormattedText(text, arrayOf())
            clearDraft = false
        }
        val c = client ?: throw java.io.IOException("Telegram client is not ready")
        c.send(TdApi.SendMessage().apply {
            this.chatId = chatId
            inputMessageContent = content
        }) { result ->
            val elapsed = java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started)
            if (result is TdApi.Error) android.util.Log.w(TAG, "tdlib_send_error trace=$trace kind=${result.code} elapsed_ms=$elapsed")
            else android.util.Log.i(TAG, "tdlib_send_ack trace=$trace elapsed_ms=$elapsed")
        }
    }

    // --- updates -------------------------------------------------------------------------

    private fun onUpdate(update: TdApi.Object) {
        when (update) {
            is TdApi.UpdateAuthorizationState -> onAuthorization(update.authorizationState)
            is TdApi.UpdateConnectionState -> {
                android.util.Log.i(TAG, "conn=${update.state.javaClass.simpleName}")
                online = update.state is TdApi.ConnectionStateReady
                TelegramSession.tunnelReady = online && botChatId != 0L
                sink?.onConnectionChanged(online)
            }
            is TdApi.UpdateMessageSendSucceeded -> logTunnelSendState(update.message, "sent", 0)
            is TdApi.UpdateMessageSendFailed -> {
                if (update.error.code == 429) {
                    val seconds = Regex("(?:FLOOD_WAIT_|retry after )(\\d+)", RegexOption.IGNORE_CASE)
                        .find(update.error.message ?: "")?.groupValues?.getOrNull(1)?.toLongOrNull()
                        ?.coerceIn(1L, 300L) ?: 5L
                    tunnelSendGate.deferForNanos(java.util.concurrent.TimeUnit.SECONDS.toNanos(seconds))
                    android.util.Log.w(TAG, "telegram_rate_limited retry_after_s=$seconds")
                }
                logTunnelSendState(update.message, "failed", update.error.code)
            }
            is TdApi.UpdateNewMessage -> onMessage(update.message)
            else -> Unit
        }
    }

    private fun onAuthorization(state: TdApi.AuthorizationState) {
        val s = sink ?: return
        // State names only: no phone numbers, user ids, or key material are ever logged.
        android.util.Log.i(TAG, "auth=${state.javaClass.simpleName}")
        when (state) {
            is TdApi.AuthorizationStateWaitTdlibParameters -> sendParameters()
            is TdApi.AuthorizationStateWaitPhoneNumber -> s.onAuthStep(AuthStep.WaitPhone)
            is TdApi.AuthorizationStateWaitCode -> s.onAuthStep(AuthStep.WaitCode)
            is TdApi.AuthorizationStateWaitPassword -> s.onAuthStep(AuthStep.WaitPassword(state.passwordHint))
            is TdApi.AuthorizationStateReady -> {
                s.onAuthStep(AuthStep.Ready)
                resolveBotChat()
            }
            is TdApi.AuthorizationStateClosed, is TdApi.AuthorizationStateClosing -> s.onAuthStep(AuthStep.Closed)
            else -> s.onError("Telegram requires an unsupported login step")
        }
    }

    private fun sendParameters() {
        val creds = credentials ?: return
        val directory = File(filesDir, "tdlib").apply { mkdirs() }
        val parameters = TdApi.SetTdlibParameters().apply {
            useTestDc = false
            databaseDirectory = File(directory, "db").path
            filesDirectory = File(directory, "files").path
            databaseEncryptionKey = databaseKey
            useFileDatabase = true
            useChatInfoDatabase = true
            useMessageDatabase = true
            useSecretChats = false
            apiId = creds.apiId
            apiHash = creds.apiHash
            systemLanguageCode = "en"
            deviceModel = "Android"
            systemVersion = android.os.Build.VERSION.RELEASE
            applicationVersion = "0.1.0"
        }
        request(parameters)
    }

    /** Finds the Jarvis bot's private chat by its public username. The username is not a secret. */
    private fun resolveBotChat() {
        val search = TdApi.SearchPublicChat().apply { username = botUsername }
        client?.send(search) { result ->
            when (result) {
                is TdApi.Chat -> {
                    botChatId = result.id
                    TelegramSession.tunnelReady = online
                    android.util.Log.i(TAG, "bot chat resolved; online=$online tunnelReady=${TelegramSession.tunnelReady}")
                }
                is TdApi.Error -> {
                    android.util.Log.w(TAG, "bot chat lookup failed: code=${result.code}")
                    sink?.onError("Could not find the Jarvis bot: ${result.message}")
                }
                else -> Unit
            }
        }
    }

    private fun onMessage(message: TdApi.Message) {
        if (message.isOutgoing || message.chatId != botChatId || botChatId == 0L) return
        val content = message.content as? TdApi.MessageText ?: return
        val text = content.text.text
        if (text.startsWith(TunnelFrames.PREFIX)) {
            TelegramSession.tunnel.onText(text)  // Jarvis Link relay response from the Mac
            return
        }
        val match = ACK_RE.matchEntire(text) ?: return
        sink?.onPingAck(match.groupValues[1])
    }

    /** TDLib's SendMessage callback only means the request entered TDLib; this update is server-side send completion. */
    private fun logTunnelSendState(message: TdApi.Message?, outcome: String, errorCode: Int) {
        if (message == null || message.chatId != botChatId || botChatId == 0L) return
        val content = message.content as? TdApi.MessageText ?: return
        val text = content.text.text
        if (!text.startsWith(TunnelFrames.PREFIX)) return
        val trace = runCatching {
            val frame = com.jarvis.link.LinkJson.parseObject(text.removePrefix(TunnelFrames.PREFIX))
            val id = frame["id"] as? String ?: ""
            java.security.MessageDigest.getInstance("SHA-256").digest(id.toByteArray(Charsets.UTF_8))
                .take(6).joinToString("") { "%02x".format(it.toInt() and 0xff) }
        }.getOrDefault("unknown")
        val serverDateMs = message.date.toLong() * 1000L
        if (outcome == "sent") android.util.Log.i(TAG, "tdlib_server_sent trace=$trace server_date_ms=$serverDateMs")
        else android.util.Log.w(TAG, "tdlib_server_send_failed trace=$trace code=$errorCode server_date_ms=$serverDateMs")
    }

    // --- helpers ----------------------------------------------------------------------------

    private fun request(function: TdApi.Function<*>) {
        val c = client ?: return
        c.send(function) { result ->
            if (result is TdApi.Error) sink?.onError("Telegram: ${result.message}")
        }
    }

    companion object {
        const val BOT_USERNAME = "JarvisMac0w0507bot"
        const val TAG = "JarvisTelegram"
        private val ACK_RE = Regex("${TelegramStatus.ACK_PREFIX.trim()} ([A-Za-z0-9_-]{8,64})")
    }
}
