package com.jarvis.companion.fastpath

/** Client command parser for intentionally creating a supervisor task. It never grants a write capability. */
object DevelopmentIntentClassifier {
    private val command = Regex("^/task(?:\\s+|$)", RegexOption.IGNORE_CASE)

    data class TaskCommand(val isCommand: Boolean, val objective: String?)

    fun parseTaskCommand(prompt: String): TaskCommand {
        val trimmed = prompt.trimStart()
        val match = command.find(trimmed) ?: return TaskCommand(false, null)
        val objective = trimmed.substring(match.range.last + 1).trim()
        return TaskCommand(true, objective.takeIf { it.isNotBlank() })
    }

    /** Ordinary prose is always assistant scope; only an explicit non-empty /task opts into development. */
    fun capabilityScope(prompt: String): String =
        if (parseTaskCommand(prompt).let { it.isCommand && !it.objective.isNullOrBlank() }) "development" else "assistant"
}
