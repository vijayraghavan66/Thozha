package com.jarvis.companion.voice

import java.util.Locale

/** Deterministic voice policy checks; the platform recognizer/TTS services still need device verification. */
object VoicePolicyChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        val candidates = listOf(
            VoiceCandidate("network-high", "en-IN", 500, 100, networkRequired = true, dataInstalled = true),
            VoiceCandidate("missing-high", "en-IN", 500, 100, networkRequired = false, dataInstalled = false),
            VoiceCandidate("offline-normal", "en-IN", 300, 200, networkRequired = false, dataInstalled = true),
            VoiceCandidate("offline-high", "en-US", 500, 100, networkRequired = false, dataInstalled = true),
            VoiceCandidate("offline-low", "en", 200, 100, networkRequired = false, dataInstalled = true),
            VoiceCandidate("tamil-offline", "ta-IN", 500, 100, networkRequired = false, dataInstalled = true),
        )
        val en = VoicePolicy.bestOfflineVoice(candidates, Locale.forLanguageTag("en-IN"))
        check(en?.name == "offline-normal") { "language+region match should outrank a higher-quality different-region voice" }
        check(en?.networkRequired == false && en?.dataInstalled == true) { "voice selection must not pick network or uninstalled data" }
        check(VoicePolicy.bestOfflineVoice(candidates, Locale.forLanguageTag("ta-IN"))?.name == "tamil-offline") {
            "voice selection should preserve Tamil when an installed offline voice exists"
        }
        check(VoicePolicy.bestOfflineVoice(candidates.filter { it.networkRequired }, Locale.US) == null) {
            "network-only TTS must not be selected implicitly"
        }
        check(VoiceFocusPolicy.shouldStop(-1) && VoiceFocusPolicy.shouldStop(-2) && VoiceFocusPolicy.shouldStop(-3)) {
            "permanent, transient and duck focus losses must stop the active session"
        }
        check(!VoiceFocusPolicy.shouldStop(1)) { "focus gain must not be treated as a loss" }
        val turn = VoiceSessionGeneration()
        val first = turn.begin()
        check(turn.isCurrent(first)) { "new voice session should accept callbacks" }
        turn.invalidate()
        check(!turn.isCurrent(first)) { "cancelled session callbacks must be discarded" }
        val second = turn.begin()
        check(second != first && turn.isCurrent(second) && !turn.isCurrent(first)) {
            "a restarted voice turn must ignore late callbacks from the previous turn"
        }
        println("Voice policy checks passed: 9 assertions")
    }
}
