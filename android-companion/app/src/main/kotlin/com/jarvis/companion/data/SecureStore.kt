package com.jarvis.companion.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.MessageDigest
import java.net.InetAddress
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import java.util.UUID

/** Non-secret connection settings are stored normally; credentials stay encrypted with Android Keystore. */
class SecureStore(context: Context) {
    private val prefs = context.getSharedPreferences("jarvis_companion", Context.MODE_PRIVATE)

    fun baseUrl(): String = prefs.getString(KEY_BASE_URL, "") ?: ""
    fun setBaseUrl(value: String) {
        val normalized = value.trim().trimEnd('/')
        val old = baseUrl()
        val edit = prefs.edit().putString(KEY_BASE_URL, normalized)
        // Never send an existing device token to a newly selected private host.
        if (old != normalized) {
            edit.remove(KEY_DEVICE_ID).remove(KEY_TOKEN_IV).remove(KEY_TOKEN_CIPHER)
        }
        edit.apply()
    }
    fun deviceId(): String = prefs.getString(KEY_DEVICE_ID, "") ?: ""
    fun lastMacConnection(): Pair<Long, String> =
        prefs.getLong(KEY_MAC_CONNECTED_AT, 0L) to (prefs.getString(KEY_MAC_TRANSPORT, "") ?: "")

    fun recordMacConnection(transport: String) {
        require(transport in setOf("usb", "wifi", "tailscale"))
        prefs.edit()
            .putLong(KEY_MAC_CONNECTED_AT, System.currentTimeMillis())
            .putString(KEY_MAC_TRANSPORT, transport)
            .apply()
    }
    fun token(): String? = prefs.getString(KEY_TOKEN_CIPHER, null)?.let { cipherText ->
        val iv = prefs.getString(KEY_TOKEN_IV, null) ?: return@let null
        runCatching { decrypt(decode(iv), decode(cipherText)) }.getOrNull()
    }

