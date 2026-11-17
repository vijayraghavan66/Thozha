package com.jarvis.companion.link

import android.content.Context
import com.jarvis.link.LinkRuntime
import com.jarvis.link.LinkStateStore
import java.io.File

/**
 * Non-secret Link state (tenant, pinned host root, certificates, enrollment status) in app-private
 * preferences. Private keys are never stored here; they live in Android Keystore.
 */
class SharedPrefsLinkStateStore(context: Context) : LinkStateStore {
    private val prefs = context.getSharedPreferences("jarvis_link_state", Context.MODE_PRIVATE)
    override fun read(): String? = prefs.getString("json", null)
    override fun write(json: String) { prefs.edit().putString("json", json).apply() }
    fun clear() { prefs.edit().clear().apply() }
}

/** Explicit connection mode. The app never switches mode on its own. */
enum class LinkMode(val wire: String) {
    LOCAL_USB("local_usb"),
    TEST_RELAY("test_relay"),
    ;

    companion object {
        fun fromWire(value: String?): LinkMode = entries.firstOrNull { it.wire == value } ?: LOCAL_USB
    }
}

class LinkSettings(context: Context) {
    private val prefs = context.getSharedPreferences("jarvis_link_settings", Context.MODE_PRIVATE)
    var mode: LinkMode
        get() = LinkMode.fromWire(prefs.getString("mode", null))
        set(value) { prefs.edit().putString("mode", value.wire).apply() }
    var relayUrl: String
        get() = prefs.getString("relay_url", DEFAULT_TEST_RELAY_URL) ?: DEFAULT_TEST_RELAY_URL
        set(value) { prefs.edit().putString("relay_url", value.trim()).apply() }

    /** The FCM token last accepted by the relay. Only a hash is needed to avoid re-registering the same value. */
    var lastRegisteredPushToken: String
        get() = prefs.getString("last_push_token_sha256", "") ?: ""
        set(value) { prefs.edit().putString("last_push_token_sha256", java.security.MessageDigest.getInstance("SHA-256").digest(value.toByteArray()).joinToString("") { "%02x".format(it) }).apply() }

    fun pushTokenAlreadyRegistered(token: String): Boolean =
        lastRegisteredPushToken == java.security.MessageDigest.getInstance("SHA-256").digest(token.toByteArray()).joinToString("") { "%02x".format(it) }

    /** True only the first time it is called: the notification permission dialog is shown once, not repeatedly. */
    fun markNotificationsAskedOnce(): Boolean {
        val asked = prefs.getBoolean("notifications_asked", false)
        if (!asked) prefs.edit().putBoolean("notifications_asked", true).apply()
        return !asked
    }

    companion object {
        /** Reached through `adb reverse tcp:8787 tcp:8787` to the Mac's loopback relay. Test relay only. */
        const val DEFAULT_TEST_RELAY_URL = "http://127.0.0.1:8787"
    }
}

/** Builds the runtime with Keystore keys and app-private stores. The directory holds only ciphertext and IDs. */
object LinkService {
    fun runtime(context: Context): LinkRuntime = LinkRuntime(
        keys = LinkKeystoreKeys,
        state = SharedPrefsLinkStateStore(context),
        storeDir = File(context.filesDir, "jarvis_link").apply { mkdirs() },
        // Telegram carries the same signed requests when it is connected; otherwise the USB/HTTPS path is used.
        transportFactory = { _ -> com.jarvis.companion.telegram.TelegramSession.activeTunnel() },
    )
}
