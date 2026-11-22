package com.jarvis.companion.data

import android.content.Context
import android.util.Log
import com.jarvis.companion.BuildConfig
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.net.URLEncoder
import java.net.URL
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import java.time.OffsetDateTime
import java.time.Instant
import com.jarvis.companion.model.Approval
import com.jarvis.companion.model.ApiException
import com.jarvis.companion.model.JarvisTask
import com.jarvis.companion.model.LinkTaskResult
import com.jarvis.companion.model.PairingInvitation
import com.jarvis.companion.model.PairingSession
import com.jarvis.companion.model.PairingStatus
import com.jarvis.companion.model.PairedDevice
import com.jarvis.companion.model.TaskDetail
import com.jarvis.companion.model.TaskEvent
import com.jarvis.companion.model.TaskArtifact
import com.jarvis.companion.ui.PerformanceTrace
import com.jarvis.link.ApprovalReview
import com.jarvis.link.DevelopmentPlanReview

class JarvisApi(private val context: Context, private val secureStore: SecureStore) {
    /** Fixed read-only status fields from the paired Mac; this is not a shell or general command endpoint. */
    fun getMacSystemInfo(traceId: String? = null): String {
        val token = secureStore.token()?.takeIf { it.isNotBlank() }
            ?: throw ApiException("This phone is not paired with the Mac. Open Settings and check the connection.", 401)
        val response = request(
            requireLoopbackBaseUrl(),
            "/v1/jarvis/system/info",
            "GET",
            null,
            token,
            allowLocalWifi = true,
            traceId = traceId,
        )
        if (!response.optBoolean("online", false)) throw ApiException("The Mac did not confirm its online status.")
        val battery = if (response.has("battery_percent") && !response.isNull("battery_percent")) {
            "Battery ${response.getInt("battery_percent")}%" + when {
                response.optBoolean("charging", false) -> " and charging"
                response.has("charging") && !response.isNull("charging") -> " and not charging"
                else -> ""
            }
        } else "Battery status unavailable"
        val uptime = response.optString("uptime").takeIf { it.isNotBlank() }
            ?: throw ApiException("The Mac response did not include its uptime.")
        return "Mac is online. $battery. System uptime: $uptime."
    }

    /** Runs one read-only free-text turn through the Mac supervisor and configured Codex engine. */
    fun submitLinkTask(
        objective: String,
        idempotencyKey: String,
        capabilityScope: String = "assistant",
        traceId: String? = null,
    ): LinkTaskResult {
        val cleaned = objective.trim()
        if (cleaned.isEmpty() || cleaned.toByteArray(Charsets.UTF_8).size > 4096) {
            throw ApiException("Write a request under 4096 bytes.")
        }
        if (!idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) {
            throw ApiException("This request could not be identified safely. Try again.")
        }
        if (capabilityScope !in setOf("assistant", "development")) {
            throw ApiException("This request has an unsupported authorization scope.")
        }
        val token = secureStore.token()?.takeIf { it.isNotBlank() }
            ?: throw ApiException("This phone is not paired with the Mac. Open Settings and check the connection.", 401)
        val requesterDeviceId = secureStore.deviceId().takeIf { it.matches(Regex("[0-9a-f]{32}")) }
            ?: throw ApiException("This phone's pairing identity is incomplete. Pair it with the Mac again.", 401)
        val body = JSONObject()
            .put("objective", cleaned)
            .put("capability_scope", JSONArray().put(capabilityScope))
            .put("idempotency_key", idempotencyKey)
            .put("requester_device_id", requesterDeviceId)
        val json = request(
            requireLoopbackBaseUrl(),
            "/v1/jarvis/link/tasks?mode=async",
            "POST",
            body,
            token,
            readTimeoutMs = 180_000,
            allowLocalWifi = true,
            traceId = traceId,
        )
        return parseLinkTaskResult(json)
    }

    /** Resolves an uncertain or interrupted request without creating a second task. */
    fun getLinkTaskStatus(idempotencyKey: String): LinkTaskResult {
        if (!idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) {
            throw ApiException("This request could not be identified safely. Try again.")
        }
        val token = secureStore.token()?.takeIf { it.isNotBlank() }
            ?: throw ApiException("This phone is not paired with the Mac. Open Settings and check the connection.", 401)
        val json = request(
            requireLoopbackBaseUrl(),
            "/v1/jarvis/link/tasks/" + pathSegment(idempotencyKey),
            "GET",
            null,
            token,
            allowLocalWifi = true,
        )
        return parseLinkTaskResult(json)
    }

    /** Cancels one owned Link task and revokes any approval that has not been consumed. */
    fun cancelLinkTask(idempotencyKey: String): LinkTaskResult {
        if (!idempotencyKey.matches(Regex("[A-Za-z0-9._~-]{16,128}"))) {
            throw ApiException("This request could not be identified safely.")
        }
        val token = secureStore.token()?.takeIf { it.isNotBlank() }
            ?: throw ApiException("This phone is not paired with the Mac. Open Settings and check the connection.", 401)
        val json = request(
            requireLoopbackBaseUrl(),
            "/v1/jarvis/link/tasks/" + pathSegment(idempotencyKey) + "/cancel",
            "POST",
            JSONObject(),
            token,
            allowLocalWifi = true,
        )
        return parseLinkTaskResult(json)
    }

