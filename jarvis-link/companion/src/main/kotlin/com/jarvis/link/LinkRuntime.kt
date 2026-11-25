package com.jarvis.link

import java.io.File
import java.security.KeyFactory
import java.security.KeyPair
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec

/** Supplies this companion's signing and ECDH identities. Android uses Keystore-backed keys. */
interface LinkKeyProvider {
    fun identities(): Pair<SigningIdentity, EncryptionIdentity>
}

/** Reads and writes the companion's non-secret state (tenant, pinned root, certificates, enrollment). */
interface LinkStateStore {
    fun read(): String?
    fun write(json: String)
}

/** File-backed state store. Contains no private keys. */
class FileLinkStateStore(private val file: File) : LinkStateStore {
    override fun read(): String? = if (file.isFile) file.readText(Charsets.UTF_8) else null
    override fun write(json: String) = writeAtomically(file, json)
}

/**
 * TEST-ONLY software keys in a plain file. Never used by the Android app, which uses Keystore keys.
 * Lives only in temporary test directories created by the integration harness.
 */
class SoftwareTestKeyProvider(private val file: File) : LinkKeyProvider {
    override fun identities(): Pair<SigningIdentity, EncryptionIdentity> {
        if (!file.isFile) {
            val signing = LinkCrypto.newSigningIdentity()
            val encryption = LinkCrypto.newEncryptionIdentity(signing)
            writeAtomically(file, LinkJson.encode(mapOf(
                "signing_private_pkcs8" to LinkCrypto.b64url(signing.keyPair.private.encoded),
                "signing_public_spki" to LinkCrypto.b64url(signing.keyPair.public.encoded),
                "encryption_private_pkcs8" to LinkCrypto.b64url(encryption.keyPair.private.encoded),
                "encryption_public_spki" to LinkCrypto.b64url(encryption.keyPair.public.encoded),
            )))
        }
        val f = LinkJson.parseObject(file.readText(Charsets.UTF_8))
        val factory = KeyFactory.getInstance("EC")
        fun pub(v: String) = factory.generatePublic(X509EncodedKeySpec(LinkCrypto.decodeBase64Url(v)))
        fun priv(v: String) = factory.generatePrivate(PKCS8EncodedKeySpec(LinkCrypto.decodeBase64Url(v)))
        val signingPair = KeyPair(pub(f.string("signing_public_spki")), priv(f.string("signing_private_pkcs8")))
        val encryptionPair = KeyPair(pub(f.string("encryption_public_spki")), priv(f.string("encryption_private_pkcs8")))
        val signing = LinkCrypto.newSigningIdentity(LinkCrypto.deviceIdForSigningPublicKey(signingPair.public.encoded), signingPair)
        return signing to EncryptionIdentity(signing.deviceId, encryptionPair)
    }
}

/** Result of a status read. Never includes key material. */
data class LinkStatus(
    val deviceId: String?,
    val tenantId: String?,
    val enrollmentPending: Boolean,
    val certificatesVerified: Boolean,
    val outboxSize: Int,
)

/** Outcome of durably consuming one task-scoped WSS event or checkpoint. */
enum class TaskEventStreamPersistResult { PERSISTED, ALREADY_PERSISTED }

/**
 * Every companion operation the app and the CLI need. Relay access is always the real HTTP client, and
 * every message is verified and decrypted by the production Kotlin protocol code.
 */
/** Result of one sync. [newApprovals] are requests not announced before; [error] is a code, never relay text. */
data class SyncReport(
    val newApprovals: List<ApprovalReview>,
    val updates: List<Map<String, Any>>,
    val flushed: Int,
    val error: String,
)

