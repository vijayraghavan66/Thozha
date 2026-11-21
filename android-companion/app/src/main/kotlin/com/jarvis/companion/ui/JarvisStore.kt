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
import com.jarvis.companion.link.LinkBackgroundSync
import com.jarvis.companion.link.LinkService
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.fastpath.FastCommand
import com.jarvis.companion.fastpath.FastCommandClassifier
import com.jarvis.companion.localai.LocalAiManager
import com.jarvis.companion.localai.LocalAiPhase
import com.jarvis.companion.localai.LocalAiRequestMode
import com.jarvis.companion.localai.RequestRoute
import com.jarvis.companion.localai.RequestRoutingPolicy
import com.jarvis.companion.model.ApiException
import com.jarvis.companion.data.JarvisApi
import com.jarvis.companion.data.SecureStore
import com.jarvis.companion.telegram.TelegramSession
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
val ActiveStates = setOf("CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL", "PAUSED", "SENT", "QUEUED", "UNCONFIRMED")

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
    var approvals by mutableStateOf<List<ApprovalReview>>(emptyList())
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
    var telegram by mutableStateOf<TelegramSnapshot?>(null)
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
        val runtime = LinkService.runtime(app)
        var conversation = runtime.conversation()
        val pairedToMac = hasMacApiPairing()
        var reachableAt = macApiLastSyncMs
        var apiError = macApiSyncError
        if (pairedToMac) {
            // Reconcile only requests previously submitted by this phone using their original idempotency keys.
            // A lost HTTP response therefore observes the same task rather than creating a duplicate.
            conversation.filter { row ->
                row["transport"] == "api" && (row["state"] as? String) in setOf(
                    "CREATED", "QUEUED", "RUNNING", "PLANNING", "WAITING_APPROVAL",
                )
            }.forEach { row ->
                val id = row["message_id"] as? String ?: return@forEach
                runCatching { jarvisApi.getLinkTaskStatus(id) }
                    .onSuccess { result -> runtime.recordLocalTaskResult(id, result.taskId, result.state, result.summary) }
                    .onFailure { failure ->
                        // Preserve an ambiguous in-flight state on transport errors; never report false failure.
                        if (failure is ApiException && failure.statusCode == 404) {
                            runtime.recordLocalTaskResult(id, "", "FAILED", "The Mac no longer has this task record.")
                        } else if (failure !is ApiException || failure.statusCode != null) {
                            apiError = "The Mac task status could not be confirmed yet."
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
                        secureStore.recordMacConnection("usb")
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
        val updatesSnapshot = runtime.recentUpdates()
        val decisionIds = (approvalDecisions.keys + pending.map { it.approvalId }).distinct()
        val runtimeDecisionStates = decisionIds.mapNotNull { id ->
            runtime.approvalDecisionState(id)?.let { id to it }
        }.toMap()
        val status = runtime.status()
        val (syncAt, syncError) = runtime.lastSync()
        val snapshot = TelegramSession.controller?.snapshot()
        ui {
            entries = mergeEntries(conversation, localHistory.all())
            taskUpdates = updatesSnapshot
            localAiSnapshot = localAi.status()
            approvals = pending
            reconcileRuntimeDecisionStates(pending, runtimeDecisionStates)
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

    /** Retries queued sends and polls the Mac for updates. Safe to call repeatedly; only one sync runs at a time. */
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
        val requestTrace = java.security.MessageDigest.getInstance("SHA-256")
            .digest(requestId.toByteArray(Charsets.UTF_8)).take(6).joinToString("") { "%02x".format(it.toInt() and 0xff) }
        val submitStartedNs = System.nanoTime()
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
                val requestStored = runCatching { localHistory.recordRequest(requestId, text, System.currentTimeMillis()) }
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
            if (runCatching { runtime.recordLocalTaskRequest(requestId, text, transport = transport) }.isFailure) {
                ui {
                    submitting = false
                    draft = text
                    notice = "Jarvis could not save this request on your phone."
                }
                return@background
            }
            // Show the user's bubble as pending now, not after the network call returns.
            refreshLocal()
            if (usePairedMacApi) {
                runCatching {
                    if (directReadOnlyCommand) {
                        Triple(requestId, "COMPLETED", jarvisApi.getMacSystemInfo())
                    } else {
                        val result = jarvisApi.submitLinkTask(text, requestId)
                        Triple(result.taskId, result.state, result.summary)
                    }
                }
                    .onSuccess { (taskId, state, summary) ->
                        secureStore.recordMacConnection("usb")
                        runtime.recordLocalTaskResult(requestId, taskId, state, summary)
                        ui {
                            macApiLastSyncMs = System.currentTimeMillis()
                            macApiSyncError = ""
                        }
                        android.util.Log.i(
                            "JarvisStore",
                            "mac_api_task_result trace=$requestTrace task_id=${taskId.take(8)} state=$state elapsed_ms=${java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - submitStartedNs)}",
                        )
                    }
                    .onFailure { failure ->
                        val unresolved = failure is ApiException && failure.statusCode == null
                        val site = failure.stackTrace.firstOrNull()?.let { "${it.className.substringAfterLast('.')}.${it.methodName}" } ?: "unknown"
                        val status = (failure as? ApiException)?.statusCode
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
        background {
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
                .put("updated_at_unix_ms", item.updatedAtUnixMs))
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
