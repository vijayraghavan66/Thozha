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
    fun speak(text: String, speakerId: Int = defaultSpeakerId, cancelled: () -> Boolean = { false }): Long {
        val start = SystemClock.elapsedRealtime()
        val audio = (tts ?: error("Kokoro model is not loaded")).generate(text, sid = speakerId)
        check(audio.samples.isNotEmpty()) { "Kokoro generated no audio" }
        Log.i("JarvisNeuralTts", "event=pcm_generated sid=$speakerId samples=${audio.samples.size} sample_rate=${audio.sampleRate} duration_ms=${audio.samples.size * 1000L / audio.sampleRate}")
        if (cancelled()) return (System.nanoTime() - start) / 1_000_000
        val track = AudioTrack.Builder().setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()).setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_FLOAT).setSampleRate(audio.sampleRate).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build()).setBufferSizeInBytes(audio.samples.size * 4).setTransferMode(AudioTrack.MODE_STATIC).build()
        try {
            val written = track.write(audio.samples, 0, audio.samples.size, AudioTrack.WRITE_BLOCKING)
            check(written > 0) { "AudioTrack PCM write failed: $written" }
            var first = false
            track.setPositionNotificationPeriod((audio.sampleRate / 20).coerceAtLeast(1))
            track.setPlaybackPositionUpdateListener(object : AudioTrack.OnPlaybackPositionUpdateListener {
                override fun onMarkerReached(track: AudioTrack) { Log.i("JarvisNeuralTts", "event=audio_marker_completed sid=$speakerId frames=${track.playbackHeadPosition}") }
                override fun onPeriodicNotification(track: AudioTrack) { if (!first && track.playbackHeadPosition > 0) { first = true; Log.i("JarvisNeuralTts", "event=first_playback_frames sid=$speakerId frames=${track.playbackHeadPosition} elapsed_ms=${SystemClock.elapsedRealtime()}") } }
            })
            if (!cancelled()) { track.play(); Log.i("JarvisNeuralTts", "event=audio_track_play sid=$speakerId written=$written encoding=PCM_FLOAT sample_rate=${audio.sampleRate}") }
            while (!cancelled() && track.playState == AudioTrack.PLAYSTATE_PLAYING && track.playbackHeadPosition < audio.samples.size) Thread.sleep(20)
            check(cancelled() || track.playbackHeadPosition > 0) { "AudioTrack ended without playing frames" }
        } finally { if (track.playState != AudioTrack.PLAYSTATE_STOPPED) track.stop(); track.release() }
        return SystemClock.elapsedRealtime() - start
    }
    override fun close() { tts?.release(); tts = null }
}
