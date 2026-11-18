package com.jarvis.companion.ui

import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.jarvis.companion.link.LinkBackgroundSync
import com.jarvis.companion.link.LinkService
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.model.ApiException
import com.jarvis.companion.telegram.TelegramSession
import com.jarvis.companion.telegram.TelegramSnapshot
import com.jarvis.link.ApprovalReview
import com.jarvis.link.LinkStatus
import com.jarvis.link.SafeCapability
import com.jarvis.link.TaskSubmission
import java.util.UUID
import java.util.concurrent.Executors

enum class Tab(val label: String) { HOME("Home"), ACTIVITY("Activity"), APPROVALS("Approvals"), LIVE("Live") }

enum class Overlay { SETTINGS, HISTORY }

/** States the Mac reports while work is still open. Used for polling and for the presence signal. */
val ActiveStates = setOf("CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL", "PAUSED", "SENT", "QUEUED")

/**
 * Holds everything the Compose screens show. All values come from LinkRuntime, the Telegram session, and the
 * sync that LinkBackgroundSync performs. Nothing here fabricates a task state or an approval.
 *
 * Runtime calls run on one background thread; results are posted back to the main thread before state changes.
 */
class JarvisStore(private val app: Context) {
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor { r -> Thread(r, "jarvis-ui-io").apply { isDaemon = true } }
    private val sessions = ChatSessions(app)
    private var destroyed = false

    var tab by mutableStateOf(Tab.HOME)
    var overlay by mutableStateOf<Overlay?>(null)
    var entries by mutableStateOf<List<Map<String, Any>>>(emptyList())
    var approvals by mutableStateOf<List<ApprovalReview>>(emptyList())
    var linkStatus by mutableStateOf<LinkStatus?>(null)
    var lastSyncMs by mutableStateOf(0L)
    var lastSyncError by mutableStateOf("")
    var telegram by mutableStateOf<TelegramSnapshot?>(null)
    var session by mutableStateOf(sessions.active())
    var sessionList by mutableStateOf(sessions.all())
    var draft by mutableStateOf("")
    var submitting by mutableStateOf(false)
    var syncing by mutableStateOf(false)
    var decisionInFlight by mutableStateOf(false)
    var decisionNote by mutableStateOf("")
    var notice by mutableStateOf("")
    var now by mutableStateOf(System.currentTimeMillis())
    /** Set by the host activity; asks Android for one runtime permission, on the user's tap only. */
    var permissionRequester: ((String) -> Unit)? = null

    var reduceMotion by mutableStateOf(app.getSharedPreferences("jarvis_ui", Context.MODE_PRIVATE).getBoolean("reduce_motion", false))

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
    fun hasActiveWork(): Boolean = entries.any { (it["state"] as? String) in ActiveStates }

    fun hasRunningWork(): Boolean = entries.any { (it["state"] as? String) in setOf("CREATED", "PLANNING", "RUNNING", "VERIFYING") }

    fun refreshLocal() = background {
        val runtime = LinkService.runtime(app)
        val conversation = runtime.conversation()
        val pending = runtime.pendingApprovals()
        val status = runtime.status()
        val (syncAt, syncError) = runtime.lastSync()
        val snapshot = TelegramSession.controller?.snapshot()
        ui {
            entries = conversation
            approvals = pending
            linkStatus = status
            lastSyncMs = syncAt
            lastSyncError = syncError
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
        background {
            runCatching { LinkBackgroundSync.run(app) }
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
        val transport = if (TelegramSession.activeTunnel() != null) "telegram" else "link"
        val conversation = session
        sessions.assign(requestId, conversation.id)
        sessions.setTitleIfBlank(conversation.id, text)
        sessionList = sessions.all()
        submitting = true
        draft = ""
        notice = ""
        background {
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
            val delivery = runCatching {
                runtime.submit(LinkSettings(app).relayUrl, TaskSubmission(text, setOf(SafeCapability.ASSISTANT), clientRequestId = requestId), null)
            }
            delivery.onSuccess { result ->
                if (result.messageId != requestId) {
                    runtime.recordLocalTaskResult(requestId, "", "FAILED", "Jarvis could not correlate this request safely. Send it again.")
                } else {
                    runtime.recordLocalTaskResult(requestId, result.messageId, if (result.queuedOffline) "QUEUED" else "SENT", "")
                }
            }.onFailure { failure ->
                val unresolved = failure is ApiException && failure.statusCode == null
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
        decisionInFlight = true
        decisionNote = if (decision == "approve") "Sending your one-time approval to the Mac…" else "Sending your rejection…"
        background {
            val outcome = runCatching { LinkService.runtime(app).decideApproval(LinkSettings(app).relayUrl, review.approvalId, decision) }
            ui {
                decisionInFlight = false
                decisionNote = outcome.fold(
                    onSuccess = {
                        if (decision == "approve") {
                            "Approved once. The Mac has your decision; its result appears in Activity when it reports back."
                        } else {
                            "Rejected. The Mac will not run this operation."
                        }
                    },
                    onFailure = {
                        "The decision was not sent. The approval may have expired or been decided already. Check Activity."
                    },
                )
            }
            refreshLocal()
            syncNow()
        }
    }

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
        destroyed = true
        io.shutdownNow()
        main.removeCallbacksAndMessages(null)
    }

    private fun ui(block: () -> Unit) {
        main.post { if (!destroyed) block() }
    }

    private fun background(block: () -> Unit) {
        io.execute {
            try {
                block()
            } catch (failure: Throwable) {
                android.util.Log.w("JarvisStore", "background step failed: ${failure.javaClass.simpleName}")
                ui { notice = "Jarvis hit an unexpected error. Try again." }
            }
        }
    }

    private fun deliveryError(failure: Throwable): String = when (failure) {
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
}
