package com.jarvis.link

import java.io.ByteArrayOutputStream
import java.nio.charset.StandardCharsets.UTF_8
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.PublicKey
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** P-256 signing identity. Android production storage must be backed by Android Keystore. */
data class SigningIdentity(val deviceId: String, val keyPair: KeyPair) {
    val publicKeyBase64: String get() = LinkCrypto.b64(keyPair.public.encoded)
}

/** P-256 ECDH key. Android production storage must be backed by Android Keystore. */
data class EncryptionIdentity(val deviceId: String, val keyPair: KeyPair) {
    val publicKeyBase64: String get() = LinkCrypto.b64(keyPair.public.encoded)
}

data class EnvelopeHeader(
    val tenantId: String,
    val messageId: String,
    val fromDeviceId: String,
    val toDeviceId: String,
    val toKeyId: String,
    val kind: String,
    val issuedAtUnixMs: Long,
    val expiresAtUnixMs: Long,
    val ephemeralPublicKeyBase64Url: String,
    val ivBase64Url: String,
)

data class LinkEnvelope(
    val header: EnvelopeHeader,
    val ciphertextBase64Url: String,
    val senderSignatureBase64Url: String,
) {
    fun toJson(): String = FlatJson.encode(
        linkedMapOf(
            "v" to 1,
            "tenant_id" to header.tenantId,
            "message_id" to header.messageId,
            "from_device_id" to header.fromDeviceId,
            "to_device_id" to header.toDeviceId,
            "to_key_id" to header.toKeyId,
            "kind" to header.kind,
            "issued_at_unix_ms" to header.issuedAtUnixMs,
            "expires_at_unix_ms" to header.expiresAtUnixMs,
            "ephemeral_pubkey_b64url" to header.ephemeralPublicKeyBase64Url,
            "iv_b64url" to header.ivBase64Url,
            "ciphertext_b64url" to ciphertextBase64Url,
            "sender_signature_b64url" to senderSignatureBase64Url,
        ),
    )

    companion object {
        fun parse(json: String): LinkEnvelope {
            val fields = FlatJson.decode(json)
            require(fields.keys == FIELD_NAMES) { "Envelope has unexpected or missing fields" }
            require(fields.long("v") == 1L) { "Unsupported Jarvis Link version" }
            val header = EnvelopeHeader(
                tenantId = fields.string("tenant_id"),
                messageId = fields.string("message_id"),
                fromDeviceId = fields.string("from_device_id"),
                toDeviceId = fields.string("to_device_id"),
                toKeyId = fields.string("to_key_id"),
                kind = fields.string("kind"),
                issuedAtUnixMs = fields.long("issued_at_unix_ms"),
                expiresAtUnixMs = fields.long("expires_at_unix_ms"),
                ephemeralPublicKeyBase64Url = fields.string("ephemeral_pubkey_b64url"),
                ivBase64Url = fields.string("iv_b64url"),
            )
            return LinkEnvelope(header, fields.string("ciphertext_b64url"), fields.string("sender_signature_b64url"))
        }

        private val FIELD_NAMES = setOf(
            "v", "tenant_id", "message_id", "from_device_id", "to_device_id", "kind",
            "to_key_id", "issued_at_unix_ms", "expires_at_unix_ms", "ephemeral_pubkey_b64url", "iv_b64url",
            "ciphertext_b64url", "sender_signature_b64url",
        )
    }
}

data class SignedRequestProof(
    val tenantId: String,
    val deviceId: String,
    val timestampUnixMs: Long,
    val nonceBase64Url: String,
    val signatureBase64Url: String,
)

object LinkCrypto {
    private const val GCM_TAG_BITS = 128
    private const val GCM_IV_BYTES = 12
    private const val MESSAGE_ID_BYTES = 16
    private val random = SecureRandom()
    private val deviceIdRegex = Regex("^[0-9a-f]{32}$")
    private val kindSet = setOf("task.submit", "task.progress", "task.result", "approval.request", "approval.decision", "task.cancel")

    fun newSigningIdentity(): SigningIdentity {
        val pair = newP256KeyPair()
        return SigningIdentity(deviceIdForSigningPublicKey(pair.public.encoded), pair)
    }

    fun newSigningIdentity(deviceId: String, keyPair: KeyPair): SigningIdentity {
        require(deviceId == deviceIdForSigningPublicKey(keyPair.public.encoded)) { "device_id must derive from signing SPKI" }
        return SigningIdentity(deviceId, keyPair)
    }

    fun newEncryptionIdentity(signingIdentity: SigningIdentity): EncryptionIdentity =
        EncryptionIdentity(signingIdentity.deviceId, newP256KeyPair())
    fun newId(): String = UUID.randomUUID().toString().replace("-", "").lowercase()

