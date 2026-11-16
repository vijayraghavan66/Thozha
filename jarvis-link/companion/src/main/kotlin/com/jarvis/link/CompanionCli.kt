package com.jarvis.link

import java.io.File
import java.security.KeyFactory
import java.security.KeyPair
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import kotlin.system.exitProcess

/**
 * Command-line driver for the REAL Kotlin transport (JarvisLinkCompanion + HttpJarvisLinkRelay).
 * Used by the Python integration tests to exercise the Kotlin code against a live relay.
 *
 * TEST ONLY: private keys are kept in a plain state file in a temporary directory. Production Android
 * uses Android Keystore-backed KeyPairs through the same LinkCrypto/JarvisLinkCompanion types.
 *
 * Output is one JSON object on stdout. Errors are one JSON object on stderr with a stable code and
 * never contain key material, signatures, or plaintext.
 */
object CompanionCli {
    @JvmStatic
    fun main(args: Array<String>) {
        val command = args.firstOrNull() ?: usage()
        val options = parseOptions(args.drop(1))
        try {
            val result = when (command) {
                "enroll" -> enroll(options)
                "sync" -> sync(options)
                "submit" -> submit(options)
                "poll" -> poll(options)
                "flush" -> flush(options)
                "status" -> status(options)
                else -> usage()
            }
            println(LinkJson.encode(result))
        } catch (failure: Exception) {
            val code = when (failure) {
                is RelayHttpException -> failure.code
                is SecurityException -> "AUTH_FAILED"
                is java.io.IOException -> "NETWORK_UNAVAILABLE"
                else -> "PROTOCOL_ERROR"
            }
            System.err.println(LinkJson.encode(mapOf("error" to code, "exception" to failure.javaClass.simpleName)))
            exitProcess(if (code == "NETWORK_UNAVAILABLE") 3 else 2)
        }
    }

    private fun usage(): Nothing {
        System.err.println("usage: CompanionCli enroll|sync|submit|poll|flush|status --state FILE [--relay URL] ...")
        exitProcess(64)
    }

    private fun parseOptions(args: List<String>): Map<String, String> {
        val out = linkedMapOf<String, String>()
        var i = 0
        while (i < args.size) {
            val key = args[i]
            require(key.startsWith("--") && i + 1 < args.size) { "options are --name value pairs" }
            out[key.removePrefix("--")] = args[i + 1]
            i += 2
        }
        return out
    }

    private fun need(options: Map<String, String>, key: String): String =
        options[key] ?: throw IllegalArgumentException("missing --$key")

    // ---- commands ------------------------------------------------------------------------------------------

    private fun enroll(options: Map<String, String>): Map<String, Any> {
        val stateFile = File(need(options, "state"))
        val state = if (stateFile.isFile) CliState.load(stateFile) else CliState.create(stateFile, need(options, "tenant"))
        require(state.tenantId == need(options, "tenant")) { "state belongs to another tenant" }
        if (options["root-public-key"] != null) {
            state.hostRoot = TrustedHostRoot(need(options, "root-device-id"), need(options, "root-public-key"))
        }
        val relay = relay(options, state.tenantId)
        val invitation = RelayInvitation(
            tenantId = state.tenantId,
            invitationId = need(options, "invitation-id"),
            invitationSecretBase64Url = need(options, "invitation-secret"),
            expiresAtUnixMs = need(options, "invitation-expires-ms").toLong(),
        )
        val enrollment = CompanionEnrollment(state.signing, state.encryption, state.tenantId, invitation, need(options, "name"))
        val challengeMap = relay.enrollmentChallenge(enrollment.challengeRequest().toJson())
        val challenge = EnrollmentChallenge(
            tenantId = state.tenantId,
            invitationId = invitation.invitationId,
            challengeId = challengeMap.string("challenge_id"),
            challengeBase64Url = challengeMap.string("challenge_b64url"),
            deviceId = challengeMap.string("device_id"),
            expiresAtUnixMs = challengeMap.long("expires_at_unix_ms"),
        )
        val completion = relay.enrollmentComplete(enrollment.signChallenge(challenge).toJson())
        state.pending = true
        state.save()
        return mapOf("device_id" to completion.string("device_id"), "status" to completion.string("status"))
    }

