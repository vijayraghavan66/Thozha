package com.jarvis.link

import java.security.Signature
import java.nio.charset.StandardCharsets.UTF_8
import java.util.Base64
import java.io.File
import java.math.BigInteger
import java.security.AlgorithmParameters
import java.security.KeyFactory
import java.security.KeyPair
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPrivateKeySpec

object CompanionProtocolTest {
    @JvmStatic
    fun main(args: Array<String>) {
        val now = 1_800_000_000_000L
        val root = LinkCrypto.newSigningIdentity()
        val host = LinkCrypto.newSigningIdentity()
        val hostEnc = LinkCrypto.newEncryptionIdentity(host)
        val companion = LinkCrypto.newSigningIdentity()
        val companionEnc = LinkCrypto.newEncryptionIdentity(companion)
        val hostRoot = TrustedHostRoot(root.deviceId, root.publicKeyBase64)
        val hostCertificate = certificate(root, host, hostEnc, "host", now)
        val companionCertificate = certificate(root, companion, companionEnc, "companion", now)
        check(hostCertificate.verify(hostRoot, "tenant_sim", now))
        check(companionCertificate.verify(hostRoot, "tenant_sim", now))
        enrollmentProofBindsAllKeys(root, host, hostEnc, companion, companionEnc, now)
        assistantTextJsonRoundTrip()
        endToEndEnvelopeRoundTrip(root, host, hostEnc, hostCertificate, companion, companionEnc, companionCertificate, hostRoot, now)
        offlineOutboxAndRetry(root, host, hostEnc, hostCertificate, companion, companionEnc, companionCertificate, hostRoot, now)
        approvalReviewIsDigestAndExpiryBound()
        localTaskHistoryIsRestartSafe()
        relayClientRequiresHttpsAndCanonicalTenantIds()
        sharedFixtureInterop(args.firstOrNull() ?: System.getProperty("jarvis.fixture", "../fixtures/envelope-v1.test.json"))
        println("Jarvis Link Companion protocol checks passed.")
    }

    private fun assistantTextJsonRoundTrip() {
        val samples = listOf(
            "I\napprove",
            "Open the\nCalculator app",
            "literal %n stays literal",
            "**approve** and `df -h`",
        )
        for (sample in samples) {
            val encoded = LinkJson.encode(linkedMapOf("text" to sample))
            val decoded = LinkJson.parseObject(encoded)["text"]
            check(decoded == sample) { "JSON changed assistant text or newline boundaries" }
        }
    }

    /** Extracts the balanced `{...}` value of a top-level key (fixture has no braces inside strings). */
    private fun objectAt(json: String, key: String): String {
        val start = json.indexOf("\"$key\":")
        require(start >= 0) { "fixture missing $key" }
        val open = json.indexOf('{', start)
        var depth = 0
        for (i in open until json.length) {
            if (json[i] == '{') depth++
            if (json[i] == '}' && --depth == 0) return json.substring(open, i + 1)
        }
        error("unbalanced fixture object $key")
    }

    private fun stringAt(json: String, key: String): String =
        Regex("\"$key\"\\s*:\\s*\"([^\"]*)\"").find(json)?.groupValues?.get(1) ?: error("fixture missing string $key")

    private fun certificateFrom(json: String): DeviceCertificate =
        DeviceCertificate.fromMap(FlatJson.decode(json).mapValues { it.value.toString() })

    private fun privateKeyFromScalar(scalar: String, publicSpki: String): KeyPair {
        val params = AlgorithmParameters.getInstance("EC").apply { init(ECGenParameterSpec("secp256r1")) }
            .getParameterSpec(ECParameterSpec::class.java)
        val priv = KeyFactory.getInstance("EC").generatePrivate(ECPrivateKeySpec(BigInteger(scalar), params))
        return KeyPair(LinkCrypto.decodePublicKey(publicSpki), priv)
    }