class LinkRuntime(
    private val keys: LinkKeyProvider,
    private val state: LinkStateStore,
    private val storeDir: File,
    private val now: () -> Long = System::currentTimeMillis,
    /**
     * Optional alternative path to the relay. Returning null keeps the default HTTPS/loopback transport
     * (USB/adb reverse or a configured relay). The same signed requests travel either way.
     */
    private val transportFactory: ((String) -> RelayTransport?)? = null,
) {
    companion object {
        private val stateLock = Any()
        private val syncLock = Any()
        private val terminalStates = setOf("COMPLETED", "FAILED", "CANCELLED", "REJECTED", "EXPIRED", "INDETERMINATE")
        private const val QUEUED_DELIVERY_RECONCILIATION_MS = 2 * 60 * 1000L
    }

    private fun isStaleAfterTerminal(entry: Map<String, Any>, newState: String): Boolean =
        (entry["state"] as? String)?.uppercase() in terminalStates && newState.uppercase() !in terminalStates

    private fun load(): Map<String, Any> = synchronized(stateLock) { state.read()?.let { LinkJson.parseObject(it) } ?: emptyMap() }

    private fun save(values: Map<String, Any?>) = synchronized(stateLock) {
        val merged = linkedMapOf<String, Any?>()
        merged.putAll(load())
        merged.putAll(values)
        state.write(LinkJson.encode(merged))
    }

    private fun relay(relayUrl: String, tenant: String) =
        HttpJarvisLinkRelay(relayUrl, tenant, transport = transportFactory?.invoke(relayUrl))

    /** Starts enrollment. Pins the host root supplied by the operator; the relay is not trusted for it. */
    fun enroll(relayUrl: String, invitation: RelayInvitation, deviceName: String, root: TrustedHostRoot): Map<String, Any> {
        val (signing, encryption) = keys.identities()
        val enrollment = CompanionEnrollment(signing, encryption, invitation.tenantId, invitation, deviceName, now)
        val relay = relay(relayUrl, invitation.tenantId)
        val challengeMap = relay.enrollmentChallenge(enrollment.challengeRequest().toJson())
        val challenge = EnrollmentChallenge(
            tenantId = invitation.tenantId,
            invitationId = invitation.invitationId,
            challengeId = challengeMap.string("challenge_id"),
            challengeBase64Url = challengeMap.string("challenge_b64url"),
            deviceId = challengeMap.string("device_id"),
            expiresAtUnixMs = challengeMap.long("expires_at_unix_ms"),
        )
        val completion = relay.enrollmentComplete(enrollment.signChallenge(challenge).toJson())
        save(mapOf(
            "tenant_id" to invitation.tenantId,
            "device_id" to signing.deviceId,
            "host_root_device_id" to root.deviceId,
            "host_root_public_key" to root.signingPublicKeyBase64Url,
            "companion_certificate" to null,
            "host_certificate" to null,
            "enrollment_pending" to true,
        ))
        return mapOf("device_id" to completion.string("device_id"), "status" to completion.string("status"))
    }

    /** Fetches this device's and the host's certificates and verifies both against the pinned root. */
    fun sync(relayUrl: String): Map<String, Any> {
        val (signing, _) = keys.identities()
        val values = load()
        val tenant = values.string("tenant_id")
        val root = rootOf(values)
        val relay = relay(relayUrl, tenant)
        val own = relay.deviceCertificate(signing.deviceId, signedGet(signing, tenant, "/v1/devices/${signing.deviceId}/certificate"))
        check(own.verify(root, tenant, now())) { "own certificate rejected by pinned root" }
        check(own.deviceRole == "companion" && own.deviceId == signing.deviceId) { "certificate role/identity mismatch" }
        check(own.signingPublicKeyBase64Url == LinkCrypto.b64url(signing.keyPair.public.encoded)) { "certificate key mismatch" }
        val host = relay.deviceCertificate(root.deviceId, signedGet(signing, tenant, "/v1/devices/${root.deviceId}/certificate"))
        check(host.verify(root, tenant, now())) { "host certificate rejected by pinned root" }
        check(host.deviceRole == "host") { "pinned root certificate is not a host" }
        save(mapOf(
            "companion_certificate" to LinkStateJson.certificateToMap(own),
            "host_certificate" to LinkStateJson.certificateToMap(host),
            "enrollment_pending" to false,
        ))
        return mapOf("companion_device_id" to own.deviceId, "host_device_id" to host.deviceId, "verified" to true)
    }

    fun submit(relayUrl: String, submission: TaskSubmission, ttlMs: Long?): DeliveryResult =
        companion(relayUrl).submitTask(submission, ttlMs)

    /** Submits an allowlisted deterministic command through the signed encrypted outbox. */
    fun submitFastCommand(relayUrl: String, command: FastCommandRequest, clientRequestId: String): DeliveryResult =
        companion(relayUrl).submitFastCommand(command, clientRequestId = clientRequestId)

    /** Polls the relay and keeps approval requests and task updates in local (non-secret) state until decided. */
    fun poll(relayUrl: String, limit: Int = 20): PollResult {
        return companion(relayUrl).pollInbox(limit, ::persistInboxEvent)
    }

    /** Approval requests not yet decided and not expired, exactly as the Mac sent them. */
    fun pendingApprovals(): List<ApprovalReview> {
        val nowMs = now()
        return storedApprovals().filter { it.expiresAtUnixMs > nowMs }
    }

    /**
     * One background or foreground sync: retries queued sends, polls the relay, and returns approval
     * requests that have not been announced yet. Announced IDs are stored so a request notifies once.
     * Errors are reported as codes; queued messages stay queued for the next attempt.
     */
    fun syncNow(relayUrl: String): SyncReport = synchronized(syncLock) {
        var flushed = 0
        var error = ""
        try {
            flushed = flush(relayUrl)["delivered"] as Int
        } catch (failure: Exception) {
            error = errorCode(failure)
        }
        reconcileOrphanedQueuedRequests()
        val newlyPending = mutableListOf<ApprovalReview>()
        try {
            companion(relayUrl).pollInbox(20, ::persistInboxEvent)
            val announced = announcedIds()
            pendingApprovals().filter { it.approvalId !in announced }.forEach { newlyPending.add(it) }
            markAnnounced(newlyPending.map { it.approvalId })
        } catch (failure: Exception) {
            error = errorCode(failure)
        }
        save(mapOf("last_sync_ms" to now(), "last_sync_error" to error))
        return SyncReport(newlyPending, recentUpdates(), flushed, error)
    }

    private fun errorCode(failure: Exception): String = when (failure) {
        is RelayHttpException -> failure.code
        is SecurityException -> "AUTH_FAILED"
        is java.io.IOException -> "NETWORK_UNAVAILABLE"
        else -> "PROTOCOL_ERROR"
    }

    private fun announcedIds(): Set<String> =
        ((load()["announced_approval_ids"] as? List<String>) ?: emptyList()).toSet()

    private fun markAnnounced(ids: List<String>) = synchronized(stateLock) {
        if (ids.isEmpty()) return@synchronized
        val merged = (announcedIds() + ids).toList().takeLast(200)
        save(mapOf("announced_approval_ids" to merged))
    }

    /** Recent task updates from the Mac (task.result payloads), newest last. */
    fun recentUpdates(): List<Map<String, Any>> =
        (load()["updates"] as? List<Map<String, Any>>) ?: emptyList()

    /** Last locally persisted approval-decision state, keyed by approval ID. */
    fun approvalDecisionState(approvalId: String): Map<String, Any>? =
        approvalDecisionStates()[approvalId]

    private fun approvalDecisionStates(): Map<String, Map<String, Any>> {
        val raw = load()["approval_decision_states"] as? Map<*, *> ?: return emptyMap()
        return raw.entries.mapNotNull { (key, value) ->
            val id = key as? String ?: return@mapNotNull null
            val fields = value as? Map<*, *> ?: return@mapNotNull null
            val safe = linkedMapOf<String, Any>()
            fields.forEach { (k, v) -> if (k is String && v != null) safe[k] = v }
            id to safe
        }.toMap()
    }

    /**
     * Sends an approve or reject decision for one pending request. The companion checks expiry and the
     * exact digest locally; the Mac supervisor checks them again and enforces single use.
     */
    fun decideApproval(relayUrl: String, approvalId: String, decision: String): Map<String, Any> {
        require(decision == "approve" || decision == "reject") { "decision must be approve or reject" }
        val review = pendingApprovals().firstOrNull { it.approvalId == approvalId }
            ?: throw IllegalStateException("approval is not pending on this phone (expired or already decided)")
        val existing = approvalDecisionStates()[approvalId]
        if (existing != null) {
            require(existing["decision"] == decision) { "a different decision is already in flight for this approval" }
            if (existing["authoritative"] == true || existing["state"] != "DELIVERY_FAILED") return existing
        }
        val continuationObjective = if (decision == "approve") {
            conversation().firstOrNull { it["task_id"] == review.taskId }?.get("text") as? String
        } else null
        val idempotencyKey = approvalDecisionIdempotencyKey(review, decision)
        val submitting = linkedMapOf<String, Any>(
            "decision" to decision,
            "approval_id" to approvalId,
            "task_id" to review.taskId,
            "idempotency_key" to idempotencyKey,
            "state" to "SUBMITTING",
            "authoritative" to false,
            "updated_at_unix_ms" to now(),
        )
        saveDecisionState(approvalId, submitting)
        return try {
            val result = companion(relayUrl).decideApproval(
                review,
                decision,
                review.canonicalArgumentsDigest,
                idempotencyKey,
                continuationObjective,
            )
            val submitted = LinkedHashMap(submitting).apply {
                put("message_id", result.messageId)
                put("queued_offline", result.queuedOffline)
                put("relay_status", result.receipt?.status ?: "queued_locally")
                put("state", if (result.queuedOffline) "QUEUED" else "DELIVERED_WAITING_SUPERVISOR")
                put("updated_at_unix_ms", now())
            }
            saveDecisionState(approvalId, submitted)
            approvalDecisionState(approvalId) ?: submitted
        } catch (failure: Exception) {
            val failed = LinkedHashMap(submitting).apply {
                put("state", "DELIVERY_FAILED")
                put("error_code", errorCode(failure))
                put("updated_at_unix_ms", now())
            }
            saveDecisionState(approvalId, failed)
            throw failure
        }
    }

    private fun approvalDecisionIdempotencyKey(review: ApprovalReview, decision: String): String = LinkCrypto.hex(
        LinkCrypto.sha256(("jarvis-link-approval-decision-v1|${review.approvalId}|${review.canonicalArgumentsDigest}|$decision")
            .toByteArray(Charsets.UTF_8)).copyOfRange(0, 16),
    )

    private fun saveDecisionState(approvalId: String, state: Map<String, Any>) = synchronized(stateLock) {
        val merged = approvalDecisionStates().toMutableMap()
        if (merged[approvalId]?.get("authoritative") == true) return@synchronized
        merged[approvalId] = state
        save(mapOf("approval_decision_states" to merged))
    }

    private fun storedApprovals(): List<ApprovalReview> =
        (load()["approvals"] as? List<Map<String, Any>>)?.map { m ->
            ApprovalReview(
                approvalId = m.string("approval_id"),
                taskId = m.string("task_id"),
                operation = m.string("operation"),
                target = m.string("target"),
                canonicalArgumentsDigest = m.string("canonical_arguments_digest"),
                expiresAtUnixMs = m.long("expires_at_unix_ms"),
                redactedSummary = m.string("redacted_summary"),
                developmentPlan = m["development_plan"]?.let(DevelopmentPlanReview::fromMap),
            )
        } ?: emptyList()

    private fun approvalMap(review: ApprovalReview): Map<String, Any> = linkedMapOf<String, Any>(
        "approval_id" to review.approvalId,
        "task_id" to review.taskId,
        "operation" to review.operation,
        "target" to review.target,
        "canonical_arguments_digest" to review.canonicalArgumentsDigest,
        "expires_at_unix_ms" to review.expiresAtUnixMs,
        "redacted_summary" to review.redactedSummary,
    ).also { fields -> review.developmentPlan?.let { fields["development_plan"] = it.toWire() } }

    /** Messages the user sent from Home, newest last. Replies from the Mac update them by in_reply_to. */
    fun conversation(): List<Map<String, Any>> =
        (load()["conversation"] as? List<Map<String, Any>>) ?: emptyList()

    /** Durable phone-side task index and ordered lifecycle events, independent of conversation UI lifetime. */
    fun taskRegistry(): List<Map<String, Any>> = (load()["task_registry"] as? List<Map<String, Any>>) ?: emptyList()
    fun taskEventHistory(): List<Map<String, Any>> = (load()["task_event_history"] as? List<Map<String, Any>>) ?: emptyList()

    /** Updates only the selected response transport for an existing idempotent request. */
    fun updateLocalTaskTransport(requestId: String, transport: String): Boolean = synchronized(stateLock) {
        require(requestId.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "invalid request ID" }
        require(transport in setOf("api", "link", "telegram")) { "invalid conversation transport" }
        val rows = conversation().toMutableList()
        val index = rows.indexOfFirst { it["message_id"] == requestId }
        if (index < 0) return@synchronized false
        rows[index] = LinkedHashMap(rows[index]).apply { put("transport", transport); put("updated_at_unix_ms", now()) }
        save(mapOf("conversation" to rows))
        true
    }

    /**
     * Returns the durable event cursor for one submitted request. Cursors are per request because the
     * WSS route is task-scoped even though the server's TaskEvent sequence is globally increasing.
     */
    fun taskEventStreamCursor(requestId: String): Long = synchronized(stateLock) {
        require(requestId.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "invalid request ID" }
        val cursors = (load()["task_event_stream_cursors"] as? Map<*, *>) ?: emptyMap<Any, Any>()
        (cursors[requestId] as? Number)?.toLong() ?: 0L
    }

    /**
     * Atomically persists a sanitized server event, its task/conversation projection, and the WSS
     * resume cursor. Callers may acknowledge the sequence only after this method returns. A repeated
     * sequence is an idempotent success; an unknown request or mismatched supervisor task is rejected.
     */
    fun persistTaskEventStreamEvent(
        requestId: String,
        supervisorTaskId: String,
        sequence: Long,
        eventType: String,
        createdAt: String,
        taskState: String?,
        summary: String,
        toolName: String = "",
        durationMs: Long? = null,
    ): TaskEventStreamPersistResult = synchronized(stateLock) {
        require(requestId.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "invalid request ID" }
        require(supervisorTaskId.matches(Regex("[A-Za-z0-9._:-]{1,128}"))) { "invalid supervisor task ID" }
        require(sequence > 0) { "invalid task event sequence" }
        require(eventType.matches(Regex("[a-z][a-z0-9_.-]{0,79}"))) { "invalid task event type" }
        require(createdAt.length in 1..64 && createdAt.none(Char::isISOControl)) { "invalid event timestamp" }
        require(summary.length <= 1024 && summary.none(Char::isISOControl)) { "invalid task event summary" }
        require(toolName.length <= 80 && toolName.none(Char::isISOControl)) { "invalid tool name" }
        require(durationMs == null || durationMs in 0..600_000L) { "invalid task event duration" }

        val current = load()
        val cursors = ((current["task_event_stream_cursors"] as? Map<*, *>) ?: emptyMap<Any, Any>())
            .entries.mapNotNull { (key, value) ->
                val id = key as? String ?: return@mapNotNull null
                val seq = (value as? Number)?.toLong() ?: return@mapNotNull null
                id to seq
            }.toMap().toMutableMap()
        val previousCursor = cursors[requestId] ?: 0L
        if (sequence <= previousCursor) return@synchronized TaskEventStreamPersistResult.ALREADY_PERSISTED

        val conversations = conversation().toMutableList()
        val entryIndex = conversations.indexOfFirst { it["message_id"] == requestId }
        require(entryIndex >= 0) { "task event request is not present in the durable conversation" }
        val previousEntry = conversations[entryIndex]
        val boundTask = (previousEntry["task_id"] as? String).orEmpty()
        require(boundTask.isBlank() || boundTask == requestId || boundTask == supervisorTaskId) { "task event belongs to a different supervisor task" }

        val normalizedState = normalizeStreamTaskState(taskState)
            ?: when (eventType) {
                "link_task.queued" -> "QUEUED"
                "link_task.started", "tool.mcp_approval_dispatch_claimed", "approval.mac_mcp_dispatched" -> "RUNNING"
                "link_task.waiting_approval" -> "WAITING_APPROVAL"
                "link_task.completed", "approval.execution_completed" -> "COMPLETED"
                "link_task.failed", "approval.execution_failed" -> "FAILED"
                "link_task.approval_rejected" -> "REJECTED"
                "approval.expired" -> "EXPIRED"
                "task.cancelled" -> "CANCELLED"
                else -> (previousEntry["state"] as? String)?.uppercase() ?: "RUNNING"
            }
        val eventTime = runCatching { java.time.Instant.parse(createdAt).toEpochMilli() }.getOrDefault(now())
        val eventId = "wss:$requestId:$sequence"

        val history = ((current["task_event_history"] as? List<Map<String, Any>>) ?: emptyList()).toMutableList()
        if (history.none { it["event_id"] == eventId }) {
            val row = linkedMapOf<String, Any>(
                "event_id" to eventId,
                "task_id" to requestId,
                "supervisor_task_id" to supervisorTaskId,
                "sequence" to sequence,
                "status" to normalizedState,
                "event_type" to eventType,
                "timestamp_unix_ms" to eventTime,
                "event" to streamEventLabel(eventType),
                "summary" to summary,
            )
            if (toolName.isNotBlank()) row["tool_name"] = toolName
            durationMs?.let { row["duration_ms"] = it }
            history.add(row)
        }

        val registry = ((current["task_registry"] as? List<Map<String, Any>>) ?: emptyList()).toMutableList()
        val oldRegistry = registry.firstOrNull { it["request_id"] == requestId }
        val preserveTerminal = (oldRegistry?.get("status") as? String)?.uppercase() in terminalStates && normalizedState !in terminalStates
        val nextStatus = if (preserveTerminal) oldRegistry?.get("status") as? String ?: normalizedState else normalizedState
        val registryRow = linkedMapOf<String, Any>(
            "task_id" to requestId,
            "request_id" to requestId,
            "supervisor_task_id" to supervisorTaskId,
            "execution_target" to (oldRegistry?.get("execution_target") as? String ?: "MAC"),
            "status" to nextStatus,
            "created_at_unix_ms" to ((oldRegistry?.get("created_at_unix_ms") as? Number)?.toLong() ?: eventTime),
            "updated_at_unix_ms" to eventTime,
            "summary" to summary,
        )
        if (nextStatus in terminalStates) {
            registryRow["completed_at_unix_ms"] = (oldRegistry?.get("completed_at_unix_ms") as? Number)?.toLong() ?: eventTime
        }
        registry.removeAll { it["request_id"] == requestId }
        registry.add(registryRow)

        val conversationRow = LinkedHashMap(previousEntry)
        val preserveConversationTerminal = isStaleAfterTerminal(previousEntry, normalizedState)
        if (!preserveConversationTerminal) {
            conversationRow["state"] = normalizedState
            conversationRow["summary"] = summary
            conversationRow["updated_at_unix_ms"] = eventTime
            conversationRow["task_id"] = supervisorTaskId
            conversationRow["progress_seq"] = sequence
        }
        conversations[entryIndex] = conversationRow

        val updates = ((current["updates"] as? List<Map<String, Any>>) ?: emptyList()).toMutableList()
        if (updates.none { it["task_id"] == supervisorTaskId && (it["event_seq"] as? Number)?.toLong() == sequence }) {
            updates.add(linkedMapOf(
                "task_id" to supervisorTaskId,
                "in_reply_to" to requestId,
                "state" to normalizedState,
                "summary" to summary,
                "event_seq" to sequence,
                "event_type" to eventType,
                "created_at" to createdAt,
                "received_at_unix_ms" to now(),
            ))
        }
        cursors[requestId] = sequence

        save(mapOf(
            "conversation" to conversations.takeLast(100),
            "task_registry" to registry.takeLast(500),
            "task_event_history" to history.takeLast(2000),
            "updates" to updates.takeLast(200),
            "task_event_stream_cursors" to cursors,
        ))
        TaskEventStreamPersistResult.PERSISTED
    }

    /** Persists a server high-water checkpoint after all preceding public events have been saved. */
    fun persistTaskEventStreamCheckpoint(
        requestId: String,
        supervisorTaskId: String,
        sequence: Long,
    ): TaskEventStreamPersistResult = synchronized(stateLock) {
        require(requestId.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "invalid request ID" }
        require(supervisorTaskId.matches(Regex("[A-Za-z0-9._:-]{1,128}"))) { "invalid supervisor task ID" }
        require(sequence >= 0) { "invalid checkpoint sequence" }
        val current = load()
        val conversations = conversation().toMutableList()
        val entryIndex = conversations.indexOfFirst { it["message_id"] == requestId }
        require(entryIndex >= 0) { "checkpoint request is not present in the durable conversation" }
        val entry = LinkedHashMap(conversations[entryIndex])
        val boundTask = (entry["task_id"] as? String).orEmpty()
        require(boundTask.isBlank() || boundTask == requestId || boundTask == supervisorTaskId) { "checkpoint belongs to a different supervisor task" }
        entry["task_id"] = supervisorTaskId
        conversations[entryIndex] = entry

        val cursors = ((current["task_event_stream_cursors"] as? Map<*, *>) ?: emptyMap<Any, Any>())
            .entries.mapNotNull { (key, value) ->
                val id = key as? String ?: return@mapNotNull null
                val seq = (value as? Number)?.toLong() ?: return@mapNotNull null
                id to seq
            }.toMap().toMutableMap()
        val old = cursors[requestId] ?: 0L
        if (sequence <= old) return@synchronized TaskEventStreamPersistResult.ALREADY_PERSISTED
        cursors[requestId] = sequence
        val registry = ((current["task_registry"] as? List<Map<String, Any>>) ?: emptyList()).toMutableList()
        val registryIndex = registry.indexOfFirst { it["request_id"] == requestId }
        if (registryIndex >= 0) {
            registry[registryIndex] = LinkedHashMap(registry[registryIndex]).apply { put("supervisor_task_id", supervisorTaskId) }
        }
        save(mapOf("task_event_stream_cursors" to cursors, "conversation" to conversations, "task_registry" to registry))
        TaskEventStreamPersistResult.PERSISTED
    }

    private fun normalizeStreamTaskState(value: String?): String? = when (value?.trim()?.uppercase()) {
        "CREATED" -> "CREATED"
        "QUEUED", "SENT", "DELIVERED_WAITING_SUPERVISOR", "UNCONFIRMED" -> "QUEUED"
        "WAITING_APPROVAL" -> "WAITING_APPROVAL"
        "WAITING_CONNECTION", "PAUSED" -> "WAITING_CONNECTION"
        "RUNNING", "PLANNING", "VERIFYING" -> "RUNNING"
        "COMPLETED" -> "COMPLETED"
        "FAILED", "REJECTED", "EXPIRED", "INDETERMINATE" -> "FAILED"
        "CANCELLED" -> "CANCELLED"
        else -> null
    }

    private fun streamEventLabel(eventType: String): String = when (eventType) {
        "task.state" -> "Task status changed"
        "task.recovered_paused" -> "Task paused for safe recovery"
        "task.cancelled", "link_task.cancelled" -> "Task cancelled"
        "link_task.queued" -> "Task queued on Mac"
        "link_task.started" -> "Mac started the task"
        "link_task.waiting_approval" -> "Waiting for owner approval"
        "link_task.approval_consumed", "approval.consumed" -> "Approval accepted"
        "link_task.approval_rejected" -> "Approval rejected"
        "link_task.completed" -> "Task completed"
        "link_task.failed" -> "Task failed"
        "approval.created" -> "Approval request created"
        "approval.decision" -> "Approval decision recorded"
        "approval.expired" -> "Approval expired"
        "approval.execution_completed" -> "Approved operation completed"
        "approval.execution_failed" -> "Approved operation failed"
        "approval.mac_mcp_dispatched" -> "Authorized operation dispatched"
        "tool.mcp_approval_dispatch_claimed" -> "Authorized operation dispatch claimed"
        "development.tool_performance" -> "Development step completed"
        else -> "Task event"
    }

    private fun normalizedTaskState(value: String): String = when (value.trim().uppercase()) {
        "CREATED" -> "CREATED"
        "QUEUED", "SENT", "DELIVERED_WAITING_SUPERVISOR", "UNCONFIRMED" -> "QUEUED"
        "WAITING_APPROVAL" -> "WAITING_APPROVAL"
        "WAITING_CONNECTION", "PAUSED" -> "WAITING_CONNECTION"
        "COMPLETED" -> "COMPLETED"
        "FAILED", "REJECTED", "EXPIRED", "INDETERMINATE" -> "FAILED"
        "CANCELLED" -> "CANCELLED"
        else -> "RUNNING"
    }

    private fun recordTaskLifecycleLocked(
        requestId: String, supervisorTaskId: String, state: String, summary: String,
        target: String, eventId: String,
    ) {
        val time = now()
        val records = taskRegistry().toMutableList()
        val old = records.firstOrNull { it["request_id"] == requestId }
        val normalized = normalizedTaskState(state)
        val terminal = setOf("COMPLETED", "FAILED", "CANCELLED")
        if ((old?.get("status") as? String) in terminal && normalized !in terminal) return
        val isTerminal = normalized in terminal
        val record = linkedMapOf<String, Any>(
            "task_id" to requestId, "request_id" to requestId,
            "supervisor_task_id" to supervisorTaskId,
            "execution_target" to (old?.get("execution_target") as? String ?: target),
            "status" to normalized,
            "created_at_unix_ms" to (old?.get("created_at_unix_ms") as? Long ?: time),
            "updated_at_unix_ms" to time,
            "summary" to summary,
        ).also { row -> if (isTerminal) row["completed_at_unix_ms"] = old?.get("completed_at_unix_ms") as? Long ?: time }
        val nextRecords = records.filterNot { it["request_id"] == requestId }.plus(record).takeLast(500)
        val oldEvents = taskEventHistory()
        val duplicate = oldEvents.any { it["event_id"] == eventId }
        val newEvents = if (duplicate) oldEvents else oldEvents.plus(linkedMapOf<String, Any>(
            "event_id" to eventId, "task_id" to requestId, "supervisor_task_id" to supervisorTaskId,
            "status" to normalized, "timestamp_unix_ms" to time,
            "event" to when (normalized) {
                "CREATED" -> "Task created on phone"
                "QUEUED" -> "Task queued for delivery"
                "WAITING_APPROVAL" -> "Waiting for owner approval"
                "WAITING_CONNECTION" -> "Waiting for connection"
                "COMPLETED" -> "Task completed"
                "FAILED" -> "Task failed"
                "CANCELLED" -> "Task cancelled"
                else -> "Task running"
            }, "summary" to summary,
        )).takeLast(2000)
        save(mapOf("task_registry" to nextRecords, "task_event_history" to newEvents))
    }

    /** Stores a local USB task under its API idempotency key before the request can time out. */
    fun recordLocalTaskRequest(
        idempotencyKey: String,
        text: String,
        transport: String = "api",
        capabilityScope: String = "assistant",
        lookupLinkStatus: Boolean = true,
        executionTarget: String = "MAC",
    ) = synchronized(stateLock) {
        require(idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "invalid task idempotency key" }
        require(transport in setOf("api", "link", "telegram")) { "invalid conversation transport" }
        require(capabilityScope in setOf("assistant", "development")) { "invalid task capability scope" }
        require(executionTarget in setOf("PHONE", "MAC")) { "invalid execution target" }
        val cleaned = text.trim()
        require(cleaned.isNotEmpty() && cleaned.toByteArray(Charsets.UTF_8).size <= 4096) { "message must be 1–4096 bytes" }
        val entries = conversation().filterNot { it["message_id"] == idempotencyKey }.toMutableList()
        entries.add(linkedMapOf(
            "message_id" to idempotencyKey,
            "text" to cleaned,
            "created_at_unix_ms" to now(),
            "state" to "CREATED",
            "summary" to "",
            "task_id" to "",
            "transport" to transport,
            "capability_scope" to capabilityScope,
            "lookup_link_status" to (lookupLinkStatus && executionTarget == "MAC"),
            "updated_at_unix_ms" to now(),
        ))
        save(mapOf("conversation" to entries.takeLast(100)))
        val existingTask = taskRegistry().firstOrNull { it["request_id"] == idempotencyKey }
        if (existingTask == null) recordTaskLifecycleLocked(idempotencyKey, "", "CREATED", "", executionTarget, "$idempotencyKey:created")
    }

    /** Persists an actual local or Mac-reported lifecycle transition; duplicate event IDs are ignored. */
    fun recordTaskLifecycle(requestId: String, supervisorTaskId: String, state: String, summary: String, target: String = "MAC", eventId: String? = null) = synchronized(stateLock) {
        require(requestId.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "invalid request ID" }
        require(target in setOf("PHONE", "MAC")) { "invalid execution target" }
        recordTaskLifecycleLocked(requestId, supervisorTaskId, state, summary, target, eventId ?: "$requestId:${state.uppercase()}:$supervisorTaskId")
    }

    /** Records the Mac's actual supervisor result or the last known task state. */
    fun recordLocalTaskResult(idempotencyKey: String, taskId: String, state: String, summary: String) {
        applyToConversation(idempotencyKey, taskId, state.uppercase(), summary)
        recordTaskLifecycle(idempotencyKey, taskId, state, summary)
    }

    /** Corrects only the exact local 404 terminal marker after a fresh authenticated Mac status response. */
    fun reconcileMissingApiTaskResult(idempotencyKey: String, taskId: String, state: String, summary: String): Boolean {
        require(idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "invalid task idempotency key" }
        require(taskId.matches(Regex("[0-9a-f]{32}"))) { "invalid supervisor task ID" }
        val normalizedState = state.trim().uppercase()
        require(normalizedState in setOf("CREATED", "PLANNING", "RUNNING", "VERIFYING", "WAITING_APPROVAL", "PAUSED", "COMPLETED", "FAILED", "CANCELLED")) { "invalid supervisor task state" }
        synchronized(stateLock) {
            var repaired = false
            val entries = conversation().map { entry ->
                if (
                    entry["message_id"] == idempotencyKey && entry["transport"] == "api" &&
                    entry["state"] == "FAILED" && entry["task_id"] == "" &&
                    entry["summary"] == "The Mac no longer has this task record."
                ) {
                    repaired = true
                    LinkedHashMap(entry).apply {
                        put("task_id", taskId)
                        put("state", normalizedState)
                        put("summary", summary)
                        put("updated_at_unix_ms", now())
                    }
                } else entry
            }
            if (repaired) save(mapOf("conversation" to entries))
            return repaired
        }
    }

    /** Sends a typed request to the Mac supervisor. Queued locally if the relay is unreachable. */
    fun sendRequest(relayUrl: String, text: String): Map<String, Any> {
        val cleaned = text.trim()
        require(cleaned.isNotEmpty() && cleaned.toByteArray(Charsets.UTF_8).size <= 4096) { "message must be 1–4096 bytes" }
        val result = submit(relayUrl, TaskSubmission(cleaned, setOf(SafeCapability.ASSISTANT)), null)
        synchronized(stateLock) {
        val entries = conversation().toMutableList()
        entries.add(linkedMapOf(
            "message_id" to result.messageId,
            "text" to cleaned,
            "created_at_unix_ms" to now(),
            "state" to if (result.queuedOffline) "QUEUED" else "SENT",
            "summary" to "",
            "updated_at_unix_ms" to now(),
        ))
        save(mapOf("conversation" to entries.takeLast(100)))
        }
        return linkedMapOf("message_id" to result.messageId, "queued_offline" to result.queuedOffline)
    }

    private fun applyToConversation(messageId: String, taskId: String, state: String, summary: String) = synchronized(stateLock) {
        val entries = conversation().map { entry ->
            if (entry["message_id"] == messageId && !isStaleAfterTerminal(entry, state)) LinkedHashMap(entry).apply {
                put("state", state); put("summary", summary); put("task_id", taskId); put("updated_at_unix_ms", now())
            } else entry
        }
        save(mapOf("conversation" to entries))
    }

    /** Approval/continuation replies may reference the decision envelope; task ID is the stable fallback. */
    private fun applyToConversationTask(taskId: String, state: String, summary: String) = synchronized(stateLock) {
        val entries = conversation().map { entry ->
            if (entry["task_id"] == taskId && !isStaleAfterTerminal(entry, state)) LinkedHashMap(entry).apply {
                put("state", state); put("summary", summary); put("updated_at_unix_ms", now())
            } else entry
        }
        save(mapOf("conversation" to entries))
    }

    /** Result of the last sync attempt: time in Unix ms (0 = never) and an error code ("" = none). */
    fun lastSync(): Pair<Long, String> {
        val values = load()
        return ((values["last_sync_ms"] as? Long) ?: 0L) to ((values["last_sync_error"] as? String) ?: "")
    }

    /** Persists a received event and its replay ID in one state write before the relay is acknowledged. */
    internal fun persistInboxEvent(messageId: String, expiresAt: Long, event: CompanionEvent): Boolean = synchronized(stateLock) {
        val received = ((load()["received_event_ids"] as? Map<String, Long>) ?: emptyMap())
            .filterValues { it > now() }.toMutableMap()
        if (messageId in received) return@synchronized false
        received[messageId] = expiresAt
        recordEventsLocked(listOf(event), received)
        true
    }

    internal fun recordEvents(events: List<CompanionEvent>) = synchronized(stateLock) {
        recordEventsLocked(events, null)
    }

    private fun recordEventsLocked(events: List<CompanionEvent>, received: Map<String, Long>?) {
        var approvals = storedApprovals().toMutableList()
        val updates = ((load()["updates"] as? List<Map<String, Any>>) ?: emptyList()).toMutableList()
        val decisionStates = approvalDecisionStates().toMutableMap()
        var entries = conversation()
        val acceptedRegistryEvents = mutableListOf<Pair<CompanionEvent.TaskUpdate, String>>()
        for (event in events) {
            when (event) {
                is CompanionEvent.ApprovalPending -> {
                    if (decisionStates[event.review.approvalId]?.get("authoritative") != true) {
                        approvals = approvals.filter { it.approvalId != event.review.approvalId }.toMutableList()
                        approvals.add(event.review)
                    }
                }
                is CompanionEvent.TaskUpdate -> {
                    val decisionResult = event.approvalDecisionResult
                    val acceptedDecision = decisionResult?.takeIf { result ->
                        val pending = decisionStates[result.approvalId]
                        val pendingMatches = if (pending != null) {
                            pending["task_id"] == result.taskId && pending["decision"] == result.decision &&
                                ((pending["message_id"] as? String).isNullOrBlank() || pending["message_id"] == result.inReplyTo)
                        } else {
                            approvals.firstOrNull { it.approvalId == result.approvalId }?.taskId == result.taskId
                        }
                        result.taskId == event.taskId && result.inReplyTo == event.inReplyTo && event.inReplyTo.isNotBlank() && pendingMatches
                    }
                    val updateState = acceptedDecision?.executionState ?: event.state
                    val updateSummary = acceptedDecision?.summary?.takeIf { it.isNotBlank() } ?: event.summary
                    fun matches(entry: Map<String, Any>): Boolean = if (event.inReplyTo.isNotBlank())
                        entry["message_id"] == event.inReplyTo else event.taskId.isNotBlank() && entry["task_id"] == event.taskId
                    val matching = entries.filter(::matches)
                    val taskMatching = entries.filter { event.taskId.isNotBlank() && it["task_id"] == event.taskId }
                    val targetEntries = if (matching.isNotEmpty()) matching else taskMatching
                    val stale = targetEntries.isNotEmpty() && targetEntries.all { isStaleAfterTerminal(it, updateState) }
                    val highestProgressSeq = targetEntries.mapNotNull { (it["progress_seq"] as? Number)?.toLong() }.maxOrNull() ?: 0L
                    val staleProgress = event.eventSeq != null && event.eventSeq <= highestProgressSeq
                    val duplicateProgress = event.eventSeq != null && updates.any {
                        it["task_id"] == event.taskId && (it["event_seq"] as? Number)?.toLong() == event.eventSeq
                    }
                    if (!stale && !staleProgress && !duplicateProgress) {
                        val linkedRequestId = (matching.firstOrNull()?.get("message_id") as? String)
                            ?: event.inReplyTo.takeIf { it.isNotBlank() }
                            ?: taskMatching.firstOrNull()?.get("message_id") as? String
                            ?: ""
                        if (linkedRequestId.isNotBlank()) acceptedRegistryEvents.add(event to linkedRequestId)
                        updates.add(linkedMapOf<String, Any>(
                            "task_id" to event.taskId,
                            "state" to updateState,
                            "summary" to updateSummary,
                            "received_at_unix_ms" to now(),
                        ).also { update ->
                            event.eventSeq?.let { seq -> update["event_seq"] = seq }
                            acceptedDecision?.let { update["approval_decision_result"] = it.toMap() }
                        })
                    }
                    entries = entries.map { entry ->
                        val matchesUpdate = matches(entry) || (acceptedDecision != null && event.taskId.isNotBlank() && entry["task_id"] == event.taskId)
                        if (matchesUpdate && !isStaleAfterTerminal(entry, updateState) && !staleProgress && !duplicateProgress) LinkedHashMap(entry).apply {
                            put("state", updateState); put("summary", updateSummary)
                            event.eventSeq?.let { put("progress_seq", it) }
                            // The canonical answer is the verified body; the summary is only a preview for cards.
                            if (event.body != null) put("body", event.body) else remove("body")
                            put("body_complete", event.bodyComplete); put("body_integrity_failed", event.integrityFailed)
                            if (event.taskId.isNotBlank()) put("task_id", event.taskId)
                            acceptedDecision?.let { put("approval_decision_result", it.toMap()) }
                            put("updated_at_unix_ms", now())
                        } else entry
                    }
                    acceptedDecision?.let { result ->
                        val reconciled = LinkedHashMap(result.toMap()).apply {
                            put("state", result.executionState)
                            put("authoritative", true)
                        }
                        decisionStates[result.approvalId] = reconciled
                        approvals = approvals.filterNot { it.approvalId == result.approvalId }.toMutableList()
                    }
                }
                else -> Unit
            }
        }
        val changes = linkedMapOf<String, Any?>("approvals" to approvals.map(::approvalMap), "updates" to updates.takeLast(20), "conversation" to entries,
            "approval_decision_states" to decisionStates)
        if (received != null) changes["received_event_ids"] = received
        save(changes)
        acceptedRegistryEvents.forEach { (event, requestId) ->
            recordTaskLifecycle(requestId, event.taskId, event.state, event.summary, "MAC",
                event.eventSeq?.let { "${event.taskId}:$it" } ?: "${event.taskId}:${event.state}:${event.inReplyTo}:${event.summary.hashCode()}")
        }
    }

    fun flush(relayUrl: String): Map<String, Any> {
        val companion = companion(relayUrl)
        val receipts = companion.flushOutbox()
        receipts.forEach { receipt ->
            val entry = conversation().firstOrNull { it["message_id"] == receipt.messageId }
            if (entry?.get("state") == "QUEUED") {
                applyToConversation(
                    receipt.messageId,
                    receipt.messageId,
                    "SENT",
                    "Relay accepted this request; waiting for Jarvis on the Mac.",
                )
            }
        }
        if (receipts.isNotEmpty()) {
            val delivered = receipts.associate { it.messageId to it.status }
            val decisions = approvalDecisionStates().toMutableMap()
            var changed = false
            for ((approvalId, previous) in decisions.toMap()) {
                val messageId = previous["message_id"] as? String ?: continue
                val relayStatus = delivered[messageId] ?: continue
                decisions[approvalId] = LinkedHashMap(previous).apply {
                    put("queued_offline", false)
                    put("relay_status", relayStatus)
                    put("state", "DELIVERED_WAITING_SUPERVISOR")
                    put("updated_at_unix_ms", now())
                }
                changed = true
            }
            if (changed) save(mapOf("approval_decision_states" to decisions))
        }
        return linkedMapOf(
            "delivered" to receipts.size,
            "remaining" to companion.outboxSize(),
            "connection" to companion.connectionState.name,
            "last_error" to (companion.lastErrorCode ?: ""),
        )
    }

    /** Surface a queued local request when its envelope is gone but no Mac result ever arrived. */
    private fun reconcileOrphanedQueuedRequests() = synchronized(stateLock) {
        val timestamp = now()
        val outbox = FileMessageOutbox(File(storeDir, "outbox.json"))
        val parsedOutbox = outbox.list().mapNotNull { raw ->
            runCatching { raw to LinkEnvelope.parse(raw) }.getOrNull()
        }
        val expired = parsedOutbox.filter { (_, envelope) -> envelope.header.expiresAtUnixMs <= timestamp }
        expired.forEach { (_, envelope) -> outbox.remove(envelope.header.messageId) }
        val liveOutboxIds = parsedOutbox.asSequence()
            .map { (_, envelope) -> envelope.header }
            .filter { it.expiresAtUnixMs > timestamp }
            .map { it.messageId }
            .toSet()
        var changed = false
        val decisions = approvalDecisionStates().toMutableMap()
        for ((approvalId, previous) in decisions.toMap()) {
            val messageId = previous["message_id"] as? String ?: continue
            val stateName = previous["state"] as? String
            val updatedAt = (previous["updated_at_unix_ms"] as? Number)?.toLong() ?: continue
            if (stateName == "QUEUED" && messageId !in liveOutboxIds &&
                timestamp - updatedAt >= QUEUED_DELIVERY_RECONCILIATION_MS
            ) {
                decisions[approvalId] = LinkedHashMap(previous).apply {
                    put("state", "DELIVERY_FAILED")
                    put("queued_offline", false)
                    put("error_code", "REQUEST_EXPIRED")
                    put("updated_at_unix_ms", timestamp)
                }
                changed = true
            }
        }
        val updated = conversation().map { entry ->
            val messageId = entry["message_id"] as? String ?: return@map entry
            val stateName = (entry["state"] as? String)?.uppercase()
            val transportName = entry["transport"] as? String
            val createdAt = (entry["created_at_unix_ms"] as? Number)?.toLong() ?: return@map entry
            if (stateName == "QUEUED" && transportName in setOf("link", "telegram") &&
                messageId !in liveOutboxIds && timestamp - createdAt >= QUEUED_DELIVERY_RECONCILIATION_MS
            ) {
                changed = true
                LinkedHashMap(entry).apply {
                    put("state", "UNCONFIRMED")
                    put("summary", "No Mac delivery receipt was confirmed for this queued request. Check task status before retrying.")
                    put("updated_at_unix_ms", timestamp)
                }
            } else entry
        }
        if (changed) save(mapOf("conversation" to updated, "approval_decision_states" to decisions))
    }

    fun status(): LinkStatus {
        val values = load()
        val certificatesVerified = values["companion_certificate"] != null && values["host_certificate"] != null
        return LinkStatus(
            deviceId = values["device_id"] as? String,
            tenantId = values["tenant_id"] as? String,
            enrollmentPending = values["enrollment_pending"] == true,
            certificatesVerified = certificatesVerified,
            outboxSize = FileMessageOutbox(File(storeDir, "outbox.json")).list().size,
        )
    }

    /**
     * Registers this device's FCM token with the relay. Signed by the enrolled device identity, so only this
     * device can set its own token. Returns false when the device is not enrolled yet (nothing is sent).
     */
    fun registerPushToken(relayUrl: String, token: String): Boolean {
        val values = load()
        if (values["enrollment_pending"] == true || values["companion_certificate"] == null) return false
        val (signing, _) = keys.identities()
        val tenant = values.string("tenant_id")
        val body = LinkJson.encode(mapOf("token" to token))
        val path = "/v1/devices/${signing.deviceId}/push-token"
        val proof = LinkCrypto.signRequest(tenant, signing, now(), LinkCrypto.newNonce(), "PUT", path, body.toByteArray(Charsets.UTF_8))
        relay(relayUrl, tenant).setPushToken(signing.deviceId, body, mapOf(
            "X-Jarvis-Link-Device" to proof.deviceId,
            "X-Jarvis-Link-Timestamp" to proof.timestampUnixMs.toString(),
            "X-Jarvis-Link-Nonce" to proof.nonceBase64Url,
            "X-Jarvis-Link-Signature" to proof.signatureBase64Url,
        ))
        return true
    }

    /** Deletes persisted companion state. The Keystore key is removed separately by the caller. */
    fun forgetLocalState() {
        state.write("{}")
        File(storeDir, "outbox.json").delete()
        File(storeDir, "inbox.json").delete()
    }

    private fun rootOf(values: Map<String, Any>): TrustedHostRoot = TrustedHostRoot(
        deviceId = values.string("host_root_device_id"),
        signingPublicKeyBase64Url = values.string("host_root_public_key"),
    )

    private fun signedGet(signing: SigningIdentity, tenant: String, path: String): Map<String, String> {
        val proof = LinkCrypto.signRequest(tenant, signing, now(), LinkCrypto.newNonce(), "GET", path, ByteArray(0))
        return mapOf(
            "X-Jarvis-Link-Device" to proof.deviceId,
            "X-Jarvis-Link-Timestamp" to proof.timestampUnixMs.toString(),
            "X-Jarvis-Link-Nonce" to proof.nonceBase64Url,
            "X-Jarvis-Link-Signature" to proof.signatureBase64Url,
        )
    }

    private fun companion(relayUrl: String): JarvisLinkCompanion {
        val (signing, encryption) = keys.identities()
        val values = load()
        val tenant = values.string("tenant_id")
        val companionCertificate = (values["companion_certificate"] as? Map<String, Any>)?.let(LinkStateJson::certificateFromMap)
            ?: error("sync certificates before messaging")
        val hostCertificate = (values["host_certificate"] as? Map<String, Any>)?.let(LinkStateJson::certificateFromMap)
            ?: error("sync certificates before messaging")
        return JarvisLinkCompanion(
            tenantId = tenant,
            signingIdentity = signing,
            encryptionIdentity = encryption,
            companionCertificate = companionCertificate,
            hostRoot = rootOf(values),
            hostCertificate = hostCertificate,
            relay = relay(relayUrl, tenant),
            outbox = FileMessageOutbox(File(storeDir, "outbox.json")),
            inbox = FileMessageInbox(File(storeDir, "inbox.json")),
            now = now,
        ).also { it.setOnline(true) }
    }
}
