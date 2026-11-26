package com.jarvis.companion.data

import org.json.JSONObject
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.EncodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel
import java.math.BigInteger
import java.security.KeyFactory
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPrivateKeySpec
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.util.Base64

/** JVM protocol harness for strict legacy USB and v2 direct local-Wi-Fi invitation parsing. */
object PairingInvitationProtocolChecks {
    private const val PUBLIC_KEY = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaxfR8uEsQkf4vOblY6RA8ncDfYEt6zOg9KE5RdiYwpZP40Li/hp/m47n60p8D54WK84zV2sxXs7LtkBoN79R9Q=="
    private const val SERVER_FINGERPRINT = "sha256:5cd252fb0ce8932436faf8ccd1040981b89ee4ad6b9fe9e2a2b7e71aacb27cd3"

    @JvmStatic
    fun main(args: Array<String>) {
        val v1 = invitation("openjarvis-pairing-v1")
        val parsedV1 = PairingInvitationParser.parse(v1)
        check(parsedV1.serverUrl == "http://localhost:8000")
        check(parsedV1.gatewayAddress == null)

        val v2 = invitation("openjarvis-pairing-v2")
        val parsedV2 = PairingInvitationParser.parse(v2)
        check(parsedV2.serverUrl == "https://thozha-mac.local:8443")
        check(parsedV2.gatewayAddress == "192.168.1.8")
        check(parsedV2.gatewayPort == 8443)
        check(parsedV2.gatewayCertificateSha256 == "sha256:" + "a".repeat(64))
        check(parsedV2.loopbackBaseUrl == "http://127.0.0.1:18000")

        val legacyV2 = PairingInvitationParser.parse(invitation("openjarvis-pairing-v2", includeLoopback = false))
        check(legacyV2.loopbackBaseUrl == "http://127.0.0.1:8000")

        val portableV2 = PairingInvitationParser.parse(
            invitation("openjarvis-pairing-v2", gatewayPort = 8444, loopbackUrl = "http://127.0.0.1:18000"),
        )
        check(portableV2.serverUrl == "https://thozha-mac.local:8444")
        check(portableV2.gatewayPort == 8444)
        check(portableV2.loopbackBaseUrl == "http://127.0.0.1:18000")

        expectRejected(v2.replace("192.168.1.8", "8.8.8.8")) // signature mismatch before routing
        expectRejected(invitation("openjarvis-pairing-v2", address = "8.8.8.8"))
        expectRejected(invitation("openjarvis-pairing-v2", address = "127.0.0.1"))
        expectRejected(invitation("openjarvis-pairing-v2", address = "169.254.2.3"))
        expectRejected(invitation("openjarvis-pairing-v2", address = "192.168.001.008"))
        expectRejected(invitation("openjarvis-pairing-v2", serverUrl = "http://thozha-mac.local:8443"))
        expectRejected(invitation("openjarvis-pairing-v2", serverUrl = "https://thozha-mac.local:443"))
        expectRejected(invitation("openjarvis-pairing-v2", gatewayPort = 8445))
        expectRejected(invitation("openjarvis-pairing-v2", loopbackUrl = "http://192.168.1.8:18000"))
        expectRejected(invitation("openjarvis-pairing-v2", certificatePin = "sha256:xyz"))
        expectRejected(invitation("openjarvis-pairing-v2", expiry = Instant.now().minusSeconds(10)))
        expectRejected(invitation("openjarvis-pairing-v2", expiry = Instant.now().plusSeconds(720)))

        val densePayload = invitation("openjarvis-pairing-v2", challenge = "c".repeat(480))
        check(densePayload.length > 1_000) { "Long signed QR fixture is not representative" }
        val bitmap = QRCodeWriter().encode(
            densePayload,
            BarcodeFormat.QR_CODE,
            1_600,
            1_600,
            mapOf(
                EncodeHintType.CHARACTER_SET to "UTF-8",
                EncodeHintType.ERROR_CORRECTION to ErrorCorrectionLevel.M,
            ),
        )
        val pixels = IntArray(bitmap.width * bitmap.height) { index ->
            if (bitmap.get(index % bitmap.width, index / bitmap.width)) 0xff000000.toInt() else -1
        }
        val decoderHints = mapOf(DecodeHintType.TRY_HARDER to true)
        val decoded = MultiFormatReader().decode(
            BinaryBitmap(HybridBinarizer(RGBLuminanceSource(bitmap.width, bitmap.height, pixels))),
            decoderHints,
        ).text
        check(decoded == densePayload) { "ZXing changed a long signed invitation payload" }
        check(PairingInvitationParser.parse(decoded).challenge == "c".repeat(480)) {
            "Long decoded invitation did not reach the production signature/parser path"
        }

        println("Pairing invitation protocol checks passed: v1/v2 validation, tamper/expiry rejection, and ZXing round-trip for a >1KB signed QR payload.")
    }

    private fun invitation(
        protocol: String,
        serverUrl: String? = null,
        address: String = "192.168.1.8",
        gatewayPort: Int = 8443,
        loopbackUrl: String? = "http://127.0.0.1:18000",
        includeLoopback: Boolean = true,
        certificatePin: String = "sha256:" + "a".repeat(64),
        challenge: String = "one-time-challenge-value-123456",
        expiry: Instant = Instant.now().plusSeconds(120),
    ): String {
        val fields = linkedMapOf(
            "protocol" to protocol,
            "server_url" to (serverUrl ?: if (protocol.endsWith("v2")) "https://thozha-mac.local:$gatewayPort" else "http://localhost:8000"),
            "server_fingerprint" to SERVER_FINGERPRINT,
            "server_public_key" to PUBLIC_KEY,
            "invitation_id" to "1".repeat(32),
            "challenge" to challenge,
            "expires_at" to OffsetDateTime.ofInstant(expiry, ZoneOffset.UTC).toString(),
        )
        if (protocol.endsWith("v2")) {
            fields["gateway_address"] = address
            fields["gateway_port"] = gatewayPort.toString()
            fields["gateway_certificate_sha256"] = certificatePin
            if (includeLoopback && loopbackUrl != null) fields["loopback_url"] = loopbackUrl
        }
        val signature = signer().apply {
            update(PairingInvitationSignature.canonicalBytes(fields))
        }.sign()
        return JSONObject(fields as Map<*, *>).put("server_signature", Base64.getEncoder().encodeToString(signature)).toString()
    }

    private fun signer(): Signature {
        val parameters = java.security.AlgorithmParameters.getInstance("EC").apply {
            init(ECGenParameterSpec("secp256r1"))
        }.getParameterSpec(ECParameterSpec::class.java)
        val privateKey = KeyFactory.getInstance("EC").generatePrivate(ECPrivateKeySpec(BigInteger.ONE, parameters))
        return Signature.getInstance("SHA256withECDSA").apply { initSign(privateKey) }
    }

    private fun expectRejected(payload: String) {
        check(runCatching { PairingInvitationParser.parse(payload) }.isFailure) {
            "Invalid or tampered pairing invitation unexpectedly parsed."
        }
    }
}