    /** Fetches and verifies this device's and the host's certificates against the pinned host root. */
    private fun sync(options: Map<String, String>): Map<String, Any> {
        val state = CliState.load(File(need(options, "state")))
        val root = state.hostRoot ?: error("no pinned host root in state")
        val relay = relay(options, state.tenantId)
        val own = relay.deviceCertificate(state.signing.deviceId, signed(state, relay, "GET", "/v1/devices/${state.signing.deviceId}/certificate"))
        check(own.verify(root, state.tenantId, System.currentTimeMillis())) { "own certificate rejected by pinned root" }
        check(own.deviceRole == "companion" && own.deviceId == state.signing.deviceId) { "certificate role/identity mismatch" }
        check(own.signingPublicKeyBase64Url == LinkCrypto.b64url(state.signing.keyPair.public.encoded)) { "certificate key mismatch" }
        val host = relay.deviceCertificate(root.deviceId, signed(state, relay, "GET", "/v1/devices/${root.deviceId}/certificate"))
        check(host.verify(root, state.tenantId, System.currentTimeMillis())) { "host certificate rejected by pinned root" }
        check(host.deviceRole == "host") { "pinned root certificate is not a host" }
        state.companionCertificate = own
        state.hostCertificate = host
        state.pending = false
        state.save()
        return mapOf("companion_device_id" to own.deviceId, "host_device_id" to host.deviceId, "verified" to true)
    }

    private fun submit(options: Map<String, String>): Map<String, Any> {
        val companion = companion(options)
        val scope = need(options, "scope").split(",").map { wire ->
            SafeCapability.entries.firstOrNull { it.wireName == wire } ?: throw IllegalArgumentException("unknown capability")
        }.toSet()
        val submission = TaskSubmission(need(options, "objective"), scope)
        val ttl = options["ttl-ms"]?.toLong()
        val result = companion.submitTask(submission, ttl)
        return mapOf(
            "message_id" to result.messageId,
            "queued_offline" to result.queuedOffline,
            "relay_status" to (result.receipt?.status ?: "queued_locally"),
        )
    }

    private fun poll(options: Map<String, String>): Map<String, Any> {
        val companion = companion(options)
        val page = companion.pollInbox(options["limit"]?.toInt() ?: 20)
        return mapOf("events" to page.events.map { it.toMap() }, "cursor" to (page.cursor ?: ""))
    }

    private fun flush(options: Map<String, String>): Map<String, Any> {
        val companion = companion(options)
        val receipts = companion.flushOutbox()
        return mapOf(
            "delivered" to receipts.size,
            "remaining" to companion.outboxSize(),
            "connection" to companion.connectionState.name,
            "last_error" to (companion.lastErrorCode ?: ""),
        )
    }

    private fun status(options: Map<String, String>): Map<String, Any> {
        val state = CliState.load(File(need(options, "state")))
        return mapOf(
            "device_id" to state.signing.deviceId,
            "tenant_id" to state.tenantId,
            "pending" to state.pending,
            "has_companion_certificate" to (state.companionCertificate != null),
            "has_host_certificate" to (state.hostCertificate != null),
        )
    }

    // ---- helpers -----------------------------------------------------------------------------------------

    private fun relay(options: Map<String, String>, tenant: String) = HttpJarvisLinkRelay(need(options, "relay"), tenant)

    private fun signed(state: CliState, relay: HttpJarvisLinkRelay, method: String, path: String): Map<String, String> {
        val proof = LinkCrypto.signRequest(state.tenantId, state.signing, System.currentTimeMillis(), LinkCrypto.newNonce(), method, path, ByteArray(0))
        return mapOf(
            "X-Jarvis-Link-Device" to proof.deviceId,
            "X-Jarvis-Link-Timestamp" to proof.timestampUnixMs.toString(),
            "X-Jarvis-Link-Nonce" to proof.nonceBase64Url,
            "X-Jarvis-Link-Signature" to proof.signatureBase64Url,
        )
    }

    private fun companion(options: Map<String, String>): JarvisLinkCompanion {
        val stateFile = File(need(options, "state"))
        val state = CliState.load(stateFile)
        val companionCertificate = state.companionCertificate ?: error("run sync before messaging")
        val hostCertificate = state.hostCertificate ?: error("run sync before messaging")
        val root = state.hostRoot ?: error("no pinned host root")
        val relay = relay(options, state.tenantId)
        val dir = stateFile.parentFile
        return JarvisLinkCompanion(
            tenantId = state.tenantId,
            signingIdentity = state.signing,
            encryptionIdentity = state.encryption,
            companionCertificate = companionCertificate,
            hostRoot = root,
            hostCertificate = hostCertificate,
            relay = relay,
            outbox = FileMessageOutbox(File(dir, "outbox.json")),
            inbox = FileMessageInbox(File(dir, "inbox.json")),
        ).also { it.setOnline(true) }
    }
}