    /** Pins the Mac's Wi-Fi TLS certificate using a nonce-bound signature obtained over USB. */
    fun pinLocalGatewayIdentity(): LocalGatewayPin {
        val loopback = requireLoopbackBaseUrl()
        val bearer = secureStore.token()?.takeIf { it.isNotBlank() }
            ?: throw ApiException("Pair this phone with the Mac before enabling local Wi-Fi.", 401)
        val pinned = secureStore.pinnedServerIdentity() ?: bootstrapPinnedServerIdentity()
        val nonce = newMigrationNonce()
        val response = request(
            loopback,
            "/v1/jarvis/local-gateway/identity",
            "POST",
            JSONObject().put("nonce", nonce),
            bearer,
        )
        val serverPublicKey = response.requiredString("server_public_key")
        val serverFingerprint = response.requiredString("server_fingerprint")
        val identity = try {
            ServerMigrationIdentity.verifyLocalGatewayIdentity(
                pinned = pinned,
                expectedNonce = nonce,
                protocol = response.requiredString("protocol"),
                responseNonce = response.requiredString("nonce"),
                hostname = response.requiredString("hostname"),
                portText = response.requiredString("port"),
                certificateSha256 = response.requiredString("certificate_sha256"),
                serverPublicKeyBase64 = serverPublicKey,
                serverFingerprint = serverFingerprint,
                signatureBase64 = response.requiredString("signature"),
            )
        } catch (failure: ServerMigrationIdentity.VerificationException) {
            throw ApiException("The Mac Wi-Fi identity failed verification (${failure.reason.name}).")
        }
        val presentedKey = Base64.getDecoder().decode(serverPublicKey)
        if (!MessageDigest.isEqual(pinned.publicKeyDer, presentedKey) || pinned.fingerprint != serverFingerprint) {
            throw ApiException("The Wi-Fi gateway is not the paired Mac.")
        }
        val pin = LocalGatewayPin(identity.hostname, identity.port, identity.certificateSha256)
        val addressNonce = newMigrationNonce()
        val addressResponse = request(
            loopback,
            "/v1/jarvis/local-gateway/address",
            "POST",
            JSONObject().put("nonce", addressNonce),
            bearer,
        )
        val addressKey = addressResponse.requiredString("server_public_key")
        val addressFingerprint = addressResponse.requiredString("server_fingerprint")
        val address = try {
            ServerMigrationIdentity.verifyLocalGatewayAddress(
                pinned = pinned,
                pin = pin,
                expectedNonce = addressNonce,
                protocol = addressResponse.requiredString("protocol"),
                responseNonce = addressResponse.requiredString("nonce"),
                hostname = addressResponse.requiredString("hostname"),
                portText = addressResponse.requiredString("port"),
                addressText = addressResponse.requiredString("address"),
                certificateSha256 = addressResponse.requiredString("certificate_sha256"),
                serverPublicKeyBase64 = addressKey,
                serverFingerprint = addressFingerprint,
                signatureBase64 = addressResponse.requiredString("signature"),
            )
        } catch (failure: ServerMigrationIdentity.VerificationException) {
            throw ApiException("The Mac Wi-Fi address failed verification (${failure.reason.name}).")
        }
        val addressPin = pin.copy(address = address)
        try {
            secureStore.saveLocalGatewayPin(addressPin)
        } catch (failure: SecurityException) {
            throw ApiException(failure.message ?: "The Mac Wi-Fi certificate changed.")
        }
        return addressPin
    }

    private fun parseLinkTaskResult(json: JSONObject): LinkTaskResult {
        val state = json.requiredString("state").uppercase()
        val resultText = json.optString("result_text").takeUnless { json.isNull("result_text") || it == "null" }
        val summary = json.optString("summary").takeUnless { json.isNull("summary") || it == "null" }
        return LinkTaskResult(
            taskId = json.requiredString("task_id"),
            state = state,
            summary = linkTaskSummary(state, resultText, summary),
            resultDigest = json.optString("result_digest").takeUnless { json.isNull("result_digest") || it == "null" }.orEmpty(),
        )
    }

    fun parseInvitationPayload(rawPayload: String): PairingInvitation {
        if (rawPayload.length > 8192) throw ApiException("This QR is too large to be a Jarvis pairing invitation.")
        val json = try { JSONObject(rawPayload) } catch (_: Exception) {
            throw ApiException("This QR is not a Jarvis pairing invitation.")
        }
        val requiredKeys = setOf(
            "protocol",
            "server_url",
            "server_fingerprint",
            "server_public_key",
            "invitation_id",
            "challenge",
            "expires_at",
            "server_signature",
        )
        val actualKeys = mutableSetOf<String>()
        val iterator = json.keys()
        while (iterator.hasNext()) actualKeys.add(iterator.next())
        if (actualKeys != requiredKeys) {
            throw ApiException("The invitation QR has unexpected or missing fields. Scan a fresh Jarvis QR.")
        }
        if (json.requiredString("protocol") != "openjarvis-pairing-v1") {
            throw ApiException("This QR uses an unsupported Jarvis pairing protocol.")
        }
        verifyInvitationSignature(json)
        val expiry = json.requiredString("expires_at")
        val expiryInstant = runCatching { OffsetDateTime.parse(expiry).toInstant() }
            .getOrElse { throw ApiException("The invitation expiry time is invalid.") }
        if (!expiryInstant.isAfter(Instant.now())) throw ApiException("This pairing invitation has expired. Create a new QR on the Mac.")
        return PairingInvitation(
            invitationId = json.requiredString("invitation_id"),
            challenge = json.requiredString("challenge"),
            expiresAt = expiry,
            serverUrl = validateBaseUrl(json.requiredString("server_url")),
            serverFingerprint = json.requiredString("server_fingerprint"),
            serverPublicKeyBase64 = json.requiredString("server_public_key"),
            verifiedServerKeyFingerprint = fingerprint(Base64.getDecoder().decode(json.requiredString("server_public_key"))),
        )
    }

