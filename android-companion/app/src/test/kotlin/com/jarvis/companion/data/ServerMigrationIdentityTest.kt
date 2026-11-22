package com.jarvis.companion.data

import java.math.BigInteger
import java.net.InetAddress
import java.nio.charset.StandardCharsets
import java.security.AlgorithmParameters
import java.security.KeyFactory
import java.security.MessageDigest
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPrivateKeySpec
import java.util.Base64

/**
 * Dependency-free JVM test harness. Run with ./gradlew :app:verifyMigrationIdentity.
 * The fixed QR key/signature are test-only values published by the backend fixture.
 */
object ServerMigrationIdentityTest {
    private const val PUBLIC_KEY_B64 =
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaxfR8uEsQkf4vOblY6RA8ncDfYEt6zOg9KE5RdiYwpZP40Li/hp/m47n60p8D54WK84zV2sxXs7LtkBoN79R9Q=="
    private const val SERVER_FINGERPRINT =
        "sha256:5cd252fb0ce8932436faf8ccd1040981b89ee4ad6b9fe9e2a2b7e71aacb27cd3"

    @JvmStatic
    fun main(args: Array<String>) {
        verifiesFixedBootstrapHmac()
        acceptsOnlyTailscaleDnsRanges()
        verifiesSignedCandidateChallenge()
        rejectsCandidateNonceOriginAndSignatureChanges()
        verifiesPinnedLocalGatewayIdentity()
        verifiesSignedLocalGatewayAddressFallback()
        println("ServerMigrationIdentity fixed-vector checks passed.")
    }

    private fun verifiesFixedBootstrapHmac() {
        val token = "test-device-token"
        val nonce = "nonce/雪"
        val deviceId = "device/1"
        val proof = "24ee8b223165fb0772d51c337d070aefd8e583ae6e05e088b4498db6266244e3"
        val pin = ServerMigrationIdentity.verifyBootstrap(
            bearerToken = token,
            expectedDeviceId = deviceId,
            expectedNonce = nonce,
            responseDeviceId = deviceId,
            responseNonce = nonce,
            serverPublicKeyBase64 = PUBLIC_KEY_B64,
            serverFingerprint = SERVER_FINGERPRINT,
            proofHmacHex = proof,
        )
        assertEquals(SERVER_FINGERPRINT, pin.fingerprint)
        assertBytesEqual(Base64.getDecoder().decode(PUBLIC_KEY_B64), pin.publicKeyDer)

        val failure = expectFailure {
            ServerMigrationIdentity.verifyBootstrap(
                bearerToken = token,
                expectedDeviceId = deviceId,
                expectedNonce = nonce,
                responseDeviceId = deviceId,
                responseNonce = nonce,
                serverPublicKeyBase64 = PUBLIC_KEY_B64,
                serverFingerprint = SERVER_FINGERPRINT,
                proofHmacHex = proof.dropLast(1) + "0",
            )
        }
        assertEquals(ServerMigrationIdentity.Failure.INVALID_HMAC, failure.reason)
    }

    private fun acceptsOnlyTailscaleDnsRanges() {
        val target = ServerMigrationIdentity.validateTailscaleTarget(
            "https://mac.tailnet.ts.net",
        ) {
            listOf(
                InetAddress.getByName("100.64.10.20"),
                InetAddress.getByName("fd7a:115c:a1e0::20"),
            )
        }
        assertEquals("https://mac.tailnet.ts.net", target.baseUrl)
        assertEquals(target.baseUrl, target.origin)

        expectIllegalArgument {
            ServerMigrationIdentity.validateTailscaleTarget("https://mac.tailnet.ts.net") {
                listOf(InetAddress.getByName("100.64.10.20"), InetAddress.getByName("8.8.8.8"))
            }
        }
        expectIllegalArgument {
            ServerMigrationIdentity.validateTailscaleTarget("https://mac.local") {
                listOf(InetAddress.getByName("100.64.10.20"))
            }
        }
        expectIllegalArgument {
            ServerMigrationIdentity.validateTailscaleTarget("http://mac.tailnet.ts.net") {
                listOf(InetAddress.getByName("100.64.10.20"))
            }
        }
    }

    private fun verifiesSignedCandidateChallenge() {
        val publicDer = Base64.getDecoder().decode(PUBLIC_KEY_B64)
        val pin = PinnedServerIdentity(publicDer, SERVER_FINGERPRINT)
        val nonce = "migration-nonce/雪"
        val origin = "https://mac.tailnet.ts.net"
        val fields = identityFields(nonce, origin)
        val signature = signWithFixtureKey(fields)
        assertEquals(
            null,
            ServerMigrationIdentity.verifyIdentityChallenge(
                pinned = pin,
                expectedNonce = nonce,
                expectedOrigin = origin,
                protocol = fields.getValue("protocol"),
                responseNonce = nonce,
                responseOrigin = origin,
                serverPublicKeyBase64 = PUBLIC_KEY_B64,
                serverFingerprint = SERVER_FINGERPRINT,
                signatureBase64 = signature,
            ),
        )
    }

