package com.jarvis.companion.data

import com.jarvis.companion.model.ApiException
import com.jarvis.companion.model.PairingInvitation
import org.json.JSONObject
import java.net.Inet4Address
import java.net.InetAddress
import java.net.URI
import java.security.MessageDigest
import java.time.Instant
import java.time.OffsetDateTime
import java.util.Base64

/** Strict, side-effect-free parser for the signed USB v1 and local-Wi-Fi v2 pairing QR formats. */
internal object PairingInvitationParser {
    private val v1Keys = setOf(
        "protocol", "server_url", "server_fingerprint", "server_public_key",
        "invitation_id", "challenge", "expires_at", "server_signature",
    )
    private val v2LegacyKeys = v1Keys + setOf("gateway_address", "gateway_port", "gateway_certificate_sha256")
    private val v2Keys = v2LegacyKeys + "loopback_url"
    private val supportedWifiPorts = setOf(8443, 8444)
    private val localHostname = Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.local")

    fun parse(rawPayload: String): PairingInvitation {
        if (rawPayload.length > 8192) throw ApiException("This QR is too large to be a Jarvis pairing invitation.")
        val json = try { JSONObject(rawPayload) } catch (_: Exception) {
            throw ApiException("This QR is not a Jarvis pairing invitation.")
        }
        val actualKeys = buildSet {
            val iterator = json.keys()
            while (iterator.hasNext()) add(iterator.next())
        }
        val protocol = json.optString("protocol")
        if ((protocol == "openjarvis-pairing-v1" && actualKeys != v1Keys) ||
            (protocol == "openjarvis-pairing-v2" && actualKeys !in setOf(v2LegacyKeys, v2Keys)) ||
            protocol !in setOf("openjarvis-pairing-v1", "openjarvis-pairing-v2")
        ) throw ApiException("The invitation QR has unexpected or missing fields. Scan a fresh Jarvis QR.")

        val fields = linkedMapOf<String, String>()
        actualKeys.filter { it != "server_signature" }.forEach { key ->
            fields[key] = requiredString(json, key)
        }
        val serverKey = requiredString(json, "server_public_key")
        val serverFingerprint = requiredString(json, "server_fingerprint")
        when (PairingInvitationSignature.verify(
            fields,
            serverKey,
            serverFingerprint,
            requiredString(json, "server_signature"),
        )) {
            null -> Unit
            PairingInvitationSignature.Failure.INVALID_KEY -> throw ApiException("The Mac invitation contains an invalid public key.")
            PairingInvitationSignature.Failure.FINGERPRINT_MISMATCH -> throw ApiException("The Mac public-key fingerprint does not match the invitation.")
            PairingInvitationSignature.Failure.INVALID_SIGNATURE -> throw ApiException("The Mac invitation signature is malformed.")
            PairingInvitationSignature.Failure.SIGNATURE_MISMATCH -> throw ApiException("The Mac invitation signature does not verify.")
        }

        val invitationId = requiredString(json, "invitation_id")
        if (!invitationId.matches(Regex("[0-9a-f]{32}"))) throw ApiException("The Mac invitation ID is malformed.")
        val challenge = requiredString(json, "challenge")
        if (challenge.length !in 16..512) throw ApiException("The Mac invitation challenge is malformed.")
        val expiry = requiredString(json, "expires_at")
        val expiryInstant = runCatching { OffsetDateTime.parse(expiry).toInstant() }
            .getOrElse { throw ApiException("The invitation expiry time is invalid.") }
        val remaining = java.time.Duration.between(Instant.now(), expiryInstant)
        if (remaining.isNegative || remaining.isZero) {
            throw ApiException("This pairing invitation has expired. Create a new QR on the Mac.")
        }
        if (remaining > java.time.Duration.ofMinutes(10)) {
            throw ApiException("This pairing invitation expiry is too far in the future.")
        }

        val local = if (protocol == "openjarvis-pairing-v2") {
            validateLocalWifi(
                requiredString(json, "server_url"),
                requiredString(json, "gateway_address"),
                requiredString(json, "gateway_port"),
                requiredString(json, "gateway_certificate_sha256"),
            )
        } else null
        val loopbackBaseUrl = if (protocol == "openjarvis-pairing-v2" && json.has("loopback_url")) {
            validateLoopbackBaseUrl(requiredString(json, "loopback_url"))
        } else if (protocol == "openjarvis-pairing-v2") {
            "http://127.0.0.1:8000"
        } else null

        val publicDer = Base64.getDecoder().decode(serverKey)
        return PairingInvitation(
            invitationId = invitationId,
            challenge = challenge,
            expiresAt = expiry,
            serverUrl = local?.let { "https://${it.hostname}:${it.port}" }
                ?: validateLoopbackBaseUrl(requiredString(json, "server_url")),
            serverFingerprint = serverFingerprint,
            serverPublicKeyBase64 = serverKey,
            verifiedServerKeyFingerprint = fingerprint(publicDer),
            gatewayAddress = local?.address,
            gatewayPort = local?.port,
            gatewayCertificateSha256 = local?.certificateSha256,
            loopbackBaseUrl = loopbackBaseUrl,
        )
    }

