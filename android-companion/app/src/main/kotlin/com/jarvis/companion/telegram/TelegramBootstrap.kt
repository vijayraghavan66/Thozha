package com.jarvis.companion.telegram

import android.content.Context
import android.os.SystemClock
import java.util.concurrent.Executors

/**
 * Starts the process-wide Telegram session once, when the app opens and whenever credentials exist.
 * Restores the existing TDLib session from private storage; it never asks for a login again and never resets.
 */
object TelegramBootstrap {
    private val worker = Executors.newSingleThreadExecutor { r -> Thread(r, "jarvis-telegram-start") }
    private val lock = Any()
    private const val PREFS = "jarvis_remote_transport"
    private const val KEY_TELEGRAM_ENABLED = "telegram_remote_enabled"

    /** Existing authenticated sessions migrate as opted-in; a fresh installation defaults to local-only. */
    fun isRemoteEnabled(context: Context): Boolean {
        val app = context.applicationContext
        val prefs = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val stored = if (prefs.contains(KEY_TELEGRAM_ENABLED)) prefs.getBoolean(KEY_TELEGRAM_ENABLED, false) else null
        val configuredBeforeMigration = TelegramCredentialStore(app).load() != null
        val enabled = TelegramRemoteModePolicy.resolve(stored, configuredBeforeMigration)
        if (stored == null) prefs.edit().putBoolean(KEY_TELEGRAM_ENABLED, enabled).apply()
        return enabled
    }

    /** Enable keeps the existing TDLib database/session. Disable stops transport without logging out or clearing it. */
    fun setRemoteEnabled(context: Context, enabled: Boolean) {
        val app = context.applicationContext
        app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putBoolean(KEY_TELEGRAM_ENABLED, enabled)
            .apply()
        if (enabled) {
            ensureRunning(app)
            return
        }
        val old = synchronized(lock) {
            TelegramSession.tunnelReady = false
            TelegramSession.frameSender = null
            TelegramSession.controller.also { TelegramSession.controller = null }
        }
        if (old != null) worker.execute { old.stop() }
    }

    /** Returns true when a session is (or has just been) started. Returns false when no credentials are stored. */
    fun ensureRunning(context: Context): Boolean {
        val app = context.applicationContext
        val store = TelegramCredentialStore(app)
        val creds = store.load() ?: return false
        if (!isRemoteEnabled(app)) return false
        synchronized(lock) {
            if (TelegramSession.controller != null) return true
            val controller = TelegramController(
                transport = TelegramSession.newTransport(app.filesDir, store.databaseKey()),
                clock = { SystemClock.elapsedRealtime() },
                nonceSource = { TelegramSession.newNonce() },
                onChange = { snap -> TelegramSession.listener?.invoke(snap) },
            )
            TelegramSession.controller = controller
            worker.execute { controller.start(creds) }  // TDLib client creation stays off the UI thread
        }
        return true
    }
}

internal object TelegramRemoteModePolicy {
    /** Keep an already authenticated user's prior choice; new installs default to local-only. */
    fun resolve(storedPreference: Boolean?, previouslyConfigured: Boolean): Boolean =
        storedPreference ?: previouslyConfigured
}
