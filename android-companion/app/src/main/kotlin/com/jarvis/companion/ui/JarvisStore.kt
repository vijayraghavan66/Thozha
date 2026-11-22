package com.jarvis.companion.ui

import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.jarvis.companion.presentation.ApprovalDecisionPhase
import com.jarvis.companion.presentation.ApprovalDecisionPresentation
import com.jarvis.companion.presentation.ApprovalDecisionStatus
import com.jarvis.companion.presentation.shouldReconcileApiTask
import com.jarvis.companion.link.LinkBackgroundSync
import com.jarvis.companion.link.LinkService
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.link.AssistantNamePolicy
import com.jarvis.companion.fastpath.FastCommand
import com.jarvis.companion.fastpath.FastCommandClassifier
import com.jarvis.companion.fastpath.DevelopmentIntentClassifier
import com.jarvis.companion.localai.LocalAiManager
import com.jarvis.companion.localai.LocalAiPhase
import com.jarvis.companion.localai.LocalAiRequestMode
import com.jarvis.companion.localai.RequestRoute
import com.jarvis.companion.localai.RequestRoutingPolicy
import com.jarvis.companion.model.ApiException
import com.jarvis.companion.model.TaskArtifact
import com.jarvis.companion.model.Approval
import com.jarvis.companion.data.JarvisApi
import com.jarvis.companion.data.SecureStore
import com.jarvis.companion.telegram.TelegramSession
import com.jarvis.companion.telegram.TelegramBootstrap
import com.jarvis.companion.telegram.TelegramSnapshot
import com.jarvis.link.ApprovalReview
import com.jarvis.link.FastCommandRequest
import com.jarvis.link.LinkStatus
import com.jarvis.link.RelayHttpException
import com.jarvis.link.SafeCapability
import com.jarvis.link.TaskSubmission
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

enum class Tab(val label: String) { HOME("Home"), ACTIVITY("Activity"), APPROVALS("Approvals"), LIVE("Connection") }

enum class Overlay { SETTINGS, HISTORY }

/** States the Mac reports while work is still open. Used for polling and for the presence signal. */
val ActiveStates = setOf("CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL", "WAITING_CONNECTION", "PAUSED", "SENT", "QUEUED", "UNCONFIRMED")

/**
 * Holds everything the Compose screens show. All values come from LinkRuntime, the Telegram session, and the
 * sync that LinkBackgroundSync performs. Nothing here fabricates a task state or an approval.
 *
 * Local state, outbound submissions, and sync use separate lanes; UI state changes stay on the main thread.
 */
class JarvisStore(private val app: Context) {
    private val main = Handler(Looper.getMainLooper())
    private val lanes = ExecutionLanes()
    private val sessions = ChatSessions(app)
    private val localHistory = LocalAiConversationHistory(app)
    private val localAi = LocalAiManager.get(app)
    private val secureStore = SecureStore(app)
    private val jarvisApi = JarvisApi(app, secureStore)
    private val approvalDecisionPrefs = app.getSharedPreferences("jarvis_approval_decisions", Context.MODE_PRIVATE)
    private var destroyed = false

    var tab by mutableStateOf(Tab.HOME)
    var overlay by mutableStateOf<Overlay?>(null)
    var entries by mutableStateOf<List<Map<String, Any>>>(emptyList())
    /** Authenticated task-update envelopes persisted by LinkRuntime; rendered as real activity only. */
    var taskUpdates by mutableStateOf<List<Map<String, Any>>>(emptyList())
    var taskRegistry by mutableStateOf<List<Map<String, Any>>>(emptyList())
    var taskEventHistory by mutableStateOf<List<Map<String, Any>>>(emptyList())
    var approvals by mutableStateOf<List<ApprovalReview>>(emptyList())
    var taskArtifacts by mutableStateOf<Map<String, List<TaskArtifact>>>(emptyMap())
    var artifactLoadingTasks by mutableStateOf<Set<String>>(emptySet())
    var savedArtifacts by mutableStateOf<Map<String, android.net.Uri>>(emptyMap())
    var cancellingRequests by mutableStateOf<Set<String>>(emptySet())
    /** Pending OpenJarvis API approvals, keyed by the authenticated Mac approval ID. */
    private var supervisorApprovals: Map<String, Approval> = emptyMap()
    /** Persisted UI reconciliation only; supervisor remains authoritative for every decision and action. */
    var approvalDecisions by mutableStateOf(loadApprovalDecisions())
    var linkStatus by mutableStateOf<LinkStatus?>(null)
    var lastSyncMs by mutableStateOf(0L)
    var lastSyncError by mutableStateOf("")
    /** Existing loopback OpenJarvis pairing is independent from the experimental Jarvis Link relay identity. */
    var macApiPaired by mutableStateOf(hasMacApiPairing())
    var macApiSyncing by mutableStateOf(false)
    var macApiLastSyncMs by mutableStateOf(0L)
    var macApiSyncError by mutableStateOf("")
    var localWifiPin by mutableStateOf(runCatching { secureStore.localGatewayPin() }.getOrNull())
    var lastMacApiTransport by mutableStateOf(secureStore.lastMacConnection().second)
    var localWifiSetupRunning by mutableStateOf(false)
    var localWifiSetupMessage by mutableStateOf("")
    var assistantName by mutableStateOf(LinkSettings(app).assistantName)
    var telegram by mutableStateOf<TelegramSnapshot?>(null)
    var telegramRemoteEnabled by mutableStateOf(TelegramBootstrap.isRemoteEnabled(app))
    var session by mutableStateOf(sessions.active())
    var sessionList by mutableStateOf(sessions.all())
    var draft by mutableStateOf("")
    var submitting by mutableStateOf(false)
    var syncing by mutableStateOf(false)
    var decisionInFlight by mutableStateOf(false)
    var decisionNote by mutableStateOf(
        approvalDecisions.values.maxByOrNull { it.updatedAtUnixMs }?.let(ApprovalDecisionPresentation::label).orEmpty(),
    )
    private var currentDecisionApprovalId: String? = null
    @Volatile private var lastMacApiProbeAttemptMs = 0L
    @Volatile private var lastSupervisorApprovalPollMs = 0L
    @Volatile private var lastSupervisorDecisionPollMs = 0L
    @Volatile private var cachedSupervisorApprovalPairs = emptyList<Pair<Approval, ApprovalReview>>()
    var notice by mutableStateOf("")
    var now by mutableStateOf(System.currentTimeMillis())
    /** Set by the host activity; asks Android for one runtime permission, on the user's tap only. */
    var permissionRequester: ((String) -> Unit)? = null

    var reduceMotion by mutableStateOf(app.getSharedPreferences("jarvis_ui", Context.MODE_PRIVATE).getBoolean("reduce_motion", false))
    var aiRequestMode by mutableStateOf(loadAiRequestMode())
    var localAiSnapshot by mutableStateOf(localAi.status())
    var localAiRequestRunning by mutableStateOf(false)
    @Volatile private var localAiRequestId: String? = null

