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

/** Debug-only in-process deterministic harness; invoke by explicit ADB component. */
class KokoroInferenceDebugActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val status = TextView(this).apply { text = "Running Kokoro on-device inference…"; textSize = 20f }
        setContentView(status)
        Thread {
            val tag = "JarvisNeuralTts"
            try {
                Log.i(tag, "event=debug_harness_enter uid=${android.os.Process.myUid()} package=$packageName")
                val dir = KokoroAssets.provision(applicationContext)
                val engine = KokoroNeuralTts(dir)
                val loadMs = engine.load()
                val focus = getSystemService(AUDIO_SERVICE) as AudioManager
                val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                    .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setOnAudioFocusChangeListener {}.build()
                check(focus.requestAudioFocus(request) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) { "Media audio focus denied" }
                for ((sid, name) in listOf(5 to "am_adam", 1 to "af_bella")) {
                    val start = SystemClock.elapsedRealtime()
                    val elapsed = engine.speak("Good evening. I'm Jarvis. Your assistant is ready. What would you like to do today?", sid)
                    Log.i(tag, "event=debug_voice_complete name=$name sid=$sid model_load_ms=$loadMs synthesis_and_play_ms=$elapsed turn_ms=${SystemClock.elapsedRealtime()-start}")
                    val info = Debug.MemoryInfo(); Debug.getMemoryInfo(info)
                    Log.i(tag, "event=memory pss_kb=${info.totalPss} private_dirty_kb=${info.totalPrivateDirty}")
                }
                engine.close(); focus.abandonAudioFocusRequest(request)
                Log.i(tag, "event=debug_harness_complete success=true")
                runOnUiThread { status.text = "Kokoro male and female samples completed. Review JarvisNeuralTts logcat." }
            } catch (t: Throwable) {
                Log.e(tag, "event=debug_harness_failed type=${t.javaClass.name} detail=${t.message}", t)
                runOnUiThread { status.text = "Kokoro failed: ${t.javaClass.simpleName}: ${t.message}" }
            }
        }.start()
    }
}
