package com.jarvis.companion

import android.text.SpannableStringBuilder
import android.text.style.StyleSpan
import android.text.style.TypefaceSpan
import android.graphics.Typeface

/**
 * Renders only two small Markdown conventions from assistant prose. It never parses HTML, links,
 * images, or commands; all unmatched markers and control text remain ordinary visible text.
 */
internal object AssistantTextFormatter {
    private val inline = Regex("\\*\\*([^*\\n]+)\\*\\*|`([^`\\n]+)`")

    enum class Style { PLAIN, BOLD, CODE }
    data class Segment(val text: String, val style: Style)

    fun segments(source: String): List<Segment> {
        val result = mutableListOf<Segment>()
        var cursor = 0
        inline.findAll(source).forEach { match ->
            if (match.range.first > cursor) result += Segment(source.substring(cursor, match.range.first), Style.PLAIN)
            val emphasized = match.groups[1]?.value
            result += Segment(emphasized ?: match.groups[2]!!.value, if (emphasized != null) Style.BOLD else Style.CODE)
            cursor = match.range.last + 1
        }
        if (cursor < source.length) result += Segment(source.substring(cursor), Style.PLAIN)
        if (result.isEmpty()) result += Segment("", Style.PLAIN)
        return result
    }

    fun render(source: String): CharSequence {
        val out = SpannableStringBuilder()
        segments(source).forEach { segment ->
            val start = out.length
            out.append(segment.text)
            if (out.length > start) {
                when (segment.style) {
                    Style.PLAIN -> Unit
                    Style.BOLD -> out.setSpan(StyleSpan(Typeface.BOLD), start, out.length, 0)
                    Style.CODE -> out.setSpan(TypefaceSpan("monospace"), start, out.length, 0)
                }
            }
        }
        return out
    }
}
