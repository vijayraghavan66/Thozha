package com.jarvis.companion.data

import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetAddress
import java.net.URI
import java.nio.charset.StandardCharsets
import java.security.AlgorithmParameters
import java.security.KeyFactory
import java.security.MessageDigest
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

internal object ServerMigrationIdentity {
    enum class Failure {
        NONCE_MISMATCH,
        DEVICE_MISMATCH,
        INVALID_PUBLIC_KEY,
        FINGERPRINT_MISMATCH,
        INVALID_HMAC,
        PROTOCOL_MISMATCH,
        TARGET_ORIGIN_MISMATCH,
        PIN_MISMATCH,
        INVALID_SIGNATURE,
        SIGNATURE_MISMATCH,
        GATEWAY_HOST_MISMATCH,
        GATEWAY_CERT_PIN_INVALID,
        GATEWAY_ADDRESS_INVALID,
    }

    class VerificationException(val reason: Failure) : IllegalArgumentException(reason.name)

    data class TailscaleTarget(val baseUrl: String, val origin: String, val host: String)
    data class LocalGatewayIdentity(val hostname: String, val port: Int, val certificateSha256: String)

    /**
     * Verifies the one-time identity bootstrap over the existing USB/loopback connection.
     * The HMAC key is SHA-256(bearer token), so the raw token is never part of the proof payload.
     */
    fun verifyBootstrap(
        bearerToken: String,
        expectedDeviceId: String,
        expectedNonce: String,
        responseDeviceId: String,
        responseNonce: String,
        serverPublicKeyBase64: String,
        serverFingerprint: String,
        proofHmacHex: String,
    ): PinnedServerIdentity {
        if (!constantStringEquals(expectedNonce, responseNonce)) {
            throw VerificationException(Failure.NONCE_MISMATCH)
        }
        if (!constantStringEquals(expectedDeviceId, responseDeviceId)) {
            throw VerificationException(Failure.DEVICE_MISMATCH)
        }
        if (bearerToken.isBlank()) throw VerificationException(Failure.INVALID_HMAC)

        val publicKeyDer = decodePublicKey(serverPublicKeyBase64)
        requireP256(publicKeyDer)
        val derivedFingerprint = fingerprint(publicKeyDer)
        if (!constantStringEquals(derivedFingerprint, serverFingerprint)) {
            throw VerificationException(Failure.FINGERPRINT_MISMATCH)
        }
        val suppliedProof = decodeLowerHex32(proofHmacHex)
            ?: throw VerificationException(Failure.INVALID_HMAC)

        val message = "openjarvis-migration-bootstrap:v1\n" +
            responseNonce + "\n" +
            responseDeviceId + "\n" +
            serverFingerprint + "\n" +
            serverPublicKeyBase64
        val tokenHash = MessageDigest.getInstance("SHA-256")
            .digest(bearerToken.toByteArray(StandardCharsets.UTF_8))
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(tokenHash, "HmacSHA256"))
        val expectedProof = mac.doFinal(message.toByteArray(StandardCharsets.UTF_8))
        if (!MessageDigest.isEqual(expectedProof, suppliedProof)) {
            throw VerificationException(Failure.INVALID_HMAC)
        }
        return PinnedServerIdentity(publicKeyDer, derivedFingerprint)
    }

    /**
     * Validates a candidate URL before any bearer token is sent to it.
     * All DNS answers must be inside Tailscale's assigned address ranges.
     */
    fun validateTailscaleTarget(
        value: String,
        resolve: (String) -> List<InetAddress> = { host -> InetAddress.getAllByName(host).toList() },
    ): TailscaleTarget {
        val uri = try {
            URI(value.trim())
        } catch (_: Exception) {
            throw IllegalArgumentException("Enter a valid HTTPS Tailscale hostname.")
        }
        val host = uri.host?.lowercase().orEmpty()
        if (uri.scheme?.lowercase() != "https" ||
            uri.userInfo != null ||
            uri.query != null ||
            uri.fragment != null ||
            (!uri.path.isNullOrBlank() && uri.path != "/") ||
            (uri.port != -1 && uri.port != 443) ||
            !host.endsWith(".ts.net") ||
            host == ".ts.net"
        ) {
            throw IllegalArgumentException("Migration accepts only a canonical HTTPS *.ts.net origin on port 443.")
        }
        val addresses = try {
            resolve(host)
        } catch (_: Exception) {
            throw IllegalArgumentException("The Tailscale hostname did not resolve.")
        }
        if (addresses.isEmpty() || addresses.any { !isTailscaleAddress(it) }) {
            throw IllegalArgumentException("The Tailscale hostname resolved outside the allowed Tailscale ranges.")
        }
        val origin = "https://" + host
        return TailscaleTarget(origin, origin, host)
    }

    /** Verifies an unauthenticated candidate-host challenge against the already stored pin. */
    fun verifyIdentityChallenge(
        pinned: PinnedServerIdentity,
        expectedNonce: String,
        expectedOrigin: String,
        protocol: String,
        responseNonce: String,
        responseOrigin: String,
        serverPublicKeyBase64: String,
        serverFingerprint: String,
        signatureBase64: String,
    ): Failure? {
        if (protocol != "openjarvis-identity-challenge-v1") return Failure.PROTOCOL_MISMATCH
        if (!constantStringEquals(expectedNonce, responseNonce)) return Failure.NONCE_MISMATCH
        if (!constantStringEquals(expectedOrigin, responseOrigin)) return Failure.TARGET_ORIGIN_MISMATCH

        val presentedDer = try {
            Base64.getDecoder().decode(serverPublicKeyBase64)
        } catch (_: IllegalArgumentException) {
            return Failure.INVALID_PUBLIC_KEY
        }
        if (!isP256(presentedDer)) return Failure.INVALID_PUBLIC_KEY
        if (!MessageDigest.isEqual(pinned.publicKeyDer, presentedDer)) return Failure.PIN_MISMATCH
        if (!constantStringEquals(pinned.fingerprint, fingerprint(presentedDer)) ||
            !constantStringEquals(pinned.fingerprint, serverFingerprint)
        ) {
            return Failure.FINGERPRINT_MISMATCH
        }

        val signedFields = linkedMapOf(
            "protocol" to protocol,
            "nonce" to responseNonce,
            "target_origin" to responseOrigin,
            "server_public_key" to serverPublicKeyBase64,
            "server_fingerprint" to serverFingerprint,
        )
        return when (PairingInvitationSignature.verify(
            signedFields,
            serverPublicKeyBase64,
            serverFingerprint,
            signatureBase64,
        )) {
            null -> null
            PairingInvitationSignature.Failure.INVALID_KEY -> Failure.INVALID_PUBLIC_KEY
            PairingInvitationSignature.Failure.FINGERPRINT_MISMATCH -> Failure.FINGERPRINT_MISMATCH
            PairingInvitationSignature.Failure.INVALID_SIGNATURE -> Failure.INVALID_SIGNATURE
            PairingInvitationSignature.Failure.SIGNATURE_MISMATCH -> Failure.SIGNATURE_MISMATCH
        }
    }

    /** Verifies the signed Wi-Fi gateway name and TLS certificate pin obtained over paired loopback. */
    fun verifyLocalGatewayIdentity(
        pinned: PinnedServerIdentity,
        expectedNonce: String,
        protocol: String,
        responseNonce: String,
        hostname: String,
        portText: String,
        certificateSha256: String,
        serverPublicKeyBase64: String,
        serverFingerprint: String,
        signatureBase64: String,
    ): LocalGatewayIdentity {
        if (protocol != "openjarvis-local-gateway-identity-v1") {
            throw VerificationException(Failure.PROTOCOL_MISMATCH)
        }
        if (!constantStringEquals(expectedNonce, responseNonce)) {
            throw VerificationException(Failure.NONCE_MISMATCH)
        }
        if (!hostname.matches(Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.local"))) {
            throw VerificationException(Failure.GATEWAY_HOST_MISMATCH)
        }
        val port = portText.toIntOrNull()
        if (port == null || port !in 1024..65535) {
            throw VerificationException(Failure.GATEWAY_HOST_MISMATCH)
        }
        if (!certificateSha256.matches(Regex("sha256:[0-9a-f]{64}"))) {
            throw VerificationException(Failure.GATEWAY_CERT_PIN_INVALID)
        }
        val presentedDer = try {
            Base64.getDecoder().decode(serverPublicKeyBase64)
        } catch (_: IllegalArgumentException) {
            throw VerificationException(Failure.INVALID_PUBLIC_KEY)
        }
        if (!isP256(presentedDer)) throw VerificationException(Failure.INVALID_PUBLIC_KEY)
        if (!MessageDigest.isEqual(pinned.publicKeyDer, presentedDer)) {
            throw VerificationException(Failure.PIN_MISMATCH)
        }
        if (!constantStringEquals(pinned.fingerprint, fingerprint(presentedDer)) ||
            !constantStringEquals(pinned.fingerprint, serverFingerprint)
        ) {
            throw VerificationException(Failure.FINGERPRINT_MISMATCH)
        }
        val fields = linkedMapOf(
            "protocol" to protocol,
            "nonce" to responseNonce,
            "hostname" to hostname,
            "port" to portText,
            "certificate_sha256" to certificateSha256,
            "server_public_key" to serverPublicKeyBase64,
            "server_fingerprint" to serverFingerprint,
        )
        when (PairingInvitationSignature.verify(fields, serverPublicKeyBase64, serverFingerprint, signatureBase64)) {
            null -> Unit
            PairingInvitationSignature.Failure.INVALID_KEY -> throw VerificationException(Failure.INVALID_PUBLIC_KEY)
            PairingInvitationSignature.Failure.FINGERPRINT_MISMATCH -> throw VerificationException(Failure.FINGERPRINT_MISMATCH)
            PairingInvitationSignature.Failure.INVALID_SIGNATURE -> throw VerificationException(Failure.INVALID_SIGNATURE)
            PairingInvitationSignature.Failure.SIGNATURE_MISMATCH -> throw VerificationException(Failure.SIGNATURE_MISMATCH)
        }
        return LocalGatewayIdentity(hostname, port, certificateSha256)
    }

    /** Verifies a nonce-bound private gateway IP signed by the already-pinned Mac identity. */
    fun verifyLocalGatewayAddress(
        pinned: PinnedServerIdentity,
        pin: LocalGatewayPin,
        expectedNonce: String,
        protocol: String,
        responseNonce: String,
        hostname: String,
        portText: String,
        addressText: String,
        certificateSha256: String,
        serverPublicKeyBase64: String,
        serverFingerprint: String,
        signatureBase64: String,
    ): String {
        if (protocol != "openjarvis-local-gateway-address-v1") {
            throw VerificationException(Failure.PROTOCOL_MISMATCH)
        }
        if (!constantStringEquals(expectedNonce, responseNonce)) {
            throw VerificationException(Failure.NONCE_MISMATCH)
        }
        if (hostname != pin.hostname || portText != pin.port.toString() || certificateSha256 != pin.certificateSha256) {
            throw VerificationException(Failure.GATEWAY_HOST_MISMATCH)
        }
        val address = parsePrivateAddress(addressText)
            ?: throw VerificationException(Failure.GATEWAY_ADDRESS_INVALID)
        val presentedDer = decodePublicKey(serverPublicKeyBase64)
        requireP256(presentedDer)
        if (!MessageDigest.isEqual(pinned.publicKeyDer, presentedDer)) {
            throw VerificationException(Failure.PIN_MISMATCH)
        }
        if (!constantStringEquals(pinned.fingerprint, fingerprint(presentedDer)) ||
            !constantStringEquals(pinned.fingerprint, serverFingerprint)
        ) {
            throw VerificationException(Failure.FINGERPRINT_MISMATCH)
        }
        val fields = linkedMapOf(
            "protocol" to protocol,
            "nonce" to responseNonce,
            "hostname" to hostname,
            "port" to portText,
            "address" to addressText,
            "certificate_sha256" to certificateSha256,
            "server_public_key" to serverPublicKeyBase64,
            "server_fingerprint" to serverFingerprint,
        )
        when (PairingInvitationSignature.verify(fields, serverPublicKeyBase64, serverFingerprint, signatureBase64)) {
            null -> Unit
            PairingInvitationSignature.Failure.INVALID_KEY -> throw VerificationException(Failure.INVALID_PUBLIC_KEY)
            PairingInvitationSignature.Failure.FINGERPRINT_MISMATCH -> throw VerificationException(Failure.FINGERPRINT_MISMATCH)
            PairingInvitationSignature.Failure.INVALID_SIGNATURE -> throw VerificationException(Failure.INVALID_SIGNATURE)
            PairingInvitationSignature.Failure.SIGNATURE_MISMATCH -> throw VerificationException(Failure.SIGNATURE_MISMATCH)
        }
        return address.hostAddress ?: throw VerificationException(Failure.GATEWAY_ADDRESS_INVALID)
    }

    private fun parsePrivateAddress(value: String): InetAddress? {
        val ipv4 = value.matches(Regex("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}"))
        val ipv6 = value.contains(':') && value.matches(Regex("[0-9a-fA-F:.]+"))
        if (!ipv4 && !ipv6) return null
        val address = runCatching { InetAddress.getByName(value) }.getOrNull() ?: return null
        return address.takeIf(::isAllowedPrivateGatewayAddress)
    }

    private fun isAllowedPrivateGatewayAddress(address: InetAddress): Boolean {
        if (address.isAnyLocalAddress || address.isLoopbackAddress || address.isLinkLocalAddress || address.isMulticastAddress) return false
        return when (address) {
            is Inet4Address -> {
                val bytes = address.address.map { it.toInt() and 0xff }
                bytes[0] == 10 || (bytes[0] == 172 && bytes[1] in 16..31) || (bytes[0] == 192 && bytes[1] == 168)
            }
            is Inet6Address -> (address.address[0].toInt() and 0xfe) == 0xfc
            else -> false
        }
    }

    fun fingerprint(publicKeyDer: ByteArray): String =
        "sha256:" + MessageDigest.getInstance("SHA-256")
            .digest(publicKeyDer)
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    fun isTailscaleAddress(address: InetAddress): Boolean {
        val bytes = address.address
        if (address is Inet4Address && bytes.size == 4) {
            val a = bytes[0].toInt() and 0xff
            val b = bytes[1].toInt() and 0xff
            val c = bytes[2].toInt() and 0xff
            val inCgnat = a == 100 && b in 64..127
            val reserved = (b == 100 && c == 0) ||
                (b == 100 && c == 100) ||
                (b == 115 && c in 92..93)
            return inCgnat && !reserved
        }
        if (address is Inet6Address && bytes.size == 16) {
            val prefix = byteArrayOf(0xfd.toByte(), 0x7a, 0x11, 0x5c, 0xa1.toByte(), 0xe0.toByte())
            return prefix.indices.all { bytes[it] == prefix[it] }
        }
        return false
    }

    private fun decodePublicKey(value: String): ByteArray =
        try {
            Base64.getDecoder().decode(value)
        } catch (_: IllegalArgumentException) {
            throw VerificationException(Failure.INVALID_PUBLIC_KEY)
        }

    private fun requireP256(encodedPublicKey: ByteArray) {
        if (!isP256(encodedPublicKey)) throw VerificationException(Failure.INVALID_PUBLIC_KEY)
    }

    internal fun isP256(encodedPublicKey: ByteArray): Boolean {
        val publicKey = try {
            KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(encodedPublicKey)) as? ECPublicKey
        } catch (_: Exception) {
            null
        } ?: return false
        val expected = try {
            AlgorithmParameters.getInstance("EC").apply {
                init(ECGenParameterSpec("secp256r1"))
            }.getParameterSpec(ECParameterSpec::class.java)
        } catch (_: Exception) {
            return false
        }
        val actual = publicKey.params
        return actual.curve.field == expected.curve.field &&
            actual.curve.a == expected.curve.a &&
            actual.curve.b == expected.curve.b &&
            actual.generator == expected.generator &&
            actual.order == expected.order &&
            actual.cofactor == expected.cofactor
    }

    private fun decodeLowerHex32(value: String): ByteArray? {
        if (value.length != 64 || value.any { it !in '0'..'9' && it !in 'a'..'f' }) return null
        return ByteArray(32) { index ->
            ((value[index * 2].digitToInt(16) shl 4) + value[index * 2 + 1].digitToInt(16)).toByte()
        }
    }

    private fun constantStringEquals(a: String, b: String): Boolean =
        MessageDigest.isEqual(a.toByteArray(StandardCharsets.UTF_8), b.toByteArray(StandardCharsets.UTF_8))
}
