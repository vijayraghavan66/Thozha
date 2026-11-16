package com.jarvis.link

import java.nio.charset.StandardCharsets.UTF_8

/** Host-signed certificate for another device; relay public keys are never trusted directly. */
data class DeviceCertificate(
    val version: String,
    val tenantId: String,
    val deviceId: String,
    val deviceRole: String,
    val signingPublicKeyBase64Url: String,
    val encryptionPublicKeyBase64Url: String,
    val notBeforeUnixMs: Long,
    val expiresAtUnixMs: Long,
    val certificateId: String,
    val issuerDeviceId: String,
    val signatureBase64Url: String,
) {
    fun signingBytes(): ByteArray = listOf(
        "JARVIS-LINK-DEVICE-CERT-V1",
        version,
        tenantId,
        deviceId,
        deviceRole,
        signingPublicKeyBase64Url,
        encryptionPublicKeyBase64Url,
        notBeforeUnixMs.toString(),
        expiresAtUnixMs.toString(),
        certificateId,
        issuerDeviceId,
    ).joinToString("\n").toByteArray(UTF_8)

    fun verify(pinnedHostRoot: TrustedHostRoot, expectedTenantId: String, nowUnixMs: Long): Boolean {
        if (version != "1" || tenantId != expectedTenantId || issuerDeviceId != pinnedHostRoot.deviceId) return false
        if (deviceRole !in setOf("host", "companion")) return false
        if (!certificateId.matches(Regex("^[0-9a-f]{32}$")) || !deviceId.matches(Regex("^[0-9a-f]{32}$"))) return false
        if (notBeforeUnixMs > nowUnixMs || expiresAtUnixMs <= nowUnixMs || expiresAtUnixMs <= notBeforeUnixMs) return false
        if (expiresAtUnixMs - notBeforeUnixMs > MAX_CERT_LIFETIME_MS) return false
        return runCatching {
            requireP256(signingPublicKeyBase64Url)
            requireP256(encryptionPublicKeyBase64Url)
            val derivedDeviceId = LinkCrypto.deviceIdForSigningPublicKey(
                LinkCrypto.decodeBase64Url(signingPublicKeyBase64Url),
            )
            require(deviceId == derivedDeviceId) { "Device ID does not match signing SPKI" }
            LinkCrypto.verify(
                LinkCrypto.decodePublicKey(pinnedHostRoot.signingPublicKeyBase64Url),
                signingBytes(),
                signatureBase64Url,
            )
        }.getOrDefault(false)
    }

    private fun requireP256(publicKeyBase64Url: String) {
        val key = LinkCrypto.decodePublicKey(publicKeyBase64Url) as? java.security.interfaces.ECPublicKey
            ?: error("Device cert key is not EC")
        require(key.params.curve.field.fieldSize == 256) { "Device cert key must be P-256" }
    }

    companion object {
        const val MAX_CERT_LIFETIME_MS = 365L * 24 * 60 * 60 * 1000

        fun fromMap(fields: Map<String, String>): DeviceCertificate {
            val required = setOf(
                "v", "tenant_id", "device_id", "device_role", "signing_public_key_b64url",
                "encryption_public_key_b64url", "not_before_unix_ms", "expires_at_unix_ms",
                "certificate_id", "issuer_device_id", "signature_b64url",
            )
            require(fields.keys == required) { "Device certificate fields are incomplete or unexpected" }
            return DeviceCertificate(
                version = fields.getValue("v"),
                tenantId = fields.getValue("tenant_id"),
                deviceId = fields.getValue("device_id"),
                deviceRole = fields.getValue("device_role"),
                signingPublicKeyBase64Url = fields.getValue("signing_public_key_b64url"),
                encryptionPublicKeyBase64Url = fields.getValue("encryption_public_key_b64url"),
                notBeforeUnixMs = fields.getValue("not_before_unix_ms").toLong(),
                expiresAtUnixMs = fields.getValue("expires_at_unix_ms").toLong(),
                certificateId = fields.getValue("certificate_id"),
                issuerDeviceId = fields.getValue("issuer_device_id"),
                signatureBase64Url = fields.getValue("signature_b64url"),
            )
        }
    }
}

/** Public root key is pinned by an out-of-band/local pairing flow or test fixture. */
data class TrustedHostRoot(val deviceId: String, val signingPublicKeyBase64Url: String)

