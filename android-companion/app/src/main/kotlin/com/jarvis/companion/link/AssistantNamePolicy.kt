package com.jarvis.companion.link

/** User-facing personal assistant name. Product branding is configured separately. */
object AssistantNamePolicy {
    const val DEFAULT_NAME = "Assistant"
    const val MAX_CODE_POINTS = 24

    /** Returns a trimmed, display-safe name or null when the input is empty, too long, or contains controls. */
    fun normalize(raw: String): String? {
        val name = raw.trim()
        if (name.isEmpty() || name.codePointCount(0, name.length) > MAX_CODE_POINTS) return null
        if (name.codePoints().anyMatch { Character.isISOControl(it) }) return null
        return name
    }
}