    /** Decrypts the language-neutral fixture produced outside Kotlin, proving wire interop. */
    private fun sharedFixtureInterop(path: String) {
        val text = File(path).readText()
        val fixtureNow = 2_000_000_010_000L
        val tenant = stringAt(text, "tenant_id")
        val root = TrustedHostRoot(stringAt(text, "host_device_id"), stringAt(text, "host_root_signing_public_key_der_b64url"))
        val sender = certificateFrom(objectAt(text, "sender_certificate"))
        val recipientCert = certificateFrom(objectAt(text, "recipient_certificate"))
        check(sender.verify(root, tenant, fixtureNow)) { "fixture sender certificate rejected" }
        check(recipientCert.verify(root, tenant, fixtureNow)) { "fixture recipient certificate rejected" }
        val scalars = FlatJson.decode(objectAt(text, "test_only_private_scalars"))
        val pair = privateKeyFromScalar(scalars.string("companion_encryption"), recipientCert.encryptionPublicKeyBase64Url)
        val envelope = LinkEnvelope.parse(objectAt(text, "envelope"))
        val plaintext = LinkCrypto.decryptEnvelope(envelope, EncryptionIdentity(recipientCert.deviceId, pair), recipientCert, sender, root, fixtureNow)
        val expected = FlatJson.decode(objectAt(text, "expected_plaintext"))
        val actual = FlatJson.decode(plaintext)
        check(actual.string("approval_id") == expected.string("approval_id") && actual.string("operation_digest") == expected.string("operation_digest")) { "fixture plaintext mismatch" }
        // Tampered ciphertext must be rejected (GCM tag / signature).
        val tampered = envelope.copy(ciphertextBase64Url = envelope.ciphertextBase64Url.dropLast(2) + if (envelope.ciphertextBase64Url.takeLast(2) == "AA") "BB" else "AA")
        val rejected = runCatching { LinkCrypto.decryptEnvelope(tampered, EncryptionIdentity(recipientCert.deviceId, pair), recipientCert, sender, root, fixtureNow) }.isFailure
        check(rejected) { "tampered fixture ciphertext was accepted" }
    }

    private fun certificate(
        root: SigningIdentity,
        device: SigningIdentity,
        encryption: EncryptionIdentity,
        role: String,
        now: Long,
    ): DeviceCertificate {
        val unsigned = DeviceCertificate(
            version = "1",
            tenantId = "tenant_sim",
            deviceId = device.deviceId,
            deviceRole = role,
            signingPublicKeyBase64Url = device.publicKeyBase64,
            encryptionPublicKeyBase64Url = encryption.publicKeyBase64,
            notBeforeUnixMs = now - 1_000,
            expiresAtUnixMs = now + 60_000,
            certificateId = LinkCrypto.newId(),
            issuerDeviceId = root.deviceId,
            signatureBase64Url = "",
        )
        return unsigned.copy(signatureBase64Url = LinkCrypto.sign(root.keyPair.private, unsigned.signingBytes()))
    }

