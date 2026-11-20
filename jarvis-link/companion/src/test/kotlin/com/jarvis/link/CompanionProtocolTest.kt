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
        typedFastCommandEnvelopeIsAllowed(companion, hostCertificate, hostRoot, now)
        trustedAppAndBatteryFastCommandsAreNarrowlyTyped()
        fastCommandExpiryCoversObservedTelegramRetryWindow(now)
        endToEndEnvelopeRoundTrip(root, host, hostEnc, hostCertificate, companion, companionEnc, companionCertificate, hostRoot, now)
        offlineOutboxAndRetry(root, host, hostEnc, hostCertificate, companion, companionEnc, companionCertificate, hostRoot, now)
        staleTimestampRetryKeepsAuthenticatedEnvelopeQueued(hostCertificate, companion, companionEnc, companionCertificate, hostRoot, now)
        crashAfterRelayAcceptancePreservesExactEnvelope(hostCertificate, companion, companionEnc, companionCertificate, hostRoot, now)
        inboxPersistenceBeforeAck(host, hostCertificate, companion, companionEnc, companionCertificate, hostRoot, now)
        approvalReviewIsDigestAndExpiryBound()
        approvalDecisionRequiresAuthoritativeCorrelatedResult()
        developmentPlanApprovalRequiresCompleteDigestBoundDetails()
        localTaskHistoryIsRestartSafe()
        progressSequenceDeduplicatesOutOfOrderEvents()
        staleProgressCannotReopenTerminalTask()
        concurrentLocalStateAndOutbox(hostCertificate, hostRoot, companion, now)
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

    private fun typedFastCommandEnvelopeIsAllowed(
        companion: SigningIdentity, hostCertificate: DeviceCertificate, hostRoot: TrustedHostRoot, now: Long,
    ) {
        val requestId = "12".repeat(16)
        val envelope = LinkCrypto.createEnvelope(
            "tenant_sim", companion, hostCertificate, hostRoot, "fast.command",
            FastCommandRequest.MacStatus.toJson(), nowUnixMs = now, ttlMs = 30_000, messageId = requestId,
        )
        check(envelope.header.kind == "fast.command" && envelope.header.messageId == requestId) {
            "typed fast command must retain its request correlation ID"
        }
    }

    private fun trustedAppAndBatteryFastCommandsAreNarrowlyTyped() {
        check(LinkJson.parseObject(FastCommandRequest.MacBatteryStatus.toJson()) == mapOf(
            "command" to "mac.battery", "version" to 1L,
        )) { "battery status should have a fixed versioned wire shape" }
        val calculator = LinkJson.parseObject(FastCommandRequest.OpenApplication("com.apple.calculator", "ab".repeat(16)).toJson())
        check(calculator["app"] == "com.apple.calculator" && calculator["command"] == "app.open")
        val spotify = LinkJson.parseObject(FastCommandRequest.OpenApplication("com.spotify.client", "cd".repeat(16)).toJson())
        check(spotify["app"] == "com.spotify.client" && spotify["command"] == "app.open")
        check(runCatching { FastCommandRequest.OpenApplication("/System/Applications/Calculator.app", "ef".repeat(16)) }.isFailure) {
            "arbitrary executable paths must never be serialized as app targets"
        }
        check(runCatching { FastCommandRequest.OpenApplication("com.apple.calculator --args", "01".repeat(16)) }.isFailure) {
            "app arguments must never be accepted in the typed app target"
        }
    }

    private fun fastCommandExpiryCoversObservedTelegramRetryWindow(now: Long) {
        val root = LinkCrypto.newSigningIdentity()
        val host = LinkCrypto.newSigningIdentity()
        val hostEncryption = LinkCrypto.newEncryptionIdentity(host)
        val phone = LinkCrypto.newSigningIdentity()
        val phoneEncryption = LinkCrypto.newEncryptionIdentity(phone)
        val hostCertificate = certificate(root, host, hostEncryption, "host", now)
        val phoneCertificate = certificate(root, phone, phoneEncryption, "companion", now)
        val relay = MemoryRelay(phone)
        val companion = JarvisLinkCompanion(
            "tenant_sim", phone, phoneEncryption, phoneCertificate,
            TrustedHostRoot(root.deviceId, root.publicKeyBase64), hostCertificate, relay,
            MemoryMessageOutbox(), MemoryMessageInbox(), now = { now },
        ).also { it.setOnline(true) }
        val requestId = "79".repeat(16)
        companion.submitFastCommand(FastCommandRequest.MacBatteryStatus, clientRequestId = requestId)
        val envelope = LinkEnvelope.parse(relay.queued.single())
        check(envelope.header.expiresAtUnixMs - envelope.header.issuedAtUnixMs == FAST_COMMAND_DEFAULT_TTL_MS) {
            "typed requests must outlive the observed 134-second Telegram 429 backoff"
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

    private fun staleTimestampRetryKeepsAuthenticatedEnvelopeQueued(
        hostCertificate: DeviceCertificate,
        companion: SigningIdentity,
        companionEnc: EncryptionIdentity,
        companionCertificate: DeviceCertificate,
        hostRoot: TrustedHostRoot,
        initialNow: Long,
    ) {
        var now = initialNow
        val relay = MemoryRelay(companion).apply { staleTimestampOnce = true }
        val outbox = MemoryMessageOutbox()
        val client = JarvisLinkCompanion(
            tenantId = "tenant_sim",
            signingIdentity = companion,
            encryptionIdentity = companionEnc,
            companionCertificate = companionCertificate,
            hostRoot = hostRoot,
            hostCertificate = hostCertificate,
            relay = relay,
            outbox = outbox,
            inbox = MemoryMessageInbox(),
            now = { now },
        )
        client.setOnline(true)
        val requestId = "cd".repeat(16)
        val submitted = client.submitTask(TaskSubmission("safe read-only question", setOf(SafeCapability.READ_ONLY), requestId))
        check(submitted.queuedOffline && outbox.list().size == 1) { "timestamp skew must retain the encrypted request for retry" }
        check(client.connectionState == LinkConnectionState.OFFLINE && client.lastErrorCode == "REQUEST_STALE_RETRY") {
            "a stale signed timestamp must not require device re-pairing"
        }
        val persistedEnvelope = outbox.list().single()
        now += 70_000
        client.setOnline(true)
        val receipts = client.flushOutbox()
        check(receipts.size == 1 && outbox.list().isEmpty()) { "freshly signed retry should deliver the persisted envelope" }
        check(relay.requestTimestamps.size == 2 && relay.requestTimestamps[1] - relay.requestTimestamps[0] == 70_000L) {
            "retry must use a fresh request timestamp"
        }
        check(relay.queued.single() == persistedEnvelope) { "retry must preserve the exact encrypted message bytes" }
    }

    private fun inboxPersistenceBeforeAck(
        host: SigningIdentity, hostCert: DeviceCertificate, phone: SigningIdentity, phoneEnc: EncryptionIdentity,
        phoneCert: DeviceCertificate, hostRoot: TrustedHostRoot, now: Long,
    ) {
        val directory = java.nio.file.Files.createTempDirectory("inbox-atomic").toFile()
        val base = FileLinkStateStore(File(directory, "state.json"))
        var failWrite = false
        val state = object : LinkStateStore {
            override fun read() = base.read()
            override fun write(json: String) { if (failWrite) throw java.io.IOException("disk full") else base.write(json) }
        }
        fun runtime() = LinkRuntime(SoftwareTestKeyProvider(File(directory, "keys.json")), state, directory, now = { now })
        val first = runtime()
        val requestId = "ab".repeat(16)
        val taskId = "cd".repeat(16)
        first.recordLocalTaskRequest(requestId, "explain Java", "telegram")
        val payload = LinkJson.encode(mapOf("task_id" to taskId, "state" to "COMPLETED", "summary" to "real answer", "in_reply_to" to requestId))
        val incoming = LinkCrypto.createEnvelope("tenant_sim", host, phoneCert, hostRoot, "task.result", payload, nowUnixMs = now, ttlMs = 30_000)
        val relay = MemoryRelay(phone)
        relay.incoming += InboxItem(incoming, hostCert)
        fun client() = JarvisLinkCompanion("tenant_sim", phone, phoneEnc, phoneCert, hostRoot, hostCert, relay,
            MemoryMessageOutbox(), MemoryMessageInbox(), now = { now }).also { it.setOnline(true) }
        failWrite = true
        check(runCatching { client().pollInbox(persistEvent = first::persistInboxEvent) }.isFailure)
        check(relay.acks.isEmpty()) { "failed persistence must never acknowledge" }
        failWrite = false
        relay.crashBeforeAck = true
        check(client().pollInbox(persistEvent = first::persistInboxEvent).events.size == 1)
        check(relay.acks.isEmpty())
        val recovered = runtime()
        check(recovered.conversation().single()["summary"] == "real answer")
        check(recovered.recentUpdates().size == 1)
        relay.crashBeforeAck = false
        check(client().pollInbox(persistEvent = recovered::persistInboxEvent).events.isEmpty())
        check(recovered.recentUpdates().size == 1) { "redelivery duplicated event" }
        check(relay.acks.size == 1 && relay.acks[0].second == AckStatus.RECEIVED)
        directory.deleteRecursively()
    }

    private fun crashAfterRelayAcceptancePreservesExactEnvelope(
        hostCert: DeviceCertificate, phone: SigningIdentity, phoneEnc: EncryptionIdentity,
        phoneCert: DeviceCertificate, hostRoot: TrustedHostRoot, now: Long,
    ) {
        val directory = java.nio.file.Files.createTempDirectory("outbox-crash").toFile()
        val relay = MemoryRelay(phone)
        val file = File(directory, "outbox.json")
        fun client() = JarvisLinkCompanion("tenant_sim", phone, phoneEnc, phoneCert, hostRoot, hostCert, relay,
            FileMessageOutbox(file), MemoryMessageInbox(), now = { now }).also { it.setOnline(true) }
        val first = client()
        relay.crashAfterPost = true
        val requestId = "ab".repeat(16)
        check(runCatching { first.submitTask(TaskSubmission("recover after crash", setOf(SafeCapability.READ_ONLY), requestId)) }.isFailure)
        check(relay.queued.size == 1)
        val acceptedBytes = relay.queued.single()
        check(FileMessageOutbox(file).list() == listOf(acceptedBytes)) { "accepted ciphertext was not durable before posting" }
        relay.crashAfterPost = false
        check(runCatching { first.submitTask(TaskSubmission("recover after crash", setOf(SafeCapability.READ_ONLY), requestId)) }.isFailure)
        check(FileMessageOutbox(file).list() == listOf(acceptedBytes)) { "same ID replaced durable ciphertext" }
        val restarted = client()
        check(restarted.flushOutbox().size == 1)
        check(relay.queued.size == 2 && relay.queued[1] == acceptedBytes) { "retry changed the signed bytes or ID" }
        check(FileMessageOutbox(file).list().isEmpty())
        directory.deleteRecursively()
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

    private fun developmentPlanApprovalRequiresCompleteDigestBoundDetails() {
        val planFields = linkedMapOf<String, Any>(
            "allowed_ops" to listOf("artifact_manifest", "run_tests", "write_file"),
            "allowed_paths" to listOf("src", "tests"),
            "device_id" to "d".repeat(32),
            "max_duration_s" to 900L,
            "max_file_bytes" to 4096L,
            "max_repairs" to 1L,
            "max_test_runs" to 2L,
            "max_total_bytes" to 8192L,
            "max_writes" to 3L,
            "plan_id" to "plan-approval-0001",
            "ttl_s" to 600L,
            "user_id" to "device:" + "d".repeat(32),
            "workspace_root" to "/tmp/jarvis-project",
        )
        val planDigest = sha256Hex(LinkJson.encode(planFields).toByteArray(UTF_8))
        val plan = DevelopmentPlanReview(
            planId = "plan-approval-0001",
            deviceId = "d".repeat(32),
            userId = "device:" + "d".repeat(32),
            workspaceRoot = "/tmp/jarvis-project",
            allowedOps = listOf("artifact_manifest", "run_tests", "write_file"),
            allowedPaths = listOf("src", "tests"),
            maxFileBytes = 4096L,
            maxTotalBytes = 8192L,
            maxWrites = 3L,
            maxTestRuns = 2L,
            maxRepairs = 1L,
            maxDurationSeconds = 900L,
            ttlSeconds = 600L,
            planDigest = planDigest,
        )
        val target = "taskflow:plan:${plan.planId}"
        val arguments = linkedMapOf<String, Any>("plan" to planFields, "plan_digest" to planDigest)
        val operationPayload = linkedMapOf<String, Any>(
            "arguments" to arguments,
            "operation" to "mcp:jarvis_dev:approve_plan",
            "target" to target,
        )
        val operationDigest = sha256Hex(LinkJson.encode(operationPayload).toByteArray(UTF_8))
        val payload = linkedMapOf<String, Any>(
            "approval_id" to "a".repeat(32),
            "task_id" to "b".repeat(32),
            "operation" to "mcp:jarvis_dev:approve_plan",
            "target" to target,
            "canonical_arguments_digest" to operationDigest,
            "expires_at_unix_ms" to 1_800_000_060_000L,
            "redacted_summary" to "Review the full bounded plan below.",
            "development_plan" to plan.toWire(),
        )
        val parsed = ApprovalReview.parse(FlatJson.encode(payload))
        check(parsed.developmentPlan == plan) { "complete plan details should round-trip through the approval event" }
        check(parsed.canonicalArgumentsDigest == operationDigest)
        check((CompanionEvent.ApprovalPending(parsed).toMap()["development_plan"] as? Map<*, *>)?.get("plan_digest") == planDigest)

        val directory = java.nio.file.Files.createTempDirectory("development-plan-approval-history").toFile()
        try {
            val state = FileLinkStateStore(File(directory, "state.json"))
            val first = LinkRuntime(
                keys = SoftwareTestKeyProvider(File(directory, "keys.json")),
                state = state,
                storeDir = directory,
                now = { 1_800_000_000_000L },
            )
            first.recordEvents(listOf(CompanionEvent.ApprovalPending(parsed)))
            val recovered = LinkRuntime(
                keys = SoftwareTestKeyProvider(File(directory, "keys.json")),
                state = state,
                storeDir = directory,
                now = { 1_800_000_000_000L },
            )
            check(recovered.pendingApprovals().single().developmentPlan == plan) {
                "complete verified plan details should survive app-state restoration"
            }
        } finally {
            directory.deleteRecursively()
        }

        val missingPlan = payload.toMutableMap().apply { remove("development_plan") }
        check(runCatching { ApprovalReview.parse(FlatJson.encode(missingPlan)) }.isFailure) {
            "development plan approvals must fail closed when structured details are missing"
        }
        val tamperedPlan = plan.toWire().toMutableMap().apply { put("max_writes", 999L) }
        val tampered = payload.toMutableMap().apply { put("development_plan", tamperedPlan) }
        check(runCatching { ApprovalReview.parse(FlatJson.encode(tampered)) }.isFailure) {
            "tampered plan details must not appear as an approvable card"
        }
        val badOperationDigest = payload.toMutableMap().apply { put("canonical_arguments_digest", "c".repeat(64)) }
        check(runCatching { ApprovalReview.parse(FlatJson.encode(badOperationDigest)) }.isFailure) {
            "displayed plan must match the supervisor's exact operation digest"
        }
        val mutablePaths = parsed.developmentPlan!!.allowedPaths as MutableList<String>
        mutablePaths.add("outside-approved-scope")
        check(runCatching { parsed.validateForDecision(operationDigest, 1_000) }.isFailure) {
            "mutated plan details must fail closed at the approval action"
        }
    }

    private fun sha256Hex(bytes: ByteArray): String = java.security.MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it) }

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

    private fun approvalDecisionRequiresAuthoritativeCorrelatedResult() {
        var now = 1_800_000_000_000L
        val directory = java.nio.file.Files.createTempDirectory("approval-decision-state").toFile()
        try {
            val root = LinkCrypto.newSigningIdentity()
            val host = LinkCrypto.newSigningIdentity()
            val phoneKeys = SoftwareTestKeyProvider(File(directory, "keys.json"))
            val (phone, phoneEncryption) = phoneKeys.identities()
            val hostCertificate = certificate(root, host, LinkCrypto.newEncryptionIdentity(host), "host", now)
            val phoneCertificate = certificate(root, phone, phoneEncryption, "companion", now)
            val hostRoot = TrustedHostRoot(root.deviceId, root.publicKeyBase64)
            val state = FileLinkStateStore(File(directory, "state.json"))
            state.write(LinkJson.encode(mapOf(
                "tenant_id" to "tenant_sim",
                "device_id" to phone.deviceId,
                "host_root_device_id" to hostRoot.deviceId,
                "host_root_public_key" to hostRoot.signingPublicKeyBase64Url,
                "companion_certificate" to LinkStateJson.certificateToMap(phoneCertificate),
                "host_certificate" to LinkStateJson.certificateToMap(hostCertificate),
                "enrollment_pending" to false,
            )))
            var offline = true
            var exchanges = 0
            val transport = object : RelayTransport {
                override fun exchange(method: String, pathAndQuery: String, headers: Map<String, String>, body: ByteArray): RelayRawResponse {
                    exchanges++
                    if (offline) throw java.io.IOException("temporary test outage")
                    check(method == "POST" && pathAndQuery == "/v1/messages")
                    val envelope = LinkEnvelope.parse(body.toString(UTF_8))
                    return RelayRawResponse(200, LinkJson.encode(mapOf(
                        "message_id" to envelope.header.messageId,
                        "status" to "queued",
                        "expires_at_unix_ms" to envelope.header.expiresAtUnixMs,
                    )))
                }
            }
            val runtime = LinkRuntime(
                keys = phoneKeys,
                state = state,
                storeDir = directory,
                now = { now },
                transportFactory = { transport },
            )
            val approval = ApprovalReview(
                approvalId = "a".repeat(32),
                taskId = "b".repeat(32),
                operation = "mcp:jarvis_mac:open_app",
                target = "Calculator",
                canonicalArgumentsDigest = "c".repeat(64),
                expiresAtUnixMs = now + 60_000,
                redactedSummary = "Open the allowlisted Calculator app.",
            )
            runtime.recordEvents(listOf(CompanionEvent.ApprovalPending(approval)))
            val queued = runtime.decideApproval("http://127.0.0.1:8787", approval.approvalId, "approve")
            check(queued["state"] == "QUEUED" && queued["queued_offline"] == true)
            check(runtime.pendingApprovals().single().approvalId == approval.approvalId) {
                "transport queueing must not be presented as supervisor approval"
            }
            val duplicateTap = runtime.decideApproval("http://127.0.0.1:8787", approval.approvalId, "approve")
            check(duplicateTap["message_id"] == queued["message_id"] && exchanges == 1) {
                "duplicate tap must not create or send a second decision while one is unresolved"
            }

            offline = false
            runtime.flush("http://127.0.0.1:8787")
            check(runtime.approvalDecisionState(approval.approvalId)?.get("state") == "DELIVERED_WAITING_SUPERVISOR")
            check(runtime.pendingApprovals().single().approvalId == approval.approvalId) {
                "relay acceptance is not an authoritative supervisor decision"
            }
            val decisionMessageId = queued["message_id"] as String
            val result = mapOf(
                "kind" to "approval.decision_result",
                "approval_id" to approval.approvalId,
                "task_id" to approval.taskId,
                "decision" to "approve",
                "status" to "CONSUMED",
                "execution_state" to "COMPLETED",
                "executed" to true,
                "success" to true,
                "continued" to false,
                "simulated" to false,
                "result_summary" to "Calculator opened.",
            )
            val legacyConsumed = result - "execution_state" - "executed" - "success"
            val legacyEvent = decodeCompanionEvent("task.result", LinkJson.encode(mapOf(
                "task_id" to approval.taskId,
                "state" to "CONSUMED",
                "summary" to "Approval consumed.",
                "in_reply_to" to queued["message_id"],
                "result" to legacyConsumed,
            ))) as CompanionEvent.TaskUpdate
            check(legacyEvent.approvalDecisionResult?.executionState == "INDETERMINATE" &&
                legacyEvent.approvalDecisionResult?.executed == false && legacyEvent.approvalDecisionResult?.success == null) {
                "legacy consumed responses without terminal evidence must never be treated as completed"
            }
            val wrongCorrelation = LinkJson.encode(mapOf(
                "task_id" to approval.taskId,
                "state" to "COMPLETED",
                "summary" to "wrong correlation",
                "in_reply_to" to "e".repeat(32),
                "result" to result,
            ))
            runtime.recordEvents(listOf(decodeCompanionEvent("task.result", wrongCorrelation)))
            check(runtime.pendingApprovals().single().approvalId == approval.approvalId) {
                "an unrelated result must not consume the visible approval"
            }

            val correlated = LinkJson.encode(mapOf(
                "task_id" to approval.taskId,
                "state" to "COMPLETED",
                "summary" to "Calculator opened.",
                "in_reply_to" to decisionMessageId,
                "result" to result,
            ))
            val eventId = "f".repeat(32)
            check(runtime.persistInboxEvent(eventId, now + 30_000, decodeCompanionEvent("task.result", correlated)))
            check(runtime.pendingApprovals().isEmpty()) {
                "approval should leave the actionable list only after the correlated host decision result"
            }
            val authoritative = runtime.approvalDecisionState(approval.approvalId)!!
            check(authoritative["authoritative"] == true && authoritative["execution_state"] == "COMPLETED")
            check(!runtime.persistInboxEvent(eventId, now + 30_000, decodeCompanionEvent("task.result", correlated))) {
                "duplicate host result must not be applied twice"
            }
            val recent = runtime.recentUpdates().last()["approval_decision_result"] as Map<*, *>
            check(recent["approval_id"] == approval.approvalId && recent["in_reply_to"] == decisionMessageId)

            val queuedRequestId = "79".repeat(16)
            runtime.recordLocalTaskRequest(queuedRequestId, "What is my Mac battery level?", transport = "telegram")
            offline = true
            val queuedRequest = runtime.submitFastCommand(
                "http://127.0.0.1:8787", FastCommandRequest.MacBatteryStatus, queuedRequestId,
            )
            check(queuedRequest.queuedOffline)
            runtime.recordLocalTaskResult(queuedRequestId, queuedRequestId, "QUEUED", "Queued on phone")
            now += FAST_COMMAND_DEFAULT_TTL_MS + 1
            val debugOutbox = FileMessageOutbox(File(directory, "outbox.json")).list()
                .map { raw -> LinkEnvelope.parse(raw).header.let { it.messageId + ":" + it.expiresAtUnixMs } }
            runtime.syncNow("http://127.0.0.1:8787")
            val expiredEntry = runtime.conversation().first { it["message_id"] == queuedRequestId }
            check(expiredEntry["state"] == "UNCONFIRMED") {
                "an expired local envelope with no Mac receipt must not remain silently queued: now=$now status=${runtime.status()} outbox=$debugOutbox entry=$expiredEntry"
            }
            check((expiredEntry["summary"] as String).contains("Check task status"))
            check(runtime.status().outboxSize == 0)
        } finally {
            directory.deleteRecursively()
        }
    }

    private fun staleProgressCannotReopenTerminalTask() {
        val dir = java.nio.file.Files.createTempDirectory("link-terminal-order").toFile()
        val runtime = LinkRuntime(SoftwareTestKeyProvider(File(dir, "keys.json")), FileLinkStateStore(File(dir, "state.json")), dir, now = { 1_800_000_000_000L })
        val requestId = "ef".repeat(16)
        val taskId = "ab".repeat(16)
        runtime.recordLocalTaskRequest(requestId, "test ordering", "telegram")
        val expiry = 1_800_000_030_000L
        check(runtime.persistInboxEvent("11".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "COMPLETED", "real result", requestId)))
        check(runtime.persistInboxEvent("22".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "running", "old progress", requestId)))
        check(runtime.persistInboxEvent("33".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "PLANNING", "older progress", "")))
        val entry = runtime.conversation().single()
        check(entry["state"] == "COMPLETED" && entry["summary"] == "real result") { "stale progress reopened completed task" }
        check(runtime.recentUpdates().size == 1) { "stale progress appeared in recent updates" }
        check(!runtime.persistInboxEvent("22".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "RUNNING", "old progress", requestId)))
        dir.deleteRecursively()
    }

    private fun progressSequenceDeduplicatesOutOfOrderEvents() {
        val decoded = decodeCompanionEvent(
            "task.progress",
            "{\"task_id\":\"${"ab".repeat(16)}\",\"state\":\"RUNNING\",\"summary\":\"Working\",\"event_seq\":9}",
        ) as CompanionEvent.TaskUpdate
        check(decoded.eventSeq == 9L && decoded.toMap()["event_seq"] == 9L) { "task.progress sequence was not decoded" }

        val dir = java.nio.file.Files.createTempDirectory("link-progress-sequence").toFile()
        val runtime = LinkRuntime(
            SoftwareTestKeyProvider(File(dir, "keys.json")),
            FileLinkStateStore(File(dir, "state.json")), dir,
            now = { 1_800_000_000_000L },
        )
        val requestId = "ef".repeat(16)
        val taskId = "ab".repeat(16)
        runtime.recordLocalTaskRequest(requestId, "slow task", "telegram")
        runtime.recordLocalTaskResult(requestId, taskId, "QUEUED", "Task accepted")
        val expiry = 1_800_000_030_000L
        check(runtime.persistInboxEvent("11".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "RUNNING", "newer", eventSeq = 9)))
        check(runtime.persistInboxEvent("22".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "PLANNING", "out of order", eventSeq = 8)))
        check(runtime.persistInboxEvent("33".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "RUNNING", "duplicate seq", eventSeq = 9)))
        val entry = runtime.conversation().single()
        check(entry["state"] == "RUNNING" && entry["summary"] == "newer" && entry["progress_seq"] == 9L) {
            "out-of-order task.progress replaced newer state"
        }
        check(runtime.recentUpdates().size == 1) { "duplicate/out-of-order progress entered activity history" }
        check(runtime.persistInboxEvent("44".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "COMPLETED", "done", body = "verified answer")))
        check(runtime.persistInboxEvent("55".repeat(16), expiry, CompanionEvent.TaskUpdate(taskId, "RUNNING", "late", eventSeq = 10)))
        check(runtime.conversation().single()["state"] == "COMPLETED") { "late progress reopened terminal task" }
        dir.deleteRecursively()
    }

    private fun concurrentLocalStateAndOutbox(hostCert: DeviceCertificate, hostRoot: TrustedHostRoot, phone: SigningIdentity, now: Long) {
        val directory = java.nio.file.Files.createTempDirectory("link-concurrent").toFile()
        val stateFile = File(directory, "state.json")
        fun runtime() = LinkRuntime(SoftwareTestKeyProvider(File(directory, "keys.json")), FileLinkStateStore(stateFile), directory, now = { now })
        val first = runtime()
        val second = runtime()
        val threads = (0 until 30).map { i -> Thread {
            val id = i.toString(16).padStart(32, '0')
            (if (i % 2 == 0) first else second).recordLocalTaskRequest(id, "request $i", "telegram")
        }.apply { start() } }
        threads.forEach { it.join() }
        check(first.conversation().size == 30) { "concurrent state updates lost requests" }
        val id = "0".repeat(32)
        first.recordLocalTaskResult(id, "a".repeat(32), "COMPLETED", "finished")
        second.recordLocalTaskResult(id, "", "SENT", "")
        check(first.conversation().first { it["message_id"] == id }["state"] == "COMPLETED")
        val outboxFile = File(directory, "outbox.json")
        val boxes = listOf(FileMessageOutbox(outboxFile), FileMessageOutbox(outboxFile))
        val envelopes = (0 until 12).map { i -> LinkCrypto.createEnvelope("tenant_sim", phone, hostCert, hostRoot, "task.submit", "{}", nowUnixMs = now, messageId = (i + 100).toString(16).padStart(32, '0')).toJson() }
        val workers = envelopes.mapIndexed { i, raw -> Thread { boxes[i % 2].enqueue(raw) }.apply { start() } }
        workers.forEach { it.join() }
        check(FileMessageOutbox(outboxFile).list().size == envelopes.size) { "concurrent outbox writers lost messages" }
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
    var crashAfterPost = false
    var crashBeforeAck = false
    var staleTimestampOnce = false
    val requestTimestamps = mutableListOf<Long>()
    val incoming = mutableListOf<InboxItem>()
    val acks = mutableListOf<Pair<String, AckStatus>>()

    override fun postMessage(envelopeJson: String, headers: Map<String, String>): RelayReceipt {
        requestTimestamps += headers.getValue("X-Jarvis-Link-Timestamp").toLong()
        verifyRequest(headers, "POST", "/v1/messages", envelopeJson.toByteArray(UTF_8), companion)
        if (staleTimestampOnce) {
            staleTimestampOnce = false
            throw RelayHttpException(401, "TIMESTAMP_SKEW")
        }
        val envelope = LinkEnvelope.parse(envelopeJson)
        queued += envelopeJson
        if (crashAfterPost) throw AssertionError("simulated process death after relay acceptance")
        return RelayReceipt(envelope.header.messageId, "queued", envelope.header.expiresAtUnixMs)
    }

    override fun inbox(pathAndQuery: String, headers: Map<String, String>): InboxPage {
        verifyRequest(headers, "GET", pathAndQuery, ByteArray(0), companion)
        return InboxPage(incoming.toList(), null)
    }

    override fun acknowledge(messageId: String, bodyJson: String, headers: Map<String, String>) {
        if (crashBeforeAck) throw AssertionError("simulated crash before ack")
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
