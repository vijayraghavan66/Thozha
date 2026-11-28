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

/** Display name for the person using the companion; independent from the Thozha brand and assistant name. */
object OwnerNamePolicy {
    const val DEFAULT_NAME = ""
    const val MAX_CODE_POINTS = 32

    /** Empty resets to a neutral greeting; non-empty values must be display-safe and bounded. */
    fun normalize(raw: String): String? {
        val name = raw.trim()
        if (name.codePointCount(0, name.length) > MAX_CODE_POINTS) return null
        if (name.codePoints().anyMatch { Character.isISOControl(it) }) return null
        return name
    }

    fun greeting(raw: String): String = normalize(raw)?.takeIf { it.isNotBlank() }
        ?.let { "Hello, $it" }
        ?: "Hello there"
}
