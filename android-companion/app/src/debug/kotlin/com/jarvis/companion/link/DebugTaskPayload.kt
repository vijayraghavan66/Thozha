package com.jarvis.companion.link

import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.util.Base64

/** Safe text encoding for the debug-only ADB broadcast. Plaintext is never logged. */
internal object DebugTaskPayload {
    private const val MAX_UTF8_BYTES = 4096
    private const val MAX_BASE64URL_CHARS = ((MAX_UTF8_BYTES + 2) / 3) * 4

    fun encode(text: String): String {
        val bytes = text.toByteArray(Charsets.UTF_8)
        require(bytes.isNotEmpty() && bytes.size <= MAX_UTF8_BYTES) { "objective must be 1–4096 UTF-8 bytes" }
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    fun decode(encoded: String): String {
        require(encoded.isNotEmpty() && encoded.length <= MAX_BASE64URL_CHARS) { "encoded objective length is invalid" }
        require(encoded.all { it in 'A'..'Z' || it in 'a'..'z' || it in '0'..'9' || it == '-' || it == '_' }) {
            "objective must be unpadded Base64URL"
        }
        val bytes = try {
            Base64.getUrlDecoder().decode(encoded)
        } catch (failure: IllegalArgumentException) {
            throw IllegalArgumentException("objective is not valid Base64URL", failure)
        }
        require(bytes.isNotEmpty() && bytes.size <= MAX_UTF8_BYTES) { "objective must be 1–4096 UTF-8 bytes" }
        return Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes))
            .toString()
    }
}
