package com.jarvis.link

import java.io.IOException
import java.nio.charset.StandardCharsets.UTF_8
import java.util.UUID

enum class LinkConnectionState { OFFLINE, CONNECTING, CONNECTED, RECONNECTING, REAUTH_REQUIRED }
enum class AckStatus { RECEIVED, REJECTED }

data class RelayReceipt(val messageId: String, val status: String, val expiresAtUnixMs: Long)
data class InboxItem(val envelope: LinkEnvelope, val senderCertificate: DeviceCertificate)
data class InboxPage(val messages: List<InboxItem>, val nextCursor: String?)

/** HTTP implementation is injected so this protocol layer stays testable and Android-ready. */
interface JarvisLinkRelayApi {
    fun postMessage(envelopeJson: String, headers: Map<String, String>): RelayReceipt
    /** [pathAndQuery] is the exact path that was signed, so the signed and sent targets cannot diverge. */
    fun inbox(pathAndQuery: String, headers: Map<String, String>): InboxPage
    /** [bodyJson] is the exact body bytes that were signed. */
    fun acknowledge(messageId: String, bodyJson: String, headers: Map<String, String>)
    fun deviceCertificate(deviceId: String, headers: Map<String, String>): DeviceCertificate
    fun enrollmentChallenge(bodyJson: String): Map<String, Any>
    fun enrollmentComplete(bodyJson: String): Map<String, Any>
}

/** Android implementation should back this with an encrypted local queue. */
interface MessageOutbox {
    fun enqueue(envelopeJson: String)
    fun list(): List<String>
    fun remove(messageId: String)
}

/** Persist message IDs through expiry so a redelivered envelope is never processed twice. */
interface MessageInbox {
    fun recordIfNew(messageId: String, expiresAtUnixMs: Long): Boolean
    fun forgetExpired(nowUnixMs: Long)
}

class MemoryMessageOutbox : MessageOutbox {
    private val messages = linkedMapOf<String, String>()
    @Synchronized override fun enqueue(envelopeJson: String) {
        val envelope = LinkEnvelope.parse(envelopeJson)
        val prior = messages[envelope.header.messageId]
        require(prior == null || prior == envelopeJson) { "message ID already has different queued bytes" }
        messages.putIfAbsent(envelope.header.messageId, envelopeJson)
    }
    @Synchronized override fun list(): List<String> = messages.values.toList()
    @Synchronized override fun remove(messageId: String) { messages.remove(messageId) }
}

class MemoryMessageInbox : MessageInbox {
    private val expires = mutableMapOf<String, Long>()
    @Synchronized override fun recordIfNew(messageId: String, expiresAtUnixMs: Long): Boolean {
        if (messageId in expires) return false
        expires[messageId] = expiresAtUnixMs
        return true
    }
    @Synchronized override fun forgetExpired(nowUnixMs: Long) { expires.entries.removeIf { it.value <= nowUnixMs } }
}

enum class SafeCapability(val wireName: String) {
    READ_ONLY("read_only"), WEB_RESEARCH("web_research"), DESKTOP_OBSERVATION("desktop_observation"), ASSISTANT("assistant"),
    /** Bounded project work. Honoured only for companions the Mac operator has allowlisted. */
    DEVELOPMENT("development")
}

data class TaskSubmission(
    val objective: String,
    val capabilityScope: Set<SafeCapability>,
    val clientRequestId: String = LinkCrypto.newId(),
) {
    init {
        require(objective.isNotBlank() && objective.toByteArray(UTF_8).size <= 4096)
        require(clientRequestId.matches(Regex("^[0-9a-f]{32}$")))
        require(capabilityScope.isNotEmpty())
    }

    fun toJson(): String {
        // Exactly the two fields the host accepts. Idempotency is the envelope message_id, not a payload field.
        val capabilities = capabilityScope.map { it.wireName }.sorted().joinToString(prefix = "[", postfix = "]") { FlatJson.quote(it) }
        return "{\"objective\":${FlatJson.quote(objective)},\"capability_scope\":$capabilities}"
    }
}

/** Versioned typed requests; callers cannot serialize arbitrary commands or shell text. */
sealed interface FastCommandRequest {
    fun toJson(): String

    data object MacStatus : FastCommandRequest {
        override fun toJson() = "{\"command\":\"mac.status\",\"version\":1}"
    }

