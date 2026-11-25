package com.jarvis.companion.data

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import com.jarvis.companion.BuildConfig
import com.jarvis.companion.model.TaskEvent
import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.net.InetAddress
import java.net.URI
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * Paired-device WSS event client for the restricted local gateway route. Discovery is used only to
 * obtain a candidate private address; the saved certificate fingerprint and DNS SAN authenticate
 * the Mac before the paired bearer can be sent. This client does not submit tasks or expose tools.
 */
class AndroidLocalGatewayTaskEventWssChannel(
    context: Context,
    private val secureStore: SecureStore = SecureStore(context.applicationContext),
) : TaskEventChannel {
    private val appContext = context.applicationContext
    private val callbackExecutor = java.util.concurrent.Executor { command -> Handler(Looper.getMainLooper()).post(command) }

    override fun open(
        idempotencyKey: String,
        persistedCursor: () -> Long,
        persister: TaskEventPersister,
        listener: TaskEventChannelListener,
    ): TaskEventChannelSession {
        require(idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "Invalid task event key." }
        val cursor = runCatching(persistedCursor).getOrElse {
            return FailedSession(listener, "PERSISTED_CURSOR_UNAVAILABLE")
        }
        require(cursor >= 0) { "Invalid persisted task event cursor." }
        if (Build.VERSION.SDK_INT >= 37 && appContext.checkSelfPermission(LOCAL_NETWORK_PERMISSION) != PackageManager.PERMISSION_GRANTED) {
            return FailedSession(listener, "LOCAL_NETWORK_PERMISSION_REQUIRED")
        }
        val token = secureStore.token()?.takeIf(String::isNotBlank)
            ?: return FailedSession(listener, "PAIRING_CREDENTIAL_UNAVAILABLE")
        val deviceId = secureStore.deviceId().takeIf { it.matches(Regex("[0-9a-f]{32}")) }
            ?: return FailedSession(listener, "PAIRING_IDENTITY_UNAVAILABLE")
        val pin = try {
            secureStore.localGatewayPin()
        } catch (_: SecurityException) {
            return FailedSession(listener, "GATEWAY_PIN_INVALID")
        } ?: return FailedSession(listener, "GATEWAY_PIN_UNAVAILABLE")

        return WssSession(
            appContext = appContext,
            taskKey = idempotencyKey,
            cursorProvider = persistedCursor,
            persister = persister,
            token = token,
            deviceId = deviceId,
            pin = pin,
            listener = listener,
            callbackExecutor = callbackExecutor,
        ).also { it.connect() }
    }

    private class FailedSession(
        listener: TaskEventChannelListener,
        code: String,
    ) : TaskEventChannelSession {
        init { runCatching { listener.onState(TaskEventChannelState.FAILED, code) } }
        override fun updateDiscoveredGateway(candidate: DiscoveredMacGateway?): Boolean = false
        override fun close() = Unit
    }

    private class WssSession(
        private val appContext: Context,
        private val taskKey: String,
        private val cursorProvider: () -> Long,
        private val persister: TaskEventPersister,
        private val token: String,
        @Suppress("UNUSED_PARAMETER") private val deviceId: String,
        private val pin: LocalGatewayPin,
        private val listener: TaskEventChannelListener,
        private val callbackExecutor: java.util.concurrent.Executor,
    ) : TaskEventChannelSession {
        private val closed = AtomicBoolean(false)
        private val lock = Any()
        private val scheduler = Executors.newSingleThreadScheduledExecutor { runnable ->
            Thread(runnable, "thozha-task-events-reconnect").apply { isDaemon = true }
        }
        private val persistenceExecutor = Executors.newSingleThreadExecutor { runnable ->
            Thread(runnable, "thozha-task-event-persist").apply { isDaemon = true }
        }
        @Volatile private var socket: WebSocket? = null
        @Volatile private var httpClient: OkHttpClient? = null
        @Volatile private var candidate: DiscoveredMacGateway? = null
        @Volatile private var currentState = TaskEventChannelState.CONNECTING
        private var lastAcknowledged = 0L
        private val ackLedger = TaskEventAckLedger(0L)
        private var readyCursor = -1L
        private var requestedCursor = 0L
        private var boundTaskId: String? = null
        private var reconnectAttempt = 0
        private var reconnectFuture: ScheduledFuture<*>? = null
        private var terminalProtocolFailure = false

        fun connect() {
            if (closed.get() || terminalProtocolFailure) return
            if (Build.VERSION.SDK_INT >= 37 &&
                appContext.checkSelfPermission(LOCAL_NETWORK_PERMISSION) != PackageManager.PERMISSION_GRANTED
            ) {
                terminalFailure("LOCAL_NETWORK_PERMISSION_REQUIRED")
                return
            }
            val gateway = candidate ?: signedAddressGateway(pin, SystemClock.elapsedRealtime())?.also {
                candidate = it
                diagnostic("wss_signed_address_fallback")
            }
            if (gateway == null) {
                transition(TaskEventChannelState.DISCONNECTED, "GATEWAY_DISCOVERY_REQUIRED")
                return
            }
            val addressList = validateGatewayAgainstPin(gateway, pin)
            if (addressList.isEmpty()) {
                terminalFailure("GATEWAY_IDENTITY_MISMATCH")
                return
            }
            val cursor = runCatching(cursorProvider).getOrElse {
                terminalFailure("PERSISTED_CURSOR_UNAVAILABLE")
                return
            }
            if (cursor < 0) {
                terminalFailure("INVALID_PERSISTED_CURSOR")
                return
            }
            synchronized(lock) {
                lastAcknowledged = cursor
                ackLedger.resetFromDurableCursor(cursor)
                readyCursor = -1L
                requestedCursor = cursor
            }
            transition(if (reconnectAttempt == 0) TaskEventChannelState.CONNECTING else TaskEventChannelState.RECONNECTING, null)
            val trustManager = try {
                LocalGatewayTls.trustManager(pin.certificateSha256)
            } catch (_: Exception) {
                terminalFailure("GATEWAY_TLS_PIN_INVALID")
                return
            }
            val client = httpClient ?: OkHttpClient.Builder()
                .dns(object : Dns {
                    override fun lookup(hostname: String): List<InetAddress> = if (hostname.equals(pin.hostname, ignoreCase = true)) {
                        validateGatewayAgainstPin(candidate ?: gateway, pin).ifEmpty { addressList }
                    } else Dns.SYSTEM.lookup(hostname)
                })
                .sslSocketFactory(SSLContext.getInstance("TLS").apply { init(null, arrayOf(trustManager), null) }.socketFactory, trustManager)
                .hostnameVerifier(LocalGatewayTls.hostnameVerifier(pin.hostname))
                .connectTimeout(4, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .writeTimeout(5, TimeUnit.SECONDS)
                .pingInterval(20, TimeUnit.SECONDS)
                .build()
                .also { httpClient = it }
            val url = buildTaskEventsWssUrl(pin.hostname, pin.port, taskKey, cursor)
            val request = Request.Builder()
                .url(url)
                .header("Authorization", "Bearer $token")
                .header("Sec-WebSocket-Protocol", TASK_EVENT_SUBPROTOCOL)
                .build()
            socket = client.newWebSocket(request, object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    if (socket !== webSocket) return
                    if (response.header("Sec-WebSocket-Protocol") != TASK_EVENT_SUBPROTOCOL) {
                        terminalProtocolFailure(webSocket, "WSS_SUBPROTOCOL_MISMATCH")
                        return
                    }
                    reconnectAttempt = 0
                    diagnostic("wss_open")
                    // Do not signal CONNECTED until the service confirms the cursor and protocol.
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    if (socket !== webSocket) return
                    handleFrame(webSocket, text)
                }

                override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                    if (socket !== webSocket) return
                    terminalProtocolFailure(webSocket, "BINARY_FRAME_UNSUPPORTED")
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    val wasCurrent = socket === webSocket
                    if (wasCurrent) socket = null
                    diagnostic("wss_failed", "exception=${t.javaClass.simpleName} http=${response?.code ?: 0}")
                    if (!wasCurrent) return
                    if (response?.code == 401 || response?.code == 403) terminalFailure("WSS_AUTH_REJECTED")
                    else if (hasCertificateFailure(t)) terminalFailure("GATEWAY_TLS_REJECTED")
                    else scheduleReconnect("WSS_DISCONNECTED")
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    val wasCurrent = socket === webSocket
                    if (wasCurrent) socket = null
                    if (wasCurrent && !closed.get()) scheduleReconnect("WSS_CLOSED_$code")
                }
            })
        }

        private fun handleFrame(webSocket: WebSocket, raw: String) {
            if (raw.length > MAX_FRAME_CHARS) return terminalProtocolFailure(webSocket, "FRAME_TOO_LARGE")
            val json = runCatching { JSONObject(raw) }.getOrNull()
                ?: return terminalProtocolFailure(webSocket, "INVALID_JSON_FRAME")
            if (json.optInt("protocol", -1) != TASK_EVENT_PROTOCOL) {
                return terminalProtocolFailure(webSocket, "UNSUPPORTED_EVENT_PROTOCOL")
            }
            when (json.optString("type")) {
                "ready" -> {
                    val ready = parseTaskEventReady(json) ?: return terminalProtocolFailure(webSocket, "INVALID_READY_FRAME")
                    val requested = synchronized(lock) { requestedCursor }
                    if (ready.afterSequence != requested) return terminalProtocolFailure(webSocket, "WSS_CURSOR_MISMATCH")
                    synchronized(lock) {
                        readyCursor = ready.afterSequence
                        lastAcknowledged = maxOf(lastAcknowledged, ready.afterSequence)
                        ackLedger.resetToPersisted(lastAcknowledged)
                    }
                    transition(TaskEventChannelState.CONNECTED, null)
                }
                "event" -> {
                    if (readyCursor < 0) return terminalProtocolFailure(webSocket, "EVENT_BEFORE_READY")
                    val event = parseTaskEvent(json, taskKey)
                        ?: return terminalProtocolFailure(webSocket, "INVALID_TASK_EVENT")
                    val eventSequence = event.sequence
                    if (!bindTaskId(event.supervisorTaskId)) return terminalProtocolFailure(webSocket, "TASK_ID_MISMATCH")
                    if (eventSequence <= lastAcknowledged) {
                        sendAck(webSocket, event.supervisorTaskId, lastAcknowledged)
                        return
                    }
                    when (synchronized(lock) { ackLedger.offer(eventSequence) }) {
                        EventSequenceOffer.DUPLICATE -> return
                        EventSequenceOffer.OUT_OF_ORDER -> return terminalProtocolFailure(webSocket, "EVENT_SEQUENCE_OUT_OF_ORDER")
                        EventSequenceOffer.BACKPRESSURE -> return terminalProtocolFailure(webSocket, "EVENT_BACKPRESSURE_LIMIT")
                        EventSequenceOffer.ACCEPTED -> Unit
                    }
                    persistEvent(event)
                }
                "checkpoint" -> {
                    if (readyCursor < 0) return terminalProtocolFailure(webSocket, "CHECKPOINT_BEFORE_READY")
                    val checkpoint = parseTaskEventCheckpoint(json, taskKey)
                        ?: return terminalProtocolFailure(webSocket, "INVALID_CHECKPOINT")
                    val eventSequence = checkpoint.sequence
                    if (!bindTaskId(checkpoint.supervisorTaskId)) return terminalProtocolFailure(webSocket, "TASK_ID_MISMATCH")
                    synchronized(lock) {
                        if (eventSequence <= lastAcknowledged) {
                            sendAck(webSocket, checkpoint.supervisorTaskId, lastAcknowledged)
                            return
                        }
                    }
                    when (synchronized(lock) { ackLedger.offer(eventSequence) }) {
                        EventSequenceOffer.DUPLICATE -> return
                        EventSequenceOffer.OUT_OF_ORDER -> return terminalProtocolFailure(webSocket, "CHECKPOINT_SEQUENCE_OUT_OF_ORDER")
                        EventSequenceOffer.BACKPRESSURE -> return terminalProtocolFailure(webSocket, "EVENT_BACKPRESSURE_LIMIT")
                        EventSequenceOffer.ACCEPTED -> Unit
                    }
                    persistCheckpoint(checkpoint)
                }
                "heartbeat" -> Unit
                else -> terminalProtocolFailure(webSocket, "UNKNOWN_EVENT_FRAME")
            }
        }

        private fun sendAck(webSocket: WebSocket, taskId: String, sequence: Long): Boolean =
            webSocket.send(buildTaskEventAck(taskId, sequence))

        private fun bindTaskId(candidateTaskId: String): Boolean = synchronized(lock) {
            if (!candidateTaskId.matches(Regex(TASK_ID_PATTERN))) return@synchronized false
            val existing = boundTaskId
            if (existing != null && existing != candidateTaskId) return@synchronized false
            boundTaskId = candidateTaskId
            true
        }

        private fun persistEvent(event: TaskEventEnvelope) {
            try {
                persistenceExecutor.execute {
                    if (closed.get()) return@execute
                    val completed = persistBeforeTaskEventAck(
                        persist = { persister.persistEvent(event) },
                    ) { acknowledgeAfterPersistence(event.supervisorTaskId, event.sequence) }
                    if (!completed) {
                        diagnostic("event_persist_failed", "reason=not_committed")
                        failPersistence()
                        return@execute
                    }
                    dispatch { listener.onEventPersisted(event) }
                }
            } catch (_: java.util.concurrent.RejectedExecutionException) {
                // Closed session: no ACK is emitted; the durable cursor will be replayed on next open.
            }
        }

        private fun persistCheckpoint(checkpoint: TaskEventCursorCheckpoint) {
            try {
                persistenceExecutor.execute {
                    if (closed.get()) return@execute
                    val completed = persistBeforeTaskEventAck(
                        persist = { persister.persistCheckpoint(checkpoint) },
                    ) { acknowledgeAfterPersistence(checkpoint.supervisorTaskId, checkpoint.sequence) }
                    if (!completed) {
                        diagnostic("checkpoint_persist_failed", "reason=not_committed")
                        failPersistence()
                        return@execute
                    }
                    dispatch { listener.onCheckpointPersisted(checkpoint) }
                }
            } catch (_: java.util.concurrent.RejectedExecutionException) {
                // Closed session: no ACK is emitted; the durable cursor will be replayed on next open.
            }
        }

        private fun acknowledgeAfterPersistence(taskId: String, sequence: Long) {
            val ackToSend = synchronized(lock) {
                val ack = ackLedger.markPersisted(sequence)
                if (ack != null) lastAcknowledged = maxOf(lastAcknowledged, ack)
                ack
            } ?: return
            val active = socket
            if (active != null && !sendAck(active, taskId, ackToSend)) {
                diagnostic("ack_queue_failed", "seq=$ackToSend")
            }
        }

        private fun failPersistence() {
            if (closed.get()) return
            val previous = socket
            socket = null
            previous?.close(1011, "local event persistence failed")
            scheduleReconnect("EVENT_PERSISTENCE_FAILED")
        }

        override fun updateDiscoveredGateway(candidate: DiscoveredMacGateway?): Boolean {
            if (candidate == null) {
                val signedFallback = signedAddressGateway(pin, SystemClock.elapsedRealtime())
                if (signedFallback != null) {
                    val current = this.candidate
                    if (current?.serviceId == signedFallback.serviceId && current.privateAddresses == signedFallback.privateAddresses) {
                        return true
                    }
                    this.candidate = signedFallback
                    reconnectAttempt = 0
                    reconnectFuture?.cancel(false)
                    val previous = socket
                    socket = null
                    previous?.close(1000, "using signed paired gateway endpoint")
                    reconnectFuture = null
                    if (!closed.get()) scheduler.execute { connect() }
                    diagnostic("wss_signed_address_fallback")
                    return true
                }
                val previous = socket
                socket = null
                this.candidate = null
                reconnectFuture?.cancel(false)
                reconnectFuture = null
                previous?.close(1000, "gateway service disappeared")
                transition(TaskEventChannelState.DISCONNECTED, "GATEWAY_SERVICE_LOST")
                return true
            }
            val newAddresses = validateGatewayAgainstPin(candidate, pin)
            if (newAddresses.isEmpty()) return false
            val current = this.candidate
            if (!canReplaceGatewayCandidate(current, candidate)) return false
            if (current != null && current.advertisedTlsHostname == candidate.advertisedTlsHostname &&
                current.port == candidate.port && current.privateAddresses == candidate.privateAddresses
            ) return true
            this.candidate = candidate
            reconnectAttempt = 0
            reconnectFuture?.cancel(false)
            val previous = socket
            socket = null
            previous?.close(1000, "gateway address updated")
            reconnectFuture = null
            if (closed.get()) return false
            scheduler.execute { connect() }
            return true
        }

        private fun scheduleReconnect(code: String) {
            if (closed.get() || terminalProtocolFailure) return
            val attempt = reconnectAttempt++
            val delayMs = reconnectDelayMs(attempt)
            transition(TaskEventChannelState.RECONNECTING, code)
            reconnectFuture?.cancel(false)
            reconnectFuture = scheduler.schedule({ connect() }, delayMs, TimeUnit.MILLISECONDS)
        }

        private fun transition(state: TaskEventChannelState, diagnosticCode: String?) {
            if (closed.get() && state != TaskEventChannelState.CLOSED) return
            currentState = state
            diagnosticCode?.let { diagnostic("channel_state", "state=${state.name} code=$it") }
            dispatch { listener.onState(state, diagnosticCode) }
        }

        private fun dispatch(callback: () -> Unit) {
            callbackExecutor.execute {
                if (!closed.get()) runCatching(callback).onFailure { diagnostic("listener_failed", "exception=${it.javaClass.simpleName}") }
            }
        }

        private fun terminalProtocolFailure(webSocket: WebSocket, code: String) {
            terminalProtocolFailure = true
            webSocket.close(1002, "protocol error")
            terminalFailure(code)
        }

        private fun terminalFailure(code: String) {
            terminalProtocolFailure = true
            socket?.cancel()
            socket = null
            transition(TaskEventChannelState.FAILED, code)
        }

        private fun hasCertificateFailure(error: Throwable): Boolean {
            var cause: Throwable? = error
            repeat(8) {
                if (cause is CertificateException || cause is javax.net.ssl.SSLPeerUnverifiedException) return true
                cause = cause?.cause
            }
            return false
        }

        private fun diagnostic(event: String, detail: String = "") {
            if (BuildConfig.DEBUG) Log.i(TAG, "event=$event $detail elapsed_ms=${SystemClock.elapsedRealtime()}".trim())
        }

        override fun close() {
            if (!closed.compareAndSet(false, true)) return
            reconnectFuture?.cancel(false)
            socket?.close(1000, "client closed")
            socket = null
            httpClient?.dispatcher?.executorService?.shutdown()
            httpClient?.connectionPool?.evictAll()
            httpClient = null
            scheduler.shutdownNow()
            // Stop accepting new persistence work and let an in-flight durable write finish.
            // The next session resumes from the persisted cursor; closure never ACKs new work.
            persistenceExecutor.shutdown()
            currentState = TaskEventChannelState.CLOSED
            callbackExecutor.execute { runCatching { listener.onState(TaskEventChannelState.CLOSED, null) } }
        }
    }

    private companion object {
        const val LOCAL_NETWORK_PERMISSION = "android.permission.ACCESS_LOCAL_NETWORK"
        const val TASK_EVENT_SUBPROTOCOL = "thozha.task-events.v1"
        const val TASK_EVENT_PROTOCOL = 1
        const val TASK_ID_PATTERN = "[0-9a-f]{32}"
        const val MAX_FRAME_CHARS = 128 * 1024
        const val TAG = "ThozhaTaskWss"
    }
}

