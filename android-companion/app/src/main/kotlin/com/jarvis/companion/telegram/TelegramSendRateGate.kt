package com.jarvis.companion.telegram

/** Serializes bursty relay frames so concurrent inbox sync and user sends respect Telegram chat pacing. */
internal class TelegramSendRateGate(
    private val intervalNanos: Long = 1_100_000_000L,
    private val nowNanos: () -> Long = System::nanoTime,
    private val pauseNanos: (Long) -> Unit = { nanos ->
        if (nanos > 0) java.util.concurrent.TimeUnit.NANOSECONDS.sleep(nanos)
    },
) {
    private val lock = Any()
    private var nextSlotNanos = 0L

    fun awaitSlot() {
        val wait = synchronized(lock) {
            val now = nowNanos()
            val slot = maxOf(now, nextSlotNanos)
            nextSlotNanos = slot + intervalNanos
            slot - now
        }
        pauseNanos(wait)
    }

    fun deferForNanos(durationNanos: Long) = synchronized(lock) {
        nextSlotNanos = maxOf(nextSlotNanos, nowNanos() + durationNanos.coerceIn(0L, 300_000_000_000L))
    }
}
