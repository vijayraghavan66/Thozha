package com.jarvis.companion.telegram

import java.io.File
import java.io.FileOutputStream
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import kotlin.math.min

/** Local, non-secret Telegram pacing state. No account, chat, token, or message data is stored here. */
internal data class TelegramRetryState(
    val retryUntilUnixMs: Long = 0L,
    val transientAttempt: Int = 0,
    val blockedCode: String? = null,
)

internal enum class TelegramRetryDisposition { READY, RATE_LIMITED, PERMANENTLY_BLOCKED }

internal fun telegramRetryDisposition(state: TelegramRetryState, nowMs: Long): TelegramRetryDisposition = when {
    state.blockedCode != null -> TelegramRetryDisposition.PERMANENTLY_BLOCKED
    state.retryUntilUnixMs > nowMs -> TelegramRetryDisposition.RATE_LIMITED
    else -> TelegramRetryDisposition.READY
}

internal class TelegramRetryStateStore(
    private val file: File,
    private val nowMs: () -> Long = System::currentTimeMillis,
    private val jitterFactor: () -> Double = { 0.75 + kotlin.random.Random.nextDouble() * 0.5 },
) {
    private val lock = Any()

    fun state(): TelegramRetryState = synchronized(lock) { readState() }

    fun remainingMs(): Long = synchronized(lock) {
        (readState().retryUntilUnixMs - nowMs()).coerceIn(0L, MAX_SERVER_RETRY_MS)
    }

    fun deferForServerRetryAfter(seconds: Long): TelegramRetryState = synchronized(lock) {
        val bounded = seconds.coerceIn(1L, MAX_SERVER_RETRY_MS / 1_000L)
        val prior = readState()
        persist(prior.copy(retryUntilUnixMs = maxOf(prior.retryUntilUnixMs, nowMs() + bounded * 1_000L), transientAttempt = 0))
    }

    fun deferTransientFailure(): TelegramRetryState = synchronized(lock) {
        val prior = readState()
        val attempt = (prior.transientAttempt + 1).coerceAtMost(MAX_TRANSIENT_ATTEMPT)
        val baseMs = min(MAX_TRANSIENT_RETRY_MS, 1_000L shl (attempt - 1).coerceIn(0, 6))
        val factor = jitterFactor().coerceIn(0.5, 1.5)
        val delayMs = (baseMs * factor).toLong().coerceIn(500L, MAX_TRANSIENT_RETRY_MS)
        persist(prior.copy(retryUntilUnixMs = maxOf(prior.retryUntilUnixMs, nowMs() + delayMs), transientAttempt = attempt))
    }

    fun blockAutomaticRetry(code: String): TelegramRetryState = synchronized(lock) {
        val safe = code.uppercase().takeIf { it.matches(Regex("[A-Z0-9_]{1,48}")) } ?: "SEND_REJECTED"
        persist(readState().copy(blockedCode = safe))
    }

    fun clearPermanentBlock(): TelegramRetryState = synchronized(lock) {
        persist(readState().copy(blockedCode = null))
    }

    fun recordSuccess(): TelegramRetryState = synchronized(lock) {
        val current = readState()
        val next = if (current.retryUntilUnixMs <= nowMs() && current.blockedCode == null) TelegramRetryState() else current
        persist(next)
    }

    private fun readState(): TelegramRetryState {
        if (!file.exists()) return TelegramRetryState()
        return try {
            val fields = file.readText(Charsets.US_ASCII).lineSequence().take(4).toList()
            val deadline = fields.getOrNull(0)?.toLongOrNull() ?: error("invalid deadline")
            val attempts = fields.getOrNull(1)?.toIntOrNull()?.takeIf { it in 0..MAX_TRANSIENT_ATTEMPT } ?: error("invalid attempt")
            val rawBlock = fields.getOrNull(2).orEmpty()
            val block = rawBlock.takeIf(String::isNotBlank)?.takeIf { it.matches(Regex("[A-Z0-9_]{1,48}")) }
                ?: if (rawBlock.isBlank()) null else error("invalid block code")
            TelegramRetryState(deadline.coerceIn(0L, nowMs() + MAX_SERVER_RETRY_MS), attempts, block)
        } catch (_: Exception) {
            // Invalid retry state fails closed for the maximum server retry interval.
            TelegramRetryState(nowMs() + MAX_SERVER_RETRY_MS, 0, "RETRY_STATE_INVALID")
        }
    }

    private fun persist(state: TelegramRetryState): TelegramRetryState {
        if (state.retryUntilUnixMs == 0L && state.transientAttempt == 0 && state.blockedCode == null) {
            file.delete()
            return state
        }
        val parent = file.parentFile ?: throw IllegalStateException("retry state needs a parent directory")
        if (!parent.exists() && !parent.mkdirs()) throw IllegalStateException("retry state directory is unavailable")
        val temp = File.createTempFile("${file.name}.", ".tmp", parent)
        try {
            FileOutputStream(temp).use { out ->
                out.write("${state.retryUntilUnixMs}\n${state.transientAttempt}\n${state.blockedCode.orEmpty()}\n".toByteArray(Charsets.US_ASCII))
                out.fd.sync()
            }
            try {
                Files.move(temp.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
            } catch (_: AtomicMoveNotSupportedException) {
                Files.move(temp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING)
            }
        } finally {
            if (temp.exists()) temp.delete()
        }
        return state
    }

    companion object {
        const val MAX_SERVER_RETRY_MS = 300_000L
        const val MAX_TRANSIENT_RETRY_MS = 60_000L
        private const val MAX_TRANSIENT_ATTEMPT = 8
    }
}