    data class TaskStatus(val taskId: String) : FastCommandRequest {
        init { require(taskId.matches(Regex("^[0-9a-f]{32}$"))) }
        override fun toJson() = "{\"command\":\"task.status\",\"task_id\":${FlatJson.quote(taskId)},\"version\":1}"
    }

    data class OpenCalculator(val idempotencyKey: String = LinkCrypto.newId()) : FastCommandRequest {
        init { require(idempotencyKey.matches(Regex("^[0-9a-f]{32}$"))) }
        override fun toJson() = "{\"app\":\"Calculator\",\"command\":\"app.open\",\"idempotency_key\":${FlatJson.quote(idempotencyKey)},\"version\":1}"
    }
}

data class RelayInvitation(
    val tenantId: String,
    val invitationId: String,
    val invitationSecretBase64Url: String,
    val expiresAtUnixMs: Long,
)

data class EnrollmentChallenge(
    val tenantId: String,
    val invitationId: String,
    val challengeId: String,
    val challengeBase64Url: String,
    val deviceId: String,
    val expiresAtUnixMs: Long,
)

data class EnrollmentRequest(
    val invitationId: String,
    val invitationSecretBase64Url: String,
    val deviceName: String,
    val deviceRole: String,
    val signingPublicKeyBase64Url: String,
    val encryptionPublicKeyBase64Url: String,
    val clientNonceBase64Url: String,
) {
    fun toJson(): String = FlatJson.encode(linkedMapOf(
        "invitation_id" to invitationId,
        "invitation_secret_b64url" to invitationSecretBase64Url,
        "device_name" to deviceName,
        "device_role" to deviceRole,
        "signing_public_key_b64url" to signingPublicKeyBase64Url,
        "encryption_public_key_b64url" to encryptionPublicKeyBase64Url,
        "client_nonce_b64url" to clientNonceBase64Url,
    ))
}

data class EnrollmentCompletion(
    val tenantId: String,
    val deviceId: String,
    val invitationId: String,
    val challengeId: String,
    val deviceName: String,
    val deviceRole: String,
    val signingPublicKeyBase64Url: String,
    val encryptionPublicKeyBase64Url: String,
    val clientNonceBase64Url: String,
    val challengeBase64Url: String,
    val signatureBase64Url: String,
) {
    fun toJson(): String = FlatJson.encode(linkedMapOf(
        // The relay requires the tenant and the derived device ID in the body; both are signed by the proof.
        "tenant_id" to tenantId,
        "device_id" to deviceId,
        "invitation_id" to invitationId,
        "challenge_id" to challengeId,
        "device_name" to deviceName,
        "device_role" to deviceRole,
        "signing_public_key_b64url" to signingPublicKeyBase64Url,
        "encryption_public_key_b64url" to encryptionPublicKeyBase64Url,
        "client_nonce_b64url" to clientNonceBase64Url,
        "challenge_b64url" to challengeBase64Url,
        "signature_b64url" to signatureBase64Url,
    ))
}