    private fun loadAiRequestMode(): LocalAiRequestMode = runCatching {
        LocalAiRequestMode.valueOf(app.getSharedPreferences("jarvis_ai_routing", Context.MODE_PRIVATE).getString("mode", null) ?: "AUTO")
    }.getOrDefault(LocalAiRequestMode.AUTO)

    fun updateAiRequestMode(mode: LocalAiRequestMode) = ui {
        app.getSharedPreferences("jarvis_ai_routing", Context.MODE_PRIVATE).edit().putString("mode", mode.name).apply()
        aiRequestMode = mode
    }

    fun updateAssistantName(value: String): Boolean {
        val normalized = AssistantNamePolicy.normalize(value) ?: return false
        return runCatching {
            LinkSettings(app).assistantName = normalized
            ui { assistantName = normalized }
        }.isSuccess
    }

    /**
     * Authenticates and stores the Mac's local HTTPS gateway pin over the existing paired route.
     * It does not enroll a device or alter the existing device token. Initial verification normally
     * requires the current USB reverse connection so the Mac can prove its identity out of band.
     */
    fun verifyLocalWifiGateway() = ui {
        if (localWifiSetupRunning) return@ui
        if (!hasMacApiPairing()) {
            localWifiSetupMessage = "Pair this phone with the Mac before setting up Wi-Fi."
            return@ui
        }
        if (android.os.Build.VERSION.SDK_INT >= 37 &&
            app.checkSelfPermission("android.permission.ACCESS_LOCAL_NETWORK") != android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            permissionRequester?.invoke("android.permission.ACCESS_LOCAL_NETWORK")
            localWifiSetupMessage = "Allow local network access, then verify again. The USB connection is unchanged."
            return@ui
        }
        localWifiSetupRunning = true
        localWifiSetupMessage = "Verifying the paired Mac's Wi-Fi identity over the current USB/loopback route…"
        background(lanes.local) {
            val result = runCatching { jarvisApi.pinLocalGatewayIdentity() }
            ui {
                localWifiSetupRunning = false
                localWifiPin = result.getOrNull() ?: runCatching { secureStore.localGatewayPin() }.getOrNull()
                localWifiSetupMessage = result.fold(
                    onSuccess = { "Mac identity verified. Wi-Fi requests use the pinned gateway when it is reachable." },
                    onFailure = { failure ->
                        failure.message?.takeIf { it.isNotBlank() }
                            ?: "The Mac Wi-Fi identity could not be verified. Check the paired connection and try again."
                    },
                )
            }
        }
    }

    fun importLocalModel(uri: android.net.Uri, licenseAccepted: Boolean) = ui {
        if (!licenseAccepted || submitting) return@ui
        submitting = true
        notice = "Importing and loading the selected model in app-private storage…"
        background(lanes.local) {
            val result = runCatching {
                val stream = app.contentResolver.openInputStream(uri) ?: error("The selected model file could not be opened.")
                stream.use { localAi.importSelectedModel(it, licenseAccepted = true) }
            }
            ui {
                submitting = false
                localAiSnapshot = localAi.status()
                notice = result.fold(
                    onSuccess = { "Local model imported and loaded. Exact Mac actions still use the supervisor approval system." },
                    onFailure = { localModelError(it) },
                )
            }
        }
    }

    fun loadLocalModel() = ui {
        if (submitting) return@ui
        submitting = true
        notice = "Loading the local model…"
        background(lanes.local) {
            val result = runCatching { localAi.loadInstalledModel() }
            ui {
                submitting = false
                localAiSnapshot = localAi.status()
                notice = result.fold(
                    onSuccess = { "Local model is loaded on this phone." },
                    onFailure = { localModelError(it) },
                )
            }
        }
    }

    fun unloadLocalModel() = ui {
        val result = runCatching { localAi.unload() }
        localAiSnapshot = localAi.status()
        notice = result.fold({ "Local model unloaded from memory." }, { localModelError(it) })
    }

    fun cancelLocalAi() = ui {
        if (!localAiRequestRunning) return@ui
        localAi.cancel()
        localAiRequestId?.let { id ->
            localHistory.recordResult(id, "CANCELLED", "", "Cancelled on this phone.", System.currentTimeMillis())
        }
        localAiRequestRunning = false
        localAiRequestId = null
        entries = mergeEntries(entries, localHistory.all())
        notice = "On-device request cancelled."
    }

    fun updateReduceMotion(value: Boolean) {
        app.getSharedPreferences("jarvis_ui", Context.MODE_PRIVATE).edit().putBoolean("reduce_motion", value).apply()
        reduceMotion = value
    }

    /** Requests in the open conversation, oldest first. */
    fun entriesInSession(): List<Map<String, Any>> =
        entries.filter { sessions.sessionOf(it["message_id"] as? String ?: "") == session.id }

    fun requestCount(sessionId: String): Int =
        entries.count { sessions.sessionOf(it["message_id"] as? String ?: "") == sessionId }

    fun latestRequestText(sessionId: String): String =
        entries.lastOrNull { sessions.sessionOf(it["message_id"] as? String ?: "") == sessionId }
            ?.get("text") as? String ?: ""

    /** Whether any request is still open on the Mac or waiting to be delivered. */
    fun hasActiveWork(): Boolean = entries.any {
        it["transport"] != "local" && (it["state"] as? String) in ActiveStates
    }

    fun hasRunningWork(): Boolean = entries.any {
        it["transport"] != "local" && (it["state"] as? String) in setOf("CREATED", "PLANNING", "RUNNING", "VERIFYING")
    }

