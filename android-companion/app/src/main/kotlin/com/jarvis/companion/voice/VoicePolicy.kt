package com.jarvis.companion.voice

import java.util.Locale

/** Describes an installed platform voice without retaining any user speech or conversation text. */
data class VoiceOutputProfile(
    val name: String,
    val localeTag: String,
    val quality: Int,
    val latency: Int,
    val offline: Boolean,
)

enum class VoiceRecognitionProvider { ON_DEVICE, SYSTEM_FALLBACK }

/** Small pure model so local voice selection can be regression-tested without Android services. */
internal data class VoiceCandidate(
    val name: String,
    val localeTag: String,
    val quality: Int,
    val latency: Int,
    val networkRequired: Boolean,
    val dataInstalled: Boolean,
)

internal fun VoiceCandidate.toProfile() = VoiceOutputProfile(
    name = name,
    localeTag = localeTag,
    quality = quality,
    latency = latency,
    offline = !networkRequired && dataInstalled,
)

internal object VoicePolicy {
    /** Chooses the best already-installed offline voice for the requested language; never downloads one. */
    fun bestOfflineVoice(candidates: Iterable<VoiceCandidate>, requested: Locale): VoiceCandidate? {
        val matching = candidates.filter { candidate ->
            val voiceLocale = Locale.forLanguageTag(candidate.localeTag)
            candidate.name.isNotBlank() && candidate.dataInstalled && !candidate.networkRequired &&
                voiceLocale.language.isNotBlank() && voiceLocale.language.equals(requested.language, ignoreCase = true)
        }
        return matching.sortedWith(
            compareBy<VoiceCandidate> { localeRank(it.localeTag, requested) }
                .thenByDescending { it.quality }
                .thenBy { it.latency }
                .thenBy { it.name },
        ).firstOrNull()
    }

    private fun localeRank(candidateTag: String, requested: Locale): Int {
        val candidate = Locale.forLanguageTag(candidateTag)
        return when {
            candidate.toLanguageTag().equals(requested.toLanguageTag(), ignoreCase = true) -> 0
            candidate.country.isNotBlank() && candidate.country.equals(requested.country, ignoreCase = true) -> 1
            candidate.country.isBlank() -> 2
            else -> 3
        }
    }
}

internal object VoiceRecognitionRetryPolicy {
    fun isRecoverable(error: VoiceControllerError): Boolean = error == VoiceControllerError.NO_MATCH || error == VoiceControllerError.SPEECH_TIMEOUT
    /** Exponential, bounded delay avoids a hot restart loop while keeping a live call recoverable. */
    fun delayMs(failedAttempts: Int): Long = (750L shl (failedAttempts.coerceAtLeast(1) - 1).coerceAtMost(3)).coerceAtMost(6_000L)
}

internal object VoiceFocusPolicy {
    /** Android reports all focus-loss variants as negative changes, including permanent loss (-1). */
    fun shouldStop(change: Int): Boolean = change < 0
}

/** Monotonic identity for speech turns; a cancel/new turn invalidates every callback from the old turn. */
internal class VoiceSessionGeneration {
    var value: Long = 0L
        private set

    fun begin(): Long = ++value
    fun invalidate(): Long = ++value
    fun isCurrent(session: Long): Boolean = session == value
}

/** One-shot speech-start gate used while platform ASR listens through assistant playback. */
internal class VoiceBargeInGate {
    private var armed = false

    fun arm() { armed = true }

    fun disarm() { armed = false }

    /** Returns true only for the first VAD speech-start event in the active listening turn. */
    fun onSpeechStarted(): Boolean {
        if (!armed) return false
        armed = false
        return true
    }
}
