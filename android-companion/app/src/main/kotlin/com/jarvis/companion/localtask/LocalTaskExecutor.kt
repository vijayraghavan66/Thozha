package com.jarvis.companion.localtask

import java.util.Locale

sealed interface LocalTaskIntent {
    data class Calculate(val left: Double, val operator: Char, val right: Double) : LocalTaskIntent
    data class Convert(val value: Double, val from: String, val to: String) : LocalTaskIntent
}

data class LocalTaskResult(val title: String, val response: String)

/** Narrow deterministic phone lane. It has no shell, network, Mac or model dependency. */
object LocalTaskExecutor {
    private val calculation = Regex("(?i)^\\s*(?:jarvis[, ]+)?(?:calculate|compute|what is|what's)\\s+(-?\\d+(?:\\.\\d+)?)\\s*([+*/-])\\s*(-?\\d+(?:\\.\\d+)?)\\s*[?!. ]*\\s*$")
    private val conversion = Regex("(?i)^\\s*(?:jarvis[, ]+)?convert\\s+(-?\\d+(?:\\.\\d+)?)\\s*([a-z]+)\\s+to\\s+([a-z]+)\\s*[?!. ]*\\s*$")
    private val factors = mapOf(
        "km" to Pair("length", 1000.0), "kilometer" to Pair("length", 1000.0), "kilometers" to Pair("length",1000.0),
        "m" to Pair("length",1.0), "meter" to Pair("length",1.0), "meters" to Pair("length",1.0),
        "mi" to Pair("length",1609.344), "mile" to Pair("length",1609.344), "miles" to Pair("length",1609.344),
        "kg" to Pair("mass",1.0), "kilogram" to Pair("mass",1.0), "kilograms" to Pair("mass",1.0),
        "g" to Pair("mass",0.001), "gram" to Pair("mass",0.001), "grams" to Pair("mass",0.001),
        "lb" to Pair("mass",0.45359237), "lbs" to Pair("mass",0.45359237), "pound" to Pair("mass",0.45359237), "pounds" to Pair("mass",0.45359237),
    )
    fun classify(text: String): LocalTaskIntent? {
        calculation.matchEntire(text)?.let { return LocalTaskIntent.Calculate(it.groupValues[1].toDouble(), it.groupValues[2][0], it.groupValues[3].toDouble()) }
        conversion.matchEntire(text)?.let { m ->
            val from = m.groupValues[2].lowercase(Locale.ROOT); val to = m.groupValues[3].lowercase(Locale.ROOT)
            val a = factors[from] ?: return null; val b = factors[to] ?: return null
            if (a.first != b.first) return null
            return LocalTaskIntent.Convert(m.groupValues[1].toDouble(), from, to)
        }
        return null
    }
    fun execute(intent: LocalTaskIntent): LocalTaskResult = when(intent) {
        is LocalTaskIntent.Calculate -> {
            val answer = when(intent.operator) { '+' -> intent.left + intent.right; '-' -> intent.left - intent.right; '*' -> intent.left * intent.right; '/' -> { require(intent.right != 0.0) { "Cannot divide by zero." }; intent.left / intent.right }; else -> error("Unsupported operator") }
            LocalTaskResult("Calculation", "${format(intent.left)} ${intent.operator} ${format(intent.right)} = ${format(answer)}")
        }
        is LocalTaskIntent.Convert -> {
            val from = factors.getValue(intent.from); val to = factors.getValue(intent.to)
            val answer = intent.value * from.second / to.second
            LocalTaskResult("Unit conversion", "${format(intent.value)} ${intent.from} = ${format(answer)} ${intent.to}")
        }
    }
    private fun format(value: Double): String = if (value.isFinite() && value % 1.0 == 0.0) value.toLong().toString() else String.format(Locale.ROOT, "%.4f", value).trimEnd('0').trimEnd('.')
}