    fun refreshLocal() = background {
        val currentWifiPin = runCatching { secureStore.localGatewayPin() }.getOrNull()
        val runtime = LinkService.runtime(app)
        var conversation = runtime.conversation()
        val pairedToMac = hasMacApiPairing()
        var reachableAt = macApiLastSyncMs
        var apiError = macApiSyncError
        var apiApprovalPairs = cachedSupervisorApprovalPairs
        if (pairedToMac) {
            // Reconcile only requests previously submitted by this phone using their original idempotency keys.
            // A lost HTTP response therefore observes the same task rather than creating a duplicate.
            conversation.filter { row -> shouldReconcileApiTask(row, System.currentTimeMillis()) }.forEach { row ->
                val id = row["message_id"] as? String ?: return@forEach
                runCatching { jarvisApi.getLinkTaskStatus(id) }
                    .onSuccess { result ->
                        if (!runtime.reconcileMissingApiTaskResult(id, result.taskId, result.state, result.summary)) {
                            runtime.recordLocalTaskResult(id, result.taskId, result.state, result.summary)
                        }
                    }
                    .onFailure { failure ->
                        // Preserve an ambiguous in-flight state on transport errors; never report false failure.
                        if (failure is ApiException && failure.statusCode == 404) {
                            runtime.recordLocalTaskResult(id, "", "FAILED", "The Mac no longer has this task record.")
                        } else if (failure !is ApiException || failure.statusCode != null) {
                            apiError = "The Mac task status could not be confirmed yet."
                        }
                    }
            }
            val approvalPollTime = System.currentTimeMillis()
            if (approvalPollTime - lastSupervisorApprovalPollMs >= SUPERVISOR_APPROVAL_POLL_INTERVAL_MS) {
                lastSupervisorApprovalPollMs = approvalPollTime
                runCatching { jarvisApi.getApprovalReviews() }
                    .onSuccess {
                        apiApprovalPairs = it
                        cachedSupervisorApprovalPairs = it
                    }
                    .onFailure { failure ->
                        if (failure !is ApiException || failure.statusCode != null) {
                            apiError = failure.message ?: "Pending Mac approvals could not be checked."
                        }
                    }
            }
            val probeTime = System.currentTimeMillis()
            if (probeTime - lastMacApiProbeAttemptMs >= MAC_API_PROBE_INTERVAL_MS) {
                lastMacApiProbeAttemptMs = probeTime
                ui { macApiSyncing = true }
                runCatching { jarvisApi.getTasks("active") }
                    .onSuccess {
                        reachableAt = System.currentTimeMillis()
                        apiError = ""
                    }
                    .onFailure { failure ->
                        apiError = when (failure) {
                            is ApiException -> failure.message ?: "The paired Mac could not be reached."
                            else -> "The paired Mac could not be reached over USB."
                        }
                    }
                ui { macApiSyncing = false }
            }
        }
        conversation = runtime.conversation()
        val pending = runtime.pendingApprovals()
        val combinedApprovals = linkedMapOf<String, ApprovalReview>()
        pending.forEach { combinedApprovals[it.approvalId] = it }
        apiApprovalPairs.forEach { (_, review) -> combinedApprovals[review.approvalId] = review }
        val directApprovalMap = apiApprovalPairs.associate { (approval, _) -> approval.id to approval }
        val updatesSnapshot = runtime.recentUpdates()
        val taskRegistrySnapshot = runtime.taskRegistry()
        val taskEventSnapshot = runtime.taskEventHistory()
        val decisionIds = (approvalDecisions.keys + combinedApprovals.keys).distinct()
        val runtimeDecisionStates = decisionIds.mapNotNull { id ->
            runtime.approvalDecisionState(id)?.let { id to it }
        }.toMap()
        val supervisorDecisionStates = reconcileSupervisorDecisionStates(conversation)
        val status = runtime.status()
        val (syncAt, syncError) = runtime.lastSync()
        val snapshot = TelegramSession.controller?.snapshot()
        val currentApiTransport = secureStore.lastMacConnection().second
        ui {
            localWifiPin = currentWifiPin
            lastMacApiTransport = currentApiTransport
            entries = mergeEntries(conversation, localHistory.all())
            taskUpdates = updatesSnapshot
            taskRegistry = taskRegistrySnapshot
            taskEventHistory = taskEventSnapshot
            localAiSnapshot = localAi.status()
            approvals = combinedApprovals.values.toList()
            supervisorApprovals = directApprovalMap
            reconcileRuntimeDecisionStates(pending, runtimeDecisionStates)
            reconcileSupervisorDecisionResults(supervisorDecisionStates)
            reconcileApprovalDecisions(updatesSnapshot)
            linkStatus = status
            lastSyncMs = syncAt
            lastSyncError = syncError
            macApiPaired = pairedToMac
            macApiLastSyncMs = reachableAt
            macApiSyncError = apiError
            if (snapshot != null) telegram = snapshot
            sessionList = sessions.all()
        }
    }

    /** Telegram reports every change through the session listener; this keeps the latest snapshot. */
    fun onTelegramSnapshot(snapshot: TelegramSnapshot) = ui { telegram = snapshot }

    /** User-controlled remote transport. Disabling it preserves the authenticated TDLib session and local tasks. */
    fun updateTelegramRemoteEnabled(enabled: Boolean) = ui {
        TelegramBootstrap.setRemoteEnabled(app, enabled)
        telegramRemoteEnabled = TelegramBootstrap.isRemoteEnabled(app)
        notice = if (telegramRemoteEnabled) {
            if (telegram == null) "Telegram remote access enabled. Open Telegram setup to sign in if needed."
            else "Telegram remote access enabled."
        } else {
            "Telegram remote access disabled. Local Mac Wi-Fi and USB remain available; Telegram sign-in data was kept."
        }
    }

