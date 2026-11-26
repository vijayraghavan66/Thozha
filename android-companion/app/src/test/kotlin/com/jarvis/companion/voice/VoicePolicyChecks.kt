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
        check(VoiceRecognitionRetryPolicy.isRecoverable(VoiceControllerError.NO_MATCH) && VoiceRecognitionRetryPolicy.isRecoverable(VoiceControllerError.SPEECH_TIMEOUT)) { "no-match and silence timeout must recover" }
        check(!VoiceRecognitionRetryPolicy.isRecoverable(VoiceControllerError.PERMISSION_REQUIRED) && !VoiceRecognitionRetryPolicy.isRecoverable(VoiceControllerError.MICROPHONE_UNAVAILABLE)) { "permission/device failures must not enter retry loop" }
        check((1..5).map(VoiceRecognitionRetryPolicy::delayMs) == listOf(750L, 1500L, 3000L, 6000L, 6000L)) { "recognition retries must use bounded exponential backoff" }
        check((0..VoiceReconnectPolicy.maxAttempts).map(VoiceReconnectPolicy::delayMs) == listOf(1000L, 2000L, 4000L, 8000L, 16000L, 0L)) {
            "Mac reconnect delays must be bounded and stop at the attempt budget"
        }
        check((0 until VoiceReconnectPolicy.maxAttempts).map(VoiceReconnectPolicy::nextDelayMs) == listOf(1000L, 2000L, 4000L, 8000L, 16000L))
        check(VoiceReconnectPolicy.nextDelayMs(VoiceReconnectPolicy.maxAttempts) == 60_000L) {
            "after bounded fast retries the phone should stay usable and periodically check Mac recovery"
        }
        val turn = VoiceSessionGeneration()
        val first = turn.begin()
        check(turn.isCurrent(first)) { "new voice session should accept callbacks" }
        turn.invalidate()
        check(!turn.isCurrent(first)) { "cancelled session callbacks must be discarded" }
        val second = turn.begin()
        check(second != first && turn.isCurrent(second) && !turn.isCurrent(first)) {
            "a restarted voice turn must ignore late callbacks from the previous turn"
        }

        val bargeIn = VoiceBargeInGate()
        check(!bargeIn.onSpeechStarted()) { "unarmed speech activity must not interrupt output" }
        bargeIn.arm()
        check(bargeIn.onSpeechStarted()) { "the first platform VAD speech-start must interrupt output" }
        check(!bargeIn.onSpeechStarted()) { "one recognition turn must emit only one interruption" }
        bargeIn.arm()
        bargeIn.disarm()
        check(!bargeIn.onSpeechStarted()) { "cancellation or TTS completion must disarm barge-in" }
        bargeIn.arm()
        check(bargeIn.onSpeechStarted()) { "a new listening turn must be recoverable after interruption" }
        check(callConnectionLabel(VoiceControllerState.IDLE, phoneFallback = true, macReady = false) == "Phone mode · ready")
        check(callConnectionLabel(VoiceControllerState.LISTENING, phoneFallback = true, macReady = false) == "Listening · phone mode")
        check(callConnectionLabel(VoiceControllerState.IDLE, phoneFallback = false, macReady = false) == "Connecting to Mac voice")
        check(callConnectionLabel(VoiceControllerState.LISTENING, phoneFallback = false, macReady = true) == "Listening")
        check(voiceTaskSpeech(mapOf("state" to "RUNNING", "summary" to "Working")) == null)
        check(voiceTaskSpeech(mapOf("state" to "COMPLETED", "summary" to "Execution completed; retrieving the final answer.", "result_delivery_state" to "PENDING")) == null)
        check(voiceTaskSpeech(mapOf("state" to "WAITING_APPROVAL", "summary" to "Review required"))!!.contains("owner approval"))
        check(voiceTaskSpeech(mapOf("state" to "COMPLETED", "summary" to "The Mac battery is 82%.", "result_delivery_state" to "DELIVERED")) == "The Mac battery is 82%.")
        check(voiceTaskSpeech(mapOf("state" to "FAILED", "summary" to "Runner unavailable")) == "Runner unavailable")
        println("Voice policy checks passed: 29 assertions")
    }
}