    private fun verifyInvitationSignature(json: JSONObject) {
        val fields = linkedMapOf<String, String>()
        listOf("challenge", "expires_at", "invitation_id", "protocol", "server_fingerprint", "server_url", "server_public_key")
            .forEach { key -> fields[key] = json.requiredString(key) }
        when (PairingInvitationSignature.verify(
            fields,
            json.requiredString("server_public_key"),
            json.requiredString("server_fingerprint"),
            json.requiredString("server_signature"),
        )) {
            null -> Unit
            PairingInvitationSignature.Failure.INVALID_KEY -> throw ApiException("The Mac invitation contains an invalid public key.")
            PairingInvitationSignature.Failure.FINGERPRINT_MISMATCH -> throw ApiException("The Mac public-key fingerprint does not match the invitation.")
            PairingInvitationSignature.Failure.INVALID_SIGNATURE -> throw ApiException("The Mac invitation signature is malformed.")
            PairingInvitationSignature.Failure.SIGNATURE_MISMATCH -> throw ApiException("The Mac invitation signature does not verify.")
        }
    }

    private fun fingerprint(encodedPublicKey: ByteArray): String =
        MessageDigest.getInstance("SHA-256")
            .digest(encodedPublicKey)
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }
            .let { "sha256:$it" }

    fun deviceName(): String {
        val manufacturer = Build.MANUFACTURER?.trim().orEmpty()
        val model = Build.MODEL?.trim().orEmpty()
        return listOf(manufacturer, model).filter { it.isNotBlank() }.distinct().joinToString(" ")
            .ifBlank { "Android device" }
    }

    fun completePairing(invitation: PairingInvitation): PairingSession {
        val base = validateBaseUrl(invitation.serverUrl)
        secureStore.setBaseUrl(base)
        val body = JSONObject()
            .put("invitation_id", invitation.invitationId)
            .put("device_name", deviceName())
            .put("public_key", secureStore.publicSigningKey())
            .put("signature", secureStore.signChallenge(invitation.challenge))
        val json = request(base, "/v1/jarvis/pair/complete", "POST", body, token = null)
        if (json.has("access_token")) throw ApiException("The Mac returned a token before local confirmation. Pairing stopped.")
        val status = json.requiredString("status").uppercase()
        if (status != "PENDING_CONFIRMATION") throw ApiException("The Mac did not place this device in pending confirmation.")
        val id = json.optString("invitation_id", json.optString("pairing_id", invitation.invitationId))
        return PairingSession(id)
    }

    fun getPairingStatus(invitationId: String): PairingStatus {
        val base = requireBaseUrl()
        val json = request(base, "/v1/jarvis/pair/status/" + pathSegment(invitationId), "GET", null, token = null)
        val status = json.requiredString("status").uppercase()
        if (json.has("access_token")) throw ApiException("The Mac returned a token before the claim step. Pairing stopped.")
        return when (status) {
            "PENDING", "PENDING_CONFIRMATION" -> PairingStatus.Pending
            "REJECTED" -> PairingStatus.Rejected
            "EXPIRED" -> PairingStatus.Expired
            "APPROVED" -> PairingStatus.Approved(json.requiredString("claim_challenge"))
            else -> throw ApiException("The Mac returned an unknown pairing status.")
        }
    }

    fun claimPairing(invitationId: String, claimChallenge: String, serverPublicKeyBase64: String, serverFingerprint: String): PairedDevice {
        val body = JSONObject()
            .put("invitation_id", invitationId)
            .put("signature", secureStore.signChallenge(claimChallenge))
        val json = request(requireBaseUrl(), "/v1/jarvis/pair/claim", "POST", body, token = null)
        val token = json.requiredString("access_token")
        val deviceId = json.optString("device_id", invitationId)
        val expiresAt = json.optString("expires_at", "")
        secureStore.savePairingWithServerIdentity(
            deviceId = deviceId,
            token = token,
            serverPublicKeyDer = Base64.getDecoder().decode(serverPublicKeyBase64),
            serverFingerprint = serverFingerprint,
        )
        return PairedDevice(deviceId, token, expiresAt)
    }

    /** Authenticate the server pin once over the current USB/loopback device-token route. */
    fun bootstrapPinnedServerIdentity(): PinnedServerIdentity {
        secureStore.pinnedServerIdentity()?.let { return it }
        val currentLoopbackUrl = requireBaseUrl()
        val bearer = secureStore.token() ?: throw ApiException("Pair this phone before pinning the Mac identity.")
        val deviceId = secureStore.deviceId()
        if (deviceId.isBlank()) throw ApiException("The paired-device identity is missing.")
        val nonce = newMigrationNonce()
        val response = request(
            currentLoopbackUrl,
            "/v1/jarvis/devices/migration-bootstrap",
            "POST",
            JSONObject().put("nonce", nonce),
            token = bearer,
        )
        val pin = try {
            ServerMigrationIdentity.verifyBootstrap(
                bearerToken = bearer,
                expectedDeviceId = deviceId,
                expectedNonce = nonce,
                responseDeviceId = response.requiredString("device_id"),
                responseNonce = response.requiredString("nonce"),
                serverPublicKeyBase64 = response.requiredString("server_public_key"),
                serverFingerprint = response.requiredString("server_fingerprint"),
                proofHmacHex = response.requiredString("proof_hmac"),
            )
        } catch (failure: ServerMigrationIdentity.VerificationException) {
            throw ApiException("The current USB Mac identity could not be authenticated (" + failure.reason.name + ").")
        }
        secureStore.saveServerIdentityPin(pin.publicKeyDer, pin.fingerprint)
        return pin
    }

    /** Uses no bearer on the candidate host; its URL changes only after pin verification. */
    fun migrateBaseUrlToTailscale(candidateUrl: String): String {
        val target = try {
            ServerMigrationIdentity.validateTailscaleTarget(candidateUrl)
        } catch (failure: IllegalArgumentException) {
            throw ApiException(failure.message ?: "The candidate is not an allowed Tailscale address.")
        }
        val pin = secureStore.pinnedServerIdentity() ?: bootstrapPinnedServerIdentity()
        val nonce = newMigrationNonce()
        val response = request(
            target.baseUrl,
            "/v1/jarvis/identity/challenge",
            "POST",
            JSONObject().put("nonce", nonce).put("target_origin", target.origin),
            token = null,
        )
        val fields = listOf("protocol", "nonce", "target_origin", "server_public_key", "server_fingerprint")
            .associateWith { response.requiredString(it) }
        val failure = ServerMigrationIdentity.verifyIdentityChallenge(
            pinned = pin,
            expectedNonce = nonce,
            expectedOrigin = target.origin,
            protocol = fields.getValue("protocol"),
            responseNonce = fields.getValue("nonce"),
            responseOrigin = fields.getValue("target_origin"),
            serverPublicKeyBase64 = fields.getValue("server_public_key"),
            serverFingerprint = fields.getValue("server_fingerprint"),
            signatureBase64 = response.requiredString("signature"),
        )
        if (failure != null) {
            throw ApiException("The candidate host failed pinned Mac identity verification (" + failure.name + ").")
        }
        secureStore.updateBaseUrlAfterPinnedIdentityVerification(
            value = target.baseUrl,
            serverPublicKeyDer = pin.publicKeyDer,
            serverFingerprint = pin.fingerprint,
        )
        return target.baseUrl
    }

    private fun newMigrationNonce(): String {
        val bytes = ByteArray(32)
        java.security.SecureRandom().nextBytes(bytes)
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    fun getTasks(state: String): List<JarvisTask> {
        val base = requireBaseUrl()
        val query = "?state=" + URLEncoder.encode(state, "UTF-8")
        val json = request(base, "/v1/jarvis/tasks$query", "GET", null, secureStore.token(), allowLocalWifi = true)
        return json.requiredArrayOf("tasks").map { task ->
            val id = task.requiredString("id", "task_id")
            JarvisTask(
                id = id,
                title = task.optString("title").ifBlank {
                    task.optString("task").ifBlank { task.optString("objective").ifBlank { "Task " + id.take(8) } }
                },
                state = task.optString("state", task.optString("status", "unknown")),
                currentStep = task.optString("current_step", ""),
                updatedAt = task.optString("updated_at", task.optString("created_at", "")),
                progress = if (task.has("progress") && !task.isNull("progress")) task.optInt("progress") else null,
                lastEvent = task.optString("last_event", ""),
            )
        }
    }

    fun getApprovals(): List<Approval> {
        val base = requireBaseUrl()
        val json = request(base, "/v1/jarvis/approvals/pending", "GET", null, secureStore.token(), allowLocalWifi = true)
        return json.requiredArrayOf("approvals").map(::parseApproval)
    }

    /** Reads supervisor approvals and verifies full development-plan details before the UI can display them. */
    fun getApprovalReviews(): List<Pair<Approval, ApprovalReview>> {
        val base = requireBaseUrl()
        val json = request(base, "/v1/jarvis/approvals/pending", "GET", null, secureStore.token(), allowLocalWifi = true)
        return json.requiredArrayOf("approvals").map { item ->
            val approval = parseApproval(item)
            approval to parseSupervisorApprovalReview(item.toKotlinMap(), approval)
        }
    }

    fun getApproval(approvalId: String, connectTimeoutMs: Int = 10_000, readTimeoutMs: Int = 25_000): Approval {
        val base = requireBaseUrl()
        val json = request(
            base,
            "/v1/jarvis/approvals/" + pathSegment(approvalId),
            "GET",
            null,
            secureStore.token(),
            connectTimeoutMs = connectTimeoutMs,
            readTimeoutMs = readTimeoutMs,
            allowLocalWifi = true,
        )
        return parseApproval(json.optJSONObject("approval") ?: json)
    }

    private fun parseApproval(item: JSONObject): Approval {
        val arguments = item.opt("canonical_arguments")
            ?: item.opt("arguments")
            ?: item.opt("parameters_summary")
            ?: throw ApiException("Approval response is missing canonical arguments.")
        return Approval(
            id = item.requiredString("id", "request_id"),
            taskId = item.optString("task_id", ""),
            operation = item.requiredString("operation", "action_type"),
            targetResource = item.requiredString("target_resource", "target"),
            reason = item.optString("reason", item.optString("description", "")),
            risk = item.optString("risk", item.optString("tier", "unspecified")),
            operationDigest = item.requiredString("operation_digest", "expected_operation_digest"),
            createdAt = item.optString("created_at", ""),
            expiresAt = item.optString("expires_at", ""),
            parametersSummary = if (arguments is String) arguments else arguments.toString(),
            status = item.optString("status", "PENDING").uppercase(),
        )
    }

    fun registerFcmToken(registrationToken: String) {
        require(registrationToken.isNotBlank() && registrationToken.length <= 4096)
        val body = JSONObject().put("token", registrationToken)
        request(
            requireBaseUrl(),
            "/v1/jarvis/devices/fcm-token",
            "POST",
            body,
            secureStore.token(),
            allowLocalWifi = true,
        )
    }

    fun unregisterFcmToken() {
        request(
            requireBaseUrl(),
            "/v1/jarvis/devices/fcm-token",
            "DELETE",
            null,
            secureStore.token(),
            allowLocalWifi = true,
        )
    }

    fun getTaskDetail(taskId: String): TaskDetail {
        val base = requireBaseUrl()
        val json = request(base, "/v1/jarvis/tasks/${pathSegment(taskId)}", "GET", null, secureStore.token(), allowLocalWifi = true)
        val taskJson = json.optJSONObject("task") ?: json
        val id = taskJson.requiredString("id", "task_id")
        val task = JarvisTask(
            id = id,
            title = taskJson.optString("title").ifBlank {
                taskJson.optString("objective").ifBlank { "Task " + id.take(8) }
            },
            state = taskJson.optString("state", taskJson.optString("status", "unknown")),
            currentStep = taskJson.optString("current_step", ""),
            updatedAt = taskJson.optString("updated_at", taskJson.optString("created_at", "")),
            progress = if (taskJson.has("progress") && !taskJson.isNull("progress")) taskJson.optInt("progress") else null,
            lastEvent = taskJson.optString("last_event", ""),
        )
        val events = json.optionalArrayOf("events").mapIndexed { index, event ->
            val payload = event.optJSONObject("payload")
            val type = event.optString("event_type", event.optString("state", event.optString("type", "event")))
            val details = listOf(
                payload?.optString("to")?.takeIf { !it.isNullOrBlank() }?.let { "State: " + it },
                payload?.optString("step")?.takeIf { !it.isNullOrBlank() }?.let { "Step: " + it },
                payload?.optString("operation")?.takeIf { !it.isNullOrBlank() }?.let { "Operation: " + it },
                payload?.optString("target")?.takeIf { !it.isNullOrBlank() }?.let { "Target: " + it },
                payload?.optString("reason")?.takeIf { !it.isNullOrBlank() }?.let { "Reason: " + it },
            ).filterNotNull().joinToString("\n")
            TaskEvent(
                id = event.optString("seq", event.optString("id", "event-$index")),
                at = event.optString("at", event.optString("created_at", "")),
                state = type,
                message = event.optString("message").ifBlank {
                    event.optString("description").ifBlank { details.ifBlank { "Event recorded." } }
                },
                toolName = event.optString("tool_name", payload?.optString("tool_name", "") ?: ""),
                durationMs = if (event.has("duration_ms") && !event.isNull("duration_ms")) event.optLong("duration_ms") else null,
            )
        }
        return TaskDetail(task, events)
    }

    /** Lists only archives already associated with this authenticated device's completed task. */
    fun getTaskArtifacts(taskId: String, traceId: String? = null): List<TaskArtifact> {
        require(taskId.matches(Regex("[0-9a-f]{32}")))
        val token = secureStore.token()?.takeIf { it.isNotBlank() }
            ?: throw ApiException("This phone is not paired with the Mac.", 401)
        val json = request(
            requireBaseUrl(), "/v1/jarvis/tasks/${pathSegment(taskId)}/artifacts", "GET", null,
            token, connectTimeoutMs = 10_000, readTimeoutMs = 25_000, allowLocalWifi = true,
            traceId = traceId,
        )
        return json.requiredArrayOf("artifacts").map { item ->
            val artifactId = item.requiredString("artifact_id")
            val fileName = item.requiredString("file_name")
            val size = item.optLong("size_bytes", -1)
            val archiveDigest = item.requiredString("archive_sha256")
            val manifestDigest = item.requiredString("manifest_sha256")
            if (!artifactId.matches(Regex("[0-9a-f]{64}")) ||
                !fileName.matches(Regex("[A-Za-z0-9][A-Za-z0-9._-]{0,100}\\.zip")) ||
                size !in 1..5_242_880 ||
                !archiveDigest.matches(Regex("[0-9a-f]{64}")) ||
                !manifestDigest.matches(Regex("[0-9a-f]{64}"))
            ) throw ApiException("The Mac returned invalid project archive metadata.")
            TaskArtifact(
                taskId = taskId, artifactId = artifactId, fileName = fileName,
                sizeBytes = size, archiveSha256 = archiveDigest, manifestSha256 = manifestDigest,
                downloadPath = "/v1/jarvis/tasks/${pathSegment(taskId)}/artifacts/$artifactId/download",
            )
        }
    }

    /** Downloads an archive with the paired TLS identity, then verifies size and SHA-256 before returning bytes. */
    fun downloadTaskArtifact(artifact: TaskArtifact, traceId: String? = null): ByteArray {
        val startedNs = System.nanoTime()
        traceId?.let { PerformanceTrace.mark(it, "artifact_download_start") }
        require(artifact.taskId.matches(Regex("[0-9a-f]{32}")))
        require(artifact.artifactId.matches(Regex("[0-9a-f]{64}")))
        require(artifact.sizeBytes in 1..5_242_880)
        val token = secureStore.token()?.takeIf { it.isNotBlank() }
            ?: throw ApiException("This phone is not paired with the Mac.", 401)
        val base = requireBaseUrl()
        val wifiPin = runCatching { secureStore.localGatewayPin() }.getOrNull()
        val loopbackReady = wifiPin != null && loopbackEndpointReachable(base)
        val wifiCandidate = wifiPin?.let { pin ->
            if (loopbackReady) LocalGatewayDiscovery.cachedOrSchedule(context, pin)
            else LocalGatewayDiscovery.discoverOrWait(context, pin)
        }
        val wifiEndpoint = selectLocalGatewayEndpoint(loopbackReady, wifiCandidate)
        val effectiveBase = wifiEndpoint?.httpsBaseUrl() ?: base
        val connection = (URL(effectiveBase + artifact.downloadPath).openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            instanceFollowRedirects = false
            connectTimeout = 10_000
            readTimeout = 60_000
            setRequestProperty("Accept", "application/zip")
            setRequestProperty("X-Jarvis-Client", "android-companion/0.1")
            if (traceId != null && traceId.matches(Regex("[0-9a-f]{12}"))) {
                setRequestProperty("X-Jarvis-Trace-ID", traceId)
            }
            setRequestProperty("Authorization", "Bearer $token")
        }
        if (wifiPin != null && wifiEndpoint != null) {
            val tls = connection as? javax.net.ssl.HttpsURLConnection
                ?: throw ApiException("The discovered Mac gateway did not use HTTPS.")
            LocalGatewayTls.configure(tls, wifiPin)
        }
        try {
            val status = connection.responseCode
            if (status !in 200..299) {
                val detail = connection.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()
                val message = runCatching { JSONObject(detail).optString("detail", "HTTP $status") }.getOrDefault("HTTP $status")
                throw ApiException(message.take(240), status)
            }
            if (connection.getHeaderField("X-Content-SHA256") != artifact.archiveSha256) {
                throw ApiException("The project archive identity changed during download.")
            }
            val declaredLength = connection.contentLengthLong
            if (declaredLength != artifact.sizeBytes) throw ApiException("The project archive size did not match its listing.")
            val transferStartedNs = System.nanoTime()
            val data = connection.inputStream.use { input ->
                val output = java.io.ByteArrayOutputStream(artifact.sizeBytes.toInt())
                val buffer = ByteArray(32 * 1024)
                var total = 0
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    total += count
                    if (total > 5_242_880 || total > artifact.sizeBytes) throw ApiException("The project archive exceeded its verified size.")
                    output.write(buffer, 0, count)
                }
                output.toByteArray()
            }
            val digest = MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }
            if (data.size.toLong() != artifact.sizeBytes || digest != artifact.archiveSha256) {
                throw ApiException("The downloaded project archive failed its SHA-256 check.")
            }
            traceId?.let {
                PerformanceTrace.mark(it, "artifact_download_verified", java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - transferStartedNs), "bytes=${data.size}")
            }
            return data
        } catch (error: ApiException) {
            throw error
        } catch (error: Exception) {
            throw ApiException(error.message ?: "The project archive could not be downloaded safely.")
        } finally {
            traceId?.let {
                PerformanceTrace.mark(it, "artifact_download_finished", java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startedNs))
            }
            connection.disconnect()
        }
    }

    fun decide(approval: Approval, decision: String, continuationObjective: String? = null): JSONObject {
        require(decision == "approve" || decision == "reject")
        val body = JSONObject()
            .put("decision", decision)
            .put("task_id", approval.taskId)
            .put("expected_operation_digest", approval.operationDigest)
            .put("requester_device_id", secureStore.deviceId())
            .put("idempotency_key", secureStore.decisionIdempotencyKey(approval.id, approval.operationDigest, decision))
        if (decision == "approve" && !continuationObjective.isNullOrBlank()) {
            body.put("continuation_objective", continuationObjective.trim())
        }
        val result = request(
            requireBaseUrl(),
            "/v1/jarvis/approvals/" + pathSegment(approval.id) + "/decision",
            "POST",
            body,
            secureStore.token(),
            readTimeoutMs = if (decision == "approve" && !continuationObjective.isNullOrBlank()) 960_000 else 25_000,
            allowLocalWifi = true,
        )
        secureStore.clearDecisionIdempotencyKey(approval.id, approval.operationDigest, decision)
        return result
    }

    private fun requireBaseUrl(): String {
        val value = secureStore.baseUrl()
        if (value.isBlank()) throw ApiException("Pair Jarvis with this Mac first.")
        val uri = try { URI(value.trim().trimEnd('/')) } catch (_: Exception) {
            throw ApiException("The saved Jarvis server URL is invalid.")
        }
        if (isLoopbackHost(uri.host.orEmpty())) return validateBaseUrl(value)
        if (secureStore.pinnedServerIdentity() == null) {
            throw ApiException("This server URL has no pinned Mac identity. Reconnect over USB and verify it first.")
        }
        return try {
            ServerMigrationIdentity.validateTailscaleTarget(value).baseUrl
        } catch (failure: IllegalArgumentException) {
            throw ApiException(failure.message ?: "The saved server URL is outside the pinned Tailscale range.")
        }
    }

    private fun requireLoopbackBaseUrl(): String {
        val value = secureStore.baseUrl()
        if (value.isBlank()) throw ApiException("Pair this phone with the Mac before sending a request.")
        return validateBaseUrl(value)
    }

    private fun validateBaseUrl(value: String): String {
        val uri = try { URI(value.trim().trimEnd('/')) } catch (_: Exception) { throw ApiException("Enter a valid private server URL.") }
        val scheme = uri.scheme?.lowercase().orEmpty()
        val host = uri.host?.lowercase().orEmpty()
        if (uri.userInfo != null || uri.query != null || uri.fragment != null || host.isBlank() ||
            (!uri.path.isNullOrBlank() && uri.path != "/")
        ) {
            throw ApiException("The invitation server URL is malformed.")
        }
        if (scheme != "http" && scheme != "https") throw ApiException("The invitation must use HTTP or HTTPS.")
        if (!isLoopbackHost(host)) {
            throw ApiException("This build accepts only USB-reverse localhost invitations. LAN, public, VPN, and tunnel hosts are blocked.")
        }
        return uri.toString().trimEnd('/')
    }

    private fun isLoopbackHost(host: String): Boolean {
        val normalized = host.removePrefix("[").removeSuffix("]").lowercase()
        return normalized == "localhost" || normalized == "127.0.0.1"
    }

    /** Detect the already-forwarded USB endpoint quickly so optional Wi-Fi discovery stays off the hot path. */
    private fun loopbackEndpointReachable(base: String): Boolean = runCatching {
        val uri = URI(base)
        if (!isLoopbackHost(uri.host.orEmpty())) return false
        val port = uri.port.takeIf { it in 1..65535 } ?: if (uri.scheme.equals("https", ignoreCase = true)) 443 else 80
        Socket().use { socket ->
            socket.connect(InetSocketAddress(uri.host, port), 120)
            true
        }
    }.getOrDefault(false)

    private fun pathSegment(value: String): String = URLEncoder.encode(value, "UTF-8").replace("+", "%20")

    private fun request(
        base: String,
        path: String,
        method: String,
        body: JSONObject?,
        token: String?,
        connectTimeoutMs: Int = 10_000,
        readTimeoutMs: Int = 25_000,
        allowLocalWifi: Boolean = false,
        traceId: String? = null,
    ): JSONObject {
        val requestStartedNs = System.nanoTime()
        val wifiPin = if (allowLocalWifi && !token.isNullOrBlank()) {
            runCatching { secureStore.localGatewayPin() }.getOrNull()
        } else null
        val discoveryStartedNs = System.nanoTime()
        val loopbackReady = wifiPin != null && loopbackEndpointReachable(base)
        val wifiCandidate = wifiPin?.let { pin ->
            if (loopbackReady) LocalGatewayDiscovery.cachedOrSchedule(context, pin)
            else LocalGatewayDiscovery.discoverOrWait(context, pin)
        }
        val wifiEndpoint = selectLocalGatewayEndpoint(loopbackReady, wifiCandidate)
        if (traceId != null) {
            PerformanceTrace.mark(
                traceId,
                "gateway_discovery_checked",
                java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - discoveryStartedNs),
                "wifi_cached=${wifiEndpoint != null}",
            )
        }
        val effectiveBase = wifiEndpoint?.httpsBaseUrl() ?: base
        var wifiPhase = "url"
        val connection = (URL(effectiveBase + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            instanceFollowRedirects = false
            connectTimeout = connectTimeoutMs
            readTimeout = readTimeoutMs
            setRequestProperty("Accept", "application/json")
            setRequestProperty("X-Jarvis-Client", "android-companion/0.1")
            if (traceId != null && traceId.matches(Regex("[0-9a-f]{12}"))) {
                setRequestProperty("X-Jarvis-Trace-ID", traceId)
            }
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
            }
            if (!token.isNullOrBlank()) setRequestProperty("Authorization", "Bearer $token")
        }
        if (wifiPin != null && wifiEndpoint != null) {
            val tls = connection as? javax.net.ssl.HttpsURLConnection
                ?: throw ApiException("The discovered Mac gateway did not use HTTPS.")
            wifiPhase = "tls_configure"
            LocalGatewayTls.configure(tls, wifiPin)
            wifiPhase = "tls_configured"
        }
        try {
            if (body != null) {
                wifiPhase = "write_body"
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            wifiPhase = "response"
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (traceId != null && traceId.matches(Regex("[0-9a-f]{12}"))) {
                val serverTrace = connection.getHeaderField("X-Jarvis-Trace-ID")
                if (serverTrace == traceId) {
                    val backendMs = connection.getHeaderField("X-Jarvis-Backend-Duration-Ms")?.toDoubleOrNull()
                    val serverWallMs = connection.getHeaderField("X-Jarvis-Server-Wall-Ms")?.toLongOrNull()
                    val detail = listOfNotNull(
                        "status=$status",
                        backendMs?.let { "backend_ms=${"%.3f".format(java.util.Locale.US, it)}" },
                        serverWallMs?.let { "server_wall_ms=$it" },
                    ).joinToString(",")
                    PerformanceTrace.mark(
                        traceId,
                        "http_response_received",
                        java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - requestStartedNs),
                        detail,
                    )
                }
            }
            if (status in 300..399) throw ApiException("The server redirected this request. Check the private HTTPS URL.", status)
            if (status !in 200..299) {
                val message = runCatching {
                    JSONObject(text).optString("detail", JSONObject(text).optString("error", "HTTP $status"))
                        .replace(Regex("[\\u0000-\\u001f]"), " ")
                        .take(240)
                }.getOrDefault("HTTP $status")
                throw ApiException(message, status)
            }
            if (allowLocalWifi && !token.isNullOrBlank()) {
                val transport = when {
                    wifiEndpoint != null -> "wifi"
                    isLoopbackHost(runCatching { URI(base).host.orEmpty() }.getOrDefault("")) -> "usb"
                    else -> "tailscale"
                }
                secureStore.recordMacConnection(transport)
                if (BuildConfig.DEBUG) {
                    val elapsedMs = java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - requestStartedNs)
                    Log.i("JarvisWifi", "request_success route=$transport status=$status elapsed_ms=$elapsedMs")
                }
            }
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        } catch (e: ApiException) {
            throw e
        } catch (e: Exception) {
            if (BuildConfig.DEBUG && wifiEndpoint != null) {
                val causes = generateSequence(e as Throwable?) { it.cause }
                    .take(5)
                    .joinToString(">") { it.javaClass.simpleName.take(40) }
                Log.w("JarvisWifi", "request_failed route=signed_private_ip phase=$wifiPhase causes=$causes")
            }
            throw ApiException(e.message ?: "Could not reach Jarvis on the private network")
        } finally {
            connection.disconnect()
        }
    }
}