    private fun rejectsCandidateNonceOriginAndSignatureChanges() {
        val pin = PinnedServerIdentity(Base64.getDecoder().decode(PUBLIC_KEY_B64), SERVER_FINGERPRINT)
        val nonce = "migration-nonce/雪"
        val origin = "https://mac.tailnet.ts.net"
        val fields = identityFields(nonce, origin)
        val signature = signWithFixtureKey(fields)
        assertEquals(
            ServerMigrationIdentity.Failure.NONCE_MISMATCH,
            verify(pin, nonce, origin, responseNonce = "replayed-nonce", signature = signature),
        )
        assertEquals(
            ServerMigrationIdentity.Failure.TARGET_ORIGIN_MISMATCH,
            verify(pin, nonce, origin, responseOrigin = "https://other.tailnet.ts.net", signature = signature),
        )
        val changedFields = fields + ("target_origin" to "https://changed.tailnet.ts.net")
        assertEquals(
            ServerMigrationIdentity.Failure.SIGNATURE_MISMATCH,
            verify(pin, nonce, origin, signature = signWithFixtureKey(changedFields)),
        )
        val badFingerprint = "sha256:" + "0".repeat(64)
        assertEquals(
            ServerMigrationIdentity.Failure.FINGERPRINT_MISMATCH,
            ServerMigrationIdentity.verifyIdentityChallenge(
                pin, nonce, origin, fields.getValue("protocol"), nonce, origin,
                PUBLIC_KEY_B64, badFingerprint, signature,
            ),
        )
    }

    private fun verifiesPinnedLocalGatewayIdentity() {
        val pin = PinnedServerIdentity(Base64.getDecoder().decode(PUBLIC_KEY_B64), SERVER_FINGERPRINT)
        val nonce = "wifi-gateway-nonce-20261116"
        val fields = linkedMapOf(
            "protocol" to "openjarvis-local-gateway-identity-v1",
            "nonce" to nonce,
            "hostname" to "jarvis-mac.local",
            "port" to "8443",
            "certificate_sha256" to "sha256:" + "d".repeat(64),
            "server_public_key" to PUBLIC_KEY_B64,
            "server_fingerprint" to SERVER_FINGERPRINT,
        )
        val signature = signWithFixtureKey(fields)
        val verified = ServerMigrationIdentity.verifyLocalGatewayIdentity(
            pinned = pin,
            expectedNonce = nonce,
            protocol = fields.getValue("protocol"),
            responseNonce = nonce,
            hostname = fields.getValue("hostname"),
            portText = fields.getValue("port"),
            certificateSha256 = fields.getValue("certificate_sha256"),
            serverPublicKeyBase64 = PUBLIC_KEY_B64,
            serverFingerprint = SERVER_FINGERPRINT,
            signatureBase64 = signature,
        )
        assertEquals("jarvis-mac.local", verified.hostname)
        assertEquals(8443, verified.port)
        assertEquals("sha256:" + "d".repeat(64), verified.certificateSha256)

        assertEquals(
            ServerMigrationIdentity.Failure.NONCE_MISMATCH,
            expectFailure {
                ServerMigrationIdentity.verifyLocalGatewayIdentity(
                    pin, nonce, fields.getValue("protocol"), "replayed", "jarvis-mac.local", "8443",
                    fields.getValue("certificate_sha256"), PUBLIC_KEY_B64, SERVER_FINGERPRINT, signature,
                )
            }.reason,
        )
        assertEquals(
            ServerMigrationIdentity.Failure.GATEWAY_HOST_MISMATCH,
            expectFailure {
                ServerMigrationIdentity.verifyLocalGatewayIdentity(
                    pin, nonce, fields.getValue("protocol"), nonce, "attacker.example", "8443",
                    fields.getValue("certificate_sha256"), PUBLIC_KEY_B64, SERVER_FINGERPRINT, signature,
                )
            }.reason,
        )
        assertEquals(
            ServerMigrationIdentity.Failure.GATEWAY_CERT_PIN_INVALID,
            expectFailure {
                ServerMigrationIdentity.verifyLocalGatewayIdentity(
                    pin, nonce, fields.getValue("protocol"), nonce, "jarvis-mac.local", "8443",
                    "sha256:bad", PUBLIC_KEY_B64, SERVER_FINGERPRINT, signature,
                )
            }.reason,
        )
    }