    private fun enrollmentProofBindsAllKeys(
        root: SigningIdentity,
        host: SigningIdentity,
        hostEnc: EncryptionIdentity,
        companion: SigningIdentity,
        companionEnc: EncryptionIdentity,
        now: Long,
    ) {
        val invitation = RelayInvitation("tenant_sim", LinkCrypto.newId(), LinkCrypto.newNonce(), now + 60_000)
        val enrollment = CompanionEnrollment(
            signingIdentity = companion,
            encryptionIdentity = companionEnc,
            tenantId = "tenant_sim",
            invitation = invitation,
            deviceName = "Android companion",
            now = { now },
        )
        val request = enrollment.challengeRequest()
        check("command" !in request.toJson() && "shell" !in request.toJson())
        val challengeBytes = ByteArray(32) { it.toByte() }
        val challenge = EnrollmentChallenge(
            tenantId = "tenant_sim",
            invitationId = invitation.invitationId,
            challengeId = LinkCrypto.newId(),
            challengeBase64Url = LinkCrypto.b64url(challengeBytes),
            deviceId = companion.deviceId,
            expiresAtUnixMs = now + 30_000,
        )
        val complete = enrollment.signChallenge(challenge)
        val signed = listOf(
            "JARVIS-LINK-ENROLL-V1", "tenant_sim", invitation.invitationId, challenge.challengeId,
            companion.deviceId, LinkCrypto.b64url("Android companion".toByteArray()), "companion",
            companion.publicKeyBase64, companionEnc.publicKeyBase64, request.clientNonceBase64Url,
            challenge.challengeBase64Url,
        ).joinToString("\n").toByteArray()
        val verify = Signature.getInstance("SHA256withECDSA")
        verify.initVerify(LinkCrypto.decodePublicKey(companion.publicKeyBase64))
        verify.update(signed)
        check(verify.verify(LinkCrypto.decodeBase64Url(complete.signatureBase64Url)))
        check(host.deviceId != companion.deviceId)
        check(root.deviceId != host.deviceId)
    }

    private fun endToEndEnvelopeRoundTrip(
        root: SigningIdentity,
        host: SigningIdentity,
        hostEnc: EncryptionIdentity,
        hostCert: DeviceCertificate,
        companion: SigningIdentity,
        companionEnc: EncryptionIdentity,
        companionCert: DeviceCertificate,
        hostRoot: TrustedHostRoot,
        now: Long,
    ) {
        val payload = "{\"objective\":\"summarize my local project\",\"capability_scope\":[\"read_only\"]}"
        val envelope = LinkCrypto.createEnvelope(
            tenantId = "tenant_sim",
            fromDevice = companion,
            recipientCertificate = hostCert,
            pinnedHostRoot = hostRoot,
            kind = "task.submit",
            payloadUtf8Json = payload,
            nowUnixMs = now,
            ttlMs = 60_000,
        )
        check(envelope.header.toKeyId == hostCert.certificateId)
        val json = envelope.toJson()
        check("summarize my local project" !in json) { "Relay envelope must not reveal plaintext" }
        val parsed = LinkEnvelope.parse(json)
        val plaintext = LinkCrypto.decryptEnvelope(parsed, hostEnc, hostCert, companionCert, hostRoot, now)
        check(plaintext == payload)
        check(runCatching {
            LinkCrypto.decryptEnvelope(
                parsed.copy(ciphertextBase64Url = LinkCrypto.b64url("tamper".toByteArray())),
                hostEnc, hostCert, companionCert, hostRoot, now,
            )
        }.isFailure)

        val wrongCert = hostCert.copy(certificateId = LinkCrypto.newId())
        check(runCatching {
            LinkCrypto.decryptEnvelope(parsed, hostEnc, wrongCert, companionCert, hostRoot, now)
        }.isFailure)
        check(root.deviceId == hostRoot.deviceId)
    }

