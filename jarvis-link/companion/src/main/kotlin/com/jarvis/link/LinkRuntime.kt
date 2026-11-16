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
) {
    private fun load(): Map<String, Any> = state.read()?.let { LinkJson.parseObject(it) } ?: emptyMap()

    private fun save(values: Map<String, Any?>) {
        val merged = linkedMapOf<String, Any?>()
        merged.putAll(load())
        merged.putAll(values)
        state.write(LinkJson.encode(merged))
    }

    private fun relay(relayUrl: String, tenant: String) = HttpJarvisLinkRelay(relayUrl, tenant)

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

    /** Polls the relay and keeps approval requests and task updates in local (non-secret) state until decided. */
    fun poll(relayUrl: String, limit: Int = 20): PollResult {
        val page = companion(relayUrl).pollInbox(limit)
        recordEvents(page.events)
        return page
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
    fun syncNow(relayUrl: String): SyncReport {
        var flushed = 0
        var error = ""
        try {
            flushed = flush(relayUrl)["delivered"] as Int
        } catch (failure: Exception) {
            error = errorCode(failure)
        }
        val newlyPending = mutableListOf<ApprovalReview>()
        try {
            recordEvents(companion(relayUrl).pollInbox(20).events)
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

    private fun markAnnounced(ids: List<String>) {
        if (ids.isEmpty()) return
        val merged = (announcedIds() + ids).toList().takeLast(200)
        save(mapOf("announced_approval_ids" to merged))
    }

    /** Recent task updates from the Mac (task.result payloads), newest last. */
    fun recentUpdates(): List<Map<String, Any>> =
        (load()["updates"] as? List<Map<String, Any>>) ?: emptyList()

    /**
     * Sends an approve or reject decision for one pending request. The companion checks expiry and the
     * exact digest locally; the Mac supervisor checks them again and enforces single use.
     */
    fun decideApproval(relayUrl: String, approvalId: String, decision: String): Map<String, Any> {
        require(decision == "approve" || decision == "reject") { "decision must be approve or reject" }
        val review = pendingApprovals().firstOrNull { it.approvalId == approvalId }
            ?: throw IllegalStateException("approval is not pending on this phone (expired or already decided)")
        val result = companion(relayUrl).decideApproval(review, decision, review.canonicalArgumentsDigest)
        save(mapOf("approvals" to storedApprovals().filter { it.approvalId != approvalId }.map(::approvalMap)))
        return linkedMapOf(
            "decision" to decision,
            "approval_id" to approvalId,
            "queued_offline" to result.queuedOffline,
            "relay_status" to (result.receipt?.status ?: "queued_locally"),
        )
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
            )
        } ?: emptyList()

    private fun approvalMap(review: ApprovalReview): Map<String, Any> = linkedMapOf(
        "approval_id" to review.approvalId,
        "task_id" to review.taskId,
        "operation" to review.operation,
        "target" to review.target,
        "canonical_arguments_digest" to review.canonicalArgumentsDigest,
        "expires_at_unix_ms" to review.expiresAtUnixMs,
        "redacted_summary" to review.redactedSummary,
    )

    /** Messages the user sent from Home, newest last. Replies from the Mac update them by in_reply_to. */
    fun conversation(): List<Map<String, Any>> =
        (load()["conversation"] as? List<Map<String, Any>>) ?: emptyList()

    /** Stores a local USB task under its API idempotency key before the request can time out. */
    fun recordLocalTaskRequest(idempotencyKey: String, text: String) {
        require(idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) { "invalid task idempotency key" }
        val cleaned = text.trim()
        require(cleaned.isNotEmpty() && cleaned.toByteArray(Charsets.UTF_8).size <= 4096) { "message must be 1–4096 bytes" }
        val entries = conversation().filterNot { it["message_id"] == idempotencyKey }.toMutableList()
        entries.add(linkedMapOf(
            "message_id" to idempotencyKey,
            "text" to cleaned,
            "created_at_unix_ms" to now(),
            "state" to "RUNNING",
            "summary" to "",
            "task_id" to "",
            "updated_at_unix_ms" to now(),
        ))
        save(mapOf("conversation" to entries.takeLast(100)))
    }

    /** Records the Mac's actual supervisor result or the last known task state. */
    fun recordLocalTaskResult(idempotencyKey: String, taskId: String, state: String, summary: String) {
        applyToConversation(idempotencyKey, taskId, state.uppercase(), summary)
    }

    /** Sends a typed request to the Mac supervisor. Queued locally if the relay is unreachable. */
    fun sendRequest(relayUrl: String, text: String): Map<String, Any> {
        val cleaned = text.trim()
        require(cleaned.isNotEmpty() && cleaned.toByteArray(Charsets.UTF_8).size <= 4096) { "message must be 1–4096 bytes" }
        val result = submit(relayUrl, TaskSubmission(cleaned, setOf(SafeCapability.READ_ONLY)), null)
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
        return linkedMapOf("message_id" to result.messageId, "queued_offline" to result.queuedOffline)
    }

    private fun applyToConversation(messageId: String, taskId: String, state: String, summary: String) {
        val entries = conversation().map { entry ->
            if (entry["message_id"] == messageId) LinkedHashMap(entry).apply {
                put("state", state); put("summary", summary); put("task_id", taskId); put("updated_at_unix_ms", now())
            } else entry
        }
        save(mapOf("conversation" to entries))
    }

    /** Result of the last sync attempt: time in Unix ms (0 = never) and an error code ("" = none). */
    fun lastSync(): Pair<Long, String> {
        val values = load()
        return ((values["last_sync_ms"] as? Long) ?: 0L) to ((values["last_sync_error"] as? String) ?: "")
    }

    private fun recordEvents(events: List<CompanionEvent>) {
        var approvals = storedApprovals().toMutableList()
        val updates = ((load()["updates"] as? List<Map<String, Any>>) ?: emptyList()).toMutableList()
        for (event in events) {
            when (event) {
                is CompanionEvent.ApprovalPending -> {
                    approvals = approvals.filter { it.approvalId != event.review.approvalId }.toMutableList()
                    approvals.add(event.review)
                }
                is CompanionEvent.TaskUpdate -> {
                    updates.add(linkedMapOf(
                        "task_id" to event.taskId, "state" to event.state, "summary" to event.summary, "received_at_unix_ms" to now(),
                    ))
                    if (event.inReplyTo.isNotBlank()) applyToConversation(event.inReplyTo, event.taskId, event.state, event.summary)
                }
                else -> Unit
            }
        }
        save(mapOf(
            "approvals" to approvals.map(::approvalMap),
            "updates" to updates.takeLast(20),
        ))
    }

    fun flush(relayUrl: String): Map<String, Any> {
        val companion = companion(relayUrl)
        val receipts = companion.flushOutbox()
        return linkedMapOf(
            "delivered" to receipts.size,
            "remaining" to companion.outboxSize(),
            "connection" to companion.connectionState.name,
            "last_error" to (companion.lastErrorCode ?: ""),
        )
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
