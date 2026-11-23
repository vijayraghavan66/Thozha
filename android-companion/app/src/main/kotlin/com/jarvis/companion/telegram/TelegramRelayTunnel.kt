package com.jarvis.companion.telegram

import com.jarvis.link.LinkJson
import com.jarvis.link.RelayRawResponse
import com.jarvis.link.RelayTransport
import java.io.IOException
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Carries the identical signed Jarvis Link relay request over Telegram, instead of HTTPS.
 *
 * Wire format (must match telegram_adapter/tunnel.py on the Mac):
 *   request : "JLT1 " + {"id","n","of","d"} frames whose reassembled JSON is {"t":"req","id","m","p","h","b"}
 *   response: the same framing, reassembling to {"t":"res","id","s","b"}
 * Every frame is ASCII and at most [MAX_FRAME_CHARS] characters, well inside Telegram's 4096-character limit.
 *
 * Telegram is untrusted transport. The relay and Mac host still verify every signature, certificate, nonce
 * and approval. This class only moves bytes and correlates responses.
 */
class TelegramRelayTunnel(
    private val sendFrame: (String) -> Unit,
    private val timeoutMs: Long = DEFAULT_TIMEOUT_MS,
    private val idSource: () -> String = { TunnelIds.next() },
    /** Diagnostics only: receives method, path and frame counts. Never headers, bodies or identifiers. */
    private val log: (String) -> Unit = {},
) : RelayTransport {
    private val lock = Any()
    private val reassembler = TunnelFrames.Reassembler()
    private val waiting = HashMap<String, ArrayBlockingQueue<RelayRawResponse>>()

    override fun exchange(method: String, pathAndQuery: String, headers: Map<String, String>, body: ByteArray): RelayRawResponse {
        val id = idSource()
        val trace = java.security.MessageDigest.getInstance("SHA-256").digest(id.toByteArray(Charsets.UTF_8))
            .take(6).joinToString("") { "%02x".format(it.toInt() and 0xff) }
        val startNs = System.nanoTime()
        val queue = ArrayBlockingQueue<RelayRawResponse>(1)
        synchronized(lock) {
            if (waiting.containsKey(id)) throw IOException("duplicate tunnel request id")
            waiting[id] = queue
        }
        try {
            val allowedHeaders = headers.filterKeys { it == "Accept" || it == "Content-Type" || it.startsWith("X-Jarvis-Link-") }
            val request = linkedMapOf<String, Any>(
                "t" to "req",
                "id" to id,
                "m" to method,
                "p" to pathAndQuery,
                "h" to allowedHeaders,
                "b" to String(body, Charsets.UTF_8),
            )
            val frames = TunnelFrames.encode(request, id)
            log("tunnel_start trace=$trace method=$method frames=${frames.size}")
            frames.forEach(sendFrame)
            log("tunnel_enqueued trace=$trace elapsed_ms=${TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startNs)}")
            val response = queue.poll(timeoutMs, TimeUnit.MILLISECONDS)
            if (response?.status == 401) {
                val error = runCatching {
                    (com.jarvis.link.LinkJson.parseObject(response.text)["error"] as? Map<*, *>)
                }.getOrNull()
                val code = (error?.get("code") as? String)?.takeIf { it.matches(Regex("^[A-Z0-9_]{1,48}$")) } ?: "unknown"
                val requestId = (error?.get("request_id") as? String)?.takeIf { it.matches(Regex("^[0-9a-fA-F-]{36}$")) }?.take(8) ?: "none"
                log("relay_rejected trace=$trace status=401 code=$code request_id=$requestId")
            }
            log("tunnel_finish trace=$trace outcome=${if (response == null) "timeout" else "response"} elapsed_ms=${TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startNs)}")
            return response ?: throw IOException("relay tunnel timed out")
        } finally {
            synchronized(lock) { waiting.remove(id) }
        }
    }

    /** Feed every incoming Telegram text message here. Non-tunnel text is ignored. */
    fun onText(text: String) {
        val message = reassembler.add(text) ?: return
        if (message["t"] != "res") return
        val id = message["id"] as? String ?: return
        val status = (message["s"] as? Number)?.toInt() ?: return
        val body = message["b"] as? String ?: return
        val queue = synchronized(lock) { waiting[id] } ?: return  // late or unknown: ignored, never delivered twice
        queue.offer(RelayRawResponse(status, body))
    }

    companion object {
        const val DEFAULT_TIMEOUT_MS = 20_000L
    }
}

