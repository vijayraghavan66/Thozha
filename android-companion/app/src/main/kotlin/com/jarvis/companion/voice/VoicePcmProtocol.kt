package com.jarvis.companion.voice

import java.nio.ByteBuffer
import java.nio.ByteOrder

internal data class VoicePcmFrame(
    val kind: Int,
    val sampleRate: Int,
    val sequence: Long,
    val elapsedMs: Long,
    val pcm: ByteArray,
)

/** Shared wire codec with jarvis-link/host/src/jarvis_link_host/voice_protocol.py. */
internal object VoicePcmProtocol {
    const val HEADER_BYTES = 20
    const val KIND_PHONE_PCM = 1
    const val KIND_MAC_PCM = 2
    private val magic = byteArrayOf('T'.code.toByte(), 'H'.code.toByte(), 'Z'.code.toByte(), 'V'.code.toByte())

    fun encodePhoneFrame(sequence: Long, elapsedMs: Long, samples: ShortArray, count: Int): ByteArray {
        require(sequence in 0..0xffff_ffffL && elapsedMs >= 0)
        require(count in 1..samples.size && count <= 32_000)
        val out = ByteBuffer.allocate(HEADER_BYTES + count * 2).order(ByteOrder.BIG_ENDIAN)
        out.put(magic).put(1).put(KIND_PHONE_PCM.toByte()).putShort(16_000.toShort())
            .putInt(sequence.toInt()).putLong(elapsedMs)
        repeat(count) { i ->
            val sample = samples[i].toInt()
            out.put((sample and 0xff).toByte())
            out.put(((sample ushr 8) and 0xff).toByte())
        }
        return out.array()
    }

    fun decodeMacFrame(payload: ByteArray): VoicePcmFrame {
        require(payload.size > HEADER_BYTES && payload.size <= 64 * 1024 && (payload.size - HEADER_BYTES) % 2 == 0) {
            "PCM frame size is invalid"
        }
        val input = ByteBuffer.wrap(payload).order(ByteOrder.BIG_ENDIAN)
        val receivedMagic = ByteArray(4).also(input::get)
        require(receivedMagic.contentEquals(magic)) { "PCM frame magic is invalid" }
        require(input.get().toInt() == 1) { "PCM frame version is unsupported" }
        val kind = input.get().toInt() and 0xff
        require(kind == KIND_MAC_PCM) { "PCM frame direction is invalid" }
        val sampleRate = input.short.toInt() and 0xffff
        require(sampleRate in setOf(16_000, 24_000, 48_000)) { "PCM sample rate is invalid" }
        val sequence = input.int.toLong() and 0xffff_ffffL
        val elapsed = input.long
        require(elapsed >= 0) { "PCM timestamp is invalid" }
        return VoicePcmFrame(kind, sampleRate, sequence, elapsed, payload.copyOfRange(HEADER_BYTES, payload.size))
    }
}