/** Produces relay enrollment requests without trusting any relay-returned public key as a root. */
class CompanionEnrollment(
    private val signingIdentity: SigningIdentity,
    private val encryptionIdentity: EncryptionIdentity,
    private val tenantId: String,
    private val invitation: RelayInvitation,
    deviceName: String,
    private val now: () -> Long = System::currentTimeMillis,
) {
    private val safeDeviceName = validateDeviceName(deviceName)
    private val clientNonce = LinkCrypto.newNonce()

    init {
        require(invitation.tenantId == tenantId)
        require(invitation.expiresAtUnixMs > now()) { "Enrollment invitation expired" }
        require(signingIdentity.deviceId == encryptionIdentity.deviceId)
        require(signingIdentity.deviceId == LinkCrypto.deviceIdForSigningPublicKey(signingIdentity.keyPair.public.encoded))
    }

    fun challengeRequest(): EnrollmentRequest = EnrollmentRequest(
        invitationId = invitation.invitationId,
        invitationSecretBase64Url = invitation.invitationSecretBase64Url,
        deviceName = safeDeviceName,
        deviceRole = "companion",
        signingPublicKeyBase64Url = LinkCrypto.b64url(signingIdentity.keyPair.public.encoded),
        encryptionPublicKeyBase64Url = LinkCrypto.b64url(encryptionIdentity.keyPair.public.encoded),
        clientNonceBase64Url = clientNonce,
    )

    fun signChallenge(challenge: EnrollmentChallenge): EnrollmentCompletion {
        require(challenge.tenantId == tenantId && challenge.invitationId == invitation.invitationId)
        require(challenge.deviceId == signingIdentity.deviceId) { "Relay derived a different device ID" }
        require(challenge.expiresAtUnixMs > now()) { "Enrollment challenge expired" }
        val signingBytes = listOf(
            "JARVIS-LINK-ENROLL-V1",
            tenantId,
            invitation.invitationId,
            challenge.challengeId,
            signingIdentity.deviceId,
            LinkCrypto.b64url(safeDeviceName.toByteArray(UTF_8)),
            "companion",
            LinkCrypto.b64url(signingIdentity.keyPair.public.encoded),
            LinkCrypto.b64url(encryptionIdentity.keyPair.public.encoded),
            clientNonce,
            challenge.challengeBase64Url,
        ).joinToString("\n").toByteArray(UTF_8)
        return EnrollmentCompletion(
            tenantId = tenantId,
            deviceId = signingIdentity.deviceId,
            invitationId = invitation.invitationId,
            challengeId = challenge.challengeId,
            deviceName = safeDeviceName,
            deviceRole = "companion",
            signingPublicKeyBase64Url = LinkCrypto.b64url(signingIdentity.keyPair.public.encoded),
            encryptionPublicKeyBase64Url = LinkCrypto.b64url(encryptionIdentity.keyPair.public.encoded),
            clientNonceBase64Url = clientNonce,
            challengeBase64Url = challenge.challengeBase64Url,
            signatureBase64Url = LinkCrypto.sign(signingIdentity.keyPair.private, signingBytes),
        )
    }

    companion object {
        fun validateDeviceName(value: String): String {
            require(value.toByteArray(UTF_8).size in 1..64) { "Device name must be 1–64 UTF-8 bytes" }
            require(value.none { it == '\r' || it == '\n' || it.code < 0x20 || it.code == 0x7f }) {
                "Device name contains a control character"
            }
            return value
        }
    }
}

