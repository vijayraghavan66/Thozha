package com.jarvis.companion.data

import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.DataInputStream
import java.net.InetAddress
import java.security.MessageDigest
import java.util.Base64

/** Compact transport encoding for the unchanged, server-signed pairing-v2 fields. */
internal object CompactPairingQr {
    const val PREFIX = "thozha-pair-v2:"
    private val magic = byteArrayOf('T'.code.toByte(), 'Z'.code.toByte(), 'P'.code.toByte(), '2'.code.toByte())

    fun expand(value: String): String {
        require(value.startsWith(PREFIX)) { "Unsupported compact pairing QR." }
        val encoded = value.removePrefix(PREFIX)
        require(encoded.length in 1..2048) { "Compact pairing QR is malformed." }
        val packet = try {
            Base64.getUrlDecoder().decode(encoded)
        } catch (_: IllegalArgumentException) {
            throw IllegalArgumentException("Compact pairing QR is malformed.")
        }
        require(packet.size <= 1024) { "Compact pairing QR is too large." }

        return try {
            DataInputStream(ByteArrayInputStream(packet)).use { input ->
                val actualMagic = input.readExact(magic.size)
                require(actualMagic.contentEquals(magic)) { "Unsupported compact pairing QR version." }
                val flags = input.readUnsignedByte()
                require(flags and 0xfe == 0) { "Compact pairing QR has unknown flags." }
                val host = input.readBytes8(field = "host").toString(Charsets.US_ASCII)
                require(host.matches(Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.local"))) {
                    "Compact pairing QR has an invalid Mac hostname."
                }
                val addressBytes = input.readExact(4)
                val address = InetAddress.getByAddress(addressBytes).hostAddress
                val port = input.readUnsignedShort()
                require(port == 8443 || port == 8444) { "Compact pairing QR uses an unsupported gateway port." }
                val certPin = input.readFixedHex(32)
                val serverFingerprint = input.readFixedHex(32)
                val publicDer = input.readBytes8(field = "public key")
                val publicKey = Base64.getEncoder().encodeToString(publicDer)
                val invitationIdBytes = input.readBytes8(field = "invitation id")
                require(invitationIdBytes.size == 16) { "Compact pairing invitation ID is malformed." }
                val invitationId = invitationIdBytes.toHex()
                val challengeBytes = input.readBytes8(field = "challenge")
                val challenge = if (flags and 1 != 0) {
                    require(challengeBytes.size == 32) { "Compact pairing challenge is malformed." }
                    challengeBytes.toHex()
                } else challengeBytes.toString(Charsets.UTF_8)
                val expiresAt = input.readBytes8(field = "expiry").toString(Charsets.UTF_8)
                val loopbackUrl = input.readBytes8(allowEmpty = true, field = "loopback").toString(Charsets.UTF_8)
                val signatureBytes = input.readBytes8(field = "signature")
                val signature = Base64.getEncoder().encodeToString(signatureBytes)
                require(input.available() == 0) { "Compact pairing QR contains trailing bytes." }

                val actualFingerprint = MessageDigest.getInstance("SHA-256").digest(publicDer).toHex()
                require(actualFingerprint == serverFingerprint) { "Compact pairing server identity is inconsistent." }
                val payload = JSONObject()
                    .put("protocol", "openjarvis-pairing-v2")
                    .put("server_url", "https://$host:$port")
                    .put("gateway_address", address)
                    .put("gateway_port", port.toString())
                    .put("gateway_certificate_sha256", "sha256:$certPin")
                    .put("server_fingerprint", "sha256:$serverFingerprint")
                    .put("server_public_key", publicKey)
                    .put("invitation_id", invitationId)
                    .put("challenge", challenge)
                    .put("expires_at", expiresAt)
                    .put("server_signature", signature)
                if (loopbackUrl.isNotEmpty()) payload.put("loopback_url", loopbackUrl)
                payload.toString()
            }
        } catch (error: IllegalArgumentException) {
            throw error
        } catch (_: Exception) {
            throw IllegalArgumentException("Compact pairing QR is malformed.")
        }
    }

    private fun DataInputStream.readBytes8(allowEmpty: Boolean = false, field: String = "data"): ByteArray {
        val length = readUnsignedByte()
        require((length in 1..255 || (allowEmpty && length == 0)) && length <= available()) {
            "Compact pairing QR is truncated ($field length=$length, remaining=${available()})."
        }
        return readExact(length)
    }

    private fun DataInputStream.readText8(field: String = "text"): String = readBytes8(field = field).toString(Charsets.UTF_8)

    private fun DataInputStream.readFixedHex(length: Int): String = readExact(length).toHex()

    private fun DataInputStream.readExact(length: Int): ByteArray = ByteArray(length).also { readFully(it, 0, length) }

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it.toInt() and 0xff) }
}