/** Frame encoding and reassembly. Pure Kotlin so it can be checked on the JVM against the Mac implementation. */
object TunnelFrames {
    const val PREFIX = "JLT1 "
    const val MAX_FRAME_CHARS = 3000
    private const val MAX_WIRE_CHARS = 4096
    // The piece is JSON-encoded again in the frame, so quotes and backslashes cost two bytes.
    // Reserve ample room for the frame metadata, including a maximum-length id.
    private const val MAX_ENCODED_PIECE_CHARS = 3900
    const val MAX_MESSAGE_CHARS = 262_144
    const val MAX_CHUNKS = 128
    private const val REASSEMBLY_TTL_MS = 60_000L
    private val ID = Regex("^[A-Za-z0-9_-]{8,64}$")

    fun encode(message: Map<String, Any>, id: String): List<String> {
        require(ID.matches(id)) { "invalid message id" }
        val text = asciiEscape(LinkJson.encode(message))
        require(text.length <= MAX_MESSAGE_CHARS) { "message too large" }
        val pieces = mutableListOf<String>()
        var start = 0
        while (start < text.length) {
            var end = start
            var encodedSize = 0
            while (end < text.length && end - start < MAX_FRAME_CHARS) {
                val cost = if (text[end] == '\\' || text[end] == '"') 2 else 1
                if (encodedSize + cost > MAX_ENCODED_PIECE_CHARS) break
                encodedSize += cost
                end++
            }
            pieces.add(text.substring(start, end))
            start = end
        }
        if (pieces.isEmpty()) pieces.add("")
        require(pieces.size <= MAX_CHUNKS) { "too many chunks" }
        return pieces.mapIndexed { index, piece ->
            PREFIX + LinkJson.encode(linkedMapOf<String, Any>("id" to id, "n" to index.toLong(), "of" to pieces.size.toLong(), "d" to piece))
        }.also { frames -> require(frames.all { it.length <= MAX_WIRE_CHARS }) { "frame too large" } }
    }

    /** Escapes every non-ASCII character as \\uXXXX so one character is one UTF-16 unit on the wire. */
    fun asciiEscape(json: String): String {
        val out = StringBuilder(json.length)
        for (ch in json) {
            if (ch.code < 0x80) out.append(ch) else out.append("\\u").append(String.format("%04x", ch.code))
        }
        return out.toString()
    }

    class Reassembler(private val clock: () -> Long = { System.currentTimeMillis() }) {
        private class Partial(val of: Int, val pieces: MutableMap<Int, String>, val at: Long, var size: Int = 0)
        private val parts = HashMap<String, Partial>()

        @Synchronized fun add(text: String): Map<String, Any>? {
            expire()
            if (text.length > MAX_WIRE_CHARS || !text.startsWith(PREFIX)) return null
            val frame = runCatching { LinkJson.parseObject(text.substring(PREFIX.length)) }.getOrNull() ?: return null
            val id = frame["id"] as? String ?: return null
            val n = (frame["n"] as? Number)?.toInt() ?: return null
            val of = (frame["of"] as? Number)?.toInt() ?: return null
            val piece = frame["d"] as? String ?: return null
            if (!ID.matches(id) || of !in 1..MAX_CHUNKS || n !in 0 until of || piece.length > MAX_FRAME_CHARS) return null
            if (!parts.containsKey(id) && parts.size >= 32) return null
            val partial = parts.getOrPut(id) { Partial(of, HashMap(), clock()) }
            if (partial.of != of) { parts.remove(id); return null }
            val existing = partial.pieces[n]
            if (existing != null && existing != piece) { parts.remove(id); return null }
            if (existing == null) partial.size += piece.length
            if (partial.size > MAX_MESSAGE_CHARS) { parts.remove(id); return null }
            partial.pieces[n] = piece
            if (partial.pieces.size < of) return null
            parts.remove(id)
            val joined = (0 until of).joinToString("") { partial.pieces.getValue(it) }
            if (joined.length > MAX_MESSAGE_CHARS) return null
            return runCatching { LinkJson.parseObject(joined) }.getOrNull()?.takeIf { it["id"] == id }
        }

        private fun expire() {
            val cutoff = clock() - REASSEMBLY_TTL_MS
            parts.entries.removeAll { it.value.at < cutoff }
        }
    }
}

/** Monotonic-enough unique ids for tunnel requests (16 URL-safe characters). */
object TunnelIds {
    private val random = java.security.SecureRandom()
    private const val ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"

    fun next(): String = buildString(16) { repeat(16) { append(ALPHABET[random.nextInt(ALPHABET.length)]) } }
}
