package com.jarvis.link

/**
 * Strict, dependency-free JSON codec used by the Jarvis Link wire layer.
 *
 * Android's org.json is unavailable to pure-JVM tests, so the protocol carries its own small parser.
 * Accepted values: objects, arrays, strings, integers (Long), booleans and null. Floats are rejected
 * because every protocol number is an integer (versions, timestamps, limits).
 */
internal object LinkJson {
    private const val MAX_BYTES = 256 * 1024

    /** Parses one JSON object. JSON null members are omitted from the result. */
    fun parseObject(text: String): Map<String, Any> {
        require(text.toByteArray(Charsets.UTF_8).size <= MAX_BYTES) { "JSON document too large" }
        val parser = Parser(text)
        val value = parser.value(depth = 0)
        parser.skipWhitespace()
        require(parser.atEnd()) { "Trailing JSON data" }
        @Suppress("UNCHECKED_CAST")
        return (value as? Map<String, Any?>
            ?: throw IllegalArgumentException("JSON root must be an object")).filterValues { it != null } as Map<String, Any>
    }

    fun encode(value: Any?): String = buildString { write(this, value) }

    private fun write(out: StringBuilder, value: Any?) {
        when (value) {
            null -> out.append("null")
            is String -> out.append('"').append(escape(value)).append('"')
            is Boolean -> out.append(if (value) "true" else "false")
            is Int, is Long -> out.append(value.toString())
            is Map<*, *> -> {
                out.append('{')
                var first = true
                for ((k, v) in value) {
                    if (v == null) continue
                    if (!first) out.append(',')
                    first = false
                    out.append('"').append(escape(k as String)).append("\":")
                    write(out, v)
                }
                out.append('}')
            }
            is List<*> -> {
                out.append('[')
                value.forEachIndexed { index, item ->
                    if (index > 0) out.append(',')
                    write(out, item)
                }
                out.append(']')
            }
            else -> throw IllegalArgumentException("Unsupported JSON value type")
        }
    }

    fun escape(value: String): String = buildString {
        value.forEach { char ->
            when (char) {
                '"' -> append("\\\"")
                '\\' -> append("\\\\")
                '\b' -> append("\\b")
                '\u000C' -> append("\\f")
                '\n' -> append("\\n")
                '\r' -> append("\\r")
                '\t' -> append("\\t")
                else -> if (char.code < 0x20) append("\\u%04x".format(char.code)) else append(char)
            }
        }
    }

    private class Parser(private val text: String) {
        private var index = 0

        fun atEnd() = index == text.length

        fun value(depth: Int): Any? {
            require(depth <= 16) { "JSON nesting too deep" }
            skipWhitespace()
            return when (peek()) {
                '{' -> obj(depth)
                '[' -> arr(depth)
                '"' -> string()
                't' -> literal("true", true)
                'f' -> literal("false", false)
                'n' -> literal("null", null)
                else -> number()
            }
        }

        private fun obj(depth: Int): Map<String, Any?> {
            expect('{'); skipWhitespace()
            val result = linkedMapOf<String, Any?>()
            if (take('}')) return result
            while (true) {
                skipWhitespace()
                val key = string()
                skipWhitespace(); expect(':')
                require(!result.containsKey(key)) { "Duplicate JSON member" }
                result[key] = value(depth + 1)
                skipWhitespace()
                if (take('}')) return result
                expect(',')
            }
        }

        private fun arr(depth: Int): List<Any?> {
            expect('['); skipWhitespace()
            val result = mutableListOf<Any?>()
            if (take(']')) return result
            while (true) {
                result += value(depth + 1)
                skipWhitespace()
                if (take(']')) return result
                expect(',')
            }
        }

        private fun string(): String {
            expect('"')
            val out = StringBuilder()
            while (true) {
                require(index < text.length) { "Unterminated JSON string" }
                val char = text[index++]
                when (char) {
                    '"' -> return out.toString()
                    '\\' -> {
                        require(index < text.length) { "Unterminated JSON escape" }
                        when (val escaped = text[index++]) {
                            '"', '\\', '/' -> out.append(escaped)
                            'b' -> out.append('\b')
                            'f' -> out.append('\u000C')
                            'n' -> out.append('\n')
                            'r' -> out.append('\r')
                            't' -> out.append('\t')
                            'u' -> {
                                require(index + 4 <= text.length) { "Invalid JSON unicode escape" }
                                val hex = text.substring(index, index + 4)
                                require(hex.all { it in '0'..'9' || it in 'a'..'f' || it in 'A'..'F' }) { "Invalid JSON unicode escape" }
                                out.append(hex.toInt(16).toChar())
                                index += 4
                            }
                            else -> error("Invalid JSON escape")
                        }
                    }
                    else -> {
                        require(char.code >= 0x20) { "Control character in JSON string" }
                        out.append(char)
                    }
                }
            }
        }

        private fun number(): Long {
            val start = index
            if (peek() == '-') index++
            val digitsStart = index
            while (index < text.length && text[index].isDigit()) index++
            require(index > digitsStart) { "Expected JSON value" }
            require(!(index < text.length && (text[index] == '.' || text[index] == 'e' || text[index] == 'E'))) {
                "Protocol JSON accepts integers only"
            }
            return text.substring(start, index).toLong()
        }

        private fun <T> literal(word: String, result: T): T {
            require(text.startsWith(word, index)) { "Invalid JSON literal" }
            index += word.length
            return result
        }

        fun skipWhitespace() { while (index < text.length && text[index] in " \t\r\n") index++ }
        private fun peek(): Char = text.getOrNull(index) ?: error("Unexpected end of JSON")
        private fun take(char: Char): Boolean = if (text.getOrNull(index) == char) { index++; true } else false
        private fun expect(char: Char) { require(take(char)) { "Expected '$char' at position $index" } }
    }
}
