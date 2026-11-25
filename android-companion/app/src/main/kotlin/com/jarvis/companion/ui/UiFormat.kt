package com.jarvis.companion.ui

import com.jarvis.companion.presentation.TransportState
import com.jarvis.companion.telegram.TelegramConnection
import com.jarvis.companion.telegram.TelegramSnapshot
import java.text.DateFormat
import java.util.Date

/** Locale-aware clock time. Empty for an unknown or zero timestamp, never a guessed value. */
fun clockTime(ms: Long): String =
    if (ms <= 0L) "" else DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(ms))

/** Relative age of a real observed event, e.g. "3 min ago". */
fun ageLabel(elapsedMs: Long): String {
    val seconds = (elapsedMs / 1000L).coerceAtLeast(0L)
    return when {
        seconds < 45 -> "just now"
        seconds < 3600 -> "${seconds / 60} min ago"
        seconds < 86_400 -> "${seconds / 3600} h ago"
        else -> "${seconds / 86_400} d ago"
    }
}

/** Time left before an approval expires, or "Expired" once it has passed. */
fun remainingLabel(expiresAtMs: Long, nowMs: Long): String {
    val left = expiresAtMs - nowMs
    if (left <= 0L) return "Expired"
    val minutes = left / 60_000L
    return when {
        minutes >= 60 -> "${minutes / 60} h ${minutes % 60} min left"
        minutes >= 1 -> "$minutes min left"
        else -> "Under a minute left"
    }
}

fun transportState(connection: TelegramConnection?): TransportState =
    connection?.let { runCatching { TransportState.valueOf(it.name) }.getOrNull() } ?: TransportState.NOT_CONFIGURED

fun telegramConnectionLabel(connection: TelegramConnection?): String = when (connection) {
    TelegramConnection.CONNECTED -> "Connected"
    TelegramConnection.CONNECTING -> "Connecting"
    TelegramConnection.RECONNECTING -> "Reconnecting"
    TelegramConnection.LOGIN_REQUIRED -> "Sign-in needed"
    TelegramConnection.ERROR -> "Error"
    TelegramConnection.NOT_CONFIGURED, null -> "Not set up"
}

fun telegramRetryStatus(snapshot: TelegramSnapshot?, nowMs: Long): String? {
    snapshot?.sendBlockedReasonCode?.let { code ->
        return when (code) {
            "AUTH_REQUIRED", "RELAY_AUTH_FAILED" -> "Telegram sending paused: authorization needs attention. Queued requests stay on this phone."
            "DEVICE_REVOKED", "DEVICE_NOT_AUTHORIZED" -> "Telegram sending paused: this device needs owner-authorized recovery."
            "RETRY_STATE_INVALID" -> "Telegram sending paused because saved retry state could not be verified."
            else -> "Telegram sending paused after a permanent relay refusal. Queued requests were preserved."
        }
    }
    val until = snapshot?.retryAfterUntilUnixMs ?: return null
    val seconds = ((until - nowMs).coerceAtLeast(0L) + 999L) / 1_000L
    if (seconds <= 0L) return null
    return "Telegram rate limit: retry in ${seconds / 60}m ${seconds % 60}s. Requests remain queued on this phone."
}

/** Expiry label for an approval's raw timestamp in Unix milliseconds. */
fun expiryClock(expiresAtMs: Long): String = clockTime(expiresAtMs)

/** Compact, locale-stable memory size used by device and local-model summaries. */
fun memoryLabel(bytes: Long?): String = bytes?.let {
    String.format(java.util.Locale.US, "%.1f GiB", it / (1024.0 * 1024.0 * 1024.0))
} ?: "unavailable"
