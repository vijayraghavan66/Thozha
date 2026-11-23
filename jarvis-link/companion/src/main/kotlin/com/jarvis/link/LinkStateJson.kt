package com.jarvis.link

/** Stable JSON form of a DeviceCertificate, used for local persistence on the companion. */
object LinkStateJson {
    fun certificateToMap(c: DeviceCertificate): Map<String, Any> = linkedMapOf(
        "v" to c.version.toLong(),
        "tenant_id" to c.tenantId,
        "device_id" to c.deviceId,
        "device_role" to c.deviceRole,
        "signing_public_key_b64url" to c.signingPublicKeyBase64Url,
        "encryption_public_key_b64url" to c.encryptionPublicKeyBase64Url,
        "not_before_unix_ms" to c.notBeforeUnixMs,
        "expires_at_unix_ms" to c.expiresAtUnixMs,
        "certificate_id" to c.certificateId,
        "issuer_device_id" to c.issuerDeviceId,
        "signature_b64url" to c.signatureBase64Url,
    )

    fun certificateFromMap(f: Map<String, Any>): DeviceCertificate = DeviceCertificate(
        version = f["v"].toString(),
        tenantId = f.string("tenant_id"),
        deviceId = f.string("device_id"),
        deviceRole = f.string("device_role"),
        signingPublicKeyBase64Url = f.string("signing_public_key_b64url"),
        encryptionPublicKeyBase64Url = f.string("encryption_public_key_b64url"),
        notBeforeUnixMs = f.long("not_before_unix_ms"),
        expiresAtUnixMs = f.long("expires_at_unix_ms"),
        certificateId = f.string("certificate_id"),
        issuerDeviceId = f.string("issuer_device_id"),
        signatureBase64Url = f.string("signature_b64url"),
    )
}