/** Complete, verified plan bounds shown before a development-plan approval can be tapped. */
data class DevelopmentPlanReview(
    val planId: String,
    val deviceId: String,
    val userId: String,
    val workspaceRoot: String,
    val allowedOps: List<String>,
    val allowedPaths: List<String>,
    val maxFileBytes: Long,
    val maxTotalBytes: Long,
    val maxWrites: Long,
    val maxTestRuns: Long,
    val maxRepairs: Long,
    val maxDurationSeconds: Long,
    val ttlSeconds: Long,
    val planDigest: String,
) {
    init {
        require(planId.matches(Regex("^[A-Za-z0-9_.-]{8,80}$")))
        require(deviceId.matches(Regex("^[0-9a-f]{32}$")))
        require(userId == "device:$deviceId")
        require(workspaceRoot.startsWith("/") && '\u0000' !in workspaceRoot)
        require(allowedOps.isNotEmpty() && allowedOps == allowedOps.distinct().sorted())
        require(allowedOps.all { it in SUPPORTED_OPS })
        require(allowedPaths.size <= MAX_PATHS && allowedPaths.all(::validPlanPath))
        require(maxFileBytes in 1..MAX_FILE_BYTES)
        require(maxTotalBytes in 1..MAX_TOTAL_BYTES)
        require(maxWrites in 0..MAX_WRITES && maxTestRuns in 0..MAX_TEST_RUNS)
        require(maxRepairs in 0..MAX_REPAIRS)
        require(maxDurationSeconds in 1..MAX_DURATION_SECONDS && ttlSeconds in 1..MAX_TTL_SECONDS)
        require(planDigest.matches(Regex("^[0-9a-f]{64}$")))
        require(sha256Hex(canonicalJson(canonicalPlanFields()).toByteArray(UTF_8)) == planDigest) {
            "Development plan fields do not match the plan digest"
        }
    }

    fun canonicalPlanFields(): Map<String, Any> = linkedMapOf(
        "plan_id" to planId,
        "device_id" to deviceId,
        "user_id" to userId,
        "workspace_root" to workspaceRoot,
        "allowed_ops" to allowedOps,
        "allowed_paths" to allowedPaths,
        "max_file_bytes" to maxFileBytes,
        "max_total_bytes" to maxTotalBytes,
        "max_writes" to maxWrites,
        "max_test_runs" to maxTestRuns,
        "max_repairs" to maxRepairs,
        "max_duration_s" to maxDurationSeconds,
        "ttl_s" to ttlSeconds,
    )

    fun toWire(): Map<String, Any> = linkedMapOf<String, Any>("plan_digest" to planDigest).apply {
        putAll(canonicalPlanFields())
    }

    companion object {
        private const val MAX_PATHS = 24
        private const val MAX_FILE_BYTES = 256L * 1024L
        private const val MAX_TOTAL_BYTES = 4L * 1024L * 1024L
        private const val MAX_WRITES = 200L
        private const val MAX_TEST_RUNS = 50L
        private const val MAX_REPAIRS = 5L
        private const val MAX_DURATION_SECONDS = 4L * 60L * 60L
        private const val MAX_TTL_SECONDS = 4L * 60L * 60L
        private val SUPPORTED_OPS = setOf("write_file", "run_tests", "artifact_manifest")
        private val PLAN_KEYS = setOf(
            "plan_id", "device_id", "user_id", "workspace_root", "allowed_ops", "allowed_paths",
            "max_file_bytes", "max_total_bytes", "max_writes", "max_test_runs", "max_repairs",
            "max_duration_s", "ttl_s", "plan_digest",
        )

        fun fromMap(value: Any?): DevelopmentPlanReview {
            val map = value as? Map<*, *> ?: error("Missing structured development plan")
            require(map.keys == PLAN_KEYS) { "Incomplete or unexpected development plan fields" }
            fun text(key: String): String = map[key] as? String ?: error("Invalid development plan field: $key")
            fun integer(key: String): Long = (map[key] as? Number)?.toLong() ?: error("Invalid development plan field: $key")
            fun strings(key: String): List<String> = (map[key] as? List<*>)
                ?.map { it as? String ?: error("Invalid development plan list: $key") }
                ?: error("Invalid development plan field: $key")
            return DevelopmentPlanReview(
                planId = text("plan_id"),
                deviceId = text("device_id"),
                userId = text("user_id"),
                workspaceRoot = text("workspace_root"),
                allowedOps = strings("allowed_ops"),
                allowedPaths = strings("allowed_paths"),
                maxFileBytes = integer("max_file_bytes"),
                maxTotalBytes = integer("max_total_bytes"),
                maxWrites = integer("max_writes"),
                maxTestRuns = integer("max_test_runs"),
                maxRepairs = integer("max_repairs"),
                maxDurationSeconds = integer("max_duration_s"),
                ttlSeconds = integer("ttl_s"),
                planDigest = text("plan_digest"),
            )
        }

        private fun validPlanPath(value: String): Boolean {
            if (value.isEmpty()) return true // empty prefix explicitly means the workspace root
            if (value.startsWith("/") || value.startsWith("~") || '\\' in value || '\u0000' in value) return false
            return value.split('/').all { part ->
                part !in setOf("", ".", "..") && part.matches(Regex("^[A-Za-z0-9_.-]{1,120}$"))
            }
        }
    }
}