    fun savePairing(deviceId: String, token: String) {
        require(token.isNotBlank())
        val (iv, encrypted) = encrypt(token)
        prefs.edit()
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_TOKEN_IV, encode(iv))
            .putString(KEY_TOKEN_CIPHER, encode(encrypted))
            .apply()
    }

    /** Atomically persists the device credential and the server key authenticated by its QR. */
    fun savePairingWithServerIdentity(
        deviceId: String,
        token: String,
        serverPublicKeyDer: ByteArray,
        serverFingerprint: String,
    ) {
        require(token.isNotBlank())
        val pin = validateServerIdentity(serverPublicKeyDer, serverFingerprint)
        val existing = pinnedServerIdentity()
        if (existing != null && !sameServerIdentity(existing, pin)) {
            throw SecurityException("The Mac server identity changed from the pinned key.")
        }
        val (tokenIv, tokenCipher) = encrypt(token)
        val (pinIv, pinCipher) = encrypt(pinRecord(pin))
        prefs.edit()
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_TOKEN_IV, encode(tokenIv))
            .putString(KEY_TOKEN_CIPHER, encode(tokenCipher))
            .putString(KEY_SERVER_PIN_IV, encode(pinIv))
            .putString(KEY_SERVER_PIN_CIPHER, encode(pinCipher))
            .apply()
    }

    fun pinnedServerIdentity(): PinnedServerIdentity? {
        val ciphertext = prefs.getString(KEY_SERVER_PIN_CIPHER, null) ?: return null
        val iv = prefs.getString(KEY_SERVER_PIN_IV, null)
            ?: throw SecurityException("Pinned server identity record is incomplete.")
        val record = try {
            decrypt(decode(iv), decode(ciphertext))
        } catch (_: Exception) {
            throw SecurityException("Pinned server identity record failed integrity validation.")
        }
        val separator = record.indexOf('\n')
        if (separator <= 0 || separator == record.lastIndex) {
            throw SecurityException("Pinned server identity record is malformed.")
        }
        val fingerprint = record.substring(0, separator)
        val publicKeyDer = try {
            decode(record.substring(separator + 1))
        } catch (_: Exception) {
            throw SecurityException("Pinned server public key is malformed.")
        }
        return validateServerIdentity(publicKeyDer, fingerprint)
    }

    fun localGatewayPin(): LocalGatewayPin? {
        val ciphertext = prefs.getString(KEY_WIFI_PIN_CIPHER, null) ?: return null
        val iv = prefs.getString(KEY_WIFI_PIN_IV, null)
            ?: throw SecurityException("Local gateway pin record is incomplete.")
        val value = try {
            decrypt(decode(iv), decode(ciphertext))
        } catch (_: Exception) {
            throw SecurityException("Local gateway pin failed integrity validation.")
        }
        val fields = value.split('\n')
        if (fields.size !in 3..4) throw SecurityException("Local gateway pin record is malformed.")
        val pin = LocalGatewayPin(fields[0], fields[1].toIntOrNull() ?: 0, fields[2], fields.getOrNull(3)?.takeIf { it.isNotBlank() })
        validateLocalGatewayPin(pin)
        return pin
    }

    /** Save only after the Mac identity signature has been verified over paired loopback. */
    internal fun saveLocalGatewayPin(pin: LocalGatewayPin) {
        if (token().isNullOrBlank() || pinnedServerIdentity() == null) {
            throw SecurityException("Pair this phone with the Mac before pinning its Wi-Fi gateway.")
        }
        validateLocalGatewayPin(pin)
        val old = localGatewayPin()
        if (old != pin) {
            // This method is internal and is called only after pinLocalGatewayIdentity verifies
            // the new certificate fingerprint with the already-paired Mac signing key over USB.
            val record = listOfNotNull(pin.hostname, pin.port.toString(), pin.certificateSha256, pin.address).joinToString("\n")
            val (iv, ciphertext) = encrypt(record)
            prefs.edit().putString(KEY_WIFI_PIN_IV, encode(iv)).putString(KEY_WIFI_PIN_CIPHER, encode(ciphertext)).apply()
        }
    }

    /** Stores the first pin only after the caller has authenticated its bootstrap response. */
    internal fun saveServerIdentityPin(serverPublicKeyDer: ByteArray, serverFingerprint: String) {
        val candidate = validateServerIdentity(serverPublicKeyDer, serverFingerprint)
        val existing = pinnedServerIdentity()
        if (existing != null) {
            if (!sameServerIdentity(existing, candidate)) {
                throw SecurityException("The Mac server identity changed from the pinned key.")
            }
            return
        }
        val (iv, ciphertext) = encrypt(pinRecord(candidate))
        prefs.edit()
            .putString(KEY_SERVER_PIN_IV, encode(iv))
            .putString(KEY_SERVER_PIN_CIPHER, encode(ciphertext))
            .apply()
    }

    /** Call only after validating the candidate server's TLS and signed identity challenge. */
    internal fun updateBaseUrlAfterPinnedIdentityVerification(
        value: String,
        serverPublicKeyDer: ByteArray,
        serverFingerprint: String,
    ) {
        if (token().isNullOrBlank() || deviceId().isBlank()) {
            throw SecurityException("Host migration requires an active paired device.")
        }
        val pinned = pinnedServerIdentity()
            ?: throw SecurityException("Pin the Mac server over the current USB connection first.")
        val presented = validateServerIdentity(serverPublicKeyDer, serverFingerprint)
        if (!sameServerIdentity(pinned, presented)) {
            throw SecurityException("The candidate host does not present the pinned Mac identity.")
        }
        prefs.edit().putString(KEY_BASE_URL, value.trim().trimEnd('/')).apply()
    }

    fun clearPairing() {
        prefs.edit().remove(KEY_DEVICE_ID).remove(KEY_TOKEN_IV).remove(KEY_TOKEN_CIPHER).apply()
    }

    fun pushNotificationsEnabled(): Boolean =
        prefs.getBoolean(KEY_PUSH_NOTIFICATIONS_ENABLED, false)

    fun setPushNotificationsEnabled(enabled: Boolean) {
        prefs.edit().putBoolean(KEY_PUSH_NOTIFICATIONS_ENABLED, enabled).apply()
    }

    /** Store only a one-way digest; the FCM registration token is not persisted locally. */
    fun isFcmTokenRegistered(token: String): Boolean =
        prefs.getString(KEY_FCM_TOKEN_HASH, null) == fcmTokenHash(token)

    fun markFcmTokenRegistered(token: String) {
        prefs.edit().putString(KEY_FCM_TOKEN_HASH, fcmTokenHash(token)).apply()
    }

    fun clearFcmTokenRegistration() {
        prefs.edit().remove(KEY_FCM_TOKEN_HASH).apply()
    }

    /** Stable across retries/process death so an uncertain decision cannot become a second action. */
    fun decisionIdempotencyKey(approvalId: String, digest: String, decision: String): String {
        require(decision == "approve" || decision == "reject")
        val raw = approvalId + "|" + digest + "|" + decision
        val key = MessageDigest.getInstance("SHA-256")
            .digest(raw.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
        val preferenceKey = "decision_idempotency_" + key
        val existing = prefs.getString(preferenceKey, null)
        if (!existing.isNullOrBlank()) return existing
        val generated = UUID.randomUUID().toString()
        prefs.edit().putString(preferenceKey, generated).apply()
        return prefs.getString(preferenceKey, generated) ?: generated
    }

    fun clearDecisionIdempotencyKey(approvalId: String, digest: String, decision: String) {
        val raw = approvalId + "|" + digest + "|" + decision
        val key = MessageDigest.getInstance("SHA-256")
            .digest(raw.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
        prefs.edit().remove("decision_idempotency_" + key).apply()
    }

    /** Public key is safe to send to the pairing endpoint; the private key never leaves Keystore. */
    fun publicSigningKey(): String {
        val ks = keyStore()
        if (!ks.containsAlias(SIGNING_ALIAS)) {
            val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, ANDROID_KEYSTORE)
            generator.initialize(
                KeyGenParameterSpec.Builder(
                    SIGNING_ALIAS,
                    KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY,
                )
                    .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .build(),
            )
            generator.generateKeyPair()
        }
        val certificate = ks.getCertificate(SIGNING_ALIAS)
            ?: error("Device signing key is unavailable")
        return encode(certificate.publicKey.encoded)
    }

    /** Stable public-key fingerprint for matching this phone in the Mac's pending-device prompt. */
    fun signingKeyFingerprint(): String {
        val publicKeyBytes = decode(publicSigningKey())
        val hex = MessageDigest.getInstance("SHA-256")
            .digest(publicKeyBytes)
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }
        // Match the Mac pending-device UI format exactly (12-byte visual prefix).
        return "SHA256:" + hex.take(24)
    }

    fun signChallenge(challenge: String): String {
        require(challenge.isNotBlank())
        val privateKey = keyStore().getKey(SIGNING_ALIAS, null)
            ?: error("Device signing key is unavailable")
        val signature = Signature.getInstance("SHA256withECDSA")
        signature.initSign(privateKey as java.security.PrivateKey)
        signature.update(challenge.toByteArray(Charsets.UTF_8))
        return encode(signature.sign())
    }

    private fun encrypt(value: String): Pair<ByteArray, ByteArray> {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, encryptionKey())
        return cipher.iv to cipher.doFinal(value.toByteArray(Charsets.UTF_8))
    }

    private fun decrypt(iv: ByteArray, ciphertext: ByteArray): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, encryptionKey(), GCMParameterSpec(128, iv))
        return cipher.doFinal(ciphertext).toString(Charsets.UTF_8)
    }

    private fun encryptionKey(): SecretKey {
        val ks = keyStore()
        val existing = ks.getKey(ENCRYPTION_ALIAS, null) as? SecretKey
        if (existing != null) return existing
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                ENCRYPTION_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    private fun keyStore(): KeyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }

    private fun encode(bytes: ByteArray): String = Base64.encodeToString(bytes, Base64.NO_WRAP)
    private fun decode(value: String): ByteArray = Base64.decode(value, Base64.NO_WRAP)

    private fun pinRecord(pin: PinnedServerIdentity): String =
        pin.fingerprint + "\n" + encode(pin.publicKeyDer)

    private fun validateServerIdentity(publicKeyDer: ByteArray, fingerprint: String): PinnedServerIdentity {
        val normalized = normalizeFingerprint(fingerprint)
        val derived = serverFingerprint(publicKeyDer)
        if (!MessageDigest.isEqual(normalized.toByteArray(Charsets.UTF_8), derived.toByteArray(Charsets.UTF_8))) {
            throw SecurityException("Server fingerprint does not match its public key.")
        }
        return PinnedServerIdentity(publicKeyDer.copyOf(), derived)
    }

    private fun validateLocalGatewayPin(pin: LocalGatewayPin) {
        require(pin.hostname.matches(Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.local"))) {
            "Local gateway hostname is invalid."
        }
        require(pin.port in 1024..65535) { "Local gateway port is invalid." }
        require(pin.certificateSha256.matches(Regex("sha256:[0-9a-f]{64}"))) {
            "Local gateway certificate pin is invalid."
        }
        pin.address?.let { value ->
            val ipv4 = value.matches(Regex("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}"))
            val ipv6 = value.contains(':') && value.matches(Regex("[0-9a-fA-F:.]+"))
            require(ipv4 || ipv6) { "Local gateway address must be an IP literal." }
            val address = if (ipv4) {
                val bytes = value.split('.').map { it.toIntOrNull()?.takeIf { octet -> octet in 0..255 }?.toByte() }
                require(bytes.size == 4 && bytes.all { it != null }) { "Local gateway IPv4 address is invalid." }
                InetAddress.getByAddress(ByteArray(4) { bytes[it]!!.toByte() })
            } else {
                InetAddress.getByName(value)
            }
            require(isAllowedPrivateGatewayAddress(address)) { "Local gateway address is not private." }
        }
    }

    private fun sameServerIdentity(a: PinnedServerIdentity, b: PinnedServerIdentity): Boolean =
        MessageDigest.isEqual(a.publicKeyDer, b.publicKeyDer) &&
            MessageDigest.isEqual(a.fingerprint.toByteArray(Charsets.UTF_8), b.fingerprint.toByteArray(Charsets.UTF_8))

    private fun normalizeFingerprint(value: String): String =
        value.trim().lowercase().removePrefix("sha256:").replace(":", "").replace(" ", "")
            .let { "sha256:" + it }

    private fun serverFingerprint(publicKeyDer: ByteArray): String =
        "sha256:" + MessageDigest.getInstance("SHA-256")
            .digest(publicKeyDer)
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun fcmTokenHash(token: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(token.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    companion object {
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val ENCRYPTION_ALIAS = "jarvis_companion_token_aes_v1"
        private const val SIGNING_ALIAS = "jarvis_companion_device_signing_v1"
        private const val KEY_BASE_URL = "base_url"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_TOKEN_IV = "token_iv"
        private const val KEY_TOKEN_CIPHER = "token_ciphertext"
        private const val KEY_SERVER_PIN_IV = "server_pin_iv"
        private const val KEY_SERVER_PIN_CIPHER = "server_pin_ciphertext"
        private const val KEY_WIFI_PIN_IV = "wifi_pin_iv"
        private const val KEY_WIFI_PIN_CIPHER = "wifi_pin_ciphertext"
        private const val KEY_MAC_CONNECTED_AT = "mac_connected_at"
        private const val KEY_MAC_TRANSPORT = "mac_transport"
        private const val KEY_PUSH_NOTIFICATIONS_ENABLED = "push_notifications_enabled"
        private const val KEY_FCM_TOKEN_HASH = "fcm_token_hash"
    }
}

data class PinnedServerIdentity(val publicKeyDer: ByteArray, val fingerprint: String)
data class LocalGatewayPin(
    val hostname: String,
    val port: Int,
    val certificateSha256: String,
    val address: String? = null,
)
