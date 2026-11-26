package com.jarvis.companion.data

import org.json.JSONObject

/** Ciphertext-only snapshot format for adding a second paired Mac without replacing the first. */
internal object ServerProfileRecords {
    const val MAX_PROFILES = 8
    private const val VERSION = 1
    private val fingerprintPattern = Regex("sha256:[0-9a-f]{64}")
    private val allowedFields = setOf(
        "base_url",
        "device_id",
        "token_iv",
        "token_ciphertext",
        "server_pin_iv",
        "server_pin_ciphertext",
        "wifi_pin_iv",
        "wifi_pin_ciphertext",
    )

    data class Record(
        val fingerprint: String,
        val label: String,
        val fields: Map<String, String>,
    )

    fun emptyJson(): String = JSONObject()
        .put("version", VERSION)
        .put("profiles", JSONObject())
        .toString()

    fun parse(raw: String?): LinkedHashMap<String, Record> {
        if (raw.isNullOrBlank()) return linkedMapOf()
        val root = try { JSONObject(raw) } catch (_: Exception) {
            throw SecurityException("Saved server profile index is malformed.")
        }
        if (root.optInt("version", -1) != VERSION) {
            throw SecurityException("Saved server profile index version is unsupported.")
        }
        val entries = root.optJSONObject("profiles")
            ?: throw SecurityException("Saved server profile index is incomplete.")
        val result = linkedMapOf<String, Record>()
        val keys = entries.keys()
        while (keys.hasNext()) {
            val id = keys.next()
            if (!id.matches(Regex("[0-9a-f]{64}"))) {
                throw SecurityException("Saved server profile identifier is malformed.")
            }
            val json = entries.optJSONObject(id)
                ?: throw SecurityException("Saved server profile is malformed.")
            val fingerprint = json.optString("fingerprint")
            if (!fingerprintPattern.matches(fingerprint) || fingerprint.removePrefix("sha256:") != id) {
                throw SecurityException("Saved server profile fingerprint is inconsistent.")
            }
            val label = json.optString("label")
            if (label.isBlank() || label.length > 256) {
                throw SecurityException("Saved server profile label is malformed.")
            }
            val fieldsJson = json.optJSONObject("fields")
                ?: throw SecurityException("Saved server profile fields are missing.")
            val fields = linkedMapOf<String, String>()
            val fieldKeys = fieldsJson.keys()
            while (fieldKeys.hasNext()) {
                val field = fieldKeys.next()
                if (field !in allowedFields) {
                    throw SecurityException("Saved server profile contains an unsupported field.")
                }
                val value = fieldsJson.opt(field)
                if (value !is String || value.length > 16_384) {
                    throw SecurityException("Saved server profile field is malformed.")
                }
                fields[field] = value
            }
            if (!fields.containsKey("server_pin_iv") || !fields.containsKey("server_pin_ciphertext")) {
                throw SecurityException("Saved server profile has no authenticated host pin.")
            }
            if (fields.containsKey("token_iv") != fields.containsKey("token_ciphertext")) {
                throw SecurityException("Saved server profile credential is incomplete.")
            }
            if (fields.containsKey("wifi_pin_iv") != fields.containsKey("wifi_pin_ciphertext")) {
                throw SecurityException("Saved server profile Wi-Fi pin is incomplete.")
            }
            result[id] = Record(fingerprint, label, fields)
        }
        if (result.size > MAX_PROFILES) throw SecurityException("Saved server profile limit exceeded.")
        return result
    }

    fun encode(records: Map<String, Record>): String {
        if (records.size > MAX_PROFILES) throw SecurityException("Saved server profile limit exceeded.")
        val entries = JSONObject()
        records.forEach { (id, record) ->
            if (!id.matches(Regex("[0-9a-f]{64}")) || id != record.fingerprint.removePrefix("sha256:")) {
                throw SecurityException("Saved server profile identifier is inconsistent.")
            }
            val fields = JSONObject()
            record.fields.forEach { (key, value) ->
                if (key !in allowedFields || value.length > 16_384) {
                    throw SecurityException("Saved server profile field is invalid.")
                }
                fields.put(key, value)
            }
            entries.put(
                id,
                JSONObject()
                    .put("fingerprint", record.fingerprint)
                    .put("label", record.label.take(256))
                    .put("fields", fields),
            )
        }
        return JSONObject().put("version", VERSION).put("profiles", entries).toString()
    }

    fun record(fingerprint: String, label: String, fields: Map<String, String>): Record {
        if (!fingerprintPattern.matches(fingerprint) || label.isBlank()) {
            throw SecurityException("Server profile identity is invalid.")
        }
        if (fields.keys.any { it !in allowedFields }) {
            throw SecurityException("Server profile contains an unsupported field.")
        }
        return Record(fingerprint, label.take(256), fields.toMap())
    }

    fun id(fingerprint: String): String {
        if (!fingerprintPattern.matches(fingerprint)) throw SecurityException("Server profile fingerprint is malformed.")
        return fingerprint.removePrefix("sha256:")
    }
}

data class ServerProfileSummary(
    val fingerprint: String,
    val label: String,
    val active: Boolean,
)
