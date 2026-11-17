package com.jarvis.companion

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.Editable
import android.text.TextWatcher
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.view.animation.DecelerateInterpolator
import com.jarvis.companion.link.LinkAlerts
import com.jarvis.companion.link.LinkBackgroundSync
import com.jarvis.companion.link.LinkKeystoreKeys
import com.jarvis.companion.link.LinkSyncJob
import com.jarvis.companion.link.LinkMode
import com.jarvis.companion.link.LinkService
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.presentation.AiEvidence
import com.jarvis.companion.presentation.ApprovalPresentation
import com.jarvis.companion.presentation.ConnectionPresentation
import com.jarvis.companion.presentation.ConversationPresentation
import com.jarvis.companion.presentation.TaskTone
import com.jarvis.companion.presentation.TimelinePresentation
import com.jarvis.companion.presentation.TransportState
import com.jarvis.companion.presentation.VoicePhase
import com.jarvis.companion.presentation.VoicePresentation
import com.jarvis.companion.presentation.views.WaveformView
import com.jarvis.companion.presentation.views.approvalCard
import com.jarvis.companion.presentation.views.conversationBubble
import com.jarvis.companion.presentation.views.emptyState
import com.jarvis.companion.presentation.views.timelineRow
import com.jarvis.link.SafeCapability
import com.jarvis.link.TaskSubmission
import com.jarvis.companion.notifications.PushRegistration
import com.jarvis.companion.data.JarvisApi
import com.jarvis.companion.data.SecureStore
import com.jarvis.companion.model.ApiException
import com.jarvis.companion.model.Approval
import com.jarvis.link.ApprovalReview
import com.jarvis.link.RelayHttpException
import java.text.DateFormat
import java.util.Date
import java.util.UUID
import java.util.concurrent.Executors

/**
 * Jarvis home. Three icon-only destinations (Home, Activity, Settings). Everything shown is real: the
 * conversation records what was sent and what the Mac replied; Activity lists the Mac's approval requests and
 * the requests sent from this phone. Technical setup lives under Settings > Advanced (AdvancedActivity).
 */
class MainActivity : Activity() {
    private enum class Destination { HOME, ACTIVITY, SETTINGS }

    private sealed interface Detail {
        data class Approval(val id: String) : Detail
        data class Task(val id: String) : Detail
        data object Privacy : Detail
        data object Live : Detail
    }

    // Jarvis Live is a visual foundation only. There is no voice backend yet, so pressing the control reports that.
    private var voicePhase = VoicePhase.IDLE
    private var voiceMessage: String? = null

    private val worker = Executors.newSingleThreadExecutor()
    private val taskWorker = Executors.newSingleThreadExecutor()
    private val controlWorker = Executors.newFixedThreadPool(2)
    private val main = Handler(Looper.getMainLooper())
    private val telegramStateListener: (com.jarvis.companion.telegram.TelegramSnapshot) -> Unit = { snapshot ->
        main.post {
            if (!destroyed) {
                if (snapshot.connection == com.jarvis.companion.telegram.TelegramConnection.CONNECTED &&
                    com.jarvis.companion.telegram.TelegramSession.activeTunnel() != null
                ) {
                    LinkSyncJob.ensureScheduled(applicationContext)
                    sync(minIntervalMs = 1_000L)
                }
                renderAll(animate = false)
            }
        }
    }
    private lateinit var root: FrameLayout
    private lateinit var content: FrameLayout
    private lateinit var navIndicator: View
    private val navButtons = mutableListOf<View>()
    private var destination = Destination.HOME
    private var detail: Detail? = null
    private var syncing = false
    private var lastSyncRequestMs = 0L
    private var composerDraft = ""
    private var composerSubmitting = false
    private var composerFocused = false
    private var homePresence: JarvisPresenceView? = null
    private var refreshingLocalTasks = false
    private var refreshingSupervisorApprovals = false
    private var taskRefreshScheduled = false
    private var supervisorApprovals: List<Approval> = emptyList()
    private var supervisorApprovalError = ""
    private val supervisorApprovalLoads = mutableSetOf<String>()
    private val supervisorApprovalDetails = mutableMapOf<String, Approval>()
    private val taskActionNotes = mutableMapOf<String, String>()
    private val taskActionBusy = mutableSetOf<String>()
    private var pinningLocalGatewayIdentity = false
    private var decisionNote = ""
    private var destroyed = false

    private val reducedMotion: Boolean by lazy {
        Settings.Global.getFloat(contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        LinkAlerts.ensureChannels(this)
        // Restores the existing Telegram session in the background (no login prompts; never resets).
        com.jarvis.companion.telegram.TelegramBootstrap.ensureRunning(this)
        buildShell()
        openFromNotification(intent)
        renderAll(animate = false)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        openFromNotification(intent)
        renderAll(animate = true)
    }

    override fun onResume() {
        super.onResume()
        com.jarvis.companion.telegram.TelegramSession.listener = telegramStateListener
        com.jarvis.companion.telegram.TelegramSession.controller?.let { telegramStateListener(it.snapshot()) }
        LinkSyncJob.ensureScheduled(applicationContext)
        pinLocalGatewayIdentityIfNeeded()
        registerPushIfNeeded()
        refreshSupervisorApprovals()
        refreshPendingLocalTasks()
        if (LinkBackgroundSync.canRun(applicationContext)) sync(minIntervalMs = 0L)
        renderAll(animate = false)
    }

    override fun onPause() {
        if (com.jarvis.companion.telegram.TelegramSession.listener === telegramStateListener) {
            com.jarvis.companion.telegram.TelegramSession.listener = null
        }
        super.onPause()
    }

    override fun onDestroy() {
        destroyed = true
        main.removeCallbacksAndMessages(null)
        worker.shutdownNow()
        taskWorker.shutdownNow()
        controlWorker.shutdownNow()
        super.onDestroy()
    }

    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (detail != null) {
            detail = null
            renderAll(animate = true)
        } else super.onBackPressed()
    }

    // ---- Shell and navigation ----------------------------------------------------------------------------

