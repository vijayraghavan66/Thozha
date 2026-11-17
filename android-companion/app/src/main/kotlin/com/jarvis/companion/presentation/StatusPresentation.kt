package com.jarvis.companion.presentation

/** Telegram transport state as reported by the transport (names mirror TelegramConnection; null means not set up). */
enum class TransportState { NOT_CONFIGURED, LOGIN_REQUIRED, CONNECTING, CONNECTED, RECONNECTING, ERROR }

/**
 * What the app has actually observed about the AI executor. NOT_VERIFIED is the honest default: an answer must
 * have been seen before the UI may say the executor answered. Derived by the caller from real answers only.
 */
enum class AiEvidence { NOT_VERIFIED, ANSWERED, NO_RECENT_ANSWER }

data class StatusRow(val label: String, val value: String, val tone: TaskTone)

data class ConnectionSummary(
    val headline: String,
    val subline: String,
    val rows: List<StatusRow>,
)

object ConnectionPresentation {
    private const val RECENT_WINDOW_MS = 10 * 60_000L

    /**
     * Separates three facts: the Telegram transport, the Mac's last reply (observed, not a live probe), and the AI
     * executor (verified only by a real answer). The headline never reports the Mac as unavailable while the
     * transport is connected.
     */
    fun summary(
        transport: TransportState?,
        lastMacReplyMs: Long?,
        ai: AiEvidence,
        nowMs: Long,
        formatAgo: (Long) -> String,
    ): ConnectionSummary {
        val transportRow = when (transport) {
            TransportState.CONNECTED -> StatusRow("Telegram", "Connected", TaskTone.COMPLETED)
            TransportState.CONNECTING -> StatusRow("Telegram", "Connecting", TaskTone.RUNNING)
            TransportState.RECONNECTING -> StatusRow("Telegram", "Reconnecting. Requests wait on this phone.", TaskTone.WAITING)
            TransportState.LOGIN_REQUIRED -> StatusRow("Telegram", "Sign-in needed", TaskTone.WAITING)
            TransportState.ERROR -> StatusRow("Telegram", "Error. Retry from Advanced settings.", TaskTone.FAILED)
            TransportState.NOT_CONFIGURED, null -> StatusRow("Telegram", "Not set up", TaskTone.UNKNOWN)
        }
        val macRow = when {
            lastMacReplyMs == null -> StatusRow("Mac", "No reply seen yet", TaskTone.UNKNOWN)
            nowMs - lastMacReplyMs <= RECENT_WINDOW_MS -> StatusRow("Mac", "Replied ${formatAgo(nowMs - lastMacReplyMs)}", TaskTone.COMPLETED)
            else -> StatusRow("Mac", "Last reply ${formatAgo(nowMs - lastMacReplyMs)}", TaskTone.UNKNOWN)
        }
        val aiRow = when (ai) {
            AiEvidence.ANSWERED -> StatusRow("AI", "Answered recently", TaskTone.COMPLETED)
            AiEvidence.NO_RECENT_ANSWER -> StatusRow("AI", "No recent answer", TaskTone.UNKNOWN)
            AiEvidence.NOT_VERIFIED -> StatusRow("AI", "Not verified yet", TaskTone.UNKNOWN)
        }
        val connected = transport == TransportState.CONNECTED
        val headline = when {
            connected && ai == AiEvidence.ANSWERED -> "Connected"
            connected -> "Connected to Telegram"
            transport == TransportState.RECONNECTING || transport == TransportState.CONNECTING -> "Reconnecting"
            transport == TransportState.LOGIN_REQUIRED -> "Sign in to Telegram"
            transport == TransportState.ERROR -> "Telegram needs attention"
            else -> "Telegram is not set up"
        }
        val subline = when {
            !connected -> "Jarvis keeps your messages on this phone until the connection returns."
            lastMacReplyMs == null -> "The Mac has not replied yet. Your requests are delivered over Telegram."
            else -> "Requests travel over Telegram. The Mac's status is from its last reply."
        }
        return ConnectionSummary(headline, subline, listOf(transportRow, macRow, aiRow))
    }
}

enum class VoicePhase { IDLE, PERMISSION_NEEDED, PERMISSION_DENIED, LISTENING, THINKING, SPEAKING, ERROR }

/** Visual state for Jarvis Live. There is no voice backend yet, so phases are only set by explicit inputs. */
data class VoiceView(
    val phase: VoicePhase,
    val title: String,
    val hint: String,
    val showWaveform: Boolean,
    val pushToTalkEnabled: Boolean,
)

object VoicePresentation {
    fun view(phase: VoicePhase, errorMessage: String? = null): VoiceView = when (phase) {
        VoicePhase.IDLE -> VoiceView(phase, "Ready", "Hold to speak", false, true)
        VoicePhase.PERMISSION_NEEDED -> VoiceView(phase, "Microphone access", "Allow the microphone to talk to Jarvis.", false, true)
        VoicePhase.PERMISSION_DENIED -> VoiceView(phase, "Microphone is off", "Turn it on in system settings to use voice.", false, false)
        VoicePhase.LISTENING -> VoiceView(phase, "Listening", "Release when you are done.", true, true)
        VoicePhase.THINKING -> VoiceView(phase, "Thinking", "Jarvis is working on your request.", false, false)
        VoicePhase.SPEAKING -> VoiceView(phase, "Speaking", "Tap to interrupt.", true, true)
        VoicePhase.ERROR -> VoiceView(phase, "Voice is unavailable", errorMessage?.take(200) ?: "Try again in a moment.", false, true)
    }
}

/** Motion policy. Reduced motion is honoured when the user asks for it or the system animator scale is zero. */
object MotionPolicy {
    fun reduced(systemAnimatorScale: Float, userRequestedReduced: Boolean): Boolean =
        userRequestedReduced || systemAnimatorScale <= 0f

    fun durationMs(baseMs: Long, reduced: Boolean): Long = if (reduced) 0L else baseMs
}