data class ApprovalReview(
    val approvalId: String,
    val taskId: String,
    val operation: String,
    val target: String,
    val canonicalArgumentsDigest: String,
    val expiresAtUnixMs: Long,
    val redactedSummary: String,
    val developmentPlan: DevelopmentPlanReview? = null,
) {
    init {
        require(approvalId.matches(Regex("^[0-9a-f]{32}$")))
        require(taskId.matches(Regex("^[0-9a-f]{32}$")))
        require(operation.isNotBlank() && target.isNotBlank())
        require(canonicalArgumentsDigest.matches(Regex("^[a-f0-9]{64}$")))
        verifyDevelopmentPlanBinding()
    }

    private fun verifyDevelopmentPlanBinding() {
        if (operation == DEVELOPMENT_PLAN_APPROVAL_OPERATION) {
            val plan = developmentPlan ?: error("Development plan approval omitted complete verified details")
            require(target == "taskflow:plan:${plan.planId}") { "Development plan target does not match the reviewed plan" }
            require(sha256Hex(canonicalJson(plan.canonicalPlanFields()).toByteArray(UTF_8)) == plan.planDigest) {
                "Development plan was modified after verification"
            }
            val arguments = linkedMapOf<String, Any>(
                "plan" to plan.canonicalPlanFields(),
                "plan_digest" to plan.planDigest,
            )
            val operationValue = linkedMapOf<String, Any>(
                "operation" to operation,
                "target" to target,
                "arguments" to arguments,
            )
            require(sha256Hex(canonicalJson(operationValue).toByteArray(UTF_8)) == canonicalArgumentsDigest) {
                "Displayed development plan does not match the approved operation digest"
            }
        } else {
            require(developmentPlan == null) { "Unexpected development plan details on another approval type" }
        }
    }

    fun validateForDecision(expectedDigest: String, nowUnixMs: Long) {
        require(expiresAtUnixMs > nowUnixMs) { "Approval expired" }
        require(expectedDigest == canonicalArgumentsDigest) { "Approval arguments changed" }
        verifyDevelopmentPlanBinding()
    }

    fun toDecisionJson(
        decision: String,
        expectedDigest: String,
        idempotencyKey: String,
        nowUnixMs: Long = System.currentTimeMillis(),
        continuationObjective: String? = null,
    ): String {
        validateForDecision(expectedDigest, nowUnixMs)
        require(decision == "approve" || decision == "reject")
        require(idempotencyKey.isNotBlank() && '\n' !in idempotencyKey && '\r' !in idempotencyKey)
        val fields = linkedMapOf(
            "approval_id" to approvalId,
            "task_id" to taskId,
            "decision" to decision,
            "operation_digest" to expectedDigest,
            "idempotency_key" to idempotencyKey,
        )
        if (decision == "approve" && !continuationObjective.isNullOrBlank()) {
            require(continuationObjective.toByteArray(UTF_8).size <= 4096) { "continuation objective is too large" }
            fields["continuation_objective"] = continuationObjective.trim()
        }
        return fields.entries.joinToString(prefix = "{", postfix = "}", separator = ",") { (key, value) ->
            FlatJson.quote(key) + ":" + FlatJson.quote(value)
        }
    }

    companion object {
        private const val DEVELOPMENT_PLAN_APPROVAL_OPERATION = "mcp:jarvis_dev:approve_plan"

        fun parse(payloadJson: String): ApprovalReview {
            val fields = FlatJson.decode(payloadJson)
            return ApprovalReview(
                approvalId = fields.string("approval_id"),
                taskId = fields.string("task_id"),
                operation = fields.string("operation"),
                target = fields.string("target"),
                canonicalArgumentsDigest = fields.string("canonical_arguments_digest"),
                expiresAtUnixMs = fields.long("expires_at_unix_ms"),
                redactedSummary = fields.string("redacted_summary"),
                developmentPlan = if (fields.containsKey("development_plan")) {
                    DevelopmentPlanReview.fromMap(fields["development_plan"])
                } else null,
            )
        }
    }
}

private fun canonicalJson(value: Any?): String = when (value) {
    null -> "null"
    is String -> "\"${LinkJson.escape(value)}\""
    is Boolean, is Int, is Long -> value.toString()
    is List<*> -> value.joinToString(prefix = "[", postfix = "]", separator = ",") { canonicalJson(it) }
    is Map<*, *> -> value.entries
        .associate { (key, item) -> (key as? String ?: error("Invalid canonical JSON key")) to item }
        .toSortedMap()
        .entries
        .joinToString(prefix = "{", postfix = "}", separator = ",") { (key, item) ->
            "\"${LinkJson.escape(key)}\":${canonicalJson(item)}"
        }
    else -> error("Unsupported canonical JSON type")
}

private fun sha256Hex(value: ByteArray): String = java.security.MessageDigest.getInstance("SHA-256")
    .digest(value).joinToString("") { "%02x".format(it) }

/** Decodes one authenticated host payload. A task body is used only when its SHA-256 matches the one the Mac sent. */
internal fun decodeCompanionEvent(kind: String, payload: String): CompanionEvent {
    val fields = FlatJson.decode(payload)
    return when (kind) {
        "approval.request" -> CompanionEvent.ApprovalPending(ApprovalReview.parse(payload))
        "task.progress", "task.result" -> {
            val text = fields["body"] as? String
            val expected = fields["body_sha256"] as? String
            val verified = text != null && expected != null && sha256Hex(text) == expected
            CompanionEvent.TaskUpdate(
                fields.string("task_id"), fields.string("state"), fields.string("summary"), (fields["in_reply_to"] as? String).orEmpty(),
                body = if (verified) text else null,
                bodyComplete = fields["body_complete"] != false,
                integrityFailed = text != null && !verified,
                eventSeq = if (kind == "task.progress") (fields["event_seq"] as? Number)?.toLong()?.takeIf { it > 0 } else null,
            )
        }
        else -> CompanionEvent.Unknown(kind)
    }
}