    /** Retries queued sends and polls the Mac for updates. Safe to call repeatedly; only one sync runs at a time. */
    fun loadTaskArtifacts(taskId: String) = ui {
        if (!taskId.matches(Regex("[0-9a-f]{32}")) || taskId in artifactLoadingTasks) return@ui
        val traceId = entries.firstOrNull { it["task_id"] == taskId }
            ?.get("message_id") as? String
        val trace = traceId?.let(PerformanceTrace::requestId).orEmpty()
        artifactLoadingTasks = artifactLoadingTasks + taskId
        background(lanes.sync) {
            val startedNs = System.nanoTime()
            if (trace.isNotBlank()) PerformanceTrace.mark(trace, "artifact_list_start")
            val result = runCatching { jarvisApi.getTaskArtifacts(taskId, trace.takeIf { it.isNotBlank() }) }
            ui {
                artifactLoadingTasks = artifactLoadingTasks - taskId
                result.onSuccess { artifacts ->
                    taskArtifacts = taskArtifacts + (taskId to artifacts)
                    if (trace.isNotBlank()) PerformanceTrace.mark(
                        trace,
                        "artifact_list_ready",
                        java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedNs),
                        "count=${artifacts.size}",
                    )
                    notice = if (artifacts.isEmpty()) "No downloadable archive is recorded for this completed task."
                    else "${artifacts.size} verified project archive${if (artifacts.size == 1) "" else "s"} available."
                }.onFailure { failure -> notice = deliveryError(failure) }
            }
        }
    }

    fun saveTaskArtifact(artifact: TaskArtifact, destination: android.net.Uri) = ui {
        notice = "Downloading and verifying ${artifact.fileName}…"
        val requestId = entries.firstOrNull { it["task_id"] == artifact.taskId }
            ?.get("message_id") as? String
        val trace = requestId?.let(PerformanceTrace::requestId).orEmpty()
        background(lanes.sync) {
            val saveStartedNs = System.nanoTime()
            if (trace.isNotBlank()) PerformanceTrace.mark(trace, "artifact_save_start")
            val result = runCatching {
                val bytes = jarvisApi.downloadTaskArtifact(artifact, trace.takeIf { it.isNotBlank() })
                val output = app.contentResolver.openOutputStream(destination, "wt")
                    ?: error("Android could not open the selected save location.")
                output.use { it.write(bytes) }
                val savedDigest = app.contentResolver.openInputStream(destination)?.use { input ->
                    val digest = java.security.MessageDigest.getInstance("SHA-256")
                    val buffer = ByteArray(32 * 1024)
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        digest.update(buffer, 0, count)
                    }
                    digest.digest().joinToString("") { "%02x".format(it) }
                } ?: error("Android could not verify the saved archive.")
                check(savedDigest == artifact.archiveSha256) { "The saved archive failed its SHA-256 check." }
                if (trace.isNotBlank()) PerformanceTrace.mark(
                    trace,
                    "artifact_document_verified",
                    java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - saveStartedNs),
                    "bytes=${bytes.size}",
                )
                bytes.size
            }
            ui {
                    if (result.isSuccess) savedArtifacts = savedArtifacts + (artifact.artifactId to destination)
                notice = result.fold(
                    onSuccess = { "Saved ${artifact.fileName} (${it} bytes). SHA-256 verified." },
                    onFailure = { "Project download failed: ${deliveryError(it)}" },
                )
            }
        }
    }

    /** Cancels one task created through the paired Mac API; no approval is created or replayed. */
    fun cancelLinkTask(requestId: String) = ui {
        if (!requestId.matches(Regex("[A-Za-z0-9._~-]{16,128}")) || requestId in cancellingRequests) return@ui
        val entry = entries.firstOrNull { it["message_id"] == requestId }
        if (entry?.get("transport") != "api") {
            notice = "This task is not on the paired Mac API route and cannot be cancelled from this control."
            return@ui
        }
        cancellingRequests = cancellingRequests + requestId
        notice = "Requesting cancellation from the Mac…"
        background(lanes.sync) {
            val result = runCatching { jarvisApi.cancelLinkTask(requestId) }
            result.onSuccess { task ->
                runCatching { LinkService.runtime(app).recordLocalTaskResult(requestId, task.taskId, task.state, task.summary) }
            }
            ui {
                cancellingRequests = cancellingRequests - requestId
                notice = result.fold(
                    onSuccess = { it.summary.ifBlank { "The Mac confirmed cancellation." } },
                    onFailure = { "Cancellation could not be confirmed: ${deliveryError(it)}" },
                )
            }
            refreshLocal()
        }
    }

    /** Explicitly reconciles one ambiguous local API request without creating a duplicate task. */
    fun reconcileLinkTask(requestId: String) = ui {
        if (!requestId.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) return@ui
        notice = "Checking the original task ID with the Mac…"
        background(lanes.sync) {
            val result = runCatching { jarvisApi.getLinkTaskStatus(requestId) }
            result.onSuccess { task ->
                runCatching {
                    val runtime = LinkService.runtime(app)
                    if (!runtime.reconcileMissingApiTaskResult(requestId, task.taskId, task.state, task.summary)) {
                        runtime.recordLocalTaskResult(requestId, task.taskId, task.state, task.summary)
                    }
                }
            }
            ui {
                notice = result.fold(
                    onSuccess = { "Mac status confirmed: ${it.state}." },
                    onFailure = { "Mac status is still unconfirmed: ${deliveryError(it)}" },
                )
            }
            refreshLocal()
        }
    }

    fun syncNow() = ui {
        if (syncing) return@ui
        syncing = true
        background(lanes.sync) {
            if (LinkSettings(app).mode == com.jarvis.companion.link.LinkMode.TEST_RELAY) {
                runCatching { LinkBackgroundSync.run(app) }
            }
            if (hasMacApiPairing()) lastMacApiProbeAttemptMs = 0L
            ui { syncing = false }
            refreshLocal()
        }
    }

    /** Sends the composer text through the same signed Link path that Telegram carries when connected. */
    fun submit() = ui {
        val text = draft.trim()
        if (submitting || text.isEmpty()) return@ui
        if (text.toByteArray(Charsets.UTF_8).size > 4096) {
            notice = "Requests can be up to 4,096 bytes."
            return@ui
        }
        val requestId = UUID.randomUUID().toString().replace("-", "")
        val fastCommand = FastCommandClassifier.classify(text)
        val selectedAiMode = aiRequestMode
        val route = RequestRoutingPolicy.route(
            requestId = requestId,
            prompt = text,
            mode = selectedAiMode,
            // A model must be loaded and verified before AUTO may use the local backend.
            localModelReady = localAi.status().phase == LocalAiPhase.READY,
            isExactFastCommand = fastCommand != null,
        )
        if (route is RequestRoute.Rejected) {
            notice = route.message
            return@ui
        }
        val directReadOnlyCommand = fastCommand is FastCommand.MacStatus ||
            fastCommand is FastCommand.MacBatteryStatus || fastCommand is FastCommand.MacSystemInfo
        val usePairedMacApi = hasMacApiPairing() && (
            directReadOnlyCommand || (fastCommand == null && route is RequestRoute.Mac)
        )
        val requestTrace = PerformanceTrace.requestId(requestId)
        val submitStartedNs = System.nanoTime()
        PerformanceTrace.mark(requestTrace, "send_pressed", detail = "route=${route.javaClass.simpleName}")
        val transport = when {
            usePairedMacApi -> "api"
            TelegramSession.activeTunnel() != null -> "telegram"
            else -> "link"
        }
        val conversation = session
        sessions.assign(requestId, conversation.id)
        sessions.setTitleIfBlank(conversation.id, text)
        sessionList = sessions.all()
        submitting = true
        draft = ""
        notice = ""
        background(if (route is RequestRoute.Local) lanes.local else lanes.send) {
            if (route is RequestRoute.Local) {
                val requestStored = runCatching {
                    localHistory.recordRequest(requestId, text, System.currentTimeMillis())
                    LinkService.runtime(app).recordLocalTaskRequest(requestId, text, transport = "api", executionTarget = "PHONE")
                    LinkService.runtime(app).recordTaskLifecycle(requestId, "", "RUNNING", "Running on this phone", "PHONE", "$requestId:phone-running")
                }
                if (requestStored.isFailure) {
                    ui {
                        submitting = false
                        draft = text
                        notice = "Jarvis could not save this on-device request."
                    }
                    return@background
                }
                localAiRequestId = requestId
                ui {
                    localAiRequestRunning = true
                    entries = mergeEntries(entries, localHistory.all())
                }
                val localOutputProduced = java.util.concurrent.atomic.AtomicBoolean(false)
                val outcome = runCatching {
                    localAi.generate(text) { chunk -> if (chunk.isNotEmpty()) localOutputProduced.set(true) }
                }
                val fallback = if (
                    outcome.isFailure &&
                    localAi.status().phase != LocalAiPhase.CANCELLED
                ) {
                    RequestRoutingPolicy.routeAfterLocalFailure(
                        requestId,
                        selectedAiMode,
                        partialOutputProduced = localOutputProduced.get(),
                    )
                } else null
                if (fallback is RequestRoute.Mac) {
                    // No local output escaped, so AUTO can safely retry the same
                    // request ID through the paired Mac path without a duplicate bubble.
                    val removed = runCatching { localHistory.remove(requestId) }.isSuccess
                    if (removed) {
                        localAiRequestId = null
                        ui {
                            localAiRequestRunning = false
                            localAiSnapshot = localAi.status()
                            entries = mergeEntries(entries, localHistory.all())
                            notice = "Local inference was unavailable before producing an answer. Sending this request to the Mac."
                        }
                        val runtime = LinkService.runtime(app)
                        val stored = runCatching { runtime.recordLocalTaskRequest(requestId, text, transport = transport) }
                        if (stored.isFailure) {
                            ui {
                                submitting = false
                                draft = text
                                notice = "Jarvis could not save the fallback request on your phone."
                            }
                            return@background
                        }
                        val delivery = runCatching {
                            runtime.submit(
                                LinkSettings(app).relayUrl,
                                TaskSubmission(text, setOf(SafeCapability.ASSISTANT), clientRequestId = requestId),
                                null,
                            )
                        }
                        delivery.onSuccess { result ->
                            runtime.recordLocalTaskResult(
                                requestId,
                                result.messageId,
                                if (result.queuedOffline) "QUEUED" else "SENT",
                                "",
                            )
                        }.onFailure { failure ->
                            val unresolved = failure is ApiException && failure.statusCode == null
                            runtime.recordLocalTaskResult(
                                requestId,
                                "",
                                if (unresolved) "RUNNING" else "FAILED",
                                deliveryError(failure),
                            )
                        }
                        ui { submitting = false }
                        refreshLocal()
                        syncNow()
                        return@background
                    }
                }
                val persisted = outcome.fold(
                    onSuccess = { answer ->
                        runCatching { localHistory.recordResult(requestId, "COMPLETED", answer, answer.take(400), System.currentTimeMillis()) }
                        runCatching { LinkService.runtime(app).recordTaskLifecycle(requestId, "", "COMPLETED", answer.take(400), "PHONE", "$requestId:phone-completed") }
                        ui { notice = "Answered on this phone using the loaded local model." }
                        "COMPLETED"
                    },
                    onFailure = { failure ->
                        val cancelled = localAi.status().phase == LocalAiPhase.CANCELLED
                        runCatching {
                            localHistory.recordResult(
                                requestId,
                                if (cancelled) "CANCELLED" else "FAILED",
                                "",
                                if (cancelled) "Cancelled on this phone." else localModelError(failure),
                                System.currentTimeMillis(),
                            )
                        }
                        runCatching { LinkService.runtime(app).recordTaskLifecycle(requestId, "", if (cancelled) "CANCELLED" else "FAILED", if (cancelled) "Cancelled on this phone." else localModelError(failure), "PHONE", "$requestId:phone-${if (cancelled) "cancelled" else "failed"}") }
                        if (!cancelled) ui { notice = localModelError(failure) }
                        if (cancelled) "CANCELLED" else "FAILED"
                    },
                )
                localAiRequestId = null
                ui {
                    localAiRequestRunning = false
                    submitting = false
                    localAiSnapshot = localAi.status()
                    entries = mergeEntries(entries, localHistory.all())
                    if (localHistory.all().firstOrNull { it["message_id"] == requestId }?.get("state") != persisted) {
                        notice = "The local task finished, but its result could not be saved on this phone."
                    }
                }
                return@background
            }
            val runtime = LinkService.runtime(app)
            val capabilityScope = DevelopmentIntentClassifier.capabilityScope(text)
            val outboxStartedNs = System.nanoTime()
            PerformanceTrace.mark(requestTrace, "outbox_insert_start")
            if (runCatching {
                    runtime.recordLocalTaskRequest(
                        requestId,
                        text,
                        transport = transport,
                        capabilityScope = capabilityScope,
                        lookupLinkStatus = !directReadOnlyCommand,
                    )
                }.isFailure
            ) {
                ui {
                    submitting = false
                    draft = text
                    notice = "Jarvis could not save this request on your phone."
                }
                return@background
            }
            PerformanceTrace.mark(
                requestTrace,
                "outbox_inserted",
                java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - outboxStartedNs),
            )
            // Show the user's bubble as pending now, not after the network call returns.
            refreshLocal()
            if (usePairedMacApi) {
                val apiStartedNs = System.nanoTime()
                PerformanceTrace.mark(requestTrace, "mac_api_request_start", detail = "transport=$transport")
                runCatching {
                    if (directReadOnlyCommand) {
                        Triple(requestId, "COMPLETED", jarvisApi.getMacSystemInfo(requestTrace))
                    } else {
                        val result = jarvisApi.submitLinkTask(text, requestId, capabilityScope, requestTrace)
                        Triple(result.taskId, result.state, result.summary)
                    }
                }
                    .onSuccess { (taskId, state, summary) ->
                        PerformanceTrace.mark(
                            requestTrace,
                            "mac_api_response",
                            java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - apiStartedNs),
                            "state=$state",
                        )
                        val resultPersistStartedNs = System.nanoTime()
                        runtime.recordLocalTaskResult(requestId, taskId, state, summary)
                        PerformanceTrace.mark(
                            requestTrace,
                            "result_persisted",
                            java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - resultPersistStartedNs),
                            "state=$state",
                        )
                        ui {
                            macApiLastSyncMs = System.currentTimeMillis()
                            macApiSyncError = ""
                            if (capabilityScope == "development") {
                                notice = "Development request submitted. No project files change until you approve its exact plan."
                            }
                        }
                        android.util.Log.i(
                            "JarvisStore",
                            "mac_api_task_result trace=$requestTrace task_id=${taskId.take(8)} state=$state elapsed_ms=${java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - submitStartedNs)}",
                        )
                        PerformanceTrace.mark(
                            requestTrace,
                            "android_result_state_updated",
                            java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - submitStartedNs),
                            "state=$state",
                        )
                    }
                    .onFailure { failure ->
                        val unresolved = failure is ApiException && failure.statusCode == null
                        val site = failure.stackTrace.firstOrNull()?.let { "${it.className.substringAfterLast('.')}.${it.methodName}" } ?: "unknown"
                        val status = (failure as? ApiException)?.statusCode
                        PerformanceTrace.mark(
                            requestTrace,
                            "mac_api_failed",
                            java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - apiStartedNs),
                            "status=${status ?: 0}",
                        )
                        android.util.Log.w(
                            "JarvisStore",
                            "mac_api_task_failed trace=$requestTrace type=${failure.javaClass.simpleName} site=$site status=$status elapsed_ms=${java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - submitStartedNs)}",
                        )
                        runtime.recordLocalTaskResult(requestId, "", if (unresolved) "RUNNING" else "FAILED", deliveryError(failure))
                        if (unresolved) lastMacApiProbeAttemptMs = 0L
                    }
                ui { submitting = false }
                refreshLocal()
                return@background
            }
            val delivery = runCatching {
                val relayUrl = LinkSettings(app).relayUrl
                when (route) {
                    is RequestRoute.FastCommandUpstream -> when (fastCommand) {
                    FastCommand.MacStatus -> runtime.submitFastCommand(relayUrl, FastCommandRequest.MacStatus, requestId)
                    FastCommand.MacBatteryStatus -> runtime.submitFastCommand(relayUrl, FastCommandRequest.MacBatteryStatus, requestId)
                    is FastCommand.TaskStatus -> runtime.submitFastCommand(relayUrl, FastCommandRequest.TaskStatus(fastCommand.taskId), requestId)
                    is FastCommand.OpenApplication -> runtime.submitFastCommand(
                        relayUrl,
                        FastCommandRequest.OpenApplication(fastCommand.appId.bundleId, requestId),
                        requestId,
                    )
                    FastCommand.MacSystemInfo -> error("Read-only Mac system info is handled by the paired Mac API")
                    null -> error("Fast-command route lost its deterministic command")
                    }
                    is RequestRoute.Mac -> runtime.submit(relayUrl, TaskSubmission(text, setOf(SafeCapability.ASSISTANT), clientRequestId = requestId), null)
                    is RequestRoute.Local -> error("Local route is unavailable until a verified local model is loaded")
                    is RequestRoute.Rejected -> error("Rejected route reached the send executor")
                }
            }
            delivery.onSuccess { result ->
                android.util.Log.i("JarvisStore", "submit_enqueued trace=$requestTrace transport=$transport elapsed_ms=${java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - submitStartedNs)} queued=${result.queuedOffline}")
                if (result.messageId != requestId) {
                    runtime.recordLocalTaskResult(requestId, "", "FAILED", "Jarvis could not correlate this request safely. Send it again.")
                } else {
                    runtime.recordLocalTaskResult(requestId, result.messageId, if (result.queuedOffline) "QUEUED" else "SENT", "")
                }
            }.onFailure { failure ->
                val unresolved = failure is ApiException && failure.statusCode == null
                val site = failure.stackTrace.firstOrNull()?.let { "${it.className.substringAfterLast('.')}.${it.methodName}" } ?: "unknown"
                val status = (failure as? ApiException)?.statusCode ?: (failure as? RelayHttpException)?.status
                val code = (failure as? RelayHttpException)?.code ?: "none"
                val relayRequestId = (failure as? RelayHttpException)?.requestId?.take(8) ?: "none"
                android.util.Log.w("JarvisStore", "submit_failed trace=$requestTrace type=${failure.javaClass.simpleName} site=$site status=$status code=$code relay_request_id=$relayRequestId elapsed_ms=${java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - submitStartedNs)}")
                runtime.recordLocalTaskResult(requestId, "", if (unresolved) "RUNNING" else "FAILED", deliveryError(failure))
            }
            ui { submitting = false }
            refreshLocal()
            syncNow()
        }
    }

    /** Sends one approve or reject for an exact pending request. The phone and the Mac both check digest and expiry. */
    fun decide(review: ApprovalReview, decision: String) = ui {
        if (decisionInFlight || decision !in setOf("approve", "reject")) return@ui
        val current = approvals.firstOrNull { it.approvalId == review.approvalId }
        if (current == null || current.expiresAtUnixMs <= now) {
            decisionNote = "This approval is no longer pending. Sync with the Mac before taking another action."
            syncNow()
            return@ui
        }
        val priorDecision = approvalDecisions[review.approvalId]
        val isExplicitRetry = ApprovalDecisionPresentation.canExplicitlyRetry(priorDecision, decision)
        if (ApprovalDecisionPresentation.blocksAnotherTap(priorDecision) && !isExplicitRetry) {
            decisionNote = ApprovalDecisionPresentation.label(approvalDecisions.getValue(review.approvalId))
            return@ui
        }
        val submitting = ApprovalDecisionPresentation.submitting(
            approvalId = current.approvalId,
            taskId = current.taskId,
            decision = decision,
            operation = current.operation,
            target = current.target,
            nowUnixMs = now,
        ) ?: return@ui
        saveApprovalDecision(submitting)
        decisionInFlight = true
        currentDecisionApprovalId = review.approvalId
        decisionNote = ApprovalDecisionPresentation.label(submitting)
        val supervisorApproval = supervisorApprovals[review.approvalId]
        background(lanes.approval) {
            if (supervisorApproval != null) {
                val runtime = LinkService.runtime(app)
                val continuationObjective = if (decision == "approve" && supervisorApproval.operation.startsWith("mcp:jarvis_dev:")) {
                    runtime.conversation()
                        .firstOrNull { it["task_id"] == supervisorApproval.taskId }
                        ?.get("text") as? String
                } else null
                val outcome = if (
                    decision == "approve" && supervisorApproval.operation == "mcp:jarvis_dev:approve_plan" &&
                    continuationObjective.isNullOrBlank()
                ) {
                    Result.failure(ApiException("The original development request could not be matched safely."))
                } else {
                    runCatching { jarvisApi.decide(supervisorApproval, decision, continuationObjective) }
                }
                ui {
                    decisionInFlight = false
                    currentDecisionApprovalId = null
                    val previous = approvalDecisions[review.approvalId] ?: submitting
                    val updated = outcome.fold(
                        onSuccess = { response ->
                            val decisionEvidence = mapOf(
                                "approval_id" to response.optString("approval_id"),
                                "task_id" to response.optString("task_id"),
                                "decision" to response.optString("decision"),
                                "status" to response.optString("status"),
                                "execution_state" to response.optString("execution_state"),
                                "executed" to response.optBoolean("executed", false),
                                "success" to response.optBoolean("success", false),
                            )
                            ApprovalDecisionPresentation.authoritative(
                                submitting,
                                decisionEvidence,
                                System.currentTimeMillis(),
                            ) ?: previous.copy(phase = ApprovalDecisionPhase.UNKNOWN, updatedAtUnixMs = System.currentTimeMillis())
                        },
                        onFailure = { failure ->
                            val isExplicitHttpFailure = failure is ApiException && failure.statusCode != null
                            previous.copy(
                                phase = if (isExplicitHttpFailure) ApprovalDecisionPhase.FAILED else ApprovalDecisionPhase.INDETERMINATE,
                                updatedAtUnixMs = System.currentTimeMillis(),
                            )
                        },
                    )
                    saveApprovalDecision(updated)
                    decisionNote = ApprovalDecisionPresentation.label(updated)
                    outcome.onSuccess { response ->
                        val taskId = response.optString("task_id", supervisorApproval.taskId)
                        val taskState = response.optString("task_state").ifBlank {
                            if (decision == "reject") "FAILED" else "RUNNING"
                        }
                        val resultText = response.optString("result_text")
                            .ifBlank { response.optString("result_summary") }
                        val row = runtime.conversation().firstOrNull { it["task_id"] == taskId }
                        val requestId = row?.get("message_id") as? String
                        if (!requestId.isNullOrBlank() && resultText.isNotBlank()) {
                            runtime.recordLocalTaskResult(requestId, taskId, taskState, resultText)
                        }
                    }
                }
                refreshLocal()
                return@background
            }
            val outcome = runCatching { LinkService.runtime(app).decideApproval(LinkSettings(app).relayUrl, review.approvalId, decision) }
            ui {
                decisionInFlight = false
                currentDecisionApprovalId = null
                val previous = approvalDecisions[review.approvalId] ?: submitting
                val updated = outcome.fold(
                    onSuccess = { result ->
                        val queued = result["queued_offline"] == true
                        ApprovalDecisionPresentation.submitted(
                            approvalId = review.approvalId,
                            taskId = review.taskId,
                            decision = decision,
                            queuedOffline = queued,
                            messageId = result["message_id"] as? String ?: "",
                            nowUnixMs = now,
                            operation = current.operation,
                            target = current.target,
                        ) ?: previous.copy(phase = ApprovalDecisionPhase.UNKNOWN, updatedAtUnixMs = now)
                    },
                    onFailure = { failure ->
                        // A lost response is ambiguous: never claim the Mac did not receive or run the decision.
                        val phase = if (failure is RelayHttpException) ApprovalDecisionPhase.DELIVERY_FAILED else ApprovalDecisionPhase.UNKNOWN
                        previous.copy(phase = phase, updatedAtUnixMs = now)
                    },
                )
                saveApprovalDecision(updated)
                decisionNote = ApprovalDecisionPresentation.label(updated)
            }
            refreshLocal()
            syncNow()
        }
    }

    fun approvalDecisionStatus(approvalId: String): ApprovalDecisionStatus? = approvalDecisions[approvalId]

    fun refreshApprovalDecisionStatus() = syncNow()

    private fun reconcileRuntimeDecisionStates(
        pending: List<ApprovalReview>,
        runtimeStates: Map<String, Map<String, Any>>,
    ) {
        var next = approvalDecisions
        for ((approvalId, raw) in runtimeStates) {
            val status = ApprovalDecisionPresentation.fromRuntimeState(
                previous = next[approvalId],
                runtime = raw,
                nowUnixMs = now,
                operation = pending.firstOrNull { it.approvalId == approvalId }?.operation.orEmpty(),
                target = pending.firstOrNull { it.approvalId == approvalId }?.target.orEmpty(),
                allowSubmitting = currentDecisionApprovalId == approvalId,
            ) ?: continue
            next = next + (approvalId to status)
        }
        if (next != approvalDecisions) {
            approvalDecisions = next
            persistApprovalDecisions(next)
            decisionNote = next.values.maxByOrNull { it.updatedAtUnixMs }?.let(ApprovalDecisionPresentation::label).orEmpty()
        }
    }

    /** Reconcile ambiguous paired-API decisions using authenticated Mac approval/task records. */
    private fun reconcileSupervisorDecisionStates(
        conversation: List<Map<String, Any>>,
    ): Map<String, ApprovalDecisionStatus> {
        val pollTime = System.currentTimeMillis()
        if (pollTime - lastSupervisorDecisionPollMs < SUPERVISOR_DECISION_POLL_INTERVAL_MS) return emptyMap()
        lastSupervisorDecisionPollMs = pollTime
        val candidates = approvalDecisions.values.filter { status ->
            status.phase in setOf(
                ApprovalDecisionPhase.SUBMITTING,
                ApprovalDecisionPhase.INDETERMINATE,
                ApprovalDecisionPhase.DELIVERED,
                ApprovalDecisionPhase.APPROVED,
                ApprovalDecisionPhase.EXECUTING,
                ApprovalDecisionPhase.UNKNOWN,
            ) || (status.phase == ApprovalDecisionPhase.COMPLETED && status.operation.isBlank())
        }
        val resolved = linkedMapOf<String, ApprovalDecisionStatus>()
        for (previous in candidates) {
            val row = conversation.firstOrNull {
                it["task_id"] == previous.taskId && it["transport"] == "api"
            } ?: continue
            val requestId = row["message_id"] as? String ?: continue
            val approval = runCatching { jarvisApi.getApproval(previous.approvalId) }.getOrNull() ?: continue
            val status = approval.status.uppercase()
            if (status == "PENDING") continue
            val taskResult = runCatching { jarvisApi.getLinkTaskStatus(requestId) }.getOrNull()
            val executionState = when {
                status == "REJECTED" -> "REJECTED"
                status == "EXPIRED" -> "EXPIRED"
                taskResult != null -> taskResult.state
                else -> status
            }
            val evidence = mapOf(
                "approval_id" to previous.approvalId,
                "task_id" to previous.taskId,
                "decision" to previous.decision,
                "status" to status,
                "execution_state" to executionState,
                "executed" to (status == "CONSUMED"),
                "success" to (status == "CONSUMED" && taskResult?.state == "COMPLETED"),
            )
            val boundPrevious = previous.copy(
                operation = previous.operation.ifBlank { approval.operation },
                target = previous.target.ifBlank { approval.targetResource },
            )
            val updated = ApprovalDecisionPresentation.authoritative(boundPrevious, evidence, pollTime) ?: continue
            resolved[previous.approvalId] = updated
            if (taskResult != null && taskResult.summary.isNotBlank()) {
                LinkService.runtime(app).recordLocalTaskResult(
                    requestId, taskResult.taskId, taskResult.state, taskResult.summary,
                )
            }
        }
        return resolved
    }

    private fun reconcileSupervisorDecisionResults(results: Map<String, ApprovalDecisionStatus>) {
        if (results.isEmpty()) return
        approvalDecisions = approvalDecisions + results
        persistApprovalDecisions(approvalDecisions)
        decisionNote = approvalDecisions.values.maxByOrNull { it.updatedAtUnixMs }
            ?.let(ApprovalDecisionPresentation::label).orEmpty()
        if (currentDecisionApprovalId in results.keys) {
            val phase = results[currentDecisionApprovalId]?.phase
            if (phase in setOf(
                    ApprovalDecisionPhase.COMPLETED,
                    ApprovalDecisionPhase.REJECTED,
                    ApprovalDecisionPhase.EXPIRED,
                    ApprovalDecisionPhase.CANCELLED,
                    ApprovalDecisionPhase.FAILED,
                )) {
                decisionInFlight = false
                currentDecisionApprovalId = null
            }
        }
    }

    private fun loadApprovalDecisions(): Map<String, ApprovalDecisionStatus> = runCatching {
        val array = JSONArray(approvalDecisionPrefs.getString("records", "[]"))
        buildMap {
            for (index in 0 until array.length()) {
                val value = array.optJSONObject(index) ?: continue
                val id = value.optString("approval_id").takeIf { it.isNotBlank() } ?: continue
                val storedPhase = runCatching { ApprovalDecisionPhase.valueOf(value.optString("phase")) }.getOrNull() ?: continue
                val status = ApprovalDecisionStatus(
                    approvalId = id,
                    taskId = value.optString("task_id"),
                    decision = value.optString("decision"),
                    phase = storedPhase,
                    updatedAtUnixMs = value.optLong("updated_at_unix_ms"),
                    messageId = value.optString("message_id"),
                    operation = value.optString("operation"),
                    target = value.optString("target"),
                )
                put(id, ApprovalDecisionPresentation.afterRestart(status))
            }
        }
    }.getOrDefault(emptyMap())

    private fun saveApprovalDecision(status: ApprovalDecisionStatus) {
        val bounded = (approvalDecisions + (status.approvalId to status)).values
            .sortedBy { it.updatedAtUnixMs }
            .takeLast(20)
            .associateBy { it.approvalId }
        approvalDecisions = bounded
        persistApprovalDecisions(bounded)
    }

    /** Reduce authenticated supervisor decision results and task states into a durable, honest UI snapshot. */
    private fun reconcileApprovalDecisions(recentUpdates: List<Map<String, Any>>) {
        var next = approvalDecisions
        for (update in recentUpdates) {
            val decisionResult = update["approval_decision_result"] as? Map<*, *>
            if (decisionResult != null) {
                val typed = decisionResult.entries.mapNotNull { (key, value) ->
                    (key as? String)?.let { it to value }
                }.toMap()
                val id = typed["approval_id"] as? String ?: continue
                val previous = next[id] ?: continue
                ApprovalDecisionPresentation.authoritative(previous, typed, now)?.let { next = next + (id to it) }
                continue
            }
            val taskId = update["task_id"] as? String ?: continue
            val state = update["state"] as? String ?: continue
            next.values.filter { it.taskId == taskId }.forEach { previous ->
                ApprovalDecisionPresentation.taskState(previous, state, now)?.let { next = next + (it.approvalId to it) }
            }
        }
        if (next != approvalDecisions) {
            approvalDecisions = next
            persistApprovalDecisions(next)
            decisionNote = next.values.maxByOrNull { it.updatedAtUnixMs }?.let(ApprovalDecisionPresentation::label).orEmpty()
        }
    }

    private fun persistApprovalDecisions(records: Map<String, ApprovalDecisionStatus>) {
        val array = JSONArray()
        records.values.sortedBy { it.updatedAtUnixMs }.takeLast(20).forEach { item ->
            array.put(JSONObject()
                .put("approval_id", item.approvalId)
                .put("task_id", item.taskId)
                .put("decision", item.decision)
                .put("phase", item.phase.name)
                .put("updated_at_unix_ms", item.updatedAtUnixMs)
                .put("operation", item.operation)
                .put("target", item.target))
        }
        approvalDecisionPrefs.edit().putString("records", array.toString()).apply()
    }

    private fun hasMacApiPairing(): Boolean =
        secureStore.deviceId().matches(Regex("[0-9a-f]{32}")) &&
            !secureStore.token().isNullOrBlank() && secureStore.baseUrl().isNotBlank()

    fun newChat() = ui {
        sessions.create()
        session = sessions.active()
        sessionList = sessions.all()
        overlay = null
        tab = Tab.HOME
    }

    fun openSession(id: String) = ui {
        sessions.open(id)
        session = sessions.active()
        overlay = null
        tab = Tab.HOME
    }

    fun openOverlay(value: Overlay?) = ui { overlay = value }

    fun selectTab(value: Tab) = ui {
        tab = value
        overlay = null
    }

    fun clearNotice() = ui { notice = "" }

    fun tick() = ui { now = System.currentTimeMillis() }

    fun destroy() {
        if (localAiRequestRunning) localAi.cancel()
        destroyed = true
        lanes.close()
        main.removeCallbacksAndMessages(null)
    }

    private fun ui(block: () -> Unit) {
        main.post { if (!destroyed) block() }
    }

    private fun background(executor: java.util.concurrent.ExecutorService = lanes.local, block: () -> Unit) {
        executor.execute {
            try {
                block()
            } catch (failure: Throwable) {
                android.util.Log.w("JarvisStore", "background step failed: ${failure.javaClass.simpleName}")
                ui { notice = "Jarvis hit an unexpected error. Try again." }
            }
        }
    }

    private fun deliveryError(failure: Throwable): String = when (failure) {
        is RelayHttpException -> when {
            failure.status == 401 && failure.code == "TIMESTAMP_SKEW" -> "Telegram delayed this request. It is saved on this phone and will retry with fresh authentication."
            failure.status == 401 -> "The saved pairing could not authenticate. Pairing data was kept; check the Mac connection in Settings."
            failure.status == 429 -> "The relay is rate-limited right now. This request stays saved on your phone for retry."
            else -> "Jarvis could not deliver this request. Check the connection, then send it again."
        }
        is ApiException -> when (failure.statusCode) {
            401 -> "The saved pairing could not authenticate. Pairing data was kept; check the Mac connection in Settings."
            403 -> "The Mac blocked this request because it is outside the enabled assistant scope."
            404 -> "The Mac did not record this request. Check the connection before sending it again."
            503 -> "Jarvis on the Mac is not ready. Check that its task runner is available."
            null -> "No confirmation yet. The Mac may still be working; this task is being checked."
            else -> "Jarvis could not accept this request. Edit it and try again."
        }
        else -> "Jarvis could not deliver this request. Check the connection, then send it again."
    }

    private companion object {
        const val MAC_API_PROBE_INTERVAL_MS = 15_000L
        const val SUPERVISOR_APPROVAL_POLL_INTERVAL_MS = 15_000L
        const val SUPERVISOR_DECISION_POLL_INTERVAL_MS = 15_000L
    }

    private fun localModelError(failure: Throwable): String = when (failure) {
        is com.jarvis.companion.localai.LocalAiException -> failure.message ?: "Local inference could not run."
        is com.jarvis.companion.localai.LocalModelException -> failure.message ?: "The selected model could not be imported."
        else -> "The local model could not be used (${failure.javaClass.simpleName})."
    }

    private fun mergeEntries(
        remote: List<Map<String, Any>>,
        local: List<Map<String, Any>>,
    ): List<Map<String, Any>> = (remote + local)
        .associateBy { it["message_id"] as? String ?: "" }
        .values
        .sortedBy { (it["created_at_unix_ms"] as? Number)?.toLong() ?: 0L }
}