    private fun offlineOutboxAndRetry(
        root: SigningIdentity,
        host: SigningIdentity,
        hostEnc: EncryptionIdentity,
        hostCert: DeviceCertificate,
        companion: SigningIdentity,
        companionEnc: EncryptionIdentity,
        companionCert: DeviceCertificate,
        hostRoot: TrustedHostRoot,
        now: Long,
    ) {
        val relay = MemoryRelay(companion)
        val outbox = MemoryMessageOutbox()
        val inbox = MemoryMessageInbox()
        val client = JarvisLinkCompanion(
            tenantId = "tenant_sim",
            signingIdentity = companion,
            encryptionIdentity = companionEnc,
            companionCertificate = companionCert,
            hostRoot = hostRoot,
            hostCertificate = hostCert,
            relay = relay,
            outbox = outbox,
            inbox = inbox,
            now = { now },
        )
        val requestId = "ab".repeat(16)
        val submitted = client.submitTask(TaskSubmission("read local project summary", setOf(SafeCapability.READ_ONLY), requestId))
        check(submitted.messageId == requestId) { "task message ID must match the phone's durable conversation ID" }
        check(submitted.queuedOffline && outbox.list().size == 1)
        client.setOnline(true)
        check(client.flushOutbox().size == 1)
        check(outbox.list().isEmpty() && relay.queued.size == 1)
        val receivedByHost = LinkEnvelope.parse(relay.queued.single())
        check(receivedByHost.header.messageId == requestId) { "host reply in_reply_to must correlate with Home" }
        val hostPlaintext = LinkCrypto.decryptEnvelope(receivedByHost, hostEnc, hostCert, companionCert, hostRoot, now)
        check("read local project summary" in hostPlaintext)
        check("shell" !in hostPlaintext)

        val approvalPayload = FlatJson.encode(linkedMapOf(
            "approval_id" to LinkCrypto.newId(),
            "task_id" to LinkCrypto.newId(),
            "operation" to "file_write",
            "target" to "scratch/test.txt",
            "canonical_arguments_digest" to "a".repeat(64),
            "expires_at_unix_ms" to (now + 30_000),
            "redacted_summary" to "Write a harmless test marker",
        ))
        val incoming = LinkCrypto.createEnvelope(
            "tenant_sim", host, companionCert, hostRoot, "approval.request", approvalPayload,
            nowUnixMs = now, ttlMs = 30_000,
        )
        relay.incoming += InboxItem(incoming, hostCert)
        val page = client.pollInbox()
        check(page.events.single() is CompanionEvent.ApprovalPending)
        check(relay.acks.single().second == AckStatus.RECEIVED)
        val duplicate = client.pollInbox()
        check(duplicate.events.isEmpty())
        check(relay.acks.size == 2) { "redelivery should be ACKed without a second event" }
    }

    private fun approvalReviewIsDigestAndExpiryBound() {
        val review = ApprovalReview(
            approvalId = "a".repeat(32),
            taskId = "b".repeat(32),
            operation = "shell_exec",
            target = "/tmp",
            canonicalArgumentsDigest = "c".repeat(64),
            expiresAtUnixMs = 2_000,
            redactedSummary = "echo a marker",
        )
        review.validateForDecision("c".repeat(64), 1_000)
        check(runCatching { review.validateForDecision("d".repeat(64), 1_000) }.isFailure)
        check(runCatching { review.validateForDecision("c".repeat(64), 2_000) }.isFailure)
        check("approval_id" in review.toDecisionJson("approve", "c".repeat(64), "idem-1", nowUnixMs = 1_000))
    }

    private fun localTaskHistoryIsRestartSafe() {
        val directory = java.nio.file.Files.createTempDirectory("jarvis-local-task-history").toFile()
        val state = FileLinkStateStore(File(directory, "state.json"))
        val requestId = "local-usb-task-20261116-000000000001"
        val first = LinkRuntime(
            keys = SoftwareTestKeyProvider(File(directory, "keys.json")),
            state = state,
            storeDir = directory,
            now = { 1_800_000_000_000L },
        )
        first.recordLocalTaskRequest(requestId, "What is Java?", transport = "link")
        first.recordLocalTaskRequest(requestId, "What is Java?", transport = "link")
        check(first.conversation().size == 1) { "re-recording one request created a duplicate conversation entry" }
        val taskId = "a".repeat(32)
        first.recordEvents(listOf(CompanionEvent.TaskUpdate(
            taskId = taskId,
            state = "COMPLETED",
            summary = "Java is a programming language.",
            inReplyTo = requestId,
        )))

        val recovered = LinkRuntime(
            keys = SoftwareTestKeyProvider(File(directory, "keys.json")),
            state = FileLinkStateStore(File(directory, "state.json")),
            storeDir = directory,
            now = { 1_800_000_000_001L },
        )
        val entry = recovered.conversation().single()
        check(entry["message_id"] == requestId)
        check(entry["task_id"] == "a".repeat(32))
        check(entry["transport"] == "link")
        check(entry["state"] == "COMPLETED")
        check(entry["summary"] == "Java is a programming language.")
        directory.deleteRecursively()
    }

