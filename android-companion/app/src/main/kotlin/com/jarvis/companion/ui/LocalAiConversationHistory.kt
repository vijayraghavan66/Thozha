package com.jarvis.companion.ui

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/** Small durable store for on-device AI turns, kept separate from the Mac/relay task database. */
internal class LocalAiConversationHistory(context: Context) {
    private val preferences = context.getSharedPreferences("jarvis_local_ai_history", Context.MODE_PRIVATE)

    init { recoverInterruptedRequests() }

    @Synchronized fun all(): List<Map<String, Any>> = runCatching {
        val array = JSONArray(preferences.getString("entries", "[]"))
        (0 until array.length()).map { index ->
            val item = array.getJSONObject(index)
            buildMap<String, Any> {
                put("message_id", item.getString("message_id"))
                put("text", item.optString("text"))
                put("state", item.optString("state", "FAILED"))
                put("summary", item.optString("summary"))
                put("body", item.optString("body"))
                put("transport", "local")
                put("body_integrity_failed", false)
                put("created_at_unix_ms", item.optLong("created_at_unix_ms"))
                put("updated_at_unix_ms", item.optLong("updated_at_unix_ms"))
            }
        }
    }.getOrDefault(emptyList())

    @Synchronized fun recordRequest(id: String, text: String, now: Long) {
        upsert(id) { previous ->
            previous.put("message_id", id)
                .put("text", text)
                .put("state", "RUNNING")
                .put("summary", "Answering on this device…")
                .put("body", "")
                .put("created_at_unix_ms", previous.optLong("created_at_unix_ms", now))
                .put("updated_at_unix_ms", now)
        }
    }

    @Synchronized fun recordResult(id: String, state: String, body: String, summary: String, now: Long) {
        require(state in setOf("COMPLETED", "FAILED", "CANCELLED"))
        upsert(id) { previous ->
            previous.put("state", state)
                .put("summary", summary.take(400))
                .put("body", body.take(MAX_BODY_CHARS))
                .put("updated_at_unix_ms", now)
        }
    }

    @Synchronized fun remove(id: String) {
        val existing = runCatching { JSONArray(preferences.getString("entries", "[]")) }.getOrDefault(JSONArray())
        val next = JSONArray()
        for (index in 0 until existing.length()) {
            val row = existing.optJSONObject(index) ?: continue
            if (row.optString("message_id") != id) next.put(row)
        }
        check(preferences.edit().putString("entries", next.toString()).commit()) {
            "Local conversation history could not be updated."
        }
    }

    private fun upsert(id: String, update: (JSONObject) -> JSONObject) {
        val existing = runCatching { JSONArray(preferences.getString("entries", "[]")) }.getOrDefault(JSONArray())
        var next = JSONArray()
        var found = false
        for (index in 0 until existing.length()) {
            val row = existing.optJSONObject(index) ?: continue
            if (row.optString("message_id") == id) {
                next.put(update(row))
                found = true
            } else next.put(row)
        }
        if (!found) next.put(update(JSONObject()))
        while (next.length() > MAX_RECORDS) {
            val trimmed = JSONArray()
            for (index in (next.length() - MAX_RECORDS) until next.length()) trimmed.put(next.get(index))
            next = trimmed
        }
        check(preferences.edit().putString("entries", next.toString()).commit()) {
            "Local conversation history could not be committed."
        }
    }

    @Synchronized private fun recoverInterruptedRequests() {
        val existing = runCatching { JSONArray(preferences.getString("entries", "[]")) }.getOrDefault(JSONArray())
        var changed = false
        val now = System.currentTimeMillis()
        for (index in 0 until existing.length()) {
            val row = existing.optJSONObject(index) ?: continue
            if (row.optString("state") == "RUNNING") {
                row.put("state", "FAILED")
                    .put("summary", "On-device inference stopped before this app session completed.")
                    .put("body", "")
                    .put("updated_at_unix_ms", now)
                changed = true
            }
        }
        if (changed) check(preferences.edit().putString("entries", existing.toString()).commit()) {
            "Interrupted local task recovery could not be committed."
        }
    }

    private companion object {
        const val MAX_RECORDS = 200
        const val MAX_BODY_CHARS = 32_768
    }
}
