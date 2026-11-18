package com.jarvis.companion

import android.Manifest
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import com.jarvis.companion.data.JarvisApi
import com.jarvis.companion.data.SecureStore
import com.jarvis.companion.link.LinkAlerts
import com.jarvis.companion.link.LinkBackgroundSync
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.link.LinkSyncJob
import com.jarvis.companion.telegram.TelegramBootstrap
import com.jarvis.companion.telegram.TelegramSession
import com.jarvis.companion.telegram.TelegramSnapshot
import com.jarvis.companion.ui.JarvisApp
import com.jarvis.companion.ui.JarvisStore

/**
 * Jarvis 3.0 host activity. The interface is native Compose (see the ui package). Link, Telegram and pairing
 * behaviour is unchanged: this activity only starts the same services and forwards their state to the store.
 */
class MainActivity : ComponentActivity() {
    private var store: JarvisStore? = null
    private var permissionInFlight: String? = null
    @Volatile private var pinning = false
    private val telegramListener: (TelegramSnapshot) -> Unit = { snapshot -> store?.onTelegramSnapshot(snapshot) }

    private val permissionLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val requested = permissionInFlight
        permissionInFlight = null
        if (requested == Manifest.permission.RECORD_AUDIO) {
            store?.notice = if (granted) {
                "Microphone ready. Hold the mic button to dictate; review the words before sending."
            } else {
                "Microphone permission wasn't granted. You can still type your request."
            }
        }
        store?.refreshLocal()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        LinkAlerts.ensureChannels(this)
        // Restores the existing Telegram session in the background (no login prompts; never resets).
        TelegramBootstrap.ensureRunning(this)
        val jarvis = JarvisStore(applicationContext)
        jarvis.permissionRequester = { permission -> requestPermission(permission) }
        store = jarvis
        setContent { JarvisApp(jarvis) }
        askForNotificationsOnce()
    }

    override fun onResume() {
        super.onResume()
        TelegramSession.listener = telegramListener
        TelegramSession.controller?.let { telegramListener(it.snapshot()) }
        LinkSyncJob.ensureScheduled(applicationContext)
        pinLocalGatewayIfNeeded()
        store?.refreshLocal()
        if (LinkBackgroundSync.canRun(applicationContext)) store?.syncNow()
    }

    override fun onPause() {
        if (TelegramSession.listener === telegramListener) TelegramSession.listener = null
        super.onPause()
    }

    override fun onDestroy() {
        store?.destroy()
        store = null
        super.onDestroy()
    }

    /** Asks for notification permission once, on the first launch; later it is requested from Settings on tap. */
    private fun askForNotificationsOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || LinkAlerts.canPost(this)) return
        if (LinkSettings(this).markNotificationsAskedOnce()) {
            requestPermission(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun requestPermission(permission: String) {
        permissionInFlight = permission
        permissionLauncher.launch(permission)
    }

    /** Verifies the Mac's Wi-Fi identity once over the already paired route. Failures stay silent, as before. */
    private fun pinLocalGatewayIfNeeded() {
        val secure = SecureStore(this)
        if (pinning || secure.token().isNullOrBlank() || secure.localGatewayPin() != null) return
        pinning = true
        Thread {
            val verified = runCatching { JarvisApi(applicationContext, secure).pinLocalGatewayIdentity() }.isSuccess
            pinning = false
            if (verified) {
                runOnUiThread { Toast.makeText(this, "Mac Wi-Fi identity verified.", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }
}