    fun newNonce(): String = b64url(ByteArray(32).also(random::nextBytes))

    fun signRequest(
        tenantId: String,
        identity: SigningIdentity,
        timestampUnixMs: Long,
        nonceBase64Url: String,
        method: String,
        pathAndQuery: String,
        body: ByteArray,
    ): SignedRequestProof {
        validateDeviceId(identity.deviceId)
        require(identity.deviceId == deviceIdForSigningPublicKey(identity.keyPair.public.encoded)) {
            "device_id must derive from signing SPKI"
        }
        require(tenantId.matches(Regex("^[A-Za-z0-9._-]{1,128}$")))
        require(method == method.uppercase()) { "HTTP method must be uppercase" }
        require(pathAndQuery.startsWith("/") && '\n' !in pathAndQuery && '\r' !in pathAndQuery)
        val bodyDigest = hex(sha256(body))
        val message = listOf(
            "JARVIS-LINK-REQUEST-V1", tenantId, identity.deviceId, timestampUnixMs.toString(),
            nonceBase64Url, method, pathAndQuery, bodyDigest,
        ).joinToString("\n").toByteArray(UTF_8)
        return SignedRequestProof(tenantId, identity.deviceId, timestampUnixMs, nonceBase64Url, sign(identity.keyPair.private, message))
    }

