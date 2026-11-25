package com.jarvis.companion.voice

import android.app.Activity
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Bundle
import android.os.Debug
import android.os.SystemClock
import android.util.Log
import android.widget.TextView
import java.util.UUID

/** Debug-only in-process deterministic harness; invoke by explicit ADB component. */
class KokoroInferenceDebugActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val runId = UUID.randomUUID().toString().take(8)
        val status = TextView(this).apply { text = "Kokoro inference $runId\nRunning in the application process…"; textSize = 18f; setPadding(32, 48, 32, 32) }
        setContentView(status)
        Thread {
            val tag = "JarvisNeuralTts"
            try {
                Log.i(tag, "event=debug_harness_enter run_id=$runId uid=${android.os.Process.myUid()} package=$packageName")
                val dir = KokoroAssets.provision(applicationContext)
                val engine = KokoroNeuralTts(dir)
                val loadMs = engine.load()
                val results = mutableListOf<String>()
                val focus = getSystemService(AUDIO_SERVICE) as AudioManager
                val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                    .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setOnAudioFocusChangeListener {}.build()
                check(focus.requestAudioFocus(request) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) { "Media audio focus denied" }
                for ((sid, name) in listOf(5 to "am_adam", 1 to "af_bella")) {
                    val start = SystemClock.elapsedRealtime()
                    val elapsed = engine.speak("Good evening. I'm Jarvis. Your assistant is ready. What would you like to do today?", sid)
                    val info = Debug.MemoryInfo(); Debug.getMemoryInfo(info)
                    val line = "$name (sid=$sid): first PCM ${elapsed.firstPcmMs} ms; playback callback ${elapsed.playbackStartMs} ms; ${elapsed.samples} samples @ ${elapsed.sampleRate} Hz; ${elapsed.audioDurationMs} ms audio; turn ${SystemClock.elapsedRealtime()-start} ms; PSS ${info.totalPss} KiB"
                    results += line
                    Log.i(tag, "event=debug_voice_complete run_id=$runId name=$name sid=$sid model_load_ms=$loadMs first_pcm_ms=${elapsed.firstPcmMs} playback_start_ms=${elapsed.playbackStartMs} generation_ms=${elapsed.generationMs} audio_duration_ms=${elapsed.audioDurationMs} samples=${elapsed.samples} sample_rate=${elapsed.sampleRate} pss_kb=${info.totalPss} turn_ms=${SystemClock.elapsedRealtime()-start}")
                    runOnUiThread { status.text = "Kokoro inference $runId\nModel load: ${loadMs} ms\n\n${results.joinToString("\n\n")}" }
                }
                engine.close(); focus.abandonAudioFocusRequest(request)
                Log.i(tag, "event=debug_harness_complete run_id=$runId success=true")
                runOnUiThread { status.text = "Kokoro inference $runId\nCompleted on device\nModel load: ${loadMs} ms\n\n${results.joinToString("\n\n")}" }
            } catch (t: Throwable) {
                Log.e(tag, "event=debug_harness_failed run_id=$runId type=${t.javaClass.name} detail=${t.message}", t)
                runOnUiThread { status.text = "Kokoro inference $runId failed\n${t.javaClass.simpleName}: ${t.message}" }
            }
        }.start()
    }
}