    private fun verifiesSignedLocalGatewayAddressFallback() {
        val pinned = PinnedServerIdentity(Base64.getDecoder().decode(PUBLIC_KEY_B64), SERVER_FINGERPRINT)
        val pin = LocalGatewayPin("jarvis-mac.local", 8443, "sha256:" + "d".repeat(64))
        val nonce = "wifi-address-nonce-20261122"
        val fields = linkedMapOf(
            "protocol" to "openjarvis-local-gateway-address-v1",
            "nonce" to nonce,
            "hostname" to pin.hostname,
            "port" to pin.port.toString(),
            "address" to "192.168.1.8",
            "certificate_sha256" to pin.certificateSha256,
            "server_public_key" to PUBLIC_KEY_B64,
            "server_fingerprint" to SERVER_FINGERPRINT,
        )
        val signature = signWithFixtureKey(fields)
        assertEquals(
            "192.168.1.8",
            ServerMigrationIdentity.verifyLocalGatewayAddress(
                pinned, pin, nonce, fields.getValue("protocol"), nonce, pin.hostname, "8443",
                "192.168.1.8", pin.certificateSha256, PUBLIC_KEY_B64, SERVER_FINGERPRINT, signature,
            ),
        )
        assertEquals(
            ServerMigrationIdentity.Failure.GATEWAY_ADDRESS_INVALID,
            expectFailure {
                ServerMigrationIdentity.verifyLocalGatewayAddress(
                    pinned, pin, nonce, fields.getValue("protocol"), nonce, pin.hostname, "8443",
                    "8.8.8.8", pin.certificateSha256, PUBLIC_KEY_B64, SERVER_FINGERPRINT, signature,
                )
            }.reason,
        )
        assertEquals(
            ServerMigrationIdentity.Failure.NONCE_MISMATCH,
            expectFailure {
                ServerMigrationIdentity.verifyLocalGatewayAddress(
                    pinned, pin, nonce, fields.getValue("protocol"), "replayed", pin.hostname, "8443",
                    "192.168.1.8", pin.certificateSha256, PUBLIC_KEY_B64, SERVER_FINGERPRINT, signature,
                )
            }.reason,
        )
        assertEquals(
            ServerMigrationIdentity.Failure.SIGNATURE_MISMATCH,
            expectFailure {
                ServerMigrationIdentity.verifyLocalGatewayAddress(
                    pinned, pin, nonce, fields.getValue("protocol"), nonce, pin.hostname, "8443",
                    "192.168.1.99", pin.certificateSha256, PUBLIC_KEY_B64, SERVER_FINGERPRINT,
                    signature,
                )
            }.reason,
        )
    }

    private fun verify(
        pin: PinnedServerIdentity,
        nonce: String,
        origin: String,
        protocol: String = "openjarvis-identity-challenge-v1",
        responseNonce: String = nonce,
        responseOrigin: String = origin,
        signature: String,
    ): ServerMigrationIdentity.Failure? =
        ServerMigrationIdentity.verifyIdentityChallenge(
            pin,
            nonce,
            origin,
            protocol,
            responseNonce,
            responseOrigin,
            PUBLIC_KEY_B64,
            SERVER_FINGERPRINT,
            signature,
        )

    private fun identityFields(nonce: String, origin: String): Map<String, String> = linkedMapOf(
        "protocol" to "openjarvis-identity-challenge-v1",
        "nonce" to nonce,
        "target_origin" to origin,
        "server_public_key" to PUBLIC_KEY_B64,
        "server_fingerprint" to SERVER_FINGERPRINT,
    )

    /** Test-only d=1 P-256 private key matching the backend fixture's public key. */
    private fun signWithFixtureKey(fields: Map<String, String>): String {
        val parameters = AlgorithmParameters.getInstance("EC").apply {
            init(ECGenParameterSpec("secp256r1"))
        }.getParameterSpec(ECParameterSpec::class.java)
        val privateKey = KeyFactory.getInstance("EC").generatePrivate(
            ECPrivateKeySpec(BigInteger.ONE, parameters),
        )
        val signer = Signature.getInstance("SHA256withECDSA")
        signer.initSign(privateKey)
        signer.update(PairingInvitationSignature.canonicalBytes(fields))
        return Base64.getEncoder().encodeToString(signer.sign())
    }

    private fun expectFailure(block: () -> Unit): ServerMigrationIdentity.VerificationException {
        try {
            block()
        } catch (failure: ServerMigrationIdentity.VerificationException) {
            return failure
        }
        error("Expected a server-identity verification failure.")
    }

    private fun expectIllegalArgument(block: () -> Unit) {
        try {
            block()
        } catch (_: IllegalArgumentException) {
            return
        }
        error("Expected an invalid Tailscale target to be rejected.")
    }

    private fun assertEquals(expected: Any?, actual: Any?) {
        check(expected == actual) { "Expected $expected but got $actual" }
    }

    private fun assertBytesEqual(expected: ByteArray, actual: ByteArray) {
        check(MessageDigest.isEqual(expected, actual)) { "Byte values differ." }
    }
}