private fun JSONObject.toKotlinMap(): Map<String, Any?> = buildMap {
    val names = keys()
    while (names.hasNext()) {
        val name = names.next()
        put(name, opt(name).toKotlinValue())
    }
}

private fun Any?.toKotlinValue(): Any? = when (this) {
    is JSONObject -> toKotlinMap()
    is JSONArray -> (0 until length()).map { opt(it).toKotlinValue() }
    JSONObject.NULL -> null
    else -> this
}

internal fun parseSupervisorApprovalReview(item: Map<String, Any?>, approval: Approval): ApprovalReview {
    val plan = if (approval.operation == "mcp:jarvis_dev:approve_plan") {
        val canonical = (item["canonical_arguments"] as? Map<*, *>)
            ?: (item["arguments"] as? Map<*, *>)
            ?: throw ApiException("The pending development approval omitted its canonical plan.")
        @Suppress("UNCHECKED_CAST")
        val canonicalFields = canonical as Map<String, Any?>
        val planFields = (canonicalFields["plan"] as? Map<*, *>)
            ?: throw ApiException("The pending development approval omitted its complete plan.")
        @Suppress("UNCHECKED_CAST")
        val completePlanFields = (planFields as Map<String, Any?>).toMutableMap().apply {
            put(
                "plan_digest",
                (canonicalFields["plan_digest"] as? String)?.takeIf { it.isNotBlank() }
                    ?: throw ApiException("The pending development approval omitted its plan digest."),
            )
        }
        runCatching { DevelopmentPlanReview.fromMap(completePlanFields) }
            .getOrElse { throw ApiException("The pending development plan could not be verified.") }
    } else null
    val expiresAtMs = runCatching { OffsetDateTime.parse(approval.expiresAt).toInstant().toEpochMilli() }
        .getOrElse { throw ApiException("The pending approval has an invalid expiry.") }
    return runCatching {
        ApprovalReview(
            approvalId = approval.id,
            taskId = approval.taskId,
            operation = approval.operation,
            target = approval.targetResource,
            canonicalArgumentsDigest = approval.operationDigest,
            expiresAtUnixMs = expiresAtMs,
            redactedSummary = approval.reason.ifBlank { approval.parametersSummary },
            developmentPlan = plan,
        )
    }.getOrElse { throw ApiException("The pending approval details could not be verified.") }
}

