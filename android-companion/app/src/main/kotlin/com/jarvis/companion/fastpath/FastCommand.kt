package com.jarvis.companion.fastpath

/** Narrow, deterministic commands that may use a typed Mac protocol instead of natural language AI. */
sealed interface FastCommand {
    data object MacStatus : FastCommand
    data object MacBatteryStatus : FastCommand
    data class TaskStatus(val taskId: String) : FastCommand
    data class OpenApplication(val appId: AllowedMacApplication) : FastCommand
}

enum class AllowedMacApplication(val bundleId: String, val displayName: String) {
    CALCULATOR("com.apple.calculator", "Calculator"),
    SPOTIFY("com.spotify.client", "Spotify"),
}

/** Returns null unless the entire normalized request is an exact supported command. */
object FastCommandClassifier {
    private val taskStatus = Regex("(?:status|show status) for task ([0-9a-f]{32})", RegexOption.IGNORE_CASE)

    fun classify(request: String): FastCommand? {
        if (request.toByteArray(Charsets.UTF_8).size > 256) return null
        val normalized = request.trim().lowercase().replace(Regex("\\s+"), " ").removeSuffix("?").removeSuffix(".").trim()
        return when {
            normalized in setOf("is my mac online", "check mac status", "is jarvis online") -> FastCommand.MacStatus
            normalized in setOf(
                "what's my mac's battery level",
                "whats my macs battery level",
                "what is my mac's battery level",
                "what is my mac battery level",
                "battery level on my mac",
            ) -> FastCommand.MacBatteryStatus
            normalized in setOf("open calculator", "open the calculator", "open calculator on my mac") ->
                FastCommand.OpenApplication(AllowedMacApplication.CALCULATOR)
            normalized in setOf("open spotify", "open spotify on my mac", "launch spotify", "launch spotify on my mac") ->
                FastCommand.OpenApplication(AllowedMacApplication.SPOTIFY)
            else -> taskStatus.matchEntire(normalized)?.groupValues?.get(1)?.let(FastCommand::TaskStatus)
        }
    }
}
