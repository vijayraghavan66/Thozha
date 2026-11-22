package com.jarvis.companion.notifications

import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.jarvis.companion.link.LinkBackgroundSync
import com.jarvis.companion.link.LinkAlerts
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.link.LinkMode
import com.jarvis.companion.link.LinkService
import com.jarvis.companion.data.JarvisApi
import com.jarvis.companion.data.SecureStore

/**
 * FCM carries only an opaque approval ID or a Link wake-up. The app fetches each approval through its
 * authenticated Mac connection before displaying a notification; push payloads never authorize actions.
 */
class JarvisMessagingService : FirebaseMessagingService() {

    override fun onNewToken(token: String) {
        super.onNewToken(token)
        // Token rotation: register the new token through the current authenticated transport.
        PushRegistration.register(applicationContext, token)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        super.onMessageReceived(message)
        val data = message.data
        if (data == mapOf("type" to WAKE_TYPE)) {
            if (LinkSettings(this).mode == LinkMode.TEST_RELAY) {
                Thread { runCatching { LinkBackgroundSync.run(applicationContext) } }.start()
            }
            return
        }
        if (data.keys != setOf("approval_id", "type") || data["type"] != APPROVAL_TYPE) return
        val approvalId = data["approval_id"] ?: return
        if (!approvalId.matches(Regex("[0-9a-f]{32}"))) return
        runCatching {
            val approval = JarvisApi(this, SecureStore(this)).getApproval(
                approvalId,
                connectTimeoutMs = 5_000,
                readTimeoutMs = 8_000,
            )
            LinkAlerts.notifySupervisorApproval(applicationContext, approval)
        }.onFailure { failure ->
            android.util.Log.w("JarvisFcm", "Authenticated approval fetch failed: ${failure::class.java.simpleName}")
        }
    }

    companion object {
        const val WAKE_TYPE = "link_wakeup"
        const val APPROVAL_TYPE = "approval_pending"
    }
}

/** Registers the device's FCM token once per distinct value. Failures are retried on the next app open. */
object PushRegistration {
    fun requestTokenAndRegister(context: android.content.Context) {
        val messaging = FirebaseMessaging.getInstance()
        messaging.isAutoInitEnabled = true
        messaging.token.addOnSuccessListener { token ->
            if (!LinkSettings(context).pushTokenAlreadyRegistered(token)) register(context, token)
        }
    }

    fun register(context: android.content.Context, token: String) {
        val settings = LinkSettings(context)
        if (token.isBlank()) return
        Thread {
            val secure = SecureStore(context)
            if (!secure.isFcmTokenRegistered(token)) {
                runCatching {
                    JarvisApi(context, secure).registerFcmToken(token)
                    secure.markFcmTokenRegistered(token)
                }
            }
            if (settings.mode == LinkMode.TEST_RELAY && !settings.pushTokenAlreadyRegistered(token)) {
                runCatching {
                    if (LinkService.runtime(context).registerPushToken(settings.relayUrl, token)) {
                        settings.lastRegisteredPushToken = token
                    }
                }
            }
        }.start()
    }
}