internal fun validateGatewayAgainstPin(candidate: DiscoveredMacGateway, pin: LocalGatewayPin): List<InetAddress> {
    if (!candidate.advertisedTlsHostname.equals(pin.hostname, ignoreCase = true) || candidate.port != pin.port) return emptyList()
    if (candidate.protocolVersion != ThozhaDnsSdContract.PROTOCOL_VERSION || candidate.privateAddresses.isEmpty()) return emptyList()
    return candidate.privateAddresses.mapNotNull { value ->
        runCatching { InetAddress.getByName(value) }.getOrNull()
            ?.takeIf(ThozhaDnsSdContract::isRoutablePrivateLanAddress)
    }.distinctBy { it.hostAddress }
}

internal fun canReplaceGatewayCandidate(current: DiscoveredMacGateway?, candidate: DiscoveredMacGateway): Boolean =
    current == null || current.serviceId.startsWith("signed-endpoint:") || current.serviceId == candidate.serviceId

internal fun buildTaskEventsWssUrl(hostname: String, port: Int, idempotencyKey: String, afterSeq: Long): String {
    require(hostname.matches(Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.local", RegexOption.IGNORE_CASE)))
    require(port in 1024..65535)
    require(idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}")))
    require(afterSeq >= 0)
    return "wss://${hostname.lowercase(Locale.ROOT)}:$port/v1/jarvis/link/tasks/$idempotencyKey/events/ws?after_seq=$afterSeq"
}

internal fun reconnectDelayMs(attempt: Int): Long {
    require(attempt >= 0)
    return (1_000L shl attempt.coerceAtMost(5)).coerceAtMost(30_000L)
}

internal fun buildTaskEventAck(supervisorTaskId: String, sequence: Long): String {
    require(supervisorTaskId.matches(Regex("[0-9a-f]{32}")))
    require(sequence > 0)
    return JSONObject().put("type", "ack").put("protocol", 1).put("task_id", supervisorTaskId).put("seq", sequence).toString()
}

/** Shared gate for both event and checkpoint persistence: false/throw never reaches the ACK action. */
internal fun persistBeforeTaskEventAck(persist: () -> Boolean, acknowledge: () -> Unit): Boolean {
    val persisted = runCatching(persist).getOrDefault(false)
    if (!persisted) return false
    acknowledge()
    return true
}

internal data class TaskEventReady(val afterSequence: Long, val latestSequence: Long)

internal fun parseTaskEventReady(frame: JSONObject): TaskEventReady? {
    if (frame.optString("type") != "ready" || frame.optInt("protocol", -1) != 1) return null
    val after = frame.optLong("after_seq", -1L)
    val latest = frame.optLong("latest_seq", -1L)
    if (after < 0 || latest < after) return null
    return TaskEventReady(after, latest)
}

internal fun parseTaskEventCheckpoint(frame: JSONObject, idempotencyKey: String): TaskEventCursorCheckpoint? {
    if (frame.optString("type") != "checkpoint" || frame.optInt("protocol", -1) != 1) return null
    val taskId = frame.optString("task_id")
    if (!taskId.matches(Regex("[0-9a-f]{32}"))) return null
    if (!idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) return null
    val sequence = frame.optLong("seq", -1L)
    if (sequence <= 0) return null
    return TaskEventCursorCheckpoint(idempotencyKey, taskId, sequence)
}

internal fun matchesPinnedGatewayCertificate(encodedCertificate: ByteArray, expectedFingerprint: String): Boolean {
    if (!expectedFingerprint.matches(Regex("sha256:[0-9a-f]{64}"))) return false
    val expected = expectedFingerprint.removePrefix("sha256:").toByteArray(Charsets.US_ASCII)
    val actual = MessageDigest.getInstance("SHA-256").digest(encodedCertificate)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }.toByteArray(Charsets.US_ASCII)
    return MessageDigest.isEqual(actual, expected)
}

internal fun matchesExactGatewayDnsSan(names: Collection<List<*>>?, expectedHostname: String): Boolean {
    if (!expectedHostname.matches(Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.local", RegexOption.IGNORE_CASE))) return false
    return names.orEmpty().any { entry ->
        entry.size >= 2 && entry[0] == 2 && (entry[1] as? String)?.equals(expectedHostname, ignoreCase = true) == true
    }
}

internal fun parseTaskEvent(frame: JSONObject, idempotencyKey: String): TaskEventEnvelope? {
    if (frame.optString("type") != "event" || frame.optInt("protocol", -1) != 1) return null
    val taskId = frame.optString("task_id")
    if (!taskId.matches(Regex("[0-9a-f]{32}"))) return null
    if (!idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) return null
    val source = frame.optJSONObject("event") ?: return null
    val sourceSeq = source.optLong("seq", -1L)
    val eventType = source.optString("event_type")
    val createdAt = source.optString("created_at")
    if (sourceSeq <= 0 || eventType !in SAFE_TASK_EVENT_TYPES || createdAt.length > 64) return null
    val payload = source.optJSONObject("payload") ?: JSONObject()
    val stateText = payload.optString("to").takeIf(String::isNotBlank)
        ?: payload.optString("state").takeIf(String::isNotBlank)
    val message = when (eventType) {
        "task.state" -> stateText?.let { "Task state: $it" } ?: "Task state updated"
        "task.recovered_paused" -> "Task paused for safe recovery"
        "task.cancelled" -> "Task cancelled"
        "link_task.queued" -> "Task queued on Mac"
        "link_task.started" -> "Mac started the task"
        "link_task.waiting_approval" -> "Task is waiting for your approval"
        "link_task.approval_consumed", "approval.consumed" -> "Approval accepted; task continuation recorded"
        "link_task.approval_rejected" -> "Approval rejected; task will not run"
        "link_task.completed" -> "Task completed"
        "link_task.failed" -> "Task failed"
        "approval.created" -> "Approval request created"
        "approval.decision" -> payload.optString("decision").takeIf(String::isNotBlank)?.let { "Approval $it" } ?: "Approval decision recorded"
        "approval.expired" -> "Approval expired"
        "approval.execution_completed" -> "Approved operation completed"
        "approval.execution_failed" -> "Approved operation failed"
        "tool.mcp_approval_dispatch_claimed" -> "Authorized operation dispatch claimed"
        "approval.mac_mcp_dispatched" -> "Authorized operation dispatched"
        "development.tool_performance" -> "Development tool step completed"
        else -> return null
    }
    val tool = payload.optString("tool").ifBlank { payload.optString("tool_name") }.take(80)
    val duration = if (payload.has("duration_ms") && !payload.isNull("duration_ms")) payload.optLong("duration_ms", -1L) else -1L
    return TaskEventEnvelope(
        idempotencyKey = idempotencyKey,
        supervisorTaskId = taskId,
        sequence = sourceSeq,
        event = TaskEvent(
            id = sourceSeq.toString(),
            at = createdAt,
            state = eventType,
            message = message,
            toolName = tool,
            durationMs = duration.takeIf { it in 0..600_000L },
        ),
        taskState = stateText?.take(80),
    )
}

private val SAFE_TASK_EVENT_TYPES = setOf(
    "task.state", "task.recovered_paused", "task.cancelled", "task.explicit_resume",
    "link_task.queued", "link_task.started", "link_task.waiting_approval",
    "link_task.approval_consumed", "link_task.approval_rejected", "link_task.completed", "link_task.failed",
    "approval.consumed", "approval.created", "approval.decision", "approval.expired",
    "approval.execution_completed", "approval.execution_failed", "approval.mac_mcp_dispatched",
    "tool.mcp_approval_dispatch_claimed", "development.tool_performance",
)