    private fun relayClientRequiresHttpsAndCanonicalTenantIds() {
        HttpJarvisLinkRelay("https://relay.example", "tenant_sim")
        HttpJarvisLinkRelay("http://127.0.0.1:8787", "tenant_sim")
        check(runCatching { HttpJarvisLinkRelay("http://relay.example", "tenant_sim") }.isFailure)
        check(runCatching { HttpJarvisLinkRelay("https://relay.example", "short") }.isFailure)
        check(runCatching { HttpJarvisLinkRelay("https://relay.example", "tenant.with.dots") }.isFailure)
        check(runCatching { HttpJarvisLinkRelay("https://relay.example", "x".repeat(65)) }.isFailure)
    }

    private fun assertEquals(expected: Any?, actual: Any?) { if (expected != actual) error("Expected <$expected>, got <$actual>") }
}

private class MemoryRelay(
    private val companion: SigningIdentity,
) : JarvisLinkRelayApi {
    val queued = mutableListOf<String>()
    val incoming = mutableListOf<InboxItem>()
    val acks = mutableListOf<Pair<String, AckStatus>>()

    override fun postMessage(envelopeJson: String, headers: Map<String, String>): RelayReceipt {
        verifyRequest(headers, "POST", "/v1/messages", envelopeJson.toByteArray(UTF_8), companion)
        val envelope = LinkEnvelope.parse(envelopeJson)
        queued += envelopeJson
        return RelayReceipt(envelope.header.messageId, "queued", envelope.header.expiresAtUnixMs)
    }

    override fun inbox(pathAndQuery: String, headers: Map<String, String>): InboxPage {
        verifyRequest(headers, "GET", pathAndQuery, ByteArray(0), companion)
        return InboxPage(incoming.toList(), null)
    }

    override fun acknowledge(messageId: String, bodyJson: String, headers: Map<String, String>) {
        val body = bodyJson.toByteArray(UTF_8)
        verifyRequest(headers, "POST", "/v1/messages/$messageId/ack", body, companion)
        acks += messageId to (if (bodyJson.contains("\"received\"")) AckStatus.RECEIVED else AckStatus.REJECTED)
    }

    override fun deviceCertificate(deviceId: String, headers: Map<String, String>): DeviceCertificate =
        throw UnsupportedOperationException("not used by the in-memory relay")

    override fun enrollmentChallenge(bodyJson: String): Map<String, Any> =
        throw UnsupportedOperationException("not used by the in-memory relay")

    override fun enrollmentComplete(bodyJson: String): Map<String, Any> =
        throw UnsupportedOperationException("not used by the in-memory relay")

    private fun verifyRequest(headers: Map<String, String>, method: String, path: String, body: ByteArray, identity: SigningIdentity) {
        if (headers["X-Jarvis-Link-Tenant"] != "tenant_sim") error("Wrong signed tenant")
        if (headers["X-Jarvis-Link-Device"] != identity.deviceId) error("Wrong signed device")
        val time = headers.getValue("X-Jarvis-Link-Timestamp").toLong()
        val nonce = headers.getValue("X-Jarvis-Link-Nonce")
        val signature = headers.getValue("X-Jarvis-Link-Signature")
        val valid = LinkCrypto.verify(
            LinkCrypto.decodePublicKey(identity.publicKeyBase64),
            LinkCrypto.requestSigningBytes("tenant_sim", identity.deviceId, time, nonce, method, path, body),
            signature,
        )
        check(valid) { "Companion request signature did not verify" }
    }
}
