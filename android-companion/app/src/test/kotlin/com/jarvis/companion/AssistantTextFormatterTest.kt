package com.jarvis.companion

/** JVM regression vectors for Markdown, newline, literal `%n`, and untrusted HTML-like text. */
object AssistantTextFormatterTest {
    @JvmStatic
    fun main(args: Array<String>) {
        val source = "I\napprove **Open the Calculator app** then run `df -h` %n <b>literal</b> **unfinished"
        val segments = AssistantTextFormatter.segments(source)
        check(segments.joinToString("") { it.text } == "I\napprove Open the Calculator app then run df -h %n <b>literal</b> **unfinished")
        check(segments.any { it.style == AssistantTextFormatter.Style.BOLD && it.text == "Open the Calculator app" })
        check(segments.any { it.style == AssistantTextFormatter.Style.CODE && it.text == "df -h" })
        check(segments.any { it.text.contains("%n") && it.text.contains("<b>literal</b>") })
        println("Assistant message formatting vectors passed.")
    }
}
