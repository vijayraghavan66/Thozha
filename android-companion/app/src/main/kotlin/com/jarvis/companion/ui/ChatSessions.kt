package com.jarvis.companion.ui

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/** One conversation. Its messages are the request records LinkRuntime already keeps, grouped by request ID. */
data class ChatSession(val id: String, val createdMs: Long, val title: String)

/**
 * Persistent grouping of requests into conversations ("New chat" starts one, "History" reopens one).
 * Only session IDs, titles and request-to-session links are stored here. Message text stays in LinkRuntime.
 * Requests recorded before sessions existed belong to the earliest session, so no history is hidden.
 */
class ChatSessions(context: Context) {
    private val prefs = context.getSharedPreferences("jarvis_chat_sessions", Context.MODE_PRIVATE)
    private var state: JSONObject = try {
        JSONObject(prefs.getString("state", null) ?: "{}")
    } catch (_: Exception) {
        JSONObject()
    }

    @Synchronized
    fun all(): List<ChatSession> {
        val array = state.optJSONArray("sessions") ?: JSONArray()
        return (0 until array.length()).map { index ->
            val item = array.getJSONObject(index)
            ChatSession(item.getString("id"), item.getLong("created"), item.optString("title"))
        }.sortedBy { it.createdMs }
    }

    /** The active conversation, created on first use. */
    @Synchronized
    fun active(): ChatSession {
        val sessions = all()
        val activeId = state.optString("active")
        sessions.firstOrNull { it.id == activeId }?.let { return it }
        return create()
    }

    @Synchronized
    fun create(): ChatSession {
        val session = ChatSession(UUID.randomUUID().toString().replace("-", ""), System.currentTimeMillis(), "")
        val array = state.optJSONArray("sessions") ?: JSONArray()
        array.put(JSONObject().put("id", session.id).put("created", session.createdMs).put("title", session.title))
        state.put("sessions", array).put("active", session.id)
        save()
        return session
    }

    @Synchronized
    fun open(id: String) {
        if (all().any { it.id == id }) {
            state.put("active", id)
            save()
        }
    }

    @Synchronized
    fun setTitleIfBlank(id: String, text: String) {
        val array = state.optJSONArray("sessions") ?: return
        for (index in 0 until array.length()) {
            val item = array.getJSONObject(index)
            if (item.getString("id") == id && item.optString("title").isBlank()) {
                item.put("title", text.trim().lineSequence().first().take(60))
                save()
                return
            }
        }
    }

    @Synchronized
    fun assign(requestId: String, sessionId: String) {
        val map = state.optJSONObject("requests") ?: JSONObject().also { state.put("requests", it) }
        map.put(requestId, sessionId)
        save()
    }

    /** The conversation a request belongs to. Unassigned requests fall back to the earliest conversation. */
    @Synchronized
    fun sessionOf(requestId: String): String? {
        state.optJSONObject("requests")?.optString(requestId)?.takeIf { it.isNotEmpty() }?.let { return it }
        return all().firstOrNull()?.id
    }

    private fun save() {
        prefs.edit().putString("state", state.toString()).apply()
    }
}
