package com.jarvis.companion.fastpath

/** Narrow, deterministic commands that may use a typed Mac protocol instead of natural language AI. */
sealed interface FastCommand {
    data object MacStatus : FastCommand
    data class TaskStatus(val taskId: String) : FastCommand
    data class OpenApplication(val appId: AllowedMacApplication) : FastCommand
}

enum class AllowedMacApplication(val canonicalName: String) {
    CALCULATOR("Calculator")
}

/** Returns null unless the entire normalized request is an exact supported command. */
object FastCommandClassifier {
    private val taskStatus = Regex("(?:status|show status) for task ([0-9a-f]{32})", RegexOption.IGNORE_CASE)

    fun classify(request: String): FastCommand? {
        if (request.toByteArray(Charsets.UTF_8).size > 256) return null
        val normalized = request.trim().lowercase().replace(Regex("\\s+"), " ")
        return when {
            normalized in setOf("is my mac online", "check mac status", "is jarvis online") -> FastCommand.MacStatus
            normalized == "open calculator" || normalized == "open the calculator" ->
                FastCommand.OpenApplication(AllowedMacApplication.CALCULATOR)
            else -> taskStatus.matchEntire(normalized)?.groupValues?.get(1)?.let(FastCommand::TaskStatus)
        }
    }
}
