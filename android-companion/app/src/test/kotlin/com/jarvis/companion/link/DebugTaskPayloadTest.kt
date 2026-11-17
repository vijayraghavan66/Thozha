package com.jarvis.companion.link

/** Regression vectors for the debug-only, safely encoded ADB request payload. */
object DebugTaskPayloadTest {
    @JvmStatic
    fun main(args: Array<String>) {
        val samples = listOf(
            "Explain Java in one sentence.",
            "  leading and trailing spaces  ",
            "日本語 punctuation — café, naïve!",
            "Line one\nLine two\tquotes: \"yes\"; symbols: @#$%^&*()",
            "emoji 🧪 and non-BMP 𝔘",
        )
        samples.forEach { sample ->
            check(DebugTaskPayload.decode(DebugTaskPayload.encode(sample)) == sample) {
                "debug ADB payload changed UTF-8 text"
            }
        }
        check(runCatching { DebugTaskPayload.decode("%%%not-base64%%") }.isFailure)
        check(runCatching { DebugTaskPayload.encode("x".repeat(4097)) }.isFailure)
        println("Debug task payload UTF-8 preservation vectors passed.")
    }
}