    fun createEnvelope(
        tenantId: String,
        fromDevice: SigningIdentity,
        recipientCertificate: DeviceCertificate,
        pinnedHostRoot: TrustedHostRoot,
        kind: String,
        payloadUtf8Json: String,
        nowUnixMs: Long = System.currentTimeMillis(),
        ttlMs: Long = 15 * 60 * 1000L,
    ): LinkEnvelope {
        validateDeviceId(fromDevice.deviceId)
        validateDeviceId(recipientCertificate.deviceId)
        require(recipientCertificate.verify(pinnedHostRoot, tenantId, nowUnixMs)) { "Recipient device certificate is not trusted" }
        require(tenantId.matches(Regex("^[A-Za-z0-9._-]{1,128}$"))) { "Invalid tenant ID" }
        require(kind in kindSet) { "Unsupported envelope kind" }
        val hostToCompanion = kind in setOf("task.progress", "task.result", "approval.request")
        val requiredRecipientRole = if (hostToCompanion) "companion" else "host"
        require(recipientCertificate.deviceRole == requiredRecipientRole) { "Message kind is not valid for recipient role" }
        require(ttlMs in 1..(24 * 60 * 60 * 1000L)) { "Message TTL must be between 1 ms and 24 hours" }
        require(payloadUtf8Json.isNotBlank() && payloadUtf8Json.toByteArray(UTF_8).size <= 64 * 1024) { "Payload is empty or too large" }

        val messageId = newId()
        val messageIdBytes = decodeHex(messageId)
        val recipientKeyBase64 = recipientCertificate.encryptionPublicKeyBase64Url
        val recipientKey = decodePublicKey(recipientKeyBase64)
        val recipientKeyId = recipientCertificate.certificateId
        val ephemeral = newP256KeyPair()
        val shared = ecdh(ephemeral.private, recipientKey)
        val info = "JarvisLink/v1\n$tenantId\n${fromDevice.deviceId}\n${recipientCertificate.deviceId}".toByteArray(UTF_8)
        val aesKey = hkdfSha256(shared, messageIdBytes, info, 32)
        val iv = ByteArray(GCM_IV_BYTES).also(random::nextBytes)
        val header = EnvelopeHeader(
            tenantId = tenantId,
            messageId = messageId,
            fromDeviceId = fromDevice.deviceId,
            toDeviceId = recipientCertificate.deviceId,
            toKeyId = recipientKeyId,
            kind = kind,
            issuedAtUnixMs = nowUnixMs,
            expiresAtUnixMs = nowUnixMs + ttlMs,
            ephemeralPublicKeyBase64Url = b64url(ephemeral.public.encoded),
            ivBase64Url = b64url(iv),
        )
        validateHeader(header, nowUnixMs, allowExpired = false)
        val aad = aadBytes(header)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(aesKey, "AES"), GCMParameterSpec(GCM_TAG_BITS, iv))
        cipher.updateAAD(aad)
        val ciphertext = cipher.doFinal(payloadUtf8Json.toByteArray(UTF_8))
        val ciphertextB64 = b64url(ciphertext)
        val signatureBytes = envelopeSigningBytes(header, hex(sha256(ciphertext)))
        val signature = sign(fromDevice.keyPair.private, signatureBytes)
        return LinkEnvelope(header, ciphertextB64, signature)
    }

    fun decryptEnvelope(
        envelope: LinkEnvelope,
        recipient: EncryptionIdentity,
        recipientCertificate: DeviceCertificate,
        senderCertificate: DeviceCertificate,
        pinnedHostRoot: TrustedHostRoot,
        nowUnixMs: Long = System.currentTimeMillis(),
    ): String {
        validateHeader(envelope.header, nowUnixMs, allowExpired = false)
        require(envelope.header.toDeviceId == recipient.deviceId) { "Envelope is addressed to another device" }
        require(recipientCertificate.deviceId == recipient.deviceId && recipientCertificate.tenantId == envelope.header.tenantId)
        require(recipientCertificate.verify(pinnedHostRoot, envelope.header.tenantId, nowUnixMs)) { "Recipient certificate is not trusted" }
        require(envelope.header.toKeyId == recipientCertificate.certificateId) { "Envelope targets a different certified key" }
        require(encryptionKeyId(recipient.keyPair.public.encoded) == encryptionKeyId(decodeBase64Url(recipientCertificate.encryptionPublicKeyBase64Url))) {
            "Recipient private key does not match its certificate"
        }
        val ciphertext = decodeBase64Url(envelope.ciphertextBase64Url)
        val expectedDigest = hex(sha256(ciphertext))
        require(senderCertificate.deviceId == envelope.header.fromDeviceId && senderCertificate.tenantId == envelope.header.tenantId) {
            "Envelope sender certificate identity mismatch"
        }
        require(senderCertificate.verify(pinnedHostRoot, envelope.header.tenantId, nowUnixMs)) { "Sender certificate is not trusted" }
        val senderKey = decodePublicKey(senderCertificate.signingPublicKeyBase64Url)
        require(verify(senderKey, envelopeSigningBytes(envelope.header, expectedDigest), envelope.senderSignatureBase64Url)) {
            "Envelope sender signature is invalid"
        }

        val ephemeralKey = decodePublicKey(envelope.header.ephemeralPublicKeyBase64Url)
        val shared = ecdh(recipient.keyPair.private, ephemeralKey)
        val info = "JarvisLink/v1\n${envelope.header.tenantId}\n${envelope.header.fromDeviceId}\n${envelope.header.toDeviceId}".toByteArray(UTF_8)
        val aesKey = hkdfSha256(shared, decodeHex(envelope.header.messageId), info, 32)
        val iv = decodeBase64Url(envelope.header.ivBase64Url)
        require(iv.size == GCM_IV_BYTES) { "Envelope IV must be 96 bits" }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(aesKey, "AES"), GCMParameterSpec(GCM_TAG_BITS, iv))
        cipher.updateAAD(aadBytes(envelope.header))
        return cipher.doFinal(ciphertext).toString(UTF_8)
    }

    fun signEnvelope(header: EnvelopeHeader, ciphertextBase64Url: String, privateKey: PrivateKey): String {
        val digest = hex(sha256(decodeBase64Url(ciphertextBase64Url)))
        return sign(privateKey, envelopeSigningBytes(header, digest))
    }

    fun requestSigningBytes(
        tenantId: String,
        deviceId: String,
        timestampUnixMs: Long,
        nonceBase64Url: String,
        method: String,
        pathAndQuery: String,
        body: ByteArray,
    ): ByteArray {
        validateDeviceId(deviceId)
        return listOf(
            "JARVIS-LINK-REQUEST-V1", tenantId, deviceId, timestampUnixMs.toString(), nonceBase64Url,
            method.uppercase(), pathAndQuery, hex(sha256(body)),
        ).joinToString("\n").toByteArray(UTF_8)
    }

    fun envelopeSigningBytes(header: EnvelopeHeader, ciphertextSha256Hex: String): ByteArray {
        require(ciphertextSha256Hex.matches(Regex("^[a-f0-9]{64}$")))
        return (aadBytes(header).toString(UTF_8) + "\n" + ciphertextSha256Hex).toByteArray(UTF_8)
    }

    fun aadBytes(header: EnvelopeHeader): ByteArray {
        validateDeviceId(header.messageId)
        return listOf(
            "JARVIS-LINK-ENVELOPE-V1", header.tenantId, header.messageId,
            header.fromDeviceId, header.toDeviceId, header.toKeyId, header.kind,
            header.issuedAtUnixMs.toString(), header.expiresAtUnixMs.toString(),
            header.ephemeralPublicKeyBase64Url, header.ivBase64Url,
        ).joinToString("\n").toByteArray(UTF_8)
    }

    internal fun hkdfSha256(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        require(length in 1..(255 * 32))
        val extract = Mac.getInstance("HmacSHA256")
        extract.init(SecretKeySpec(if (salt.isEmpty()) ByteArray(32) else salt, "HmacSHA256"))
        val prk = extract.doFinal(ikm)
        val output = ByteArrayOutputStream()
        var previous = ByteArray(0)
        var counter = 1
        while (output.size() < length) {
            val expand = Mac.getInstance("HmacSHA256")
            expand.init(SecretKeySpec(prk, "HmacSHA256"))
            expand.update(previous)
            expand.update(info)
            expand.update(counter.toByte())
            previous = expand.doFinal()
            output.write(previous)
            counter++
        }
        return output.toByteArray().copyOf(length)
    }

    internal fun sign(privateKey: PrivateKey, data: ByteArray): String {
        val signature = Signature.getInstance("SHA256withECDSA")
        signature.initSign(privateKey)
        signature.update(data)
        return b64url(signature.sign())
    }

    internal fun verify(publicKey: PublicKey, data: ByteArray, signatureBase64Url: String): Boolean {
        val signature = Signature.getInstance("SHA256withECDSA")
        signature.initVerify(publicKey)
        signature.update(data)
        return signature.verify(decodeBase64Url(signatureBase64Url))
    }

    internal fun decodePublicKey(value: String): PublicKey =
        KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(decodeBase64Url(value)))

    internal fun ecdh(privateKey: PrivateKey, publicKey: PublicKey): ByteArray {
        val agreement = KeyAgreement.getInstance("ECDH")
        agreement.init(privateKey)
        agreement.doPhase(publicKey, true)
        return agreement.generateSecret()
    }

    internal fun newP256KeyPair(): KeyPair = KeyPairGenerator.getInstance("EC").apply {
        initialize(ECGenParameterSpec("secp256r1"), random)
    }.generateKeyPair()

    fun deviceIdForSigningPublicKey(publicKeyDer: ByteArray): String = hex(sha256(publicKeyDer).copyOfRange(0, 16))

    internal fun b64(bytes: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    internal fun b64url(bytes: ByteArray): String = b64(bytes)
    internal fun decodeBase64Url(value: String): ByteArray = Base64.getUrlDecoder().decode(value)
    internal fun sha256(bytes: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(bytes)
    internal fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it.toInt() and 0xff) }
    internal fun decodeHex(value: String): ByteArray {
        require(value.length % 2 == 0 && value.matches(Regex("^[0-9a-f]+$")))
        return ByteArray(value.length / 2) { index -> value.substring(index * 2, index * 2 + 2).toInt(16).toByte() }
    }

    private fun validateHeader(header: EnvelopeHeader, now: Long, allowExpired: Boolean) {
        validateDeviceId(header.messageId)
        validateDeviceId(header.fromDeviceId)
        validateDeviceId(header.toDeviceId)
        require(header.tenantId.matches(Regex("^[A-Za-z0-9._-]{1,128}$")))
        require(header.kind in kindSet)
        require(header.toKeyId.matches(Regex("^[0-9a-f]{32}$"))) { "Invalid recipient key ID" }
        require(header.expiresAtUnixMs > header.issuedAtUnixMs)
        require(header.expiresAtUnixMs - header.issuedAtUnixMs <= 24 * 60 * 60 * 1000L)
        require(header.issuedAtUnixMs <= now + 60_000)
        if (!allowExpired) require(header.expiresAtUnixMs > now) { "Envelope expired" }
        val iv = decodeBase64Url(header.ivBase64Url)
        require(iv.size == GCM_IV_BYTES)
        val ephemeral = decodePublicKey(header.ephemeralPublicKeyBase64Url)
        require((ephemeral as? java.security.interfaces.ECPublicKey)?.params?.curve?.field?.fieldSize == 256) {
            "Ephemeral key must be P-256"
        }
    }

    fun encryptionKeyId(publicKeyDer: ByteArray): String = hex(sha256(publicKeyDer).copyOfRange(0, 16))

    private fun validateDeviceId(id: String) {
        require(deviceIdRegex.matches(id)) { "Invalid device/message ID" }
    }
}

/** Strict flat JSON codec for the relay envelope. Payload JSON remains encrypted bytes. */
internal object FlatJson {
    /** Encodes flat primitives and nested maps/lists; null members are omitted. */
    fun encode(fields: Map<String, Any>): String = LinkJson.encode(fields)

    fun decode(input: String): Map<String, Any> = LinkJson.parseObject(input)

    fun quote(value: String): String = "\"${LinkJson.escape(value)}\""
}

internal fun Map<String, Any>.string(key: String): String = this[key] as? String ?: error("Missing/invalid string field: $key")
internal fun Map<String, Any>.long(key: String): Long = this[key] as? Long ?: error("Missing/invalid numeric field: $key")
internal fun Map<String, Any>.optionalString(key: String): String? = this[key] as? String
