package com.jarvis.companion.localai

/** Explicit policy chosen by the user; AUTO is conservative and never grants tools. */
enum class LocalAiRequestMode { AUTO, LOCAL_ONLY, MAC_ONLY }

sealed interface RequestRoute {
    val requestId: String
    data class Local(override val requestId: String, val reason: String) : RequestRoute
    data class Mac(override val requestId: String, val reason: String) : RequestRoute
    /** The exact-command classifier and its authorization boundary own this path. */
    data class FastCommandUpstream(override val requestId: String) : RequestRoute
    data class Rejected(override val requestId: String, val code: RouteError, val message: String) : RequestRoute
}

enum class RouteError { INVALID_REQUEST, LOCAL_MODEL_UNAVAILABLE, LOCAL_CANNOT_HANDLE, LOCAL_INFERENCE_FAILED }

/**
 * Pure, deterministic request routing. It never executes a tool or silently changes an explicit mode.
 * The caller must run the exact supported-command classifier first and set [isExactFastCommand].
 */
object RequestRoutingPolicy {
    private const val MAX_PROMPT_BYTES = 4096
    private const val MAX_AUTO_LOCAL_BYTES = 512

    private val macOnlyPatterns = listOf(
        Regex("\\b(create|build|code|debug|compile|run|execute|open|launch|close|delete|write|edit|modify|install|browse|search the web|research|multi[- ]step|project|file|send|cancel|approve|connect|settings)\\b", RegexOption.IGNORE_CASE),
        Regex("```|\\b(stack trace|pull request|root cause|deployment|repository|terminal|shell)\\b", RegexOption.IGNORE_CASE),
    )

    fun route(
        requestId: String,
        prompt: String,
        mode: LocalAiRequestMode,
        localModelReady: Boolean,
        isExactFastCommand: Boolean = false,
    ): RequestRoute {
        if (!requestId.matches(Regex("^[A-Za-z0-9._~-]{16,128}$"))) {
            return RequestRoute.Rejected(requestId, RouteError.INVALID_REQUEST, "Request ID is invalid.")
        }
        val cleaned = prompt.trim()
        if (cleaned.isEmpty() || cleaned.toByteArray(Charsets.UTF_8).size > MAX_PROMPT_BYTES) {
            return RequestRoute.Rejected(requestId, RouteError.INVALID_REQUEST, "Request must be 1–4096 UTF-8 bytes.")
        }
        if (isExactFastCommand) return RequestRoute.FastCommandUpstream(requestId)

        val localCapable = isSimplePrompt(cleaned)
        return when (mode) {
            LocalAiRequestMode.MAC_ONLY -> RequestRoute.Mac(requestId, "Mac AI was explicitly selected.")
            LocalAiRequestMode.LOCAL_ONLY -> when {
                !localModelReady -> RequestRoute.Rejected(requestId, RouteError.LOCAL_MODEL_UNAVAILABLE, "Local AI is selected, but no verified local model is ready.")
                !localCapable -> RequestRoute.Rejected(requestId, RouteError.LOCAL_CANNOT_HANDLE, "This request needs Mac AI; Local-only mode will not fall back.")
                else -> RequestRoute.Local(requestId, "Simple request and verified local model available.")
            }
            LocalAiRequestMode.AUTO -> when {
                localModelReady && localCapable -> RequestRoute.Local(requestId, "Simple request and verified local model available.")
                else -> RequestRoute.Mac(requestId, if (localCapable) "No verified local model is ready." else "Request exceeds the local simple-task policy.")
            }
        }
    }

    /**
     * AUTO may retry the same objective on Mac only if local inference failed before emitting output.
     * LOCAL_ONLY never falls back; partial local output is never duplicated automatically.
     */
    fun routeAfterLocalFailure(
        requestId: String,
        mode: LocalAiRequestMode,
        partialOutputProduced: Boolean,
    ): RequestRoute = when {
        mode == LocalAiRequestMode.AUTO && !partialOutputProduced ->
            RequestRoute.Mac(requestId, "Local inference failed before producing output; retrying through Mac AI.")
        else -> RequestRoute.Rejected(
            requestId,
            RouteError.LOCAL_INFERENCE_FAILED,
            if (partialOutputProduced) "Local inference stopped after partial output; review or retry explicitly."
            else "Local inference failed; automatic fallback is disabled in this mode.",
        )
    }

    /** Conservative, transparent heuristic; this is routing policy, never a permission classifier. */
    fun isSimplePrompt(prompt: String): Boolean {
        val text = prompt.trim()
        if (text.isEmpty() || text.toByteArray(Charsets.UTF_8).size > MAX_AUTO_LOCAL_BYTES) return false
        if (text.count { it == '\n' } > 1 || text.count { it == '?' } > 2) return false
        return macOnlyPatterns.none { it.containsMatchIn(text) }
    }
}
