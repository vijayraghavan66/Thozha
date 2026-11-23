package com.jarvis.companion.fastpath

/**
 * Selects the supervisor task scope for an explicitly software-building request.
 * This only opens the development planning route; it never grants a write capability.
 */
object DevelopmentIntentClassifier {
    private val buildAction = Regex("\\b(create|build|implement|develop|write|generate)\\b", RegexOption.IGNORE_CASE)
    private val softwareArtifact = Regex(
        "\\b(project|application|app|software|program|script|cli|command[- ]line|website|web app|tracker|library|package)\\b",
        RegexOption.IGNORE_CASE,
    )
    private val explicitNegation = Regex("^\\s*(don't|do not|never|avoid)\\b", RegexOption.IGNORE_CASE)

    fun capabilityScope(objective: String): String {
        val cleaned = objective.trim()
        if (cleaned.isEmpty() || explicitNegation.containsMatchIn(cleaned)) return "assistant"
        return if (buildAction.containsMatchIn(cleaned) && softwareArtifact.containsMatchIn(cleaned)) {
            "development"
        } else {
            "assistant"
        }
    }
}
