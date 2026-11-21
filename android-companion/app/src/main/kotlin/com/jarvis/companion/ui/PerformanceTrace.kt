package com.jarvis.companion.ui

import android.util.Log
import java.security.MessageDigest

/** Content-free lifecycle trace. The short ID is derived from the random request ID. */
internal object PerformanceTrace {
    fun requestId(requestId: String): String = MessageDigest.getInstance("SHA-256")
        .digest(requestId.toByteArray(Charsets.UTF_8))
        .take(6)
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    fun mark(trace: String, event: String, elapsedMs: Long? = null, detail: String = "") {
        if (!trace.matches(Regex("[0-9a-f]{12}"))) return
        val safeEvent = event.filter { it.isLetterOrDigit() || it == '_' }.take(40)
        val safeDetail = detail.filter { it.isLetterOrDigit() || it in "_=-.," }.take(120)
        val duration = elapsedMs?.let { " elapsed_ms=$it" }.orEmpty()
        val extra = safeDetail.takeIf { it.isNotEmpty() }?.let { " $it" }.orEmpty()
        Log.i(
            "JarvisPerf",
            "trace=$trace event=$safeEvent wall_ms=${System.currentTimeMillis()} mono_ns=${System.nanoTime()}$duration$extra",
        )
    }
}
