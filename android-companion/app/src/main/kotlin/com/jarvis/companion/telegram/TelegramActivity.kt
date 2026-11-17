package com.jarvis.companion.telegram

import android.app.Activity
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.content.ContextCompat
import com.jarvis.companion.R

/** Telegram connection and login screen, reachable from Settings → Advanced. */
class TelegramActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private lateinit var store: TelegramCredentialStore
    private lateinit var page: LinearLayout
    private var snapshot: TelegramSnapshot? = null
    private var credentials: ApiCredentials? = null
    private var formNotice: String? = null

    private val apiIdField by lazy { field("API ID (number)", InputType.TYPE_CLASS_NUMBER) }
    private val apiHashField by lazy { field("API hash (32 characters)", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD) }
    private val phoneField by lazy { field("Phone number, international format", InputType.TYPE_CLASS_PHONE) }
    private val codeField by lazy { field("Login code", InputType.TYPE_CLASS_NUMBER) }
    private val passwordField by lazy { field("Two-step verification password", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD) }

    private val pollExpiry = object : Runnable {
        override fun run() {
            TelegramSession.controller?.expirePings(PING_TIMEOUT_MS)
            main.postDelayed(this, 1_000L)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = TelegramCredentialStore(this)
        credentials = store.load()
        page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(28))
        }
        val scroll = ScrollView(this).apply { addView(page, ViewGroup.LayoutParams(-1, -2)) }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(color(R.color.bg))
            addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))
        }
        // Edge-to-edge: respect the status bar, and let the keyboard inset replace the navigation-bar inset while open,
        // so the focused credential or code field is never hidden behind the keyboard.
        root.setOnApplyWindowInsetsListener { view, insets ->
            if (android.os.Build.VERSION.SDK_INT >= 30) {
                val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars())
                val keyboard = insets.getInsets(android.view.WindowInsets.Type.ime())
                val keyboardOpen = insets.isVisible(android.view.WindowInsets.Type.ime())
                view.setPadding(0, bars.top, 0, if (keyboardOpen) keyboard.bottom else bars.bottom)
            }
            insets
        }
        setContentView(root)
        if (credentials != null) ensureRunning()
        render()
    }

    override fun onResume() {
        super.onResume()
        // Rebind to the process-wide session and read its current state. Without this, a recreated screen
        // misses updates that already happened and stays at "Starting" (the bug this fixes).
        TelegramSession.listener = { snap -> main.post { snapshot = snap; render() } }
        TelegramSession.controller?.let { snapshot = it.snapshot() }
        render()
        main.post(pollExpiry)
        main.postDelayed(watchdog, WATCHDOG_MS)
    }

    override fun onPause() {
        super.onPause()
        main.removeCallbacks(pollExpiry)
        main.removeCallbacks(watchdog)
        TelegramSession.listener = null
    }

    // --- controller lifecycle --------------------------------------------------------

    private val worker = java.util.concurrent.Executors.newSingleThreadExecutor()
    private var startedAt = 0L
    private var diagnostic: String? = null

    /** Starts the process-wide session once. Client creation runs off the UI thread and is never repeated. */
    private fun ensureRunning() {
        if (TelegramSession.controller == null) startedAt = SystemClock.elapsedRealtime()
        TelegramBootstrap.ensureRunning(this)
    }

    /** Only used after a deliberate "Retry" from an error state. Never runs for a healthy session. */
    private fun restart() {
        TelegramSession.controller?.stop()
        TelegramSession.controller = null
        snapshot = null
        diagnostic = null
        ensureRunning()
        render()
    }

    /** Bounded diagnostic: reports a stalled start after 20 s. It never resets the session. */
    private val watchdog = Runnable {
        val snap = snapshot
        if (snap?.connection == TelegramConnection.CONNECTING || snap == null) {
            val waited = (SystemClock.elapsedRealtime() - startedAt) / 1000
            diagnostic = "TDLib has not reported an authorization state after ${waited}s. Diagnostics are in logcat (tag $TAG). The session has not been reset."
            Log.w(TAG, "startup stalled after ${waited}s; state=${snap?.auth}")
            render()
        }
    }

    // --- rendering ---------------------------------------------------------------------

    private fun render() {
        page.removeAllViews()
        page.addView(text("Telegram", 28, R.color.label, bold = true))
        page.addView(text(TelegramSession.TRANSPORT_LABEL, 13, R.color.warn, bold = true).apply { setPadding(0, dp(4), 0, dp(12)) })

        val snap = snapshot
        val connection = snap?.connection ?: if (credentials == null) TelegramConnection.NOT_CONFIGURED else TelegramConnection.CONNECTING
        page.addView(statusCard(connection, snap))
        diagnostic?.let { page.addView(text(it, 13, R.color.warn).apply { setPadding(0, 0, 0, dp(12)) }) }

        when {
            credentials == null || connection == TelegramConnection.NOT_CONFIGURED -> credentialsForm()
            connection == TelegramConnection.LOGIN_REQUIRED -> loginForm(snap?.auth)
            connection == TelegramConnection.CONNECTED -> pingSection(snap)
            connection == TelegramConnection.ERROR || connection == TelegramConnection.RECONNECTING -> {
                page.addView(button("Retry connection", R.color.accent) { restart() })
            }
            else -> page.addView(text("Starting…", 15, R.color.label_secondary))
        }

        if (credentials != null) {
            page.addView(text("Remove Telegram credentials and sign out of this app", 13, R.color.label_secondary).apply {
                setPadding(0, dp(28), 0, dp(6))
            })
            page.addView(button("Forget Telegram setup", R.color.alert) { forget() })
        }
    }

    private fun statusCard(connection: TelegramConnection, snap: TelegramSnapshot?): View {
        val (label, detail, tone) = when (connection) {
            TelegramConnection.NOT_CONFIGURED -> Triple("Not configured", "Enter your Telegram API ID and hash to begin.", R.color.label_secondary)
            TelegramConnection.LOGIN_REQUIRED -> Triple("Login required", "Sign in with your Telegram account.", R.color.warn)
            TelegramConnection.CONNECTING -> Triple("Connecting", "Starting the Telegram client.", R.color.label_secondary)
            TelegramConnection.CONNECTED -> Triple("Connected", "Messages can be exchanged with the Mac bot.", R.color.ok)
            TelegramConnection.RECONNECTING -> Triple("Reconnecting", "Telegram is reconnecting. Jarvis will retry.", R.color.warn)
            TelegramConnection.ERROR -> Triple("Error", snap?.error ?: "Telegram reported a problem.", R.color.alert)
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(14), dp(16), dp(14))
            setBackgroundColor(color(R.color.surface))
            addView(text(label, 20, tone, bold = true))
            addView(text(detail, 14, R.color.label_secondary).apply { setPadding(0, dp(4), 0, 0) })
            snap?.notice?.let { addView(text(it, 14, R.color.warn).apply { setPadding(0, dp(8), 0, 0) }) }
        }.also { it.layoutParams = LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(16) } }
    }

    private fun credentialsForm() {
        page.addView(text("Telegram API credentials", 17, R.color.label, bold = true).apply { setPadding(0, 0, 0, dp(6)) })
        page.addView(text("Create an application at my.telegram.org and enter its values here. They stay on this phone and are not shown again.", 14, R.color.label_secondary))
        page.addView(apiIdField)
        page.addView(apiHashField)
        formNotice?.let { page.addView(text(it, 14, R.color.alert).apply { setPadding(0, dp(8), 0, 0) }) }
        page.addView(button("Save credentials", R.color.accent) { saveCredentials() })
    }

    private fun loginForm(auth: AuthStep?) {
        val controller = TelegramSession.controller ?: return
        when (auth) {
            is AuthStep.WaitPassword -> {
                page.addView(text("Two-step verification", 17, R.color.label, bold = true))
                page.addView(text("Enter the password for your Telegram account.", 14, R.color.label_secondary))
                page.addView(passwordField)
                page.addView(button("Confirm password", R.color.accent) {
                    if (!controller.submitPassword(passwordField.text.toString())) passwordField.text.clear()
                    passwordField.text.clear()
                })
            }
            AuthStep.WaitCode -> {
                page.addView(text("Enter the code", 17, R.color.label, bold = true))
                page.addView(text("Telegram sent a login code to your app.", 14, R.color.label_secondary))
                page.addView(codeField)
                page.addView(button("Confirm code", R.color.accent) {
                    controller.submitCode(codeField.text.toString())
                })
            }
            else -> {
                page.addView(text("Sign in", 17, R.color.label, bold = true))
                page.addView(text("Use the phone number linked to your Telegram account.", 14, R.color.label_secondary))
                page.addView(phoneField)
                page.addView(button("Send login code", R.color.accent) {
                    controller.submitPhone(phoneField.text.toString())
                })
            }
        }
    }

    private fun pingSection(snap: TelegramSnapshot?) {
        val controller = TelegramSession.controller ?: return
        page.addView(button("Send test ping to Jarvis", R.color.accent) { controller.ping() })
        page.addView(text("Pending: ${snap?.pendingPings ?: 0}" + (snap?.lastRttMs?.let { "   Last round trip: $it ms" } ?: ""), 14, R.color.label_secondary).apply {
            setPadding(0, dp(12), 0, dp(8))
        })
        val results = controller.recentResults().takeLast(20).asReversed()
        if (results.isEmpty()) {
            page.addView(text("No pings yet.", 14, R.color.label_secondary))
        }
        results.forEach { result ->
            val line = when (result.outcome) {
                PingOutcome.ACK -> "ACK  ${result.nonce}   ${result.rttMs} ms"
                PingOutcome.TIMEOUT -> "TIMEOUT  ${result.nonce}"
            }
            page.addView(text(line, 14, if (result.outcome == PingOutcome.ACK) R.color.ok else R.color.warn).apply { setPadding(0, dp(4), 0, 0) })
        }
    }

    // --- actions ------------------------------------------------------------------

    private fun saveCredentials() {
        if (!store.save(apiIdField.text.toString(), apiHashField.text.toString())) {
            formNotice = "API ID must be a number and the API hash 32 hexadecimal characters."
            render()
            return
        }
        formNotice = null
        apiHashField.text.clear()
        credentials = store.load()
        restart()
    }

    private fun forget() {
        TelegramSession.controller?.stop()
        TelegramSession.controller = null
        store.clear()
        java.io.File(filesDir, "tdlib").deleteRecursively()  // removes the TDLib session and database
        credentials = null
        snapshot = null
        render()
    }

    // --- view helpers -------------------------------------------------------------------

    private fun field(hint: String, inputType: Int): EditText = EditText(this).apply {
        this.hint = hint
        this.inputType = inputType
        setSingleLine(true)
        setTextColor(color(R.color.label))
        setHintTextColor(color(R.color.label_secondary))
        setBackgroundColor(color(R.color.surface))
        setPadding(dp(14), dp(12), dp(14), dp(12))
        layoutParams = LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(10) }
    }

    private fun text(value: String, size: Int, colorRes: Int, bold: Boolean = false): TextView = TextView(this).apply {
        text = value
        textSize = size.toFloat()
        setTextColor(color(colorRes))
        if (bold) typeface = Typeface.DEFAULT_BOLD
    }

    private fun button(label: String, colorRes: Int, onClick: () -> Unit): Button = Button(this).apply {
        text = label
        isAllCaps = false
        setTextColor(color(R.color.on_accent))
        setBackgroundColor(color(colorRes))
        gravity = Gravity.CENTER
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(-1, dp(52)).apply { topMargin = dp(12) }
    }

    private fun color(resId: Int): Int = ContextCompat.getColor(this, resId)

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private companion object {
        const val PING_TIMEOUT_MS = 20_000L
        const val WATCHDOG_MS = 20_000L
        const val TAG = "JarvisTelegram"
    }
}