    internal data class WifiFields(val hostname: String, val address: String, val port: Int, val certificateSha256: String)

    internal fun validateLocalWifi(
        serverUrl: String,
        address: String,
        portText: String,
        certificateSha256: String,
    ): WifiFields {
        val uri = try { URI(serverUrl) } catch (_: Exception) {
            throw ApiException("The Mac local Wi-Fi address is malformed.")
        }
        val hostname = uri.host?.lowercase().orEmpty()
        val port = portText.toIntOrNull()
        if (!uri.scheme.equals("https", true) || !localHostname.matches(hostname) || port !in supportedWifiPorts || uri.port != port ||
            uri.userInfo != null || uri.query != null || uri.fragment != null ||
            (!uri.path.isNullOrBlank() && uri.path != "/")
        ) throw ApiException("This Wi-Fi invitation does not target a supported local Mac gateway.")

        val octets = address.split('.')
        if (octets.size != 4 || octets.any { part ->
                part.isEmpty() || part.length > 3 || part.any { !it.isDigit() } || part.toIntOrNull() !in 0..255
            }
        ) throw ApiException("The signed Mac gateway address is not a valid private IPv4 address.")
        val canonicalAddress = octets.joinToString(".") { it.toInt().toString() }
        if (canonicalAddress != address) throw ApiException("The signed Mac gateway address is not canonical.")
        val parsedAddress = InetAddress.getByAddress(octets.map { it.toInt().toByte() }.toByteArray())
        if (parsedAddress !is Inet4Address || !isAllowedPrivateGatewayAddress(parsedAddress)) {
            throw ApiException("The signed Mac gateway address is outside the private network ranges.")
        }
        if (!certificateSha256.matches(Regex("sha256:[0-9a-f]{64}"))) {
            throw ApiException("The signed Mac Wi-Fi certificate fingerprint is malformed.")
        }
        return WifiFields(hostname, canonicalAddress, port, certificateSha256)
    }

    private fun validateLoopbackBaseUrl(value: String): String {
        val uri = try { URI(value.trim().trimEnd('/')) } catch (_: Exception) {
            throw ApiException("The invitation server URL is malformed.")
        }
        val host = uri.host?.lowercase().orEmpty()
        if (uri.scheme?.lowercase() !in setOf("http", "https") ||
            host !in setOf("localhost", "127.0.0.1") || uri.userInfo != null || uri.query != null || uri.fragment != null ||
            (!uri.path.isNullOrBlank() && uri.path != "/")
        ) throw ApiException("This USB invitation must use a loopback Mac address.")
        return uri.toString().trimEnd('/')
    }

    private fun requiredString(json: JSONObject, key: String): String {
        val value = json.opt(key)
        if (value !is String || value.isBlank()) throw ApiException("The Mac invitation is missing a valid $key field.")
        return value
    }

    private fun fingerprint(encodedPublicKey: ByteArray): String =
        "sha256:" + MessageDigest.getInstance("SHA-256")
            .digest(encodedPublicKey)
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }
}