/** Test-state file. Holds private keys; lives only in a temporary test directory. */
internal class CliState(
    private val file: File,
    val tenantId: String,
    val signing: SigningIdentity,
    val encryption: EncryptionIdentity,
    var hostRoot: TrustedHostRoot?,
    var companionCertificate: DeviceCertificate?,
    var hostCertificate: DeviceCertificate?,
    var pending: Boolean,
) {
    fun save() {
        val fields = linkedMapOf<String, Any?>(
            "tenant_id" to tenantId,
            "signing_private_pkcs8" to LinkCrypto.b64url(signing.keyPair.private.encoded),
            "signing_public_spki" to LinkCrypto.b64url(signing.keyPair.public.encoded),
            "encryption_private_pkcs8" to LinkCrypto.b64url(encryption.keyPair.private.encoded),
            "encryption_public_spki" to LinkCrypto.b64url(encryption.keyPair.public.encoded),
            "host_root_device_id" to hostRoot?.deviceId,
            "host_root_public_key" to hostRoot?.signingPublicKeyBase64Url,
            "companion_certificate" to companionCertificate?.let(::certificateMap),
            "host_certificate" to hostCertificate?.let(::certificateMap),
            "pending" to pending,
        )
        writeAtomically(file, LinkJson.encode(fields))
        file.setReadable(false, false); file.setReadable(true, true)
    }

    companion object {
        fun create(file: File, tenantId: String): CliState {
            val signing = LinkCrypto.newSigningIdentity()
            val encryption = LinkCrypto.newEncryptionIdentity(signing)
            return CliState(file, tenantId, signing, encryption, null, null, null, false).also { it.save() }
        }

        fun load(file: File): CliState {
            val f = LinkJson.parseObject(file.readText(Charsets.UTF_8))
            val keyFactory = KeyFactory.getInstance("EC")
            fun pub(value: String) = keyFactory.generatePublic(X509EncodedKeySpec(LinkCrypto.decodeBase64Url(value)))
            fun priv(value: String) = keyFactory.generatePrivate(PKCS8EncodedKeySpec(LinkCrypto.decodeBase64Url(value)))
            val signingPair = KeyPair(pub(f.string("signing_public_spki")), priv(f.string("signing_private_pkcs8")))
            val encryptionPair = KeyPair(pub(f.string("encryption_public_spki")), priv(f.string("encryption_private_pkcs8")))
            val signing = LinkCrypto.newSigningIdentity(LinkCrypto.deviceIdForSigningPublicKey(signingPair.public.encoded), signingPair)
            val encryption = EncryptionIdentity(signing.deviceId, encryptionPair)
            val root = f.optionalString("host_root_public_key")?.let { TrustedHostRoot(f.string("host_root_device_id"), it) }
            return CliState(
                file, f.string("tenant_id"), signing, encryption, root,
                (f["companion_certificate"] as? Map<String, Any>)?.let(::certificateFrom),
                (f["host_certificate"] as? Map<String, Any>)?.let(::certificateFrom),
                f["pending"] == true,
            )
        }

        private fun certificateMap(c: DeviceCertificate): Map<String, Any> = linkedMapOf(
            "v" to c.version.toLong(), "tenant_id" to c.tenantId, "device_id" to c.deviceId, "device_role" to c.deviceRole,
            "signing_public_key_b64url" to c.signingPublicKeyBase64Url, "encryption_public_key_b64url" to c.encryptionPublicKeyBase64Url,
            "not_before_unix_ms" to c.notBeforeUnixMs, "expires_at_unix_ms" to c.expiresAtUnixMs,
            "certificate_id" to c.certificateId, "issuer_device_id" to c.issuerDeviceId, "signature_b64url" to c.signatureBase64Url,
        )

        private fun certificateFrom(f: Map<String, Any>) = DeviceCertificate(
            version = f["v"].toString(), tenantId = f.string("tenant_id"), deviceId = f.string("device_id"),
            deviceRole = f.string("device_role"), signingPublicKeyBase64Url = f.string("signing_public_key_b64url"),
            encryptionPublicKeyBase64Url = f.string("encryption_public_key_b64url"),
            notBeforeUnixMs = f.long("not_before_unix_ms"), expiresAtUnixMs = f.long("expires_at_unix_ms"),
            certificateId = f.string("certificate_id"), issuerDeviceId = f.string("issuer_device_id"),
            signatureBase64Url = f.string("signature_b64url"),
        )
    }
}

