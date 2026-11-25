package com.jarvis.companion.telegram

/** Serializes bursty relay frames so concurrent inbox sync and user sends respect Telegram chat pacing. */
internal class TelegramSendRateGate(
    private val intervalNanos: Long = 1_100_000_000L,
    private val nowNanos: () -> Long = System::nanoTime,
    private val pauseNanos: (Long) -> Unit = { nanos ->
        if (nanos > 0) java.util.concurrent.TimeUnit.NANOSECONDS.sleep(nanos)
    },
    initialDeferredNanos: Long = 0L,
) {
    private val lock = Any()
    private var nextSlotNanos = 0L
    private var deferredUntilNanos = nowNanos() + initialDeferredNanos.coerceIn(0L, TelegramRetryStateStore.MAX_SERVER_RETRY_MS * 1_000_000L)

    fun awaitSlot() {
        while (!tryAwaitSlot(Long.MAX_VALUE)) { /* A newer flood wait arrived during the wait; reserve again. */ }
    }

    /**
     * Reserves a send slot only if waiting would keep a pre-signed relay request inside its freshness window.
     * Returning false means the caller should leave the signed ciphertext queued and retry later with fresh auth.
     */
    fun tryAwaitSlot(maxWaitNanos: Long): Boolean {
        require(maxWaitNanos >= 0) { "maximum wait must be non-negative" }
        val wait = synchronized(lock) {
            val now = nowNanos()
            val slot = maxOf(now, nextSlotNanos, deferredUntilNanos)
            val delay = slot - now
            if (delay > maxWaitNanos) null else {
                nextSlotNanos = slot + intervalNanos
                delay
            }
        } ?: return false
        pauseNanos(wait)
        return synchronized(lock) { nowNanos() >= deferredUntilNanos }
    }

    fun deferForNanos(durationNanos: Long) = synchronized(lock) {
        deferredUntilNanos = maxOf(deferredUntilNanos, nowNanos() + durationNanos.coerceIn(0L, 300_000_000_000L))
        nextSlotNanos = maxOf(nextSlotNanos, deferredUntilNanos)
    }
}
