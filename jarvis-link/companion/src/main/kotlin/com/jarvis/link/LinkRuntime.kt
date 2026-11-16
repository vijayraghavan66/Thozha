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

    fun poll(relayUrl: String, limit: Int = 20): PollResult = companion(relayUrl).pollInbox(limit)

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
