package com.jarvis.companion.voice

import android.content.Context
import com.jarvis.companion.data.SecureStore
import java.net.URI

data class VoiceSessionEndpoint(val url: String, val transport: String)

fun interface VoiceSessionEndpointResolver {
    fun resolve(): VoiceSessionEndpoint
}

/** Current USB adapter. Future LAN adapters can provide a TLS-pinned WSS endpoint without changing PCM/session code. */
class PairedLoopbackVoiceEndpointResolver(context: Context) : VoiceSessionEndpointResolver {
    private val secure = SecureStore(context.applicationContext)

    override fun resolve(): VoiceSessionEndpoint {
        val base = runCatching { URI(secure.baseUrl()) }.getOrNull()
            ?: throw IllegalStateException("The paired Mac loopback URL is unavailable.")
        val host = base.host?.lowercase().orEmpty()
        if (base.scheme != "http" || host !in setOf("127.0.0.1", "localhost") ||
            base.userInfo != null || base.query != null || base.fragment != null ||
            (!base.path.isNullOrBlank() && base.path != "/")
        ) {
            throw IllegalStateException("The active voice adapter accepts only paired USB loopback in this build.")
        }
        return VoiceSessionEndpoint("ws://$host:8766/v1/voice/session", "usb-reverse")
    }
}