private fun sha256Hex(text: String): String =
    java.security.MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

sealed interface CompanionEvent {
    data class ApprovalPending(val review: ApprovalReview) : CompanionEvent
    /**
     * [inReplyTo] is the envelope message ID of the request this update answers (empty if none).
     * [body] is the complete answer, present only when its SHA-256 matched. [bodyComplete] is false when the Mac
     * cut the answer at the delivery limit. [integrityFailed] is true when a body arrived but did not verify.
     */
    data class TaskUpdate(
        val taskId: String,
        val state: String,
        val summary: String,
        val inReplyTo: String = "",
        val body: String? = null,
        val bodyComplete: Boolean = true,
        val integrityFailed: Boolean = false,
        val eventSeq: Long? = null,
    ) : CompanionEvent
    data class Unknown(val kind: String) : CompanionEvent

    /** Plain map for the CLI/JSON output. Contains only decrypted, already-authenticated fields. */
    fun toMap(): Map<String, Any> = when (this) {
        is ApprovalPending -> linkedMapOf<String, Any>(
            "type" to "approval_pending", "approval_id" to review.approvalId, "task_id" to review.taskId,
            "operation" to review.operation, "target" to review.target,
            "canonical_arguments_digest" to review.canonicalArgumentsDigest, "expires_at_unix_ms" to review.expiresAtUnixMs,
        ).also { event -> review.developmentPlan?.let { event["development_plan"] = it.toWire() } }
        is TaskUpdate -> linkedMapOf<String, Any>("type" to "task_update", "task_id" to taskId, "state" to state, "summary" to summary, "in_reply_to" to inReplyTo)
            .also { update -> eventSeq?.let { update["event_seq"] = it } }
        is Unknown -> linkedMapOf("type" to "unknown", "kind" to kind)
    }
}

data class DeliveryResult(val messageId: String, val queuedOffline: Boolean, val receipt: RelayReceipt?)
data class PollResult(val events: List<CompanionEvent>, val cursor: String?)

/**
 * Transport/session core used by the Android adapter and local simulator. Production Android
 * supplies Android Keystore keys and persistent encrypted inbox/outbox implementations.
 */
