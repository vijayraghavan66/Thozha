package com.jarvis.companion.voice

import java.nio.ByteBuffer
import java.nio.ByteOrder

object VoicePcmProtocolChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        val input = VoicePcmProtocol.encodePhoneFrame(31, 7, 1234, shortArrayOf(0x1234, -2), 2)
        check(input.size == VoicePcmProtocol.HEADER_BYTES + 4)
        check(input.copyOfRange(0, 4).contentEquals(byteArrayOf(84, 72, 90, 86)))
        check(input[4].toInt() == 2 && input[5].toInt() == VoicePcmProtocol.KIND_PHONE_PCM)
        check(ByteBuffer.wrap(input, 8, 4).order(ByteOrder.BIG_ENDIAN).int == 31)
        check(input[VoicePcmProtocol.HEADER_BYTES].toInt() and 0xff == 0x34)
        check(input[VoicePcmProtocol.HEADER_BYTES + 1].toInt() and 0xff == 0x12)

        val payload = byteArrayOf(1, 0, 0x7f, 0xff.toByte())
        val output = ByteBuffer.allocate(VoicePcmProtocol.HEADER_BYTES + payload.size).order(ByteOrder.BIG_ENDIAN)
            .put(byteArrayOf(84, 72, 90, 86)).put(2).put(2).putShort(24_000.toShort())
            .putInt(100).putInt(4).putLong(999).put(payload).array()
        val decoded = VoicePcmProtocol.decodeMacFrame(output)
        check(decoded.kind == VoicePcmProtocol.KIND_MAC_PCM && decoded.sampleRate == 24_000)
        check(decoded.turnId == 100L && decoded.sequence == 4L && decoded.elapsedMs == 999L && decoded.pcm.contentEquals(payload))

        check(runCatching { VoicePcmProtocol.decodeMacFrame(output.copyOf(23)) }.isFailure)
        check(runCatching { VoicePcmProtocol.decodeMacFrame(output.copyOf().also { it[4] = 1 }) }.isFailure)
        val outputSequence = VoiceOutputSequence()
        check(outputSequence.accept(0L))
        check(outputSequence.accept(1L))
        check(!outputSequence.accept(1L))
        outputSequence.reset()
        check(outputSequence.accept(0L))
        val turnGate = VoiceOutputTurnGate()
        check(!turnGate.accepts(100L))
        turnGate.activate(100L)
        check(turnGate.accepts(100L) && !turnGate.accepts(99L))
        turnGate.activate(101L)
        check(!turnGate.accepts(100L) && turnGate.accepts(101L))
        turnGate.invalidate()
        check(!turnGate.accepts(101L))
        println("Voice PCM protocol checks passed: 18 assertions")
    }
}