internal fun linkTaskSummary(state: String, resultText: String?, summary: String?): String {
    fun useful(value: String?): String? = value?.trim()?.takeIf { it.isNotEmpty() && it != "null" }
    return useful(resultText) ?: useful(summary) ?: when (state.uppercase()) {
        "PLANNING", "RUNNING", "VERIFYING", "CREATED", "QUEUED" -> "Jarvis is processing this task."
        "WAITING_APPROVAL" -> "Jarvis is waiting for your approval."
        "COMPLETED" -> "Task completed, but the Mac returned no result text."
        "FAILED" -> "The task failed. Check Activity for the recorded error."
        else -> "Task status: ${state.uppercase()}"
    }
}

private fun JSONObject.requiredString(vararg keys: String): String {
    for (key in keys) {
        val value = optString(key, "")
        if (value.isNotBlank() && value != "null") return value
    }
    throw ApiException("Server response is missing ${keys.first()}.")
}

private fun JSONObject.requiredArrayOf(key: String): List<JSONObject> {
    val array = optJSONArray(key) ?: throw ApiException("Server response is missing " + key + ".")
    if ((0 until array.length()).any { array.optJSONObject(it) == null }) {
        throw ApiException("Server response contains an invalid " + key + " item.")
    }
    return (0 until array.length()).mapNotNull { array.optJSONObject(it) }
}

private fun JSONObject.optionalArrayOf(key: String): List<JSONObject> {
    val array = optJSONArray(key) ?: JSONArray()
    return (0 until array.length()).mapNotNull { array.optJSONObject(it) }
}