    private fun buildShell() {
        root = FrameLayout(this).apply { setBackgroundColor(color(R.color.bg)) }
        content = FrameLayout(this)
        root.addView(content, FrameLayout.LayoutParams(-1, -1))
        root.addView(buildNav(), FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL))
        // Edge-to-edge is the default at this target SDK, so adjustResize does not move the window for the keyboard.
        // The app must read the IME inset itself: pad the content above the keyboard, hide the navigation while the
        // keyboard is open, and keep the composer and the newest message on screen.
        root.setOnApplyWindowInsetsListener { _, insets ->
            val bars = if (Build.VERSION.SDK_INT >= 30) insets.getInsets(WindowInsets.Type.systemBars()) else null
            val barBottom = bars?.bottom ?: insets.systemWindowInsetBottom
            val (imeBottom, imeVisible) = imeState(insets)
            val wasVisible = keyboardVisible
            keyboardVisible = imeVisible
            content.setPadding(0, bars?.top ?: insets.systemWindowInsetTop, 0, if (imeVisible) imeBottom else 0)
            navContainer.visibility = if (imeVisible) View.GONE else View.VISIBLE
            (navContainer.layoutParams as FrameLayout.LayoutParams).bottomMargin = barBottom + dp(12)
            content.findViewWithTag<View>(COMPOSER_TAG)?.let { composer ->
                (composer.layoutParams as? ViewGroup.MarginLayoutParams)?.bottomMargin = if (imeVisible) dp(12) else dp(112)
                composer.requestLayout()
            }
            if (imeVisible && !wasVisible) {
                content.findViewWithTag<ScrollView>(CONVERSATION_TAG)?.let { scroll -> scroll.post { scroll.fullScroll(View.FOCUS_DOWN) } }
            }
            insets
        }
        setContentView(root)
    }

    private lateinit var navContainer: FrameLayout
    private var keyboardVisible = false

    /** Keyboard height and visibility from the IME inset. Older platforms have no IME inset, so report hidden. */
    private fun imeState(insets: WindowInsets): Pair<Int, Boolean> =
        if (Build.VERSION.SDK_INT >= 30) {
            insets.getInsets(WindowInsets.Type.ime()).bottom to insets.isVisible(WindowInsets.Type.ime())
        } else 0 to false

    private fun buildNav(): View {
        navContainer = FrameLayout(this).apply {
            layoutParams = FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL).also {
                it.bottomMargin = dp(12)
            }
        }
        // Flat pill: a shadow on a plain rounded background is cut off at its bounds, so depth comes from the surface tone.
        val pill = FrameLayout(this).apply {
            background = roundRect(color(R.color.surface), dp(32))
        }
        navIndicator = View(this).apply { background = roundRect(color(R.color.surface_strong), dp(28)) }
        pill.addView(navIndicator, FrameLayout.LayoutParams(dp(56), dp(56), Gravity.TOP or Gravity.START).also {
            it.topMargin = dp(8)
        })
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(8), dp(8), dp(8), dp(8))
        }
        val icons = listOf(
            Destination.HOME to R.drawable.ic_nav_home to "Home",
            Destination.ACTIVITY to R.drawable.ic_nav_activity to "Activity",
            Destination.SETTINGS to R.drawable.ic_nav_settings to "Settings",
        )
        icons.forEach { (pair, label) ->
            val (dest, icon) = pair
            val cell = FrameLayout(this).apply {
                layoutParams = LinearLayout.LayoutParams(dp(56), dp(56))
                isClickable = true
                contentDescription = label
                isFocusable = true
            }
            cell.addView(android.widget.ImageView(this).apply {
                setImageResource(icon)
                imageTintList = android.content.res.ColorStateList.valueOf(color(R.color.label_secondary))
                scaleType = android.widget.ImageView.ScaleType.CENTER
            }, FrameLayout.LayoutParams(dp(56), dp(56)))
            cell.setOnClickListener {
                haptic(HapticFeedbackConstants.CONTEXT_CLICK)
                if (destination != dest || detail != null) {
                    destination = dest
                    detail = null
                    renderAll(animate = true)
                }
            }
            navButtons.add(cell)
            row.addView(cell)
        }
        pill.addView(row, FrameLayout.LayoutParams(-2, -2))
        navContainer.addView(pill, FrameLayout.LayoutParams(-2, -2))
        return navContainer
    }

    /** Glides the selection indicator to the selected cell. Cells are measured after layout, so this posts. */
    private fun moveIndicator(animate: Boolean) {
        val index = Destination.values().indexOf(destination)
        val cell = navButtons.getOrNull(index) ?: return
        cell.post {
            if (destroyed) return@post
            val target = cell.left.toFloat()
            if (animate && !reducedMotion) {
                navIndicator.animate().x(target).setDuration(180).setInterpolator(DecelerateInterpolator(1.4f)).start()
            } else {
                navIndicator.x = target
            }
            navButtons.forEachIndexed { i, b ->
                ((b as FrameLayout).getChildAt(0) as android.widget.ImageView).imageTintList =
                    android.content.res.ColorStateList.valueOf(color(if (i == index) R.color.label else R.color.label_secondary))
            }
        }
    }

    // ---- Rendering ---------------------------------------------------------------------------------------

    private fun renderAll(animate: Boolean) {
        if (destroyed) return
        val rendered = when (val open = detail) {
            is Detail.Approval -> approvalDetail(open.id)
            is Detail.Task -> taskDetail(open.id)
            Detail.Privacy -> privacyDetail()
            Detail.Live -> liveScreen()
            null -> when (destination) {
                Destination.HOME -> homeScreen()
                Destination.ACTIVITY -> activityScreen()
                Destination.SETTINGS -> settingsScreen()
            }
        }
        val next = if (detail is Detail.Approval || detail is Detail.Task || detail is Detail.Live) {
            ScrollView(this).apply {
                isFillViewport = false
                addView(rendered, ViewGroup.LayoutParams(-1, -2))
            }
        } else rendered
        val previous = content.childCount
        content.addView(next, FrameLayout.LayoutParams(-1, -1))
        if (animate && !reducedMotion && previous > 0) {
            next.alpha = 0f
            next.translationX = dp(8).toFloat()
            next.animate().alpha(1f).translationX(0f).setDuration(180).setInterpolator(DecelerateInterpolator(1.3f)).start()
            for (i in 0 until previous) {
                val old = content.getChildAt(i)
                old.animate().alpha(0f).setDuration(140).withEndAction {
                    if (!destroyed) content.removeView(old)
                }.start()
            }
        } else {
            for (i in 0 until previous) content.removeViewAt(0)
        }
        if (previous >= 0) moveIndicator(animate)
    }

    // ---- Home --------------------------------------------------------------------------------------------

    private fun homeScreen(): View {
        val screen = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        screen.setBackgroundColor(color(R.color.bg))
        screen.addView(homeHeader(), linear(-1, -2).also { it.setMargins(dp(20), dp(12), dp(24), dp(8)) })

        val conversation = runCatching { LinkService.runtime(this).conversation() }.getOrDefault(emptyList())
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            overScrollMode = View.OVER_SCROLL_IF_CONTENT_SCROLLS
            setBackgroundColor(color(R.color.bg))
            tag = CONVERSATION_TAG
        }
        val list = LinearLayout(this).apply {
            setBackgroundColor(color(R.color.bg))
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), dp(24))
            gravity = if (conversation.isEmpty()) Gravity.CENTER else Gravity.NO_GRAVITY
        }
        if (conversation.isEmpty()) {
            list.addView(text("What can Jarvis do for you?", 26, color(R.color.label), bold = true).apply {
                gravity = Gravity.CENTER
                setPadding(dp(8), 0, dp(8), dp(8))
            })
            list.addView(text("Write a request below. Jarvis tells you here when it is working on it.", 15, color(R.color.label_secondary)).apply {
                gravity = Gravity.CENTER
            })
        } else {
            // Same records as before; the presentation layer only decides how each reply is shown.
            ConversationPresentation.items(conversation.takeLast(120)) { timeLabel(it) }
                .forEach { list.addView(conversationBubble(it)) }
        }
        scroll.addView(list, ViewGroup.LayoutParams(-1, -2))
        scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
        screen.addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))

        val approvals = runCatching { LinkService.runtime(this).pendingApprovals() }.getOrDefault(emptyList())
        val supervisorApproval = supervisorApprovals.firstOrNull()
        when {
            approvals.isNotEmpty() -> screen.addView(approvalBanner(approvals.first()), linear(-1, -2).also { it.setMargins(dp(20), 0, dp(20), dp(10)) })
            supervisorApproval != null -> screen.addView(approvalBanner(supervisorApproval), linear(-1, -2).also { it.setMargins(dp(20), 0, dp(20), dp(10)) })
        }
        // Clearance below the composer is the navigation bar's height only when that bar is on screen.
        val composerBottom = if (keyboardVisible) dp(12) else dp(112)
        screen.addView(composer().also { it.tag = COMPOSER_TAG }, linear(-1, -2).also { it.setMargins(dp(16), 0, dp(16), composerBottom) })
        return screen
    }

    private fun messageView(entry: Map<String, Any>): View {
        val wrap = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.END
            setPadding(0, dp(8), 0, dp(8))
        }
        val bubble = text(entry["text"] as? String ?: "", 17, color(R.color.label)).apply {
            background = roundRect(color(R.color.surface), dp(20))
            setPadding(dp(16), dp(12), dp(16), dp(12))
            maxWidth = (resources.displayMetrics.widthPixels * 0.78f).toInt()
        }
        wrap.addView(bubble, LinearLayout.LayoutParams(-2, -2).also { it.gravity = Gravity.END })
        val state = humanState(entry["state"] as? String ?: "SENT")
        val time = DateFormat.getTimeInstance(DateFormat.SHORT).format(Date((entry["created_at_unix_ms"] as? Long) ?: 0L))
        wrap.addView(text("${state.first} · $time", 13, state.second).apply { setPadding(dp(6), dp(4), dp(6), 0) })
        val summary = (entry["summary"] as? String).orEmpty()
        if (summary.isNotBlank()) {
            val answer = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                background = roundRect(color(R.color.surface), dp(20))
                setPadding(dp(16), dp(12), dp(16), dp(12))
                addView(text("JARVIS", 11, color(R.color.accent), bold = true))
                addView(text("", 16, color(R.color.label)).apply {
                    setText(AssistantTextFormatter.render(summary), TextView.BufferType.SPANNABLE)
                    setPadding(0, dp(5), 0, 0)
                    setTextIsSelectable(true)
                    setLineSpacing(dp(3).toFloat(), 1f)
                })
            }
            val answerWidth = minOf(
                (resources.displayMetrics.widthPixels * 0.88f).toInt(),
                resources.displayMetrics.widthPixels - dp(40),
            )
            wrap.addView(answer, LinearLayout.LayoutParams(answerWidth, -2).also {
                it.gravity = Gravity.START
                it.topMargin = dp(10)
            })
        }
        return wrap
    }

    private fun approvalBanner(review: ApprovalReview): View = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        background = roundRect(color(R.color.surface), dp(20))
        setPadding(dp(16), dp(14), dp(16), dp(14))
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            addView(text("Approval needed", 15, color(R.color.warn), bold = true))
            addView(text(review.operation.replace('_', ' '), 17, color(R.color.label)))
        }, LinearLayout.LayoutParams(0, -2, 1f))
        addView(text("›", 26, color(R.color.label_secondary)))
        pressable { decisionNote = ""; detail = Detail.Approval(review.approvalId); renderAll(animate = true) }
    }

    private fun approvalBanner(approval: Approval): View = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        background = roundRect(color(R.color.surface), dp(20))
        setPadding(dp(16), dp(14), dp(16), dp(14))
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            addView(text("Approval needed", 15, color(R.color.warn), bold = true))
            addView(text(approval.operation.replace('_', ' '), 17, color(R.color.label)))
            addView(text(approval.targetResource, 13, color(R.color.label_secondary), maxLines = 2))
        }, LinearLayout.LayoutParams(0, -2, 1f))
        addView(text("›", 26, color(R.color.label_secondary)))
        pressable { decisionNote = ""; detail = Detail.Approval(approval.id); renderAll(animate = true) }
    }

    private fun composer(): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.BOTTOM
        }
        val field = EditText(this).apply {
            hint = "Ask Jarvis"
            setHintTextColor(color(R.color.label_secondary))
            setTextColor(color(R.color.label))
            textSize = 17f
            background = roundRect(color(R.color.surface), dp(26))
            setPadding(dp(18), dp(14), dp(18), dp(14))
            minHeight = dp(52)
            setSingleLine(true)
            setText(composerDraft)
            isEnabled = !composerSubmitting
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            contentDescription = "Message to Jarvis"
            imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_SEND
            onFocusChangeListener = View.OnFocusChangeListener { _, focused ->
                composerFocused = focused
                homePresence?.state = presenceState()
            }
            setOnEditorActionListener { _, actionId, _ ->
                if (actionId == android.view.inputmethod.EditorInfo.IME_ACTION_SEND) { submitComposer(this); true } else false
            }
        }
        row.addView(field, LinearLayout.LayoutParams(0, -2, 1f).also { it.setMargins(0, 0, dp(10), 0) })
        val send = FrameLayout(this).apply {
            background = roundRect(color(R.color.accent), dp(26))
            layoutParams = LinearLayout.LayoutParams(dp(52), dp(52))
            contentDescription = "Send to Jarvis"
            isClickable = true
            isFocusable = true
            isEnabled = !composerSubmitting
            alpha = if (composerSubmitting) 0.75f else if (composerDraft.isBlank()) 0.35f else 1f
        }
        if (composerSubmitting) {
            send.addView(android.widget.ProgressBar(this).apply {
                isIndeterminate = true
                indeterminateTintList = android.content.res.ColorStateList.valueOf(color(R.color.bg))
            }, FrameLayout.LayoutParams(dp(24), dp(24), Gravity.CENTER))
        } else {
            send.addView(android.widget.ImageView(this).apply {
                setImageResource(R.drawable.ic_send)
                scaleType = android.widget.ImageView.ScaleType.CENTER
            }, FrameLayout.LayoutParams(dp(52), dp(52)))
        }
        field.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: Editable?) {
                composerDraft = s?.toString().orEmpty()
                send.alpha = if (composerDraft.isBlank()) 0.35f else 1f
            }
        })
        pressableView(send) { submitComposer(field) }
        row.addView(send)
        return row
    }

    private fun submitComposer(field: EditText) {
        if (composerSubmitting) return
        val text = composerDraft.trim()
        if (text.isEmpty()) return
        haptic(HapticFeedbackConstants.CONFIRM)
        val requestId = UUID.randomUUID().toString().replace("-", "")
        val runtime = LinkService.runtime(this)
        val requestTransport = if (com.jarvis.companion.telegram.TelegramSession.activeTunnel() != null) "telegram" else "link"
        runCatching { runtime.recordLocalTaskRequest(requestId, text, transport = requestTransport) }.onFailure {
            toast("Jarvis could not save this request on your phone.")
            return
        }
        composerDraft = ""
        composerFocused = false
        composerSubmitting = true
        field.setText("")
        (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).hideSoftInputFromWindow(field.windowToken, 0)
        renderAll(animate = true)
        taskWorker.execute {
            // Home sends through Jarvis Link: signed and encrypted. Telegram carries it when connected, otherwise the
            // configured relay path is used. The legacy bearer-token API is no longer used for Home requests.
            val outcome = runCatching {
                val relayUrl = LinkSettings(this).relayUrl
                runtime.submit(
                    relayUrl,
                    TaskSubmission(text, setOf(SafeCapability.ASSISTANT), clientRequestId = requestId),
                    null,
                )
            }
            main.post {
                if (destroyed) return@post
                composerSubmitting = false
                outcome.fold(
                    onSuccess = { delivery ->
                        if (delivery.messageId != requestId) {
                            runtime.recordLocalTaskResult(
                                requestId,
                                "",
                                "FAILED",
                                "Jarvis could not correlate this request safely. Please retry it.",
                            )
                        } else {
                            runtime.recordLocalTaskResult(requestId, delivery.messageId, if (delivery.queuedOffline) "QUEUED" else "SENT", "")
                            sync(minIntervalMs = 0L)
                        }
                    },
                    onFailure = { failure ->
                        android.util.Log.w("JarvisHome", "request not delivered: ${failure.javaClass.simpleName}")
                        val unresolved = failure is ApiException && failure.statusCode == null
                        val message = localTaskError(failure)
                        runtime.recordLocalTaskResult(requestId, "", if (unresolved) "RUNNING" else "FAILED", message)
                    },
                )
                renderAll(animate = true)
                if (runtime.conversation().any {
                        (it["state"] as? String) in setOf("CREATED", "RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED", "SENT", "QUEUED")
                    }
                ) scheduleTaskStatusRefresh(5_000L)
                if (outcome.isFailure) main.postDelayed({ refreshPendingLocalTasks() }, 2_000L)
            }
        }
    }

    /** Reconciles uncertain in-flight requests by the same idempotency key; it never resubmits work. */
    private fun refreshPendingLocalTasks() {
        if (refreshingLocalTasks || destroyed) return
        val runtime = LinkService.runtime(this)
        val entries = runtime.conversation()
        val linkPending = entries.any {
            it["transport"] in setOf("link", "telegram") &&
                (it["state"] as? String) in setOf("RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED", "SENT", "QUEUED")
        }
        if (linkPending) {
            sync(minIntervalMs = 0L)
            val apiPendingAlso = entries.any {
                it["transport"] !in setOf("link", "telegram") &&
                    (it["state"] as? String) in setOf("CREATED", "RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED")
            }
            if (!apiPendingAlso) return
        }
        val pending = entries.filter {
            it["transport"] !in setOf("link", "telegram") &&
                (it["state"] as? String) in setOf("CREATED", "RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED")
        }
        if (pending.isEmpty()) return
        refreshingLocalTasks = true
        var requestFailed = false
        var approvalDetected = false
        worker.execute {
            val api = JarvisApi(this, SecureStore(this))
            pending.forEach { item ->
                val key = item["message_id"] as? String ?: return@forEach
                runCatching { api.getLinkTaskStatus(key) }.onSuccess { result ->
                    runtime.recordLocalTaskResult(key, result.taskId, result.state, result.summary)
                    if (result.state == "WAITING_APPROVAL") approvalDetected = true
                }.onFailure { failure ->
                    requestFailed = true
                    if (failure is ApiException && failure.statusCode == 404) {
                        runtime.recordLocalTaskResult(
                            key,
                            "",
                            "FAILED",
                            "The Mac did not record this request. Check the connection before sending it again.",
                        )
                    }
                }
            }
            main.post {
                refreshingLocalTasks = false
                if (!destroyed) {
                    renderAll(animate = false)
                    if (approvalDetected) refreshSupervisorApprovals()
                    val active = runtime.conversation().any {
                        (it["state"] as? String) in setOf("CREATED", "RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED", "SENT", "QUEUED")
                    }
                    if (active) scheduleTaskStatusRefresh(if (requestFailed) 15_000L else 5_000L)
                }
            }
        }
    }

    private fun scheduleTaskStatusRefresh(delayMs: Long) {
        if (taskRefreshScheduled || destroyed) return
        taskRefreshScheduled = true
        main.postDelayed({
            taskRefreshScheduled = false
            refreshPendingLocalTasks()
        }, delayMs)
    }

    private fun localTaskError(failure: Throwable?): String = when (failure) {
        is ApiException -> when (failure.statusCode) {
            401 -> "The saved pairing could not authenticate. Check the Mac connection in Settings; pairing data was kept."
            403 -> "The Mac blocked this request because it is outside the enabled assistant scope."
            404 -> "The Mac did not record this request. Check the connection before sending it again."
            502 -> "Jarvis could not complete this request. Check the task details before trying again."
            503 -> "Jarvis on the Mac is not ready. Check that its Codex task runner is available."
            null -> "The result has not been confirmed yet. Jarvis may still be working; check this task before sending it again."
            else -> "Jarvis could not accept this request. Edit it and try again."
        }
        else -> "Jarvis could not complete this request. Check the task details before trying again."
    }

    /** Pins the gateway certificate only over the currently paired USB/loopback route. */
    private fun pinLocalGatewayIdentityIfNeeded() {
        if (pinningLocalGatewayIdentity || destroyed) return
        val secure = SecureStore(this)
        if (secure.token().isNullOrBlank() || secure.localGatewayPin() != null) return
        pinningLocalGatewayIdentity = true
        worker.execute {
            val outcome = runCatching { JarvisApi(this, secure).pinLocalGatewayIdentity() }
            main.post {
                pinningLocalGatewayIdentity = false
                if (outcome.isSuccess && !destroyed) toast("Mac Wi-Fi identity verified.")
            }
        }
    }

    private fun refreshSupervisorApprovals() {
        if (refreshingSupervisorApprovals || destroyed) return
        val secure = SecureStore(this)
        if (secure.token().isNullOrBlank() || secure.baseUrl().isBlank()) return
        refreshingSupervisorApprovals = true
        worker.execute {
            val outcome = runCatching { JarvisApi(this, secure).getApprovals() }
            main.post {
                refreshingSupervisorApprovals = false
                outcome.fold(
                    onSuccess = { approvals ->
                        supervisorApprovals = approvals
                        approvals.forEach { supervisorApprovalDetails[it.id] = it }
                        supervisorApprovalError = ""
                    },
                    onFailure = { failure -> supervisorApprovalError = supervisorApprovalErrorText(failure) },
                )
                if (!destroyed && (destination == Destination.HOME || destination == Destination.ACTIVITY || detail is Detail.Approval)) renderAll(animate = false)
            }
        }
    }

    private fun loadSupervisorApproval(approvalId: String) {
        if (!supervisorApprovalLoads.add(approvalId) || destroyed) return
        worker.execute {
            val outcome = runCatching { JarvisApi(this, SecureStore(this)).getApproval(approvalId) }
            main.post {
                supervisorApprovalLoads.remove(approvalId)
                outcome.onSuccess { approval ->
                    supervisorApprovalDetails[approval.id] = approval
                    supervisorApprovals = if (approval.status == "PENDING") {
                        (supervisorApprovals.filterNot { it.id == approval.id } + approval)
                    } else supervisorApprovals.filterNot { it.id == approval.id }
                    supervisorApprovalError = ""
                }.onFailure { failure -> supervisorApprovalError = supervisorApprovalErrorText(failure) }
                if (!destroyed && detail == Detail.Approval(approvalId)) renderAll(animate = false)
            }
        }
    }

    private fun decideSupervisorApproval(approval: Approval, decision: String) {
        if (decision !in setOf("approve", "reject") || approval.status != "PENDING") return
        haptic(if (decision == "approve") HapticFeedbackConstants.CONFIRM else HapticFeedbackConstants.REJECT)
        decisionNote = if (decision == "approve") "Sending your one-time approval…" else "Sending your rejection…"
        renderAll(animate = false)
        controlWorker.execute {
            val outcome = runCatching {
                val api = JarvisApi(this, SecureStore(this))
                val runtime = LinkService.runtime(this)
                val taskEntry = runtime.conversation().firstOrNull { it["task_id"] == approval.taskId }
                val continuation = if (decision == "approve") taskEntry?.get("text") as? String else null
                val receipt = api.decide(approval, decision, continuation)
                val latest = runCatching { api.getApproval(approval.id) }.getOrNull()
                val pending = api.getApprovals()
                val taskResult = taskEntry?.let { entry ->
                    val key = entry["message_id"] as? String
                    if (key.isNullOrBlank()) null else runCatching { api.getLinkTaskStatus(key) }.getOrNull()
                }
                Triple(receipt.optString("status", "RECORDED").uppercase(), Pair(latest, taskResult), pending)
            }
            main.post {
                if (destroyed) return@post
                outcome.fold(
                    onSuccess = { (status, refreshed, pending) ->
                        supervisorApprovals = pending
                        refreshed.first?.let { supervisorApprovalDetails[it.id] = it }
                        refreshed.second?.let { task ->
                            val entry = LinkService.runtime(this).conversation().firstOrNull { it["task_id"] == approval.taskId }
                            val key = entry?.get("message_id") as? String
                            if (!key.isNullOrBlank()) {
                                LinkService.runtime(this).recordLocalTaskResult(key, task.taskId, task.state, task.summary)
                            }
                        }
                        decisionNote = when {
                            decision == "reject" -> "Rejected. The Mac will not run this operation."
                            refreshed.second?.state == "WAITING_APPROVAL" -> "Approved once. The task is waiting for your next approval."
                            refreshed.second?.state == "COMPLETED" -> "Approved once. The exact operation ran and the task completed."
                            status == "CONSUMED" -> "Approved once. The Mac reports that the exact operation was executed."
                            else -> "Approved once. The Mac recorded your decision."
                        }
                    },
                    onFailure = { failure -> decisionNote = supervisorApprovalErrorText(failure) },
                )
                renderAll(animate = false)
            }
        }
    }

    private fun supervisorApprovalErrorText(failure: Throwable?): String = when (failure) {
        is ApiException -> when (failure.statusCode) {
            401 -> "The Mac could not authenticate this phone. Pairing data was kept; reconnect to the Mac and refresh."
            403 -> "The Mac refused this approval request. Review it again before deciding."
            404 -> "This approval is no longer available. Refresh Activity to check its current status."
            409 -> "This approval changed, expired, or was already used. Refresh Activity before trying again."
            else -> "Jarvis could not reach the approval service. Your decision was not confirmed."
        }
        else -> "Jarvis could not reach the approval service. Your decision was not confirmed."
    }

    private fun formatExpiry(value: String): String = runCatching {
        val instant = java.time.OffsetDateTime.parse(value).toInstant()
        DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(instant.toEpochMilli()))
    }.getOrDefault(value)

    // ---- Activity ----------------------------------------------------------------------------------------

    private fun activityScreen(): View {
        val scroll = ScrollView(this).apply { isFillViewport = true }
        val page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(12), dp(24), dp(120))
        }
        page.addView(header("Activity", null))
        val approvals = runCatching { LinkService.runtime(this).pendingApprovals() }.getOrDefault(emptyList())
        if (approvals.isNotEmpty() || supervisorApprovals.isNotEmpty()) {
            page.addView(section("Needs you"))
            approvals.forEach { review ->
                page.addView(row(
                    title = review.operation.replace('_', ' '),
                    subtitle = "${review.target} · expires ${time(review.expiresAtUnixMs)}",
                    accentColor = color(R.color.warn),
                ) { decisionNote = ""; detail = Detail.Approval(review.approvalId); renderAll(animate = true) })
            }
            supervisorApprovals.forEach { approval ->
                page.addView(row(
                    title = approval.operation.replace('_', ' '),
                    subtitle = "${approval.targetResource} · expires ${formatExpiry(approval.expiresAt)}",
                    accentColor = color(R.color.warn),
                ) { decisionNote = ""; detail = Detail.Approval(approval.id); renderAll(animate = true) })
            }
        }
        if (supervisorApprovalError.isNotBlank()) {
            page.addView(emptyNote(supervisorApprovalError))
        }
        val tasks = runCatching { LinkService.runtime(this).conversation() }.getOrDefault(emptyList()).reversed().take(50)
        val sections = TimelinePresentation.sections(tasks) { timeLabel(it) }
        if (sections.isEmpty()) {
            page.addView(emptyState(R.string.jarvis_no_tasks))
        } else {
            sections.forEach { group ->
                page.addView(section(group.title))
                group.rows.forEach { entry ->
                    page.addView(timelineRow(entry) { requestId -> confirmCancelTask(requestId) }.apply {
                        setOnClickListener {
                            detail = Detail.Task(entry.requestId)
                            renderAll(animate = true)
                        }
                    })
                }
            }
        }
        scroll.addView(page, ViewGroup.LayoutParams(-1, -2))
        return scroll
    }

    private fun approvalDetail(id: String): View {
        val review = runCatching { LinkService.runtime(this).pendingApprovals() }.getOrDefault(emptyList())
            .firstOrNull { it.approvalId == id }
        val page = detailPage("Approval")
        if (review != null) {
            // The card shows the exact operation, target and fingerprint, and offers decisions only while the request
            // is pending and unexpired. The decisions still go through decide(), so the Mac re-validates everything.
            val record = mapOf<String, Any?>(
                "approval_id" to review.approvalId,
                "operation" to review.operation,
                "target" to review.target,
                "canonical_arguments_digest" to review.canonicalArgumentsDigest,
                "status" to "PENDING",
                "expires_at_unix_ms" to review.expiresAtUnixMs,
            )
            ApprovalPresentation.card(record, System.currentTimeMillis()) { remaining -> remainingLabel(remaining) }?.let { card ->
                page.addView(
                    approvalCard(card, onApprove = { decide(review, "approve") }, onReject = { decide(review, "reject") }),
                    linear(-1, -2).also { it.setMargins(0, dp(8), 0, dp(16)) },
                )
            }
            page.addView(detailField("Summary", review.redactedSummary))
            if (decisionNote.isNotBlank()) page.addView(text(decisionNote, 15, color(R.color.label_secondary)).apply { setPadding(0, 0, 0, dp(16)) })
            return page
        }
        val approval = supervisorApprovalDetails[id] ?: supervisorApprovals.firstOrNull { it.id == id }
        if (approval == null) {
            page.addView(emptyNote(if (supervisorApprovalLoads.contains(id)) "Loading the exact approval from your Mac…" else "This approval is no longer pending. Refresh Activity to check its status."))
            if (!supervisorApprovalLoads.contains(id)) loadSupervisorApproval(id)
            return page
        }
        page.addView(text(approval.operation.replace('_', ' '), 30, color(R.color.label), bold = true))
        page.addView(detailField("Target", approval.targetResource))
        page.addView(detailField("Expires", formatExpiry(approval.expiresAt)))
        page.addView(detailField("Why", approval.reason.ifBlank { "Sensitive operation requires your approval." }))
        page.addView(detailField("Canonical arguments", approval.parametersSummary))
        page.addView(detailField("Operation digest", approval.operationDigest))
        if (approval.status != "PENDING") {
            page.addView(text("Status: ${approval.status.lowercase().replace('_', ' ')}", 16, color(R.color.label_secondary), bold = true).apply {
                setPadding(0, dp(8), 0, dp(16))
            })
            return page
        }
        page.addView(View(this), LinearLayout.LayoutParams(-1, dp(24)))
        if (decisionNote.isNotBlank()) page.addView(text(decisionNote, 15, color(R.color.label_secondary)).apply { setPadding(0, 0, 0, dp(16)) })
        page.addView(actionButton("Approve once", R.color.accent, R.color.on_accent, filled = true) { decideSupervisorApproval(approval, "approve") })
        page.addView(actionButton("Reject", R.color.alert, R.color.alert, filled = false) { decideSupervisorApproval(approval, "reject") })
        return page
    }

    private fun decide(review: ApprovalReview, decision: String) {
        haptic(if (decision == "approve") HapticFeedbackConstants.CONFIRM else HapticFeedbackConstants.REJECT)
        val relay = LinkSettings(this).relayUrl
        decisionNote = if (decision == "approve") "Sending your approval…" else "Sending your rejection…"
        renderAll(animate = false)
        worker.execute {
            val outcome = runCatching { LinkService.runtime(this).decideApproval(relay, review.approvalId, decision) }
            main.post {
                if (destroyed) return@post
                decisionNote = outcome.fold(
                    onSuccess = { "Sent. Jarvis will confirm once the Mac has checked it." },
                    onFailure = { friendly(it) },
                )
                renderAll(animate = false)
                main.postDelayed({ sync(minIntervalMs = 0L) }, 6_000L)
                main.postDelayed({ sync(minIntervalMs = 0L) }, 20_000L)
            }
        }
    }

    private fun taskDetail(messageId: String): View {
        val entry = runCatching { LinkService.runtime(this).conversation() }.getOrDefault(emptyList())
            .firstOrNull { it["message_id"] == messageId }
        val page = detailPage("Task")
        if (entry == null) {
            page.addView(emptyNote("This task is no longer in your history."))
            return page
        }
        val state = humanState(entry["state"] as? String ?: "SENT")
        page.addView(text(entry["text"] as? String ?: "", 26, color(R.color.label), bold = true))
        page.addView(text(state.first, 17, state.second, bold = true).apply { setPadding(0, dp(10), 0, dp(20)) })
        page.addView(detailField("Sent", time((entry["created_at_unix_ms"] as? Long) ?: 0L)))
        page.addView(detailField("Updated", time((entry["updated_at_unix_ms"] as? Long) ?: 0L)))
        page.addView(detailField("Mac task", (entry["task_id"] as? String)?.take(12) ?: "waiting for the Mac"))
        val summary = (entry["summary"] as? String).orEmpty()
        if (summary.isNotBlank()) {
            page.addView(text("Jarvis", 13, color(R.color.accent), bold = true).apply { setPadding(0, dp(16), 0, dp(6)) })
            page.addView(text("", 17, color(R.color.label)).apply {
                setText(AssistantTextFormatter.render(summary), TextView.BufferType.SPANNABLE)
                background = roundRect(color(R.color.surface), dp(18))
                setPadding(dp(16), dp(14), dp(16), dp(14))
                setTextIsSelectable(true)
                setLineSpacing(dp(3).toFloat(), 1f)
            })
        }
        val requestId = entry["message_id"] as? String
        taskActionNotes[requestId.orEmpty()]?.takeIf { it.isNotBlank() && it != summary }?.let { note ->
            page.addView(text(note, 15, color(R.color.label_secondary)).apply { setPadding(0, dp(14), 0, dp(14)) })
        }
        if (requestId != null && (entry["state"] as? String) in setOf(
                "CREATED", "RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED",
            )
        ) {
            val cancel = actionButton(
                if (requestId in taskActionBusy) "Cancelling…" else "Cancel task",
                R.color.alert,
                R.color.alert,
                filled = false,
            ) { confirmCancelTask(requestId) }
            if (requestId in taskActionBusy) {
                cancel.isEnabled = false
                cancel.alpha = 0.6f
            }
            page.addView(cancel)
        }
        return page
    }

    private fun confirmCancelTask(requestId: String) {
        AlertDialog.Builder(this)
            .setTitle("Cancel this task?")
            .setMessage("Jarvis will stop continuing this request and revoke any approval that has not been used. A Mac action already in progress may still finish.")
            .setNegativeButton("Keep running", null)
            .setPositiveButton("Cancel task") { _, _ -> cancelTask(requestId) }
            .show()
    }

    private fun cancelTask(requestId: String) {
        if (!taskActionBusy.add(requestId)) return
        haptic(HapticFeedbackConstants.REJECT)
        taskActionNotes[requestId] = "Cancelling on your Mac…"
        renderAll(animate = false)
        controlWorker.execute {
            val outcome = runCatching {
                val api = JarvisApi(this, SecureStore(this))
                val response = api.cancelLinkTask(requestId)
                val current = if (response.summary.isBlank()) api.getLinkTaskStatus(requestId) else response
                LinkService.runtime(this).recordLocalTaskResult(
                    requestId,
                    current.taskId,
                    current.state,
                    current.summary,
                )
                current
            }
            main.post {
                if (destroyed) return@post
                taskActionBusy.remove(requestId)
                outcome.fold(
                    onSuccess = { result ->
                        taskActionNotes.remove(requestId)
                        refreshSupervisorApprovals()
                    },
                    onFailure = { failure -> taskActionNotes[requestId] = localTaskError(failure) },
                )
                renderAll(animate = false)
            }
        }
    }

    // ---- Settings ----------------------------------------------------------------------------------------

    private fun settingsScreen(): View {
        val page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(12), dp(24), dp(120))
        }
        page.addView(header("Settings", null))
        page.addView(row("Mac connection", connectionWord(), accentColor = null) {
            if (Build.VERSION.SDK_INT >= 37 &&
                checkSelfPermission(Manifest.permission.ACCESS_LOCAL_NETWORK) != PackageManager.PERMISSION_GRANTED
            ) {
                requestPermissions(arrayOf(Manifest.permission.ACCESS_LOCAL_NETWORK), REQUEST_LOCAL_NETWORK)
            } else {
                refreshSupervisorApprovals()
                refreshPendingLocalTasks()
                toast("Checking the Mac connection…")
            }
        })
        page.addView(row("Notifications", if (LinkAlerts.canPost(this)) "On" else "Off", accentColor = null) {
            if (!LinkAlerts.canPost(this)) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_POST_NOTIFICATIONS)
            else toast("Change sound and vibration in Android notification settings.")
        })
        page.addView(row("Appearance", "Follows system", accentColor = null) { toast("Jarvis follows your phone's light or dark setting.") })
        page.addView(row("Privacy and security", "", accentColor = null) { detail = Detail.Privacy; renderAll(animate = true) })
        page.addView(row("Advanced", "", accentColor = null) { startActivity(Intent(this, AdvancedActivity::class.java)) })
        return page
    }

    private fun privacyDetail(): View {
        val page = detailPage("Privacy and security")
        page.addView(text("USB requests use the paired phone token over the Mac's loopback-only API. Local Wi-Fi uses a separate authenticated gateway with the Mac TLS certificate pinned from USB. Jarvis Link relay messages are end-to-end encrypted. State-changing Mac actions wait for your exact approval and are checked again before execution.", 17, color(R.color.label)).apply {
            setPadding(0, 0, 0, dp(20))
        })
        page.addView(detailField("This phone's identity", if (LinkKeystoreKeys.hasKeys()) "Stored in Android Keystore" else "Not created yet"))
        page.addView(View(this), LinearLayout.LayoutParams(-1, dp(24)))
        page.addView(actionButton("Forget this phone", R.color.alert, R.color.alert, filled = false) {
            AlertDialog.Builder(this)
                .setTitle("Forget this phone?")
                .setMessage("Jarvis will remove this phone's identity and stop receiving requests. Your USB pairing is not affected.")
                .setPositiveButton("Forget") { _, _ ->
                    LinkKeystoreKeys.deleteIdentity()
                    LinkService.runtime(this).forgetLocalState()
                    LinkSettings(this).mode = LinkMode.LOCAL_USB
                    detail = null
                    destination = Destination.SETTINGS
                    renderAll(animate = true)
                }
                .setNegativeButton("Cancel", null)
                .show()
        })
        return page
    }

    // ---- Connection --------------------------------------------------------------------------------------

    private fun statusLine(): Pair<String, Int>? = connectionStatus().let { status -> status.headline to status.color }

    private data class HomeStatus(val headline: String, val color: Int)

    /**
     * Truthful status for Home. Telegram transport, the Mac's last observed reply and the AI (verified only by a
     * real answer) are separate facts. Telegram being connected never produces a "Mac unavailable" label.
     */
    private fun connectionStatus(): HomeStatus {
        val snapshot = com.jarvis.companion.telegram.TelegramSession.controller?.snapshot()
        val transport = snapshot?.connection?.name?.let { runCatching { TransportState.valueOf(it) }.getOrNull() }
        if (transport != null && transport != TransportState.NOT_CONFIGURED) {
            val entries = runCatching { LinkService.runtime(this).conversation() }.getOrDefault(emptyList())
            val now = System.currentTimeMillis()
            val answered = entries.lastOrNull { (it["state"] as? String) == "COMPLETED" && !(it["summary"] as? String).isNullOrBlank() }
            val ai = when {
                answered == null -> AiEvidence.NOT_VERIFIED
                now - ((answered["updated_at_unix_ms"] as? Number)?.toLong() ?: 0L) < 30 * 60_000L -> AiEvidence.ANSWERED
                else -> AiEvidence.NO_RECENT_ANSWER
            }
            // Any record the Mac has moved on (not just our own send) counts as an observed Mac update.
            val lastMacUpdate = entries.filter { (it["state"] as? String) !in setOf("QUEUED", "SENT") }
                .mapNotNull { (it["updated_at_unix_ms"] as? Number)?.toLong() }
                .maxOrNull()
            val summary = ConnectionPresentation.summary(transport, lastMacUpdate, ai, now) { ago(it) }
            val tone = when (summary.rows.first().tone) {
                TaskTone.COMPLETED -> R.color.ok
                TaskTone.FAILED -> R.color.alert
                TaskTone.WAITING, TaskTone.RUNNING -> R.color.warn
                else -> R.color.label_secondary
            }
            return HomeStatus(summary.headline, color(tone))
        }
        return HomeStatus(connectionWord(), color(R.color.label_secondary))
    }

    private fun homeHeader(): View = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        val presence = JarvisPresenceView(this@MainActivity, reducedMotion).apply { state = presenceState() }
        homePresence = presence
        addView(presence, LinearLayout.LayoutParams(dp(44), dp(44)).also { it.setMargins(0, 0, dp(10), 0) })
        addView(header("Jarvis", statusLine()), LinearLayout.LayoutParams(0, -2, 1f))
        addView(text("Live", 14, R.color.accent, bold = true).apply {
            gravity = Gravity.CENTER
            setPadding(dp(14), dp(8), dp(14), dp(8))
            background = outline(color(R.color.accent), dp(18))
            contentDescription = "Open Jarvis Live"
            pressable {
                detail = Detail.Live
                renderAll(animate = true)
            }
        }, LinearLayout.LayoutParams(-2, -2))
    }

    private fun presenceState(): JarvisPresenceView.State {
        val entries = runCatching { LinkService.runtime(this).conversation() }.getOrDefault(emptyList())
        val active = entries.lastOrNull {
            (it["state"] as? String) in setOf("CREATED", "RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED")
        }
        if (active == null && composerFocused) return JarvisPresenceView.State.LISTENING
        val current = active ?: entries.lastOrNull()
            ?: return JarvisPresenceView.State.IDLE
        return when ((current["state"] as? String).orEmpty()) {
            "CREATED", "PLANNING" -> JarvisPresenceView.State.THINKING
            "RUNNING" -> JarvisPresenceView.State.EXECUTING
            "VERIFYING" -> JarvisPresenceView.State.RESPONDING
            "WAITING_APPROVAL" -> JarvisPresenceView.State.WAITING_APPROVAL
            "PAUSED" -> JarvisPresenceView.State.PAUSED
            "COMPLETED" -> JarvisPresenceView.State.COMPLETE
            "FAILED", "REJECTED" -> JarvisPresenceView.State.FAILED
            else -> JarvisPresenceView.State.IDLE
        }
    }

    /** USB/relay path only (Telegram not set up). Wording reports what was observed, not an assumed outage. */
    private fun connectionWord(): String {
        val secure = SecureStore(this)
        if (secure.token().isNullOrBlank()) return "Pair with your Mac"
        if (pinningLocalGatewayIdentity || refreshingSupervisorApprovals || refreshingLocalTasks) return "Connecting…"
        if (supervisorApprovalError.isNotBlank()) return "Mac not reachable over USB"
        val (at, transport) = secure.lastMacConnection()
        return when {
            at == 0L -> "Connecting…"
            System.currentTimeMillis() - at > 30 * 60_000L -> "Last seen ${time(at)}"
            transport == "wifi" -> "Connected over Wi-Fi"
            transport == "usb" -> "Connected over USB"
            transport == "tailscale" -> "Connected over private network"
            else -> "Connected to your Mac"
        }
    }

    private fun sync(minIntervalMs: Long) {
        val now = System.currentTimeMillis()
        if (syncing || now - lastSyncRequestMs < minIntervalMs) return
        lastSyncRequestMs = now
        syncing = true
        worker.execute {
            val report = runCatching { LinkBackgroundSync.run(applicationContext) }.getOrNull()
            main.post {
                syncing = false
                if (!destroyed) {
                    if (report != null && report.newApprovals.isNotEmpty() && detail == null && destination == Destination.HOME) {
                        decisionNote = ""
                    }
                    renderAll(animate = false)
                    val linkPending = LinkService.runtime(this).conversation().any {
                        it["transport"] in setOf("link", "telegram") && (it["state"] as? String) in setOf(
                            "CREATED", "RUNNING", "VERIFYING", "PLANNING", "WAITING_APPROVAL", "PAUSED", "SENT", "QUEUED",
                        )
                    }
                    if (linkPending) scheduleTaskStatusRefresh(if (report != null && report.error.isEmpty()) 5_000L else 15_000L)
                }
            }
        }
    }

    private fun registerPushIfNeeded() {
        if (!LinkAlerts.canPost(this)) return
        runCatching {
            val messaging = com.google.firebase.messaging.FirebaseMessaging.getInstance()
            messaging.isAutoInitEnabled = true
            messaging.token.addOnSuccessListener { token ->
                PushRegistration.register(this, token)
            }
        }
    }

    private fun openFromNotification(intent: Intent?) {
        val approvalId = intent?.getStringExtra(LinkAlerts.EXTRA_APPROVAL_ID) ?: return
        destination = Destination.ACTIVITY
        detail = Detail.Approval(approvalId)
        decisionNote = ""
        if (intent.getStringExtra(LinkAlerts.EXTRA_APPROVAL_SOURCE) == "supervisor") {
            loadSupervisorApproval(approvalId)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_POST_NOTIFICATIONS) {
            renderAll(animate = false)
            if (LinkAlerts.canPost(this)) {
                registerPushIfNeeded()
                refreshSupervisorApprovals()
            }
        } else if (requestCode == REQUEST_LOCAL_NETWORK) {
            if (Build.VERSION.SDK_INT < 37 ||
                checkSelfPermission(Manifest.permission.ACCESS_LOCAL_NETWORK) == PackageManager.PERMISSION_GRANTED
            ) {
                refreshSupervisorApprovals()
                refreshPendingLocalTasks()
                toast("Jarvis will reconnect to the paired Mac on this Wi-Fi.")
            } else {
                toast("Local Wi-Fi access is off. USB connection remains available.")
            }
        }
    }

    // ---- Building blocks ---------------------------------------------------------------------------------

    private fun header(title: String, status: Pair<String, Int>?): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(text(title, 34, color(R.color.label), bold = true))
        if (status != null) {
            addView(LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                addView(View(this@MainActivity).apply { background = roundRect(status.second, dp(5)) },
                    LinearLayout.LayoutParams(dp(8), dp(8)).also { it.setMargins(0, 0, dp(8), 0) })
                addView(text(status.first, 14, color(R.color.label_secondary)))
            }, LinearLayout.LayoutParams(-2, -2).also { it.setMargins(0, dp(4), 0, 0) })
        }
    }

    private fun detailPage(title: String): LinearLayout {
        val page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(12), dp(24), dp(120))
        }
        val back = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(4), 0, dp(16))
            addView(android.widget.ImageView(this@MainActivity).apply {
                setImageResource(R.drawable.ic_back)
                imageTintList = android.content.res.ColorStateList.valueOf(color(R.color.label))
            }, LinearLayout.LayoutParams(dp(44), dp(44)))
            addView(text(title, 17, color(R.color.label_secondary), bold = true))
            pressable { onBackPressed() }
        }
        page.addView(back)
        return page
    }

    private fun section(title: String): View = text(title.uppercase(), 13, color(R.color.label_secondary), bold = true).apply {
        letterSpacing = 0.06f
        setPadding(0, dp(24), 0, dp(8))
    }

    private fun emptyNote(message: String): View = text(message, 15, color(R.color.label_secondary)).apply {
        setPadding(0, dp(16), 0, dp(8))
    }

    /** [accentColor] is a resolved colour value, not a resource ID. */
    private fun row(title: String, subtitle: String, accentColor: Int?, onClick: () -> Unit): View = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setPadding(0, dp(14), 0, dp(14))
        if (accentColor != null) addView(View(this@MainActivity).apply { background = roundRect(accentColor, dp(4)) },
            LinearLayout.LayoutParams(dp(8), dp(8)).also { it.setMargins(0, 0, dp(12), 0) })
        addView(LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.VERTICAL
            addView(text(title, 17, color(R.color.label), maxLines = 2))
            if (subtitle.isNotBlank()) addView(text(subtitle, 14, color(R.color.label_secondary), maxLines = 2))
        }, LinearLayout.LayoutParams(0, -2, 1f))
        addView(text("›", 24, color(R.color.label_secondary)))
        pressable(onClick)
    }

    private fun detailField(label: String, value: String): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(0, dp(12), 0, dp(12))
        addView(text(label, 13, color(R.color.label_secondary), bold = true))
        addView(text(value, 17, color(R.color.label)).apply { setPadding(0, dp(2), 0, 0) })
    }

    private fun actionButton(label: String, fillColor: Int, textColor: Int, filled: Boolean, onClick: () -> Unit): View =
        text(label, 17, color(if (filled) textColor else fillColor), bold = true).apply {
            gravity = Gravity.CENTER
            minHeight = dp(56)
            background = if (filled) roundRect(color(fillColor), dp(28)) else outline(color(fillColor), dp(28))
            layoutParams = LinearLayout.LayoutParams(-1, -2).also { it.setMargins(0, 0, 0, dp(12)) }
            pressable(onClick)
        }

    private fun text(value: String, sizeSp: Int, textColor: Int, bold: Boolean = false, maxLines: Int = Int.MAX_VALUE): TextView =
        TextView(this).apply {
            text = value
            textSize = sizeSp.toFloat()
            setTextColor(textColor)
            setTypeface(typeface, if (bold) android.graphics.Typeface.BOLD else android.graphics.Typeface.NORMAL)
            this.maxLines = maxLines
            includeFontPadding = false
        }

    private fun linear(width: Int, height: Int) = LinearLayout.LayoutParams(width, height)

    private fun View.pressable(onClick: () -> Unit) = pressableView(this, onClick)

    private fun pressableView(view: View, onClick: () -> Unit) {
        view.setOnTouchListener { target, event ->
            if (!reducedMotion) when (event.action) {
                MotionEvent.ACTION_DOWN -> target.animate().scaleX(0.98f).scaleY(0.98f).setDuration(80).setInterpolator(DecelerateInterpolator()).start()
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL ->
                    target.animate().scaleX(1f).scaleY(1f).setDuration(140).setInterpolator(DecelerateInterpolator()).start()
            }
            false
        }
        view.setOnClickListener { onClick() }
    }

    private fun roundRect(fill: Int, radius: Int) = android.graphics.drawable.GradientDrawable().apply {
        setColor(fill)
        cornerRadius = radius.toFloat()
    }

    private fun outline(stroke: Int, radius: Int) = android.graphics.drawable.GradientDrawable().apply {
        setColor(android.graphics.Color.TRANSPARENT)
        cornerRadius = radius.toFloat()
        setStroke(dp(2), stroke)
    }

    private fun haptic(kind: Int) {
        if (!reducedMotion) root.performHapticFeedback(kind)
    }

    private fun toast(message: String) = android.widget.Toast.makeText(this, message, android.widget.Toast.LENGTH_SHORT).show()

    private fun humanState(state: String): Pair<String, Int> = when (state) {
        "QUEUED" -> "Waiting to send" to color(R.color.warn)
        "SENT" -> "Sent" to color(R.color.label_secondary)
        "CREATED" -> "Received by Jarvis" to color(R.color.label_secondary)
        "PLANNING" -> "Planning" to color(R.color.accent)
        "RUNNING" -> "Working" to color(R.color.accent)
        "VERIFYING" -> "Checking result" to color(R.color.accent)
        "WAITING_APPROVAL" -> "Waiting for your approval" to color(R.color.warn)
        "APPROVED" -> "Approved" to color(R.color.ok)
        "REJECTED" -> "Rejected" to color(R.color.alert)
        "PAUSED" -> "Paused" to color(R.color.warn)
        "COMPLETED" -> "Done" to color(R.color.ok)
        "FAILED" -> "Failed" to color(R.color.alert)
        "CANCELLED" -> "Cancelled" to color(R.color.label_secondary)
        "REFUSED" -> "Declined by Mac" to color(R.color.alert)
        else -> state.lowercase().replace('_', ' ').replaceFirstChar { it.uppercase() } to color(R.color.label_secondary)
    }

    private fun friendly(failure: Throwable?): String = when (failure) {
        is ApiException -> localTaskError(failure)
        is RelayHttpException -> "Jarvis could not reach your Mac (${failure.code.lowercase().replace('_', ' ')})."
        is java.io.IOException -> "Not delivered yet. The message is saved on this phone and will be retried."
        else -> "Something went wrong. Try again."
    }

    private fun time(ms: Long): String = if (ms <= 0L) "" else DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(ms))

    private fun timeLabel(ms: Long): String = if (ms <= 0L) "" else DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(ms))

    /** Relative age for status lines, e.g. "just now", "4 min ago", "2 h ago". */
    private fun ago(elapsedMs: Long): String {
        val minutes = elapsedMs / 60_000L
        return when {
            minutes < 1L -> "just now"
            minutes < 60L -> "$minutes min ago"
            minutes < 48 * 60L -> "${minutes / 60L} h ago"
            else -> "${minutes / (24 * 60L)} d ago"
        }
    }

    private fun remainingLabel(remainingMs: Long): String {
        val minutes = remainingMs / 60_000L
        return when {
            minutes >= 1L -> "$minutes min"
            else -> "${maxOf(1L, remainingMs / 1000L)} s"
        }
    }

    /** Jarvis Live visual screen. No microphone is requested and no audio is sent: the voice backend is not connected. */
    private fun liveScreen(): View {
        val page = detailPage("Jarvis Live")
        val model = VoicePresentation.view(voicePhase, voiceMessage)
        page.addView(text(model.title, 30, color(R.color.label), bold = true))
        page.addView(text(model.hint, 16, color(R.color.label_secondary)).apply { setPadding(0, dp(6), 0, dp(20)) })
        // The bars stay at rest because there is no microphone input yet; the view never fabricates a level.
        page.addView(WaveformView(this).apply { setLevels(FloatArray(28)) }, LinearLayout.LayoutParams(-1, dp(120)))
        page.addView(actionButton("Hold to talk", R.color.accent, R.color.on_accent, filled = true) {
            voicePhase = VoicePhase.ERROR
            voiceMessage = "Voice input is not connected yet. Nothing was recorded or sent."
            haptic(HapticFeedbackConstants.REJECT)
            renderAll(animate = false)
        }.apply { isEnabled = model.pushToTalkEnabled })
        page.addView(text("Voice replies and microphone access arrive with the voice backend. Until then, use the Home composer.", 14, color(R.color.label_secondary)).apply {
            setPadding(0, dp(20), 0, 0)
        })
        return page
    }

    private fun color(id: Int): Int = getColor(id)

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density + 0.5f).toInt()

    companion object {
        private const val COMPOSER_TAG = "jarvis.composer"
        private const val CONVERSATION_TAG = "jarvis.conversation"
        private const val REQUEST_POST_NOTIFICATIONS = 4201
        private const val REQUEST_LOCAL_NETWORK = 4202
    }
}
