package com.jarvis.companion

import android.app.AlertDialog
import android.app.Activity
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import com.jarvis.companion.data.JarvisApi
import com.jarvis.companion.data.PairingInvitationSignature
import com.jarvis.companion.data.SecureStore
import com.jarvis.companion.link.LinkAlerts
import com.jarvis.companion.link.LinkBackgroundSync
import com.jarvis.companion.link.LinkSyncJob
import com.jarvis.companion.notifications.PushRegistration
import com.jarvis.companion.link.LinkKeyUnavailableException
import com.jarvis.companion.link.LinkKeystoreKeys
import com.jarvis.companion.link.LinkMode
import com.jarvis.companion.link.LinkService
import com.jarvis.companion.link.LinkSettings
import com.jarvis.link.RelayHttpException
import com.jarvis.link.RelayInvitation
import com.jarvis.link.SafeCapability
import com.jarvis.link.TaskSubmission
import com.jarvis.link.TrustedHostRoot
import com.jarvis.companion.model.Approval
import com.jarvis.companion.model.ApiException
import com.jarvis.companion.model.JarvisTask
import com.jarvis.companion.model.PairingInvitation
import com.jarvis.companion.model.PairingSession
import com.jarvis.companion.model.PairingStatus
import com.jarvis.companion.model.TaskDetail
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import org.json.JSONObject
import java.security.MessageDigest
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class AdvancedActivity : Activity() {
    private enum class Screen { TASKS, APPROVALS, ACTIVITY, COMPLETED, DEVICE, TASK_DETAIL, APPROVAL_DETAIL }

    private lateinit var store: SecureStore
    private lateinit var api: JarvisApi
    private lateinit var worker: ExecutorService
    private var destroyed = false

    private val pairingPollHandler = Handler(Looper.getMainLooper())
    private val pairingPollRunnable = Runnable { refreshPairingStatus() }
    private var screen = Screen.TASKS
    private var activeTasks: List<JarvisTask>? = null
    private var completedTasks: List<JarvisTask>? = null
    private var approvals: List<Approval>? = null
    private var selectedTask: JarvisTask? = null
    private var taskDetail: TaskDetail? = null
    private var selectedApproval: Approval? = null
    private var invitation: PairingInvitation? = null
    private var pairingSession: PairingSession? = null
    private var pairingStatus: PairingStatus? = null
    private var candidateTailscaleUrlDraft = ""
    private var isBusy = false
    private var errorMessage = ""
    private var statusMessage = ""
    private var focusApprovalId: String? = null
    private var lastAutoSyncMs = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = SecureStore(this)
        api = JarvisApi(this, store)
        worker = Executors.newSingleThreadExecutor()
        LinkAlerts.ensureChannels(this)
        openApprovalFrom(intent)
        render()
        if (isPaired()) refreshCurrent()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        openApprovalFrom(intent)
        render()
    }

    /** Opening the app (or a notification) syncs Link once, debounced, so approvals appear without a manual refresh. */
    override fun onResume() {
        super.onResume()
        if (::worker.isInitialized && LinkSettings(this).mode == LinkMode.TEST_RELAY) {
            LinkSyncJob.ensureScheduled(this)
            registerPushIfNeeded()
            autoSyncLink(minIntervalMs = 30_000L)
            // Ask once, through the normal system dialog, if notifications are not yet allowed.
            if (!LinkAlerts.canPost(this) && LinkSettings(this).markNotificationsAskedOnce()) {
                requestNotificationPermissionIfNeeded()
            }
        }
    }

    /** Fetches the FCM token once alerts are allowed, and registers it only when it has changed. */
    private fun registerPushIfNeeded() {
        if (!LinkAlerts.canPost(this)) return
        runCatching {
            val messaging = com.google.firebase.messaging.FirebaseMessaging.getInstance()
            messaging.isAutoInitEnabled = true
            messaging.token.addOnSuccessListener { token ->
                if (BuildConfig.DEBUG) android.util.Log.i("JarvisFcm", "registration token available (debug build only): $token")
                if (!LinkSettings(this).pushTokenAlreadyRegistered(token)) PushRegistration.register(this, token)
            }
        }
    }

    private fun openApprovalFrom(intent: Intent?) {
        val approvalId = intent?.getStringExtra(LinkAlerts.EXTRA_APPROVAL_ID) ?: return
        focusApprovalId = approvalId
        screen = Screen.DEVICE
        linkMessage = "Opened from a notification. Review the exact operation below before deciding."
    }

    /** Background-thread Link sync; never blocks the UI and never runs in local-USB mode. */
    private fun autoSyncLink(minIntervalMs: Long) {
        val now = System.currentTimeMillis()
        if (isBusy || now - lastAutoSyncMs < minIntervalMs) return
        lastAutoSyncMs = now
        worker.execute {
            val report = runCatching { LinkBackgroundSync.run(applicationContext) }.getOrNull()
            runOnUiThread {
                if (destroyed) return@runOnUiThread
                if (report != null) {
                    linkMessage = when {
                        report.error.isNotBlank() -> "Sync: ${report.error}. Queued messages will be retried."
                        report.newApprovals.isNotEmpty() -> "Sync: ${report.newApprovals.size} new approval request(s)."
                        else -> "Synced with the relay."
                    }
                    render()
                }
            }
        }
    }

    private fun requestNotificationPermissionIfNeeded(force: Boolean = false) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && (force || !LinkAlerts.canPost(this))) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), REQUEST_POST_NOTIFICATIONS)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        @Suppress("DEPRECATION")
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_POST_NOTIFICATIONS) {
            linkMessage = if (LinkAlerts.canPost(this)) "Notifications allowed. New approval requests will alert you."
            else "Notifications are off. Approvals still appear when you open the app."
            render()
        }
    }

    override fun onStart() {
        super.onStart()
        if (pairingSession != null && pairingStatus !is PairingStatus.Approved) schedulePairingPoll(0)
    }

    override fun onStop() {
        pairingPollHandler.removeCallbacks(pairingPollRunnable)
        super.onStop()
    }

    override fun onDestroy() {
        destroyed = true
        pairingPollHandler.removeCallbacks(pairingPollRunnable)
        if (::worker.isInitialized) worker.shutdownNow()
        super.onDestroy()
    }


    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        when (screen) {
            Screen.TASK_DETAIL, Screen.APPROVAL_DETAIL -> {
                screen = if (screen == Screen.TASK_DETAIL) Screen.TASKS else Screen.APPROVALS
                errorMessage = ""
                render()
            }
            else -> super.onBackPressed()
        }
    }

    private fun render() {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(color(R.color.ink_0))
        }
        root.setOnApplyWindowInsetsListener { view, insets ->
            if (Build.VERSION.SDK_INT >= 30) {
                val systemBars = insets.getInsets(WindowInsets.Type.systemBars())
                view.setPadding(0, systemBars.top, 0, systemBars.bottom)
            } else {
                @Suppress("DEPRECATION")
                view.setPadding(0, insets.systemWindowInsetTop, 0, insets.systemWindowInsetBottom)
            }
            insets
        }
        root.addView(buildHeader())
        if (errorMessage.isNotBlank()) root.addView(messageBanner(errorMessage, isError = true))
        if (statusMessage.isNotBlank()) root.addView(messageBanner(statusMessage, isError = false))
        root.addView(Button(this).apply {
            text = "Telegram connection test"
            isAllCaps = false
            setOnClickListener {
                startActivity(Intent(this@AdvancedActivity, com.jarvis.companion.telegram.TelegramActivity::class.java))
            }
        }, LinearLayout.LayoutParams(-1, (52 * resources.displayMetrics.density).toInt()).apply {
            leftMargin = (20 * resources.displayMetrics.density).toInt()
            rightMargin = (20 * resources.displayMetrics.density).toInt()
        })

        val scroll = ScrollView(this).apply {
            isFillViewport = true
            overScrollMode = View.OVER_SCROLL_IF_CONTENT_SCROLLS
        }
        val page = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(12), dp(20), dp(24))
        }
        when (screen) {
            Screen.TASKS -> drawTaskList(page, activeTasks, "active")
            Screen.APPROVALS -> drawApprovals(page)
            Screen.ACTIVITY -> drawActivity(page)
            Screen.COMPLETED -> drawTaskList(page, completedTasks, "completed")
            Screen.DEVICE -> drawDevice(page)
            Screen.TASK_DETAIL -> drawTaskDetail(page)
            Screen.APPROVAL_DETAIL -> drawApprovalDetail(page)
        }
        scroll.addView(page, ViewGroup.LayoutParams(-1, -2))
        root.addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))
        root.addView(buildNavigation())
        setContentView(root)
    }

    private fun buildHeader(): View {
        val wrapper = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(18), dp(20), dp(8))
        }
        val top = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val brand = text("JARVIS  /  COMPANION", 12, color(R.color.signal), bold = true).apply {
            letterSpacing = 0.08f
        }
        top.addView(brand, LinearLayout.LayoutParams(0, -2, 1f))
        if (screen == Screen.TASK_DETAIL || screen == Screen.APPROVAL_DETAIL) {
            top.addView(smallAction("Back") { onBackPressed() })
        } else if (screen != Screen.DEVICE && isPaired()) {
            top.addView(smallAction(if (isBusy) "Working…" else "Refresh") { refreshCurrent() })
        }
        wrapper.addView(top)

        val titleText = when (screen) {
            Screen.TASKS -> "Active tasks"
            Screen.APPROVALS -> "Needs your approval"
            Screen.ACTIVITY -> "Execution activity"
            Screen.COMPLETED -> "Completed"
            Screen.DEVICE -> "Pair this phone"
            Screen.TASK_DETAIL -> "Task details"
            Screen.APPROVAL_DETAIL -> "Review operation"
        }
        wrapper.addView(text(titleText, 27, color(R.color.paper), bold = true).apply {
            setPadding(0, dp(10), 0, dp(2))
        })
        val sub = when (screen) {
            Screen.TASKS -> if (isPaired()) "What Jarvis is working on now" else "A private view of your Mac's Jarvis tasks"
            Screen.APPROVALS -> "Check the exact action before deciding"
            Screen.ACTIVITY -> "Recent task events from the Mac"
            Screen.COMPLETED -> "Finished tasks and their final status"
            Screen.DEVICE -> "Scan a Mac invitation over USB-reverse localhost"
            Screen.TASK_DETAIL -> selectedTask?.title.orEmpty()
            Screen.APPROVAL_DETAIL -> "Review the operation and its exact arguments"
        }
        wrapper.addView(text(sub, 14, color(R.color.muted)))

        val state = if (isPaired()) "PAIRED · " + store.deviceId() else "NOT PAIRED"
        wrapper.addView(text(state, 11, if (isPaired()) color(R.color.success) else color(R.color.warning), bold = true).apply {
            setPadding(0, dp(8), 0, 0)
            contentDescription = if (isPaired()) "Phone is paired with device " + store.deviceId() else "Phone is not paired"
        })
        return wrapper
    }

    private fun buildNavigation(): View {
        val nav = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setBackgroundColor(color(R.color.ink_1))
            setPadding(dp(6), dp(6), dp(6), dp(8))
            elevation = dp(8).toFloat()
        }
        val items = listOf(
            Triple(Screen.TASKS, "◉", "Tasks"),
            Triple(Screen.APPROVALS, "!", "Approvals"),
            Triple(Screen.ACTIVITY, "≋", "Activity"),
            Triple(Screen.COMPLETED, "✓", "Done"),
            Triple(Screen.DEVICE, "⌘", "Device"),
        )
        items.forEach { (destination, icon, label) ->
            val selected = screen == destination || (destination == Screen.TASKS && screen == Screen.TASK_DETAIL) ||
                (destination == Screen.APPROVALS && screen == Screen.APPROVAL_DETAIL)
            val count = if (destination == Screen.APPROVALS) approvals?.size else null
            val caption = if (count != null && count > 0) "$label $count" else label
            val button = Button(this).apply {
                text = "$icon\n$caption"
                textSize = 10.5f
                isAllCaps = false
                setTextColor(color(if (selected) R.color.signal else R.color.muted))
                gravity = Gravity.CENTER
                minHeight = dp(54)
                minWidth = 0
                setPadding(dp(2), dp(3), dp(2), dp(3))
                background = rounded(
                    if (selected) color(R.color.ink_3) else color(R.color.ink_1),
                    14,
                    if (selected) color(R.color.line_bright) else color(R.color.ink_1),
                )
                contentDescription = if (count != null && count > 0) "$label, $count pending" else label
                setOnClickListener {
                    screen = destination
                    if (screen != Screen.DEVICE) pairingPollHandler.removeCallbacks(pairingPollRunnable)
                    errorMessage = ""
                    statusMessage = ""
                    render()
                    refreshCurrent()
                }
            }
            nav.addView(button, LinearLayout.LayoutParams(0, dp(58), 1f).apply {
                setMargins(dp(2), 0, dp(2), 0)
            })
        }
        return nav
    }

    private fun drawTaskList(page: LinearLayout, tasks: List<JarvisTask>?, kind: String) {
        if (!isPaired()) {
            drawNeedsPair(page, "Pair with your Mac to view tasks.")
            return
        }
        sectionEyebrow(page, if (kind == "active") "RUNNING ON YOUR MAC" else "TASK HISTORY")
        if (tasks == null) {
            drawLoading(page, "Loading $kind tasks")
            return
        }
        if (tasks.isEmpty()) {
            drawEmpty(
                page,
                if (kind == "active") "Jarvis is idle" else "No completed tasks yet",
                if (kind == "active") "New tasks will appear here when Jarvis starts working." else "Completed work will appear here with its final status.",
            )
            return
        }
        tasks.forEach { task ->
            card(page) {
                addView(text(task.title, 18, color(R.color.paper), bold = true))
                addView(text(task.state.uppercase(), 11, stateColor(task.state), bold = true).apply {
                    setPadding(0, dp(7), 0, dp(7))
                })
                if (task.currentStep.isNotBlank()) addView(labelValue("CURRENT STEP", task.currentStep))
                if (task.lastEvent.isNotBlank()) addView(labelValue("LATEST EVENT", task.lastEvent))
                if (task.updatedAt.isNotBlank()) addView(labelValue("UPDATED", task.updatedAt))
                addView(actionButton("View execution details", R.color.signal, secondary = true) {
                    selectedTask = task
                    taskDetail = null
                    screen = Screen.TASK_DETAIL
                    errorMessage = ""
                    statusMessage = ""
                    render()
                    loadTaskDetail(task.id)
                })
            }
            gap(page, 12)
        }
    }

    private fun drawApprovals(page: LinearLayout) {
        if (!isPaired()) {
            drawNeedsPair(page, "Pair with your Mac to review approval requests.")
            return
        }
        sectionEyebrow(page, "EXACT OPERATION REVIEW")
        page.addView(text(
            "Jarvis waits for your decision before an approval-gated operation can continue.",
            14, color(R.color.muted),
        ).apply { setPadding(0, 0, 0, dp(14)) })
        val items = approvals
        if (items == null) {
            drawLoading(page, "Loading approval requests")
            return
        }
        if (items.isEmpty()) {
            drawEmpty(page, "Nothing is waiting", "When Jarvis pauses for permission, the exact operation will appear here.")
            return
        }
        items.forEach { approval ->
            card(page) {
                addView(text(approval.operation, 18, color(R.color.paper), bold = true))
                addView(text("TARGET  ·  " + approval.targetResource, 13, color(R.color.signal)).apply {
                    setPadding(0, dp(6), 0, dp(9))
                })
                if (approval.reason.isNotBlank()) addView(text(approval.reason, 14, color(R.color.muted)))
                addView(text("EXPIRES  ·  " + nonempty(approval.expiresAt, "Not provided"), 11, color(R.color.warning)).apply {
                    setPadding(0, dp(10), 0, 0)
                })
                addView(actionButton("Review exact operation", R.color.signal, secondary = true) {
                    selectedApproval = approval
                    screen = Screen.APPROVAL_DETAIL
                    errorMessage = ""
                    statusMessage = ""
                    render()
                })
            }
            gap(page, 12)
        }
    }

    private fun drawActivity(page: LinearLayout) {
        if (!isPaired()) {
            drawNeedsPair(page, "Pair with your Mac to follow execution activity.")
            return
        }
        sectionEyebrow(page, "RECENT EXECUTION EVENTS")
        val tasks = ((activeTasks ?: emptyList()) + (completedTasks ?: emptyList()))
            .distinctBy { it.id }
            .sortedByDescending { it.updatedAt }
        if (activeTasks == null || completedTasks == null) {
            drawLoading(page, "Loading recent task activity")
            return
        }
        if (tasks.isEmpty()) {
            drawEmpty(page, "No activity yet", "Task events will appear here after Jarvis starts a task.")
            return
        }
        tasks.forEach { task ->
            card(page) {
                addView(text(task.state.uppercase(), 11, stateColor(task.state), bold = true))
                addView(text(task.title, 17, color(R.color.paper), bold = true).apply {
                    setPadding(0, dp(5), 0, dp(5))
                })
                addView(text(nonempty(task.lastEvent, task.currentStep.ifBlank { "No event summary was returned." }), 14, color(R.color.muted)))
                if (task.updatedAt.isNotBlank()) addView(text(task.updatedAt, 11, color(R.color.muted)).apply {
                    setPadding(0, dp(8), 0, 0)
                })
                addView(actionButton("Open task timeline", R.color.signal, secondary = true) {
                    selectedTask = task
                    taskDetail = null
                    screen = Screen.TASK_DETAIL
                    render()
                    loadTaskDetail(task.id)
                })
            }
            gap(page, 12)
        }
    }

    private fun drawTaskDetail(page: LinearLayout) {
        if (!isPaired()) {
            drawNeedsPair(page, "Pair with your Mac to view task details.")
            return
        }
        val detail = taskDetail
        if (detail == null) {
            drawLoading(page, "Loading task details")
            return
        }
        val task = detail.task
        card(page) {
            addView(text(task.title, 20, color(R.color.paper), bold = true))
            addView(text(task.state.uppercase(), 11, stateColor(task.state), bold = true).apply {
                setPadding(0, dp(8), 0, dp(10))
            })
            if (task.currentStep.isNotBlank()) addView(labelValue("CURRENT STEP", task.currentStep))
            if (task.updatedAt.isNotBlank()) addView(labelValue("LAST UPDATED", task.updatedAt))
            if (task.progress != null) addView(labelValue("SERVER REPORTED PROGRESS", task.progress.coerceIn(0, 100).toString() + "%"))
        }
        gap(page, 20)
        sectionEyebrow(page, "EXECUTION TIMELINE")
        if (detail.events.isEmpty()) {
            drawEmpty(page, "No event records", "The task endpoint did not return an event history.")
        } else {
            detail.events.forEach { event ->
                card(page, compact = true) {
                    addView(text(nonempty(event.at, "Time not provided"), 11, color(R.color.signal), bold = true))
                    addView(text(event.state, 15, color(R.color.paper), bold = true).apply {
                        setPadding(0, dp(4), 0, dp(4))
                    })
                    addView(text(nonempty(event.message, "No event description returned."), 14, color(R.color.muted)))
                    if (event.toolName.isNotBlank()) addView(labelValue("TOOL", event.toolName))
                    if (event.durationMs != null) addView(labelValue("DURATION", event.durationMs.toString() + " ms"))
                }
                gap(page, 8)
            }
        }
    }

    private fun drawApprovalDetail(page: LinearLayout) {
        if (!isPaired()) {
            drawNeedsPair(page, "Pair with your Mac before deciding on an operation.")
            return
        }
        val approval = selectedApproval
        if (approval == null) {
            drawEmpty(page, "Approval not available", "Refresh the pending approvals list to see current requests.")
            return
        }
        card(page) {
            addView(text(approval.operation, 21, color(R.color.paper), bold = true))
            addView(labelValue("TARGET RESOURCE", approval.targetResource))
            addView(labelValue("WHY JARVIS PAUSED", nonempty(approval.reason, "No reason was provided.")))
            addView(labelValue("SERVER RISK LABEL · INFORMATIONAL", approval.risk))
            addView(labelValue("RELATED TASK", nonempty(approval.taskId, "Not provided")))
            addView(labelValue("CREATED", nonempty(approval.createdAt, "Not provided")))
            addView(labelValue("EXPIRES", nonempty(approval.expiresAt, "Not provided")))
        }
        gap(page, 12)
        sectionEyebrow(page, "CANONICAL ARGUMENTS")
        card(page) {
            val args = text(approval.parametersSummary, 13, color(R.color.paper), mono = true).apply {
                setTextIsSelectable(true)
                setPadding(dp(12), dp(12), dp(12), dp(12))
                contentDescription = "Canonical operation arguments"
            }
            addView(args)
        }
        gap(page, 12)
        sectionEyebrow(page, "OPERATION FINGERPRINT")
        card(page) {
            addView(text(approval.operationDigest, 13, color(R.color.signal), mono = true).apply {
                setTextIsSelectable(true)
                contentDescription = "Operation digest, selectable"
            })
        }
        gap(page, 10)
        page.addView(text(
            "The fingerprint binds the decision to this operation. The server must verify it again at execution time. The risk label is informational.",
            12, color(R.color.muted),
        ).apply { setPadding(dp(2), 0, dp(2), dp(10)) })
        val actionRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        actionRow.addView(actionButton("Reject", R.color.danger, secondary = true) {
            confirmDecision(approval, "reject")
        }, LinearLayout.LayoutParams(0, dp(52), 1f).apply { setMargins(0, 0, dp(6), 0) })
        actionRow.addView(actionButton("Approve once", R.color.signal) {
            confirmDecision(approval, "approve")
        }, LinearLayout.LayoutParams(0, dp(52), 1f).apply { setMargins(dp(6), 0, 0, 0) })
        page.addView(actionRow)
    }

    private fun drawDevice(page: LinearLayout) {
        sectionEyebrow(page, "LOCAL DEVICE ENROLLMENT")
        if (isPaired()) {
            card(page) {
                addView(text("This phone is paired", 20, color(R.color.paper), bold = true))
                addView(labelValue("DEVICE ID", store.deviceId()))
                addView(labelValue("SAVED SERVER URL", store.baseUrl()))
                addView(text(
                    "The expiring device token is encrypted with Android Keystore. The signing private key never leaves Android Keystore.",
                    13, color(R.color.muted),
                ).apply { setPadding(0, dp(10), 0, 0) })
            }
            gap(page, 12)
            drawPinnedServerMigration(page)
        } else {
            val currentInvitation = invitation
            if (currentInvitation == null) {
                card(page) {
                    addView(text("Scan the invitation shown on your Mac", 19, color(R.color.paper), bold = true))
                    addView(text(
                        "The QR contains a short-lived invitation, not an API key. This phone signs its challenge with a private key held in Android Keystore.",
                        14, color(R.color.muted),
                    ).apply { setPadding(0, dp(7), 0, dp(12)) })
                    addView(actionButton(if (isBusy) "Preparing scanner…" else "Scan Mac invitation QR", R.color.signal) {
                        requestCameraAndScan()
                    })
                    addView(text(
                        "Google Play services scans QR codes in its own UI. This app does not request camera permission or save camera images.",
                        12, color(R.color.muted),
                    ).apply { setPadding(0, dp(10), 0, 0) })
                    if (applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE != 0) {
                        addView(actionButton("Run Mac signature test vector", R.color.muted, secondary = true) {
                            runInvitationVectorSelfTest()
                        }.apply { setPadding(dp(8), dp(6), dp(8), dp(6)) })
                    }
                }
            } else {
                card(page) {
                    addView(text("Review this Mac invitation", 19, color(R.color.paper), bold = true))
                    addView(labelValue("INVITATION ID", currentInvitation.invitationId))
                    addView(labelValue("LOCAL SERVER URL", currentInvitation.serverUrl))
                    addView(labelValue("MAC SERVER FINGERPRINT", currentInvitation.serverFingerprint))
                    addView(labelValue(
                        "VERIFIED SERVER KEY FINGERPRINT",
                        currentInvitation.verifiedServerKeyFingerprint ?: "Not verified",
                    ))
                addView(labelValue("USB REVERSE COMMAND", reverseCommand(currentInvitation.serverUrl)))
                    addView(labelValue("EXPIRES", currentInvitation.expiresAt))
                    addView(labelValue("THIS PHONE", api.deviceName()))
                    addView(labelValue("PHONE KEY FINGERPRINT", store.signingKeyFingerprint()))
                    addView(text(
                        "The Mac must separately confirm this device name and key fingerprint in its local pending-devices screen. No token is issued by scanning alone.",
                        13, color(R.color.muted),
                    ).apply { setPadding(0, dp(10), 0, dp(12)) })
                    addView(actionButton(if (isBusy) "Sending signed request…" else "Request pairing on Mac", R.color.signal) {
                        pairingSession = PairingSession(currentInvitation.invitationId)
                        pairingStatus = PairingStatus.Pending
                        runRequest("Sending a signed pairing request…", { api.completePairing(currentInvitation) }) { session ->
                            pairingSession = session
                            pairingStatus = PairingStatus.Pending
                            statusMessage = "Request sent. Confirm this phone in the Mac's local pending-devices screen."
                            schedulePairingPoll(PAIRING_POLL_INTERVAL_MS)
                        }
                    })
                    if (pairingSession == null) {
                        addView(actionButton("Scan a different invitation", R.color.muted, secondary = true) {
                            invitation = null
                            statusMessage = "Unused invitation cleared from this phone; it will expire on the Mac."
                            errorMessage = ""
                            render()
                        })
                    }
                }
            }

            val session = pairingSession
            if (session != null) {
                gap(page, 14)
                card(page) {
                    addView(text("Mac confirmation", 19, color(R.color.paper), bold = true))
                    addView(labelValue("INVITATION ID", session.invitationId))
                    addView(labelValue("DEVICE NAME", api.deviceName()))
                    addView(labelValue("PHONE KEY FINGERPRINT", store.signingKeyFingerprint()))
                    addView(text(
                        "Open the Mac's local pending-devices screen. Confirm only if this device name and fingerprint match.",
                        13, color(R.color.muted),
                    ).apply { setPadding(0, dp(8), 0, dp(8)) })
                    when (val currentStatus = pairingStatus) {
                        PairingStatus.Pending, null -> {
                            addView(text(
                                if (isBusy) "Checking for Mac confirmation…" else "Waiting for confirmation on the Mac",
                                14, color(R.color.warning), bold = true,
                            ))
                            addView(actionButton("Check status now", R.color.signal, secondary = true) {
                                refreshPairingStatus()
                            })
                        }
                        PairingStatus.Rejected -> {
                            addView(text("The Mac rejected this device request. Scan a new invitation to try again.", 14, color(R.color.danger)))
                            addView(actionButton("Scan new Mac invitation", R.color.signal, secondary = true) {
                                resetLocalInvitation("Scan the new invitation from the Mac.")
                            })
                        }
                        PairingStatus.Expired -> {
                            addView(text("This invitation expired. Create a fresh QR on the Mac and scan it again.", 14, color(R.color.warning)))
                            addView(actionButton("Scan new Mac invitation", R.color.signal, secondary = true) {
                                resetLocalInvitation("Scan the new invitation from the Mac.")
                            })
                        }
                        is PairingStatus.Approved -> addView(text("Mac confirmed this device. Finishing the signed token claim…", 14, color(R.color.success)))
                    }
                    if (pairingStatus is PairingStatus.Pending) {
                        addView(actionButton("Stop waiting on this phone", R.color.muted, secondary = true) {
                            resetLocalInvitation("Stopped checking on this phone. The Mac invitation remains pending until it expires or is rejected locally.")
                        })
                    }
                }
            }
        }

        gap(page, 12)
        card(page, compact = true) {
            addView(text("Local-only connection", 15, color(R.color.paper), bold = true))
            addView(text(
                "Connect the phone by USB and run the reverse command shown in the invitation preview on the Mac. The QR URL must use loopback. LAN, public, VPN, and tunnel addresses are blocked; no remote listener is enabled.",
                13, color(R.color.muted),
            ).apply { setPadding(0, dp(6), 0, 0) })
        }
        gap(page, 12)
        drawJarvisLink(page)
    }

    /**
     * Jarvis Link (experimental). Explicit connection mode: the app uses the local USB pairing above unless
     * the user selects the test relay. Nothing here changes the existing USB pairing or its credentials.
     */
    private fun drawJarvisLink(page: LinearLayout) {
        val settings = LinkSettings(this)
        val mode = settings.mode
        sectionEyebrow(page, "JARVIS LINK (EXPERIMENTAL)")
        card(page) {
            addView(text("Connection mode", 17, color(R.color.paper), bold = true))
            addView(text(
                when (mode) {
                    LinkMode.LOCAL_USB -> "Current mode: Local USB. Link is off; no relay is contacted."
                    LinkMode.TEST_RELAY -> "Current mode: Jarvis Link test relay. Messages go only to the relay URL below, through an encrypted end-to-end channel."
                },
                13, color(R.color.muted),
            ).apply { setPadding(0, dp(6), 0, dp(10)) })
            addView(actionButton(
                if (mode == LinkMode.LOCAL_USB) "Local USB — selected" else "Use local USB only",
                R.color.muted, secondary = mode != LinkMode.LOCAL_USB,
            ) { settings.mode = LinkMode.LOCAL_USB; linkMessage = "Switched to local USB. Link is off."; render() })
            gap(this, 8)
            addView(actionButton(
                if (mode == LinkMode.TEST_RELAY) "Jarvis Link test relay — selected" else "Use Jarvis Link test relay",
                R.color.signal, secondary = mode != LinkMode.TEST_RELAY,
            ) {
                settings.mode = LinkMode.TEST_RELAY
                linkMessage = "Test relay selected. Enter its details, then enroll."
                requestNotificationPermissionIfNeeded()
                render()
            })
            addView(text(
                "The mode changes only when you tap it. Test relay mode is for development relays reached through adb reverse; it is not a production or remote endpoint.",
                12, color(R.color.muted),
            ).apply { setPadding(0, dp(8), 0, 0) })
        }
        if (mode != LinkMode.TEST_RELAY) return

        val supported = LinkKeystoreKeys.isSupported()
        val status = runCatching { LinkService.runtime(this).status() }.getOrNull()
        gap(page, 12)
        card(page) {
            addView(text("Status", 15, color(R.color.paper), bold = true))
            addView(labelValue("RELAY URL", linkDraft["relay"].takeUnless { it.isNullOrBlank() } ?: LinkSettings.DEFAULT_TEST_RELAY_URL))
            addView(labelValue("KEYSTORE", if (!supported) "Unavailable: needs Android 12" else if (LinkKeystoreKeys.hasKeys()) "Link identity present" else "No Link identity yet"))
            addView(labelValue("ENROLLMENT", when {
                status == null -> "Unknown"
                status.enrollmentPending -> "Waiting for Mac confirmation (run sync after the Mac certifies)"
                status.certificatesVerified -> "Enrolled; certificates verified against the pinned Mac root"
                status.deviceId != null -> "Enrollment started; not yet certified"
                else -> "Not enrolled"
            }))
            addView(labelValue("QUEUED LOCALLY", (status?.outboxSize ?: 0).toString()))
            addView(labelValue("APPROVAL ALERTS", if (LinkAlerts.canPost(this@AdvancedActivity)) "Allowed: a notification shows when a request arrives" else "Not allowed: requests still appear when you open the app"))
            if (!LinkAlerts.canPost(this@AdvancedActivity)) {
                gap(this, 6)
                addView(actionButton("Allow approval alerts", R.color.signal, secondary = true) { requestNotificationPermissionIfNeeded(force = true) })
            }
            if (linkMessage.isNotBlank()) {
                addView(text(linkMessage, 13, color(R.color.paper)).apply { setPadding(0, dp(10), 0, 0) })
            }
        }
        gap(page, 12)
        card(page) {
            addView(text("Enroll this phone", 15, color(R.color.paper), bold = true))
            addView(text("Enter the values the Mac operator printed for this invitation. They are not saved except the relay URL.", 12, color(R.color.muted)))
            linkField(this, "relay", "Relay URL (http://127.0.0.1:8787 via adb reverse)", initial = settings.relayUrl)
            linkField(this, "tenant", "Tenant ID")
            linkField(this, "invitation_id", "Invitation ID (32 hex)")
            linkField(this, "invitation_secret", "Invitation secret", secret = true)
            linkField(this, "invitation_expires_ms", "Invitation expiry (Unix ms)")
            linkField(this, "root_device_id", "Mac host device ID (pinned root)")
            linkField(this, "root_public_key", "Mac host root public key (base64url SPKI)")
            linkField(this, "name", "Device name (optional)")
            gap(this, 8)
            addView(actionButton("Enroll phone with relay", R.color.signal, secondary = false) { enrollLink() })
        }
        gap(page, 12)
        drawLinkApprovals(page)
        gap(page, 12)
        card(page) {
            addView(text("Operations", 15, color(R.color.paper), bold = true))
            addView(actionButton("Sync certificates (after Mac certifies)", R.color.signal) {
                runLink("Verifying certificates against the pinned Mac root…") { LinkService.runtime(this@AdvancedActivity).sync(linkRelay()) }
            })
            gap(this, 8)
            addView(actionButton("Send harmless status check (read-only)", R.color.signal) {
                runLink("Sending encrypted status check…") {
                    val sent = LinkService.runtime(this@AdvancedActivity).submit(
                        linkRelay(), TaskSubmission("Harmless status check from the companion", setOf(SafeCapability.READ_ONLY)), null,
                    )
                    linkedMapOf<String, Any>("message_id" to sent.messageId, "queued_offline" to sent.queuedOffline)
                }
            })
            gap(this, 8)
            addView(actionButton("Check for replies", R.color.signal) {
                runLink("Checking for encrypted replies…") {
                    val page = LinkService.runtime(this@AdvancedActivity).poll(linkRelay())
                    linkedMapOf<String, Any>("events" to page.events.size, "summary" to (page.events.firstOrNull()?.toMap()?.get("summary") ?: "none"))
                }
            })
            gap(this, 8)
            addView(actionButton("Retry queued messages", R.color.signal, secondary = true) {
                runLink("Retrying queued messages…") { LinkService.runtime(this@AdvancedActivity).flush(linkRelay()) }
            })
            gap(this, 8)
            addView(actionButton("Forget local Link identity", R.color.danger, secondary = true) {
                runLink("Removing the local Link identity…") {
                    LinkKeystoreKeys.deleteIdentity()
                    LinkService.runtime(this@AdvancedActivity).forgetLocalState()
                    linkedMapOf<String, Any>("forgotten" to true, "note" to "revoke the device on the relay as well")
                }
            })
        }
    }

    /** Pending approval requests from the Mac, exactly as received and decrypted; expired ones are hidden. */
    private fun drawLinkApprovals(page: LinearLayout) {
        val runtime = LinkService.runtime(this)
        val pending = runCatching { runtime.pendingApprovals() }.getOrDefault(emptyList())
        card(page) {
            addView(text("Approval requests", 15, color(R.color.paper), bold = true))
            if (pending.isEmpty()) {
                addView(text("None pending. Tap “Check for replies” to fetch new requests from the Mac.", 13, color(R.color.muted)))
            }
            pending.forEach { review ->
                addView(labelValue("OPERATION", review.operation))
                addView(labelValue("TARGET", review.target))
                addView(labelValue("EXPIRES", java.text.DateFormat.getDateTimeInstance(
                    java.text.DateFormat.MEDIUM, java.text.DateFormat.SHORT).format(java.util.Date(review.expiresAtUnixMs))))
                addView(labelValue("DIGEST (first 16 hex)", review.canonicalArgumentsDigest.take(16)))
                if (review.approvalId == focusApprovalId) {
                    addView(text("Opened from notification", 12, color(R.color.signal), bold = true).apply { setPadding(0, dp(6), 0, 0) })
                }
                addView(labelValue("SUMMARY", review.redactedSummary))
                gap(this, 8)
                addView(actionButton("Approve", R.color.signal) { runLinkDecision(review.approvalId, "approve") })
                gap(this, 6)
                addView(actionButton("Reject", R.color.danger, secondary = true) { runLinkDecision(review.approvalId, "reject") })
                gap(this, 12)
            }
            val updates = runCatching { runtime.recentUpdates() }.getOrDefault(emptyList())
            if (updates.isNotEmpty()) {
                addView(text("Recent updates from the Mac", 12, color(R.color.muted), bold = true).apply { setPadding(0, dp(8), 0, 0) })
                updates.takeLast(3).forEach { update ->
                    addView(text("${update["state"]}: ${update["summary"]}", 13, color(R.color.paper)).apply { setPadding(0, dp(4), 0, 0) })
                }
            }
        }
    }

    private fun runLinkDecision(approvalId: String, decision: String) {
        runLink(if (decision == "approve") "Sending approval over Jarvis Link…" else "Sending rejection over Jarvis Link…") {
            LinkService.runtime(this@AdvancedActivity).decideApproval(linkRelay(), approvalId, decision)
        }
        // The relay confirms receipt at once; the Mac's result comes on a later sync. Retry with backoff while
        // this screen is open, then stop. Background delivery is the job scheduler's responsibility.
        listOf(8_000L, 16_000L, 32_000L, 60_000L, 120_000L).forEach { delay ->
            pairingPollHandler.postDelayed({ if (!destroyed) autoSyncLink(minIntervalMs = 0L) }, delay)
        }
    }

    private val linkDraft = mutableMapOf<String, String>()
    private var linkMessage = ""

    private fun linkRelay(): String = (linkDraft["relay"]?.takeIf { it.isNotBlank() } ?: LinkSettings.DEFAULT_TEST_RELAY_URL).trim()

    private fun linkField(parent: LinearLayout, key: String, hint: String, secret: Boolean = false, initial: String = "") {
        if (linkDraft[key] == null) linkDraft[key] = initial
        val field = EditText(this).apply {
            this.hint = hint
            setText(linkDraft[key] ?: "")
            setSingleLine(true)
            textSize = 13f
            setTextColor(color(R.color.paper))
            setHintTextColor(color(R.color.muted))
            inputType = if (secret) InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD else InputType.TYPE_CLASS_TEXT
            background = rounded(color(R.color.ink_3), 12, color(R.color.line))
            setPadding(dp(12), dp(10), dp(12), dp(10))
            addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
                override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
                override fun afterTextChanged(s: Editable?) { linkDraft[key] = s?.toString().orEmpty() }
            })
        }
        parent.addView(field, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) })
    }

    private fun enrollLink() {
        val tenant = linkDraft["tenant"].orEmpty().trim()
        val invitationId = linkDraft["invitation_id"].orEmpty().trim()
        val secret = linkDraft["invitation_secret"].orEmpty().trim()
        val expires = linkDraft["invitation_expires_ms"].orEmpty().trim().toLongOrNull()
        val rootId = linkDraft["root_device_id"].orEmpty().trim()
        val rootKey = linkDraft["root_public_key"].orEmpty().trim()
        val name = linkDraft["name"].orEmpty().trim().ifBlank { "Jarvis Companion (Android)" }
        if (tenant.isBlank() || invitationId.isBlank() || secret.isBlank() || expires == null || rootId.isBlank() || rootKey.isBlank()) {
            linkMessage = "Fill in tenant, invitation, expiry, and the Mac root values before enrolling."
            render()
            return
        }
        val relay = linkRelay()
        LinkSettings(this).relayUrl = relay
        runLink("Starting enrollment…") {
            LinkService.runtime(this@AdvancedActivity).enroll(
                relayUrl = relay,
                invitation = RelayInvitation(tenant, invitationId, secret, expires),
                deviceName = name,
                root = TrustedHostRoot(rootId, rootKey),
            )
        }
    }

    /** Link operations run off the main thread and never touch the USB pairing or its token. */
    private fun runLink(label: String, action: () -> Map<String, Any>) {
        if (isBusy) return
        isBusy = true
        linkMessage = label
        render()
        worker.execute {
            val result = runCatching(action)
            runOnUiThread {
                if (destroyed) return@runOnUiThread
                isBusy = false
                linkMessage = result.fold(
                    onSuccess = { values -> "Done. " + values.entries.joinToString(", ") { "${it.key}: ${it.value}" } },
                    onFailure = { failure -> describeLinkFailure(failure) },
                )
                render()
            }
        }
    }

    private fun describeLinkFailure(failure: Throwable): String = when (failure) {
        is RelayHttpException -> when (failure.code) {
            "DEVICE_REVOKED" -> "This phone was revoked on the relay. Forget the local Link identity and enroll again."
            "NOT_RECIPIENT" -> "The relay has no active recipient for this message. Check the Mac host is certified."
            "MESSAGE_EXPIRED" -> "The message expired before delivery; nothing was run."
            "INVALID_SIGNATURE", "UNAUTHORIZED" -> "The relay rejected this phone's signature. Check the device identity and tenant."
            "RATE_LIMITED" -> "The relay is rate limiting requests. Try again shortly."
            else -> "The relay refused the request (${failure.code})."
        }
        is LinkKeyUnavailableException -> failure.message ?: "Link keys are unavailable."
        is java.io.IOException -> "The relay is unreachable. Messages you sent stay queued on this phone."
        is SecurityException -> "A certificate or signature did not verify against the pinned Mac root. Nothing was accepted."
        else -> "Link request failed: ${failure.javaClass.simpleName}."
    }

    private fun drawPinnedServerMigration(page: LinearLayout) {
        val pinResult = runCatching { store.pinnedServerIdentity() }
        val pin = pinResult.getOrNull()
        card(page) {
            addView(text("Future Tailscale migration", 18, color(R.color.paper), bold = true))
            addView(text(
                "The current USB URL stays active until you explicitly verify a candidate. Tailscale must already be configured on both devices; this app does not install or enable a VPN. The candidate must use HTTPS, a *.ts.net hostname, Tailscale-only DNS addresses, and this same pinned Mac P-256 identity. The device token is not sent until verification succeeds.",
                13, color(R.color.muted),
            ).apply { setPadding(0, dp(6), 0, dp(10)) })
            if (pinResult.isFailure) {
                addView(text(
                    "The saved Mac identity pin could not be verified locally. Host migration is disabled.",
                    13, color(R.color.danger),
                ))
            } else if (pin == null) {
                addView(text(
                    "This existing pairing has no saved server pin yet. The first explicit migration attempt will authenticate a one-time pin bootstrap over the current USB connection.",
                    13, color(R.color.warning),
                ))
                addView(actionButton(
                    if (isBusy) "Authenticating USB server…" else "Pin Mac identity over USB",
                    R.color.signal,
                ) {
                    runRequest("Authenticating the current USB Mac identity…", {
                        api.bootstrapPinnedServerIdentity()
                    }) { pinned ->
                        statusMessage = "Mac identity pinned over the current USB session. The URL and device token were not changed."
                        errorMessage = ""
                    }
                })
            } else {
                addView(labelValue("PINNED MAC IDENTITY", pin.fingerprint))
                val input = EditText(this@AdvancedActivity).apply {
                    setSingleLine(true)
                    inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
                    setTextColor(color(R.color.paper))
                    setHintTextColor(color(R.color.muted))
                    hint = "https://mac.tailnet.ts.net"
                    textSize = 15f
                    setPadding(dp(12), dp(12), dp(12), dp(12))
                    background = rounded(color(R.color.ink_0), 12, color(R.color.line))
                    setText(candidateTailscaleUrlDraft)
                    isEnabled = !isBusy
                    isSaveEnabled = false
                    contentDescription = "Candidate Tailscale HTTPS URL"
                    addTextChangedListener(SimpleTextWatcher { candidateTailscaleUrlDraft = it })
                }
                addView(input, LinearLayout.LayoutParams(-1, dp(52)))
                gap(this, 8)
                addView(actionButton(if (isBusy) "Verifying Mac identity…" else "Verify and switch", R.color.signal) {
                    val candidate = candidateTailscaleUrlDraft.trim()
                    if (candidate.isBlank()) {
                        errorMessage = "Enter the Mac's Tailscale HTTPS hostname."
                        render()
                    } else {
                        runRequest("Verifying the pinned Mac identity over HTTPS…", {
                            api.migrateBaseUrlToTailscale(candidate)
                        }) { verifiedUrl ->
                            candidateTailscaleUrlDraft = ""
                            statusMessage = "The same Mac identity was verified. The saved URL changed; the encrypted device token was preserved."
                            activeTasks = null
                            approvals = null
                        }
                    }
                })
            }
            if (pinResult.isFailure) {
                addView(text("Do not remove the current USB pairing to recover a pin error.", 12, color(R.color.muted)).apply {
                    setPadding(0, dp(8), 0, 0)
                })
            }
        }
    }

    private fun requestCameraAndScan() {
        if (isBusy) return
        errorMessage = ""
        statusMessage = "Opening the Google Play services QR scanner…"
        render()
        val options = GmsBarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .enableAutoZoom()
            .build()
        GmsBarcodeScanning.getClient(this, options).startScan()
            .addOnSuccessListener { barcode ->
                val payload = barcode.rawValue
                if (payload.isNullOrBlank()) {
                    statusMessage = ""
                    errorMessage = "The scanner did not return QR contents. Scan the Mac invitation again."
                    render()
                } else {
                    handleInvitationQr(payload)
                }
            }
            .addOnCanceledListener {
                statusMessage = "QR scan canceled."
                render()
            }
            .addOnFailureListener {
                statusMessage = ""
                errorMessage = "Google Play services could not start the QR scanner. Update Play services and try again."
                render()
            }
    }

    /** Runs the shared Python-signed fixture through the production QR parser on-device. */
    private fun runInvitationVectorSelfTest() {
        try {
            val raw = assets.open("pairing_invitation_vector.test.json")
                .bufferedReader(Charsets.UTF_8).use { it.readText() }
            val json = JSONObject(raw)
            val fields = listOf(
                "challenge", "expires_at", "invitation_id", "protocol",
                "server_fingerprint", "server_url", "server_public_key",
            ).associateWith { json.getString(it) }
            val canonicalHash = MessageDigest.getInstance("SHA-256")
                .digest(PairingInvitationSignature.canonicalBytes(fields))
                .joinToString("") { "%02x".format(it.toInt() and 0xff) }
            check(canonicalHash == "fa649db2ad3ec1e3bc17a94534738176a8726ba70e0cfd0d7bd8227ccf219fbf") {
                "Kotlin canonical bytes differ from the shared Python vector"
            }
            val parsed = api.parseInvitationPayload(raw)
            check(parsed.serverFingerprint == json.getString("server_fingerprint")) {
                "Verified server fingerprint differs from the fixed vector"
            }
            check(PairingInvitationSignature.verify(
                fields + ("server_url" to "http://localhost:8001/"),
                json.getString("server_public_key"),
                json.getString("server_fingerprint"),
                json.getString("server_signature"),
            ) == PairingInvitationSignature.Failure.SIGNATURE_MISMATCH) {
                "Tampered invitation unexpectedly verified"
            }
            statusMessage = "Python/Kotlin QR signature vector passed on this phone."
            errorMessage = ""
        } catch (failure: Exception) {
            statusMessage = ""
            errorMessage = "QR signature self-test failed: ${failure.message ?: "verification error"}"
        }
        render()
    }

    private fun handleInvitationQr(rawPayload: String) {
        val parsed = runCatching { api.parseInvitationPayload(rawPayload) }
        parsed.onSuccess { scanned ->
            invitation = scanned
            pairingSession = null
            pairingStatus = null
            errorMessage = ""
            statusMessage = "Invitation loaded. Review the Mac fingerprint before requesting pairing."
        }.onFailure { failure ->
            invitation = null
            pairingSession = null
            pairingStatus = null
            statusMessage = ""
            errorMessage = failure.message ?: "This QR is not a valid Jarvis invitation."
        }
        render()
    }

    private fun resetLocalInvitation(message: String) {
        pairingPollHandler.removeCallbacks(pairingPollRunnable)
        invitation = null
        pairingSession = null
        pairingStatus = null
        errorMessage = ""
        statusMessage = message
        render()
    }

    private fun reverseCommand(serverUrl: String): String {
        val uri = runCatching { java.net.URI(serverUrl) }.getOrNull()
        val port = uri?.port?.takeIf { it > 0 } ?: if (uri?.scheme == "https") 443 else 80
        return "adb reverse tcp:" + port + " tcp:" + port
    }

    private fun schedulePairingPoll(delayMs: Long) {
        pairingPollHandler.removeCallbacks(pairingPollRunnable)
        if (screen == Screen.DEVICE && pairingSession != null && pairingStatus is PairingStatus.Pending) {
            pairingPollHandler.postDelayed(pairingPollRunnable, delayMs)
        }
    }

    private fun refreshPairingStatus() {
        val session = pairingSession ?: return
        if (screen != Screen.DEVICE || isBusy) return
        pairingPollHandler.removeCallbacks(pairingPollRunnable)
        runRequest("Checking for Mac confirmation…", { api.getPairingStatus(session.invitationId) }) { result ->
            pairingStatus = result
            when (result) {
                PairingStatus.Pending -> schedulePairingPoll(PAIRING_POLL_INTERVAL_MS)
                PairingStatus.Rejected -> {
                    pairingPollHandler.removeCallbacks(pairingPollRunnable)
                    statusMessage = ""
                    errorMessage = "The Mac rejected this device request. Scan a new invitation to try again."
                }
                PairingStatus.Expired -> {
                    pairingPollHandler.removeCallbacks(pairingPollRunnable)
                    statusMessage = ""
                    errorMessage = "This invitation expired. Create a fresh QR on the Mac and scan it again."
                }
                is PairingStatus.Approved -> {
                    pairingPollHandler.removeCallbacks(pairingPollRunnable)
                    runRequest("Mac confirmed this device. Claiming its expiring token…", {
                        api.claimPairing(
                            session.invitationId,
                            result.claimChallenge,
                            invitation?.serverPublicKeyBase64 ?: throw ApiException("Verified server identity is missing."),
                            invitation?.serverFingerprint ?: throw ApiException("Verified server fingerprint is missing."),
                        )
                    }) {
                        pairingSession = null
                        pairingStatus = null
                        invitation = null
                        screen = Screen.TASKS
                        activeTasks = null
                        approvals = null
                        statusMessage = "Phone paired. Task and approval data will load from the Mac."
                        refreshCurrent()
                    }
                }
            }
        }
    }

    private fun drawNeedsPair(page: LinearLayout, detail: String) {
        drawEmpty(page, "Connect your phone first", detail)
        page.addView(actionButton("Open device pairing", R.color.signal) {
            screen = Screen.DEVICE
            errorMessage = ""
            render()
        })
    }

    private fun drawLoading(page: LinearLayout, label: String) {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(16), dp(18), dp(16), dp(18))
            background = rounded(color(R.color.ink_2), 16, color(R.color.line))
        }
        row.addView(ProgressBar(this).apply { isIndeterminate = true }, LinearLayout.LayoutParams(dp(24), dp(24)))
        row.addView(text(label, 14, color(R.color.muted)).apply { setPadding(dp(12), 0, 0, 0) })
        page.addView(row, LinearLayout.LayoutParams(-1, -2))
    }

    private fun drawEmpty(page: LinearLayout, title: String, detail: String) {
        card(page) {
            addView(text(title, 19, color(R.color.paper), bold = true))
            addView(text(detail, 14, color(R.color.muted)).apply { setPadding(0, dp(7), 0, 0) })
        }
    }

    private fun refreshCurrent() {
        if (!isPaired()) {
            render()
            return
        }
        errorMessage = ""
        when (screen) {
            Screen.TASKS -> loadTasks("active")
            Screen.APPROVALS -> loadApprovals()
            Screen.ACTIVITY -> loadActivity()
            Screen.COMPLETED -> loadTasks("completed")
            Screen.TASK_DETAIL -> selectedTask?.let { loadTaskDetail(it.id) }
            Screen.APPROVAL_DETAIL -> refreshSelectedApproval()
            Screen.DEVICE -> Unit
        }
    }

    private fun loadTasks(state: String) {
        runRequest("Loading $state tasks…", { api.getTasks(state) }) { tasks ->
            if (state == "active") activeTasks = tasks else completedTasks = tasks
        }
    }

    private fun loadApprovals() {
        runRequest("Loading approval requests…", { api.getApprovals() }) { list ->
            approvals = list
        }
    }

    private fun loadActivity() {
        runRequest("Loading recent task activity…", {
            api.getTasks("active") to api.getTasks("completed")
        }) { (active, completed) ->
            activeTasks = active
            completedTasks = completed
        }
    }

    private fun loadTaskDetail(taskId: String) {
        runRequest("Loading task event history…", { api.getTaskDetail(taskId) }) { detail ->
            taskDetail = detail
            selectedTask = detail.task
        }
    }

    private fun refreshSelectedApproval() {
        val selected = selectedApproval ?: run {
            screen = Screen.APPROVALS
            loadApprovals()
            return
        }
        runRequest("Refreshing exact operation…", { api.getApprovals() }) { latest ->
            approvals = latest
            val found = latest.firstOrNull { it.id == selected.id }
            if (found == null) {
                selectedApproval = null
                screen = Screen.APPROVALS
                statusMessage = "This approval is no longer pending."
            } else {
                selectedApproval = found
                statusMessage = if (found.operationDigest == selected.operationDigest) {
                    "Approval details refreshed from the Mac."
                } else {
                    "The operation changed. Review the refreshed details before deciding."
                }
            }
        }
    }

    private fun confirmDecision(approval: Approval, decision: String) {
        val verb = if (decision == "approve") "Approve once" else "Reject request"
        val summary = "Operation: " + approval.operation +
            "\nTarget: " + approval.targetResource +
            "\nFingerprint: " + approval.operationDigest +
            "\n\nThe Mac will verify the exact operation fingerprint before applying this decision."
        val dialog = AlertDialog.Builder(this)
            .setTitle(if (decision == "approve") "Approve this operation?" else "Reject this request?")
            .setMessage(summary)
            .setNegativeButton("Cancel", null)
            .setPositiveButton(verb) { _, _ -> submitDecision(approval, decision) }
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE)?.requestFocus()
        }
        dialog.show()
    }

    private fun submitDecision(approval: Approval, decision: String) {
        runRequest("Submitting your decision…", {
            val current = api.getApprovals().firstOrNull { it.id == approval.id }
                ?: throw ApiException("This approval is no longer pending. Refresh the list.")
            if (current.operationDigest != approval.operationDigest ||
                current.operation != approval.operation ||
                current.targetResource != approval.targetResource ||
                current.parametersSummary != approval.parametersSummary
            ) {
                throw ApiException("The operation changed after you opened it. Review the refreshed details before deciding.")
            }
            api.decide(current, decision)
            api.getApprovals()
        }) { remaining ->
            approvals = remaining
            selectedApproval = null
            screen = Screen.APPROVALS
            statusMessage = if (decision == "approve") {
                "Approval decision recorded. Execution status will update in Tasks."
            } else {
                "Rejection recorded. This operation will not be approved by this request."
            }
        }
    }

    private fun <T> runRequest(label: String, action: () -> T, onSuccess: (T) -> Unit) {
        if (isBusy) return
        val requestScreen = screen
        val requestPairingStatus = pairingStatus
        isBusy = true
        errorMessage = ""
        statusMessage = label
        render()
        worker.execute {
            val result = runCatching(action)
            runOnUiThread {
                if (destroyed) return@runOnUiThread
                isBusy = false
                result.onSuccess { value ->
                    statusMessage = ""
                    onSuccess(value)
                }.onFailure { failure ->
                    if (failure is ApiException && failure.statusCode == 401) {
                        statusMessage = ""
                        if (requestScreen == Screen.DEVICE) {
                            errorMessage = "The Mac rejected this local pairing request (HTTP 401). No automatic retry was sent."
                        } else {
                            store.clearPairing()
                            errorMessage = "The Mac rejected this device token (HTTP 401). Pair this phone again."
                        }
                    } else if (requestScreen == Screen.DEVICE && requestPairingStatus is PairingStatus.Approved) {
                        pairingPollHandler.removeCallbacks(pairingPollRunnable)
                        invitation = null
                        pairingSession = null
                        pairingStatus = null
                        statusMessage = ""
                        errorMessage = "The Mac confirmed this phone, but the one-time token claim did not complete. Do not reuse this invitation; create a fresh one on the Mac."
                    } else {
                        errorMessage = failure.message ?: "Jarvis could not complete the request."
                        statusMessage = ""
                    }
                }
                render()
            }
        }
    }

    private fun isPaired(): Boolean = !store.token().isNullOrBlank() && store.deviceId().isNotBlank()

    private fun sectionEyebrow(parent: LinearLayout, title: String) {
        parent.addView(text(title, 11, color(R.color.signal), bold = true).apply {
            letterSpacing = 0.09f
            setPadding(dp(1), dp(8), 0, dp(10))
        })
    }

    private fun labelValue(label: String, value: String): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(8), 0, dp(3))
        }
        box.addView(text(label, 10, color(R.color.muted), bold = true).apply { letterSpacing = 0.05f })
        box.addView(text(value, 14, color(R.color.paper)).apply {
            setPadding(0, dp(3), 0, 0)
            setTextIsSelectable(true)
        })
        return box
    }

    private fun card(parent: LinearLayout, compact: Boolean = false, content: LinearLayout.() -> Unit) {
        val surface = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(if (compact) 14 else 16), dp(if (compact) 12 else 16), dp(if (compact) 14 else 16), dp(if (compact) 12 else 16))
            background = rounded(color(R.color.ink_2), 18, color(R.color.line))
            content()
        }
        parent.addView(surface, LinearLayout.LayoutParams(-1, -2))
    }

    private fun actionButton(label: String, accent: Int, secondary: Boolean = false, onClick: () -> Unit): Button {
        return Button(this).apply {
            text = label
            textSize = 15f
            isAllCaps = false
            minHeight = dp(48)
            setPadding(dp(12), dp(8), dp(12), dp(8))
            setTextColor(color(if (secondary) accent else R.color.ink_0))
            val fill = if (secondary) color(R.color.ink_3) else color(accent)
            background = RippleDrawable(
                ColorStateList.valueOf(color(R.color.signal_pressed)),
                rounded(fill, 13, if (secondary) color(accent) else fill),
                rounded(color(R.color.paper), 13, color(R.color.paper)),
            )
            isEnabled = !isBusy
            alpha = if (isEnabled) 1f else 0.58f
            setOnClickListener { if (!isBusy) onClick() }
        }
    }

    private fun smallAction(label: String, onClick: () -> Unit): Button {
        return Button(this).apply {
            text = label
            textSize = 12f
            isAllCaps = false
            minHeight = dp(42)
            setTextColor(color(R.color.signal))
            background = rounded(color(R.color.ink_2), 12, color(R.color.line))
            isEnabled = !isBusy
            setOnClickListener { if (!isBusy) onClick() }
        }
    }

    private fun messageBanner(message: String, isError: Boolean): View {
        val tone = if (isError) color(R.color.danger) else color(R.color.signal)
        return TextView(this).apply {
            text = message
            textSize = 13f
            setTextColor(color(R.color.paper))
            setPadding(dp(16), dp(11), dp(16), dp(11))
            background = rounded(color(if (isError) R.color.error_surface else R.color.notice_surface), 12, tone)
            contentDescription = (if (isError) "Error: " else "Status: ") + message
            setPadding(dp(20), dp(11), dp(20), dp(11))
        }.also {
            it.layoutParams = LinearLayout.LayoutParams(-1, -2).apply {
                setMargins(dp(20), dp(2), dp(20), dp(4))
            }
        }
    }

    private fun text(value: String, sizeSp: Int, textColor: Int, bold: Boolean = false, mono: Boolean = false): TextView {
        return TextView(this).apply {
            text = value
            textSize = sizeSp.toFloat()
            setTextColor(textColor)
            if (bold) setTypeface(typeface, Typeface.BOLD)
            if (mono) typeface = Typeface.create("monospace", Typeface.NORMAL)
            gravity = Gravity.START
            setLineSpacing(dp(2).toFloat(), 1f)
            breakStrategy = android.text.Layout.BREAK_STRATEGY_SIMPLE
        }
    }

    private fun gap(parent: LinearLayout, height: Int) {
        parent.addView(View(this), LinearLayout.LayoutParams(1, dp(height)))
    }

    private fun rounded(fill: Int, radiusDp: Int, stroke: Int): GradientDrawable =
        GradientDrawable().apply {
            setColor(fill)
            cornerRadius = dp(radiusDp).toFloat()
            setStroke(dp(1), stroke)
        }

    private fun color(resourceId: Int): Int = getColor(resourceId)
    private fun dp(value: Int): Int = (value * resources.displayMetrics.density + 0.5f).toInt()
    private fun nonempty(value: String, fallback: String): String = if (value.isBlank()) fallback else value

    private fun stateColor(state: String): Int {
        val value = state.lowercase()
        return when {
            "fail" in value || "error" in value || "reject" in value -> color(R.color.danger)
            "complete" in value || "done" in value || "success" in value -> color(R.color.success)
            "wait" in value || "approval" in value || "pending" in value -> color(R.color.warning)
            else -> color(R.color.signal)
        }
    }

    companion object {
        private const val PAIRING_POLL_INTERVAL_MS = 2_500L
        private const val REQUEST_POST_NOTIFICATIONS = 4201
    }
}

private class SimpleTextWatcher(private val onText: (String) -> Unit) : android.text.TextWatcher {
    override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit

    override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
        onText(s?.toString().orEmpty())
    }

    override fun afterTextChanged(s: android.text.Editable?) = Unit
}

