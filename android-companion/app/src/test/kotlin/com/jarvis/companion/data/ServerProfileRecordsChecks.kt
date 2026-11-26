package com.jarvis.companion.data

/** JVM checks for ciphertext-only profile snapshots used to preserve an existing Mac pairing. */
object ServerProfileRecordsChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        val oldFingerprint = "sha256:" + "a".repeat(64)
        val newFingerprint = "sha256:" + "b".repeat(64)
        val oldFields = mapOf(
            "base_url" to "http://127.0.0.1:8000",
            "device_id" to "device-old-12345678",
            "token_iv" to "encrypted-iv-old",
            "token_ciphertext" to "encrypted-token-old",
            "server_pin_iv" to "encrypted-pin-iv-old",
            "server_pin_ciphertext" to "encrypted-server-pin-old",
            "wifi_pin_iv" to "encrypted-wifi-iv-old",
            "wifi_pin_ciphertext" to "encrypted-wifi-pin-old",
        )
        val newFields = oldFields + mapOf(
            "base_url" to "http://127.0.0.1:18000",
            "device_id" to "device-new-12345678",
            "token_ciphertext" to "encrypted-token-new",
        )
        val encoded = ServerProfileRecords.encode(
            linkedMapOf(
                ServerProfileRecords.id(oldFingerprint) to ServerProfileRecords.record(
                    oldFingerprint, "Production Mac", oldFields,
                ),
                ServerProfileRecords.id(newFingerprint) to ServerProfileRecords.record(
                    newFingerprint, "Portable Thozha", newFields,
                ),
            ),
        )
        val decoded = ServerProfileRecords.parse(encoded)
        check(decoded.size == 2)
        check(decoded[ServerProfileRecords.id(oldFingerprint)]?.fields == oldFields)
        check(decoded[ServerProfileRecords.id(newFingerprint)]?.fields == newFields)
        check(!encoded.contains("device-secret-token"))

        check(runCatching {
            ServerProfileRecords.record(oldFingerprint, "Mac", oldFields + ("token" to "plaintext"))
        }.isFailure)
        check(runCatching {
            ServerProfileRecords.parse(encoded.replace("sha256:", "sha257:", ignoreCase = false))
        }.isFailure)

        val overLimit = (0..ServerProfileRecords.MAX_PROFILES).associate { index ->
            val fp = "sha256:" + index.toString(16).padStart(64, '0')
            ServerProfileRecords.id(fp) to ServerProfileRecords.record(fp, "Mac $index", oldFields)
        }
        check(runCatching { ServerProfileRecords.encode(overLimit) }.isFailure)
        println("Server profile checks passed: encrypted legacy profile retention, new active profile, schema tamper and bounded profile count.")
    }
}
