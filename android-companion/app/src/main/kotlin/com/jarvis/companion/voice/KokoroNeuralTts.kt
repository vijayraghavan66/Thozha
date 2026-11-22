package com.jarvis.companion.voice

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.SystemClock
import android.util.Log
import com.k2fsa.sherpa.onnx.OfflineTts
import com.k2fsa.sherpa.onnx.OfflineTtsConfig
import com.k2fsa.sherpa.onnx.OfflineTtsKokoroModelConfig
import com.k2fsa.sherpa.onnx.OfflineTtsModelConfig
import java.io.File

/** Local Sherpa-ONNX Kokoro adapter. Caller supplies the verified model directory and speaker ID. */
class KokoroNeuralTts(private val dir: File, private val defaultSpeakerId: Int = 5) : AutoCloseable {
    private var tts: OfflineTts? = null
    fun speakerCount(): Int = tts?.numSpeakers() ?: 0
    fun load(): Long {
        val model = File(dir, "model.onnx")
        require(model.isFile && model.canRead()) { "Native model path unreadable: ${model.absolutePath}, size=${model.length()}" }
        listOf("voices.bin", "tokens.txt", "lexicon-en-us.txt").forEach { rel -> val f = File(dir, rel); require(f.isFile && f.canRead()) { "Native asset unreadable: ${f.absolutePath}" } }
        val start = SystemClock.elapsedRealtime()
        val config = OfflineTtsConfig(model = OfflineTtsModelConfig(
            kokoro = OfflineTtsKokoroModelConfig(
                model = File(dir, "model.onnx").path, voices = File(dir, "voices.bin").path,
                tokens = File(dir, "tokens.txt").path, dataDir = "",
                lexicon = File(dir, "lexicon-en-us.txt").path, lang = "en-us",
            ), numThreads = 2, provider = "cpu",
        ))
        tts = OfflineTts(config = config)
        val elapsed = SystemClock.elapsedRealtime() - start
        Log.i("JarvisNeuralTts", "event=native_model_initialized path=${model.absolutePath} bytes=${model.length()} speakers=${tts?.numSpeakers()} load_ms=$elapsed")
        return elapsed
    }
    data class StreamingResult(val generationMs: Long, val firstPcmMs: Long, val playbackStartMs: Long, val audioDurationMs: Long, val samples: Int)

    fun speak(text: String, speakerId: Int = defaultSpeakerId, cancelled: () -> Boolean = { false }, speechEndElapsedMs: Long? = null, onFirstPlayback: (Long) -> Unit = {}): StreamingResult {
        val started = SystemClock.elapsedRealtime()
        val engine = tts ?: error("Kokoro model is not loaded")
        val rate = engine.sampleRate()
        require(rate > 0) { "Kokoro returned invalid sample rate $rate" }
        val minBuffer = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_FLOAT)
        check(minBuffer > 0) { "AudioTrack cannot support Kokoro PCM: minBuffer=$minBuffer rate=$rate" }
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_FLOAT).setSampleRate(rate).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(maxOf(minBuffer * 2, rate * 4 / 4))
            .setTransferMode(AudioTrack.MODE_STREAM).build()
        var sampleCount = 0
        var firstPcmMs = -1L
        var playbackStartMs = -1L
        var generated = false
        try {
            val callback = KokoroPcmChunkCallback { chunk ->
                if (cancelled()) 0 else {
                    if (chunk.isNotEmpty()) {
                        if (firstPcmMs < 0) {
                            firstPcmMs = SystemClock.elapsedRealtime() - started
                            Log.i("JarvisNeuralTts", "event=first_pcm sid=$speakerId synth_ms=$firstPcmMs end_to_first_pcm_ms=${speechEndElapsedMs?.let { SystemClock.elapsedRealtime()-it } ?: -1}")
                        }
                        val written = track.write(chunk, 0, chunk.size, AudioTrack.WRITE_BLOCKING)
                        check(written > 0) { "AudioTrack chunk write failed: $written" }
                        sampleCount += written
                        if (playbackStartMs < 0) {
                            track.play()
                            playbackStartMs = SystemClock.elapsedRealtime() - started
                            Log.i("JarvisNeuralTts", "event=stream_playback_started sid=$speakerId first_pcm_ms=$firstPcmMs playback_start_ms=$playbackStartMs rate=$rate")
                            onFirstPlayback(playbackStartMs)
                        }
                    }
                    1
                }
            }
            val audio = engine.generateWithCallback(text, sid = speakerId, callback = callback)
            generated = audio.samples.isNotEmpty()
            check(generated && sampleCount > 0) { "Kokoro generated no PCM chunks" }
            while (!cancelled() && track.playbackHeadPosition < sampleCount) Thread.sleep(10)
            check(cancelled() || track.playbackHeadPosition > 0) { "AudioTrack ended without playing frames" }
            val generationMs = (firstPcmMs.takeIf { it >= 0 } ?: (SystemClock.elapsedRealtime() - started))
            val duration = sampleCount * 1000L / rate
            Log.i("JarvisNeuralTts", "event=stream_complete sid=$speakerId chunks_samples=$sampleCount generated_samples=${audio.samples.size} rate=$rate duration_ms=$duration generation_to_first_pcm_ms=$generationMs playback_head=${track.playbackHeadPosition} cancelled=${cancelled()}")
            return StreamingResult(generationMs, firstPcmMs, playbackStartMs, duration, sampleCount)
        } finally {
            if (track.playState != AudioTrack.PLAYSTATE_STOPPED) track.stop()
            track.release()
        }
    }
    override fun close() { tts?.release(); tts = null }
}
