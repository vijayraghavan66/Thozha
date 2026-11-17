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

    /** Returns true when a session is (or has just been) started. Returns false when no credentials are stored. */
    fun ensureRunning(context: Context): Boolean {
        val app = context.applicationContext
        val store = TelegramCredentialStore(app)
        val creds = store.load() ?: return false
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
