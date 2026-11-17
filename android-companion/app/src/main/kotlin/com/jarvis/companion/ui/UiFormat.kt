package com.jarvis.companion.ui

import com.jarvis.companion.presentation.TransportState
import com.jarvis.companion.telegram.TelegramConnection
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

/** Expiry label for an approval's raw timestamp in Unix milliseconds. */
fun expiryClock(expiresAtMs: Long): String = clockTime(expiresAtMs)
