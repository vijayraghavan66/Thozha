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
    READ_ONLY("read_only"), WEB_RESEARCH("web_research"), DESKTOP_OBSERVATION("desktop_observation"), ASSISTANT("assistant")
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

data class ApprovalReview(
    val approvalId: String,
    val taskId: String,
    val operation: String,
    val target: String,
    val canonicalArgumentsDigest: String,
    val expiresAtUnixMs: Long,
    val redactedSummary: String,
) {
    init {
        require(approvalId.matches(Regex("^[0-9a-f]{32}$")))
        require(taskId.matches(Regex("^[0-9a-f]{32}$")))
        require(operation.isNotBlank() && target.isNotBlank())
        require(canonicalArgumentsDigest.matches(Regex("^[a-f0-9]{64}$")))
    }

    fun validateForDecision(expectedDigest: String, nowUnixMs: Long) {
        require(expiresAtUnixMs > nowUnixMs) { "Approval expired" }
        require(expectedDigest == canonicalArgumentsDigest) { "Approval arguments changed" }
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
            )
        }
    }
}

sealed interface CompanionEvent {
    data class ApprovalPending(val review: ApprovalReview) : CompanionEvent
    /** [inReplyTo] is the envelope message ID of the request this update answers (empty if none). */
    data class TaskUpdate(val taskId: String, val state: String, val summary: String, val inReplyTo: String = "") : CompanionEvent
    data class Unknown(val kind: String) : CompanionEvent

    /** Plain map for the CLI/JSON output. Contains only decrypted, already-authenticated fields. */
    fun toMap(): Map<String, Any> = when (this) {
        is ApprovalPending -> linkedMapOf(
            "type" to "approval_pending", "approval_id" to review.approvalId, "task_id" to review.taskId,
            "operation" to review.operation, "target" to review.target,
            "canonical_arguments_digest" to review.canonicalArgumentsDigest, "expires_at_unix_ms" to review.expiresAtUnixMs,
        )
        is TaskUpdate -> linkedMapOf("type" to "task_update", "task_id" to taskId, "state" to state, "summary" to summary, "in_reply_to" to inReplyTo)
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
        if (connectionState == LinkConnectionState.OFFLINE) return emptyList()
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

    fun pollInbox(limit: Int = 20): PollResult {
        require(limit in 1..100)
        if (connectionState == LinkConnectionState.OFFLINE) throw IOException("Relay offline")
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
                if (!inbox.recordIfNew(envelope.header.messageId, envelope.header.expiresAtUnixMs)) {
                    safeAck(envelope.header.messageId, AckStatus.RECEIVED, "")
                    continue
                }
                try {
                    val plaintext = LinkCrypto.decryptEnvelope(
                        envelope, encryptionIdentity, companionCertificate, item.senderCertificate, hostRoot, now(),
                    )
                    events += decodeEvent(envelope.header.kind, plaintext)
                    safeAck(envelope.header.messageId, AckStatus.RECEIVED, LinkCrypto.hex(LinkCrypto.sha256(plaintext.toByteArray(UTF_8))))
                } catch (_: Exception) {
                    safeAck(envelope.header.messageId, AckStatus.REJECTED, "")
                }
            }
            cursor = page.nextCursor
            connectionState = LinkConnectionState.CONNECTED
            lastErrorCode = null
            return PollResult(events, cursor)
        } catch (failure: Exception) {
            connectionState = if (failure is SecurityException || (failure is RelayHttpException && failure.status == 401)) LinkConnectionState.REAUTH_REQUIRED else LinkConnectionState.OFFLINE
            lastErrorCode = classify(failure)
            throw failure
        }
    }

    private fun sendOrQueue(envelope: LinkEnvelope): DeliveryResult {
        val raw = envelope.toJson()
        if (connectionState == LinkConnectionState.OFFLINE) {
            outbox.enqueue(raw)
            return DeliveryResult(envelope.header.messageId, queuedOffline = true, receipt = null)
        }
        return try {
            val receipt = postEnvelope(raw, envelope)
            DeliveryResult(envelope.header.messageId, queuedOffline = false, receipt = receipt)
        } catch (failure: Exception) {
            if (failure is RelayHttpException && failure.isPermanent()) {
                // The relay understood and refused the message (revoked, not recipient, expired, bad signature).
                // Queuing it would retry a permanent failure forever, so surface it instead.
                lastErrorCode = failure.code
                throw failure
            }
            if (failure is SecurityException || (failure is RelayHttpException && failure.status == 401)) {
                connectionState = LinkConnectionState.REAUTH_REQUIRED
                lastErrorCode = "AUTH_FAILED"
                throw failure
            }
            outbox.enqueue(raw)
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

    private fun decodeEvent(kind: String, payload: String): CompanionEvent {
        val fields = FlatJson.decode(payload)
        return when (kind) {
            "approval.request" -> CompanionEvent.ApprovalPending(ApprovalReview.parse(payload))
            "task.progress", "task.result" -> CompanionEvent.TaskUpdate(
                fields.string("task_id"), fields.string("state"), fields.string("summary"), (fields["in_reply_to"] as? String).orEmpty(),
            )
            else -> CompanionEvent.Unknown(kind)
        }
    }

    private fun RelayHttpException.isPermanent(): Boolean =
        status in 400..499 && status != 401 && status != 408 && status != 429

    private fun classify(failure: Exception): String = when (failure) {
        is SecurityException -> "AUTH_FAILED"
        is RelayHttpException -> if (failure.status == 429) "RATE_LIMITED" else "RELAY_ERROR"
        is IOException -> "NETWORK_UNAVAILABLE"
        else -> "PROTOCOL_ERROR"
    }
}
