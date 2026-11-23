package com.jarvis.companion.voice

/** Explicit Mac actions and software-build requests use the existing supervisor-backed submit path. */
object MacVoiceRequestRouting {
    private val macTarget = Regex("\\b(?:on|from|using)\\s+(?:my\\s+)?(?:mac|macbook|desktop|computer)\\b", RegexOption.IGNORE_CASE)
    private val action = Regex("\\b(?:open|launch|start|run|create|write|build|implement|fix|test|delete|install|update|restart|shutdown)\\b", RegexOption.IGNORE_CASE)
    private val imperative = Regex("^\\s*(?:please\\s+)?(?:open|launch|start|run|create|write|build|implement|fix|test|delete|install|update|restart|shutdown)\\b", RegexOption.IGNORE_CASE)
    private val softwareTarget = Regex("\\b(?:code|script|project|website|landing\\s+page|web\\s+app|android\\s+app|repository|repo|tests?|task\\s+manager|program|html|css|javascript)\\b", RegexOption.IGNORE_CASE)

    fun shouldSubmitThroughSupervisor(text: String): Boolean =
        text.isNotBlank() && action.containsMatchIn(text) &&
            (macTarget.containsMatchIn(text) || (imperative.containsMatchIn(text) && softwareTarget.containsMatchIn(text)))
}
