package com.jarvis.companion.data

import java.nio.charset.StandardCharsets
import java.security.KeyFactory
import java.security.MessageDigest
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import java.util.Base64

/** Python-compatible verifier for the Mac's canonical signed invitation payload. */
internal object PairingInvitationSignature {
    enum class Failure { INVALID_KEY, FINGERPRINT_MISMATCH, INVALID_SIGNATURE, SIGNATURE_MISMATCH }

    fun canonicalBytes(fields: Map<String, String>): ByteArray {
        val text = fields.toSortedMap().entries.joinToString(
            prefix = "{", postfix = "}", separator = ",",
        ) { (key, value) -> quotePythonJson(key) + ":" + quotePythonJson(value) }
        return text.toByteArray(StandardCharsets.UTF_8)
    }

    /** Match json.dumps(sort_keys=True,separators=(',', ':'),ensure_ascii=False). */
    private fun quotePythonJson(value: String): String = buildString(value.length + 2) {
        append('"')
        for (char in value) {
            when (char) {
                '"' -> append("\\\"")
                '\\' -> append("\\\\")
                '\b' -> append("\\b")
                '\u000C' -> append("\\f")
                '\n' -> append("\\n")
                '\r' -> append("\\r")
                '\t' -> append("\\t")
                else -> if (char.code < 0x20) {
                    append("\\u")
                    append(char.code.toString(16).padStart(4, '0'))
                } else {
                    // In particular, do not escape `/`: Python's canonical
                    // serializer leaves URL and base64 slashes unescaped.
                    append(char)
                }
            }
        }
        append('"')
    }

    fun verify(
        fields: Map<String, String>,
        publicKeyDerBase64: String,
        expectedFingerprint: String,
        signatureBase64: String,
    ): Failure? {
        val publicDer = try { Base64.getDecoder().decode(publicKeyDerBase64) }
        catch (_: IllegalArgumentException) { return Failure.INVALID_KEY }
        if (!ServerMigrationIdentity.isP256(publicDer)) return Failure.INVALID_KEY
        val signature = try { Base64.getDecoder().decode(signatureBase64) }
        catch (_: IllegalArgumentException) { return Failure.INVALID_SIGNATURE }

        val key = try {
            KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(publicDer))
        } catch (_: Exception) { return Failure.INVALID_KEY }

        val derivedFingerprint = "sha256:" + MessageDigest.getInstance("SHA-256")
            .digest(publicDer).joinToString("") { "%02x".format(it.toInt() and 0xff) }
        if (!expectedFingerprint.trim().equals(derivedFingerprint, ignoreCase = true)) {
            return Failure.FINGERPRINT_MISMATCH
        }

        return try {
            val verifier = Signature.getInstance("SHA256withECDSA")
            verifier.initVerify(key)
            verifier.update(canonicalBytes(fields))
            if (verifier.verify(signature)) null else Failure.SIGNATURE_MISMATCH
        } catch (_: Exception) {
            Failure.INVALID_SIGNATURE
        }
    }
}