class JarvisLinkCompanion(
    private val tenantId: String,
    private val signingIdentity: SigningIdentity,
    private val encryptionIdentity: EncryptionIdentity,
    private val companionCertificate: DeviceCertificate,
    private val hostRoot: TrustedHostRoot,
    private val hostCertificate: DeviceCertificate,
    private val relay: JarvisLinkRelayApi,
    private val outbox: MessageOutbox,
    private val inbox: MessageInbox,
    private val now: () -> Long = System::currentTimeMillis,
) {
    @Volatile var connectionState: LinkConnectionState = LinkConnectionState.OFFLINE
        private set
    @Volatile var lastErrorCode: String? = null
        private set
    private var cursor: String? = null

    init {
        require(signingIdentity.deviceId == encryptionIdentity.deviceId) { "Device signing/ECDH identities must match" }
        require(companionCertificate.deviceId == signingIdentity.deviceId && companionCertificate.deviceRole == "companion")
        require(companionCertificate.verify(hostRoot, tenantId, now())) { "Companion certificate is not trusted" }
        require(hostCertificate.deviceRole == "host") { "Pinned host certificate must have host role" }
        require(hostCertificate.verify(hostRoot, tenantId, now())) { "Host certificate is not signed by the pinned root" }
        require(hostCertificate.issuerDeviceId == hostRoot.deviceId) { "Host certificate issuer does not match pinned host root" }
    }

    @Synchronized fun setOnline(online: Boolean) {
        connectionState = if (online) LinkConnectionState.CONNECTED else LinkConnectionState.OFFLINE
        if (online) lastErrorCode = null
    }

    fun submitTask(submission: TaskSubmission, ttlMs: Long? = null): DeliveryResult {
        val envelope = LinkCrypto.createEnvelope(
            tenantId, signingIdentity, hostCertificate, hostRoot,
            "task.submit", submission.toJson(), nowUnixMs = now(),
            ttlMs = ttlMs ?: (15 * 60 * 1000L),
            messageId = submission.clientRequestId,
        )
        return sendOrQueue(envelope)
    }

    fun submitFastCommand(command: FastCommandRequest, ttlMs: Long? = null, clientRequestId: String? = null): DeliveryResult {
        val stableId = (command as? FastCommandRequest.OpenCalculator)?.idempotencyKey ?: clientRequestId
        require(clientRequestId == null || stableId == null || clientRequestId == stableId) { "fast command idempotency does not match" }
        val envelope = LinkCrypto.createEnvelope(
            tenantId, signingIdentity, hostCertificate, hostRoot,
            "fast.command", command.toJson(), nowUnixMs = now(),
            ttlMs = ttlMs ?: (2 * 60 * 1000L),
            messageId = stableId,
        )
        return sendOrQueue(envelope)
    }

    fun outboxSize(): Int = outbox.list().size

    fun decideApproval(
        review: ApprovalReview,
        decision: String,
        expectedDigest: String,
        continuationObjective: String? = null,
    ): DeliveryResult {
        val payload = review.toDecisionJson(
            decision,
            expectedDigest,
            UUID.randomUUID().toString(),
            now(),
            continuationObjective,
        )
        val envelope = LinkCrypto.createEnvelope(
            tenantId, signingIdentity, hostCertificate, hostRoot,
            "approval.decision", payload, nowUnixMs = now(),
        )
        return sendOrQueue(envelope)
    }

    @Synchronized fun flushOutbox(): List<RelayReceipt> {
        // OFFLINE only records the last failure. It must not block the next attempt, or one outage latches forever.
        connectionState = LinkConnectionState.RECONNECTING
        val receipts = mutableListOf<RelayReceipt>()
        try {
            for (raw in outbox.list()) {
                val envelope = LinkEnvelope.parse(raw)
                if (envelope.header.expiresAtUnixMs <= now()) {
                    outbox.remove(envelope.header.messageId)
                    continue
                }
                receipts += postEnvelope(raw, envelope)
                outbox.remove(envelope.header.messageId)
            }
            connectionState = LinkConnectionState.CONNECTED
            lastErrorCode = null
        } catch (failure: Exception) {
            connectionState = LinkConnectionState.OFFLINE
            lastErrorCode = classify(failure)
        }
        return receipts
    }

    fun pollInbox(limit: Int = 20, persistEvent: ((String, Long, CompanionEvent) -> Boolean)? = null): PollResult {
        require(limit in 1..100)
        connectionState = LinkConnectionState.CONNECTING
        try {
            inbox.forgetExpired(now())
            val path = "/v1/devices/${signingIdentity.deviceId}/inbox?limit=$limit" + (cursor?.let { "&cursor=$it" } ?: "")
            val page = relay.inbox(path, signedHeaders("GET", path, ByteArray(0)))
            val events = mutableListOf<CompanionEvent>()
            for (item in page.messages) {
                val envelope = item.envelope
                if (envelope.header.tenantId != tenantId || envelope.header.toDeviceId != signingIdentity.deviceId ||
                    item.senderCertificate.deviceId != envelope.header.fromDeviceId ||
                    item.senderCertificate.deviceRole != "host" ||
                    !item.senderCertificate.verify(hostRoot, tenantId, now())
                ) {
                    safeAck(envelope.header.messageId, AckStatus.REJECTED, "")
                    continue
                }
                val decoded = try {
                    val plaintext = LinkCrypto.decryptEnvelope(
                        envelope, encryptionIdentity, companionCertificate, item.senderCertificate, hostRoot, now(),
                    )
                    Triple(decodeEvent(envelope.header.kind, plaintext), plaintext,
                        LinkCrypto.hex(LinkCrypto.sha256(plaintext.toByteArray(UTF_8))))
                } catch (_: Exception) {
                    safeAck(envelope.header.messageId, AckStatus.REJECTED, "")
                    continue
                }
                // Persist decoded data and replay ID atomically before ACK. Persistence failures escape,
                // leaving the message queued for safe redelivery instead of rejecting or losing it.
                val isNew = if (persistEvent != null) {
                    persistEvent(envelope.header.messageId, envelope.header.expiresAtUnixMs, decoded.first)
                } else {
                    inbox.recordIfNew(envelope.header.messageId, envelope.header.expiresAtUnixMs)
                }
                if (isNew) events += decoded.first
                safeAck(envelope.header.messageId, AckStatus.RECEIVED, decoded.third)
            }
            cursor = page.nextCursor
            connectionState = LinkConnectionState.CONNECTED
            lastErrorCode = null
            return PollResult(events, cursor)
        } catch (failure: Exception) {
            connectionState = when {
                failure is SecurityException -> LinkConnectionState.REAUTH_REQUIRED
                failure is RelayHttpException && failure.status == 401 && failure.code != "TIMESTAMP_SKEW" -> LinkConnectionState.REAUTH_REQUIRED
                else -> LinkConnectionState.OFFLINE
            }
            lastErrorCode = classify(failure)
            throw failure
        }
    }

    private fun sendOrQueue(envelope: LinkEnvelope): DeliveryResult {
        val raw = envelope.toJson()
        // Persist the exact signed ciphertext before any network operation. A crash after relay acceptance
        // replays the same bytes and message ID; the relay treats that replay idempotently.
        outbox.enqueue(raw)
        if (connectionState == LinkConnectionState.OFFLINE) {
            return DeliveryResult(envelope.header.messageId, queuedOffline = true, receipt = null)
        }
        return try {
            val receipt = postEnvelope(raw, envelope)
            outbox.remove(envelope.header.messageId)
            DeliveryResult(envelope.header.messageId, queuedOffline = false, receipt = receipt)
        } catch (failure: Exception) {
            if (failure is RelayHttpException && failure.isPermanent()) {
                // The relay understood and refused the message (revoked, not recipient, expired, bad signature).
                // Queuing it would retry a permanent failure forever, so surface it instead.
                lastErrorCode = failure.code
                outbox.remove(envelope.header.messageId)
                throw failure
            }
            if (failure is RelayHttpException && failure.status == 401 && failure.code == "TIMESTAMP_SKEW") {
                // Keep the exact encrypted envelope. The next flush creates a fresh HTTP timestamp,
                // nonce and signature; the relay's strict skew window remains unchanged.
                connectionState = LinkConnectionState.OFFLINE
                lastErrorCode = "REQUEST_STALE_RETRY"
                return DeliveryResult(envelope.header.messageId, queuedOffline = true, receipt = null)
            }
            if (failure is SecurityException || (failure is RelayHttpException && failure.status == 401)) {
                connectionState = LinkConnectionState.REAUTH_REQUIRED
                lastErrorCode = "AUTH_FAILED"
                throw failure
            }
            connectionState = LinkConnectionState.OFFLINE
            lastErrorCode = classify(failure)
            DeliveryResult(envelope.header.messageId, queuedOffline = true, receipt = null)
        }
    }

    private fun postEnvelope(raw: String, envelope: LinkEnvelope): RelayReceipt {
        val body = raw.toByteArray(UTF_8)
        val receipt = relay.postMessage(raw, signedHeaders("POST", "/v1/messages", body))
        require(receipt.messageId == envelope.header.messageId) { "Relay receipt message ID mismatch" }
        return receipt
    }

    private fun signedHeaders(method: String, path: String, body: ByteArray): Map<String, String> {
        val proof = LinkCrypto.signRequest(tenantId, signingIdentity, now(), LinkCrypto.newNonce(), method, path, body)
        return mapOf(
            "X-Jarvis-Link-Tenant" to proof.tenantId,
            "X-Jarvis-Link-Device" to proof.deviceId,
            "X-Jarvis-Link-Timestamp" to proof.timestampUnixMs.toString(),
            "X-Jarvis-Link-Nonce" to proof.nonceBase64Url,
            "X-Jarvis-Link-Signature" to proof.signatureBase64Url,
        )
    }

    private fun safeAck(messageId: String, status: AckStatus, resultDigest: String) {
        runCatching {
            val path = "/v1/messages/$messageId/ack"
            val body = "{\"status\":${FlatJson.quote(status.name.lowercase())},\"result_digest\":${FlatJson.quote(resultDigest)}}"
            relay.acknowledge(messageId, body, signedHeaders("POST", path, body.toByteArray(UTF_8)))
        }
    }

    private fun decodeEvent(kind: String, payload: String): CompanionEvent = decodeCompanionEvent(kind, payload)

    private fun RelayHttpException.isPermanent(): Boolean =
        status in 400..499 && status != 401 && status != 408 && status != 429

    private fun classify(failure: Exception): String = when (failure) {
        is SecurityException -> "AUTH_FAILED"
        is RelayHttpException -> if (failure.status == 401 && failure.code == "TIMESTAMP_SKEW") "REQUEST_STALE_RETRY"
            else if (failure.status == 429) "RATE_LIMITED" else "RELAY_ERROR"
        is IOException -> "NETWORK_UNAVAILABLE"
        else -> "PROTOCOL_ERROR"
    }
}
