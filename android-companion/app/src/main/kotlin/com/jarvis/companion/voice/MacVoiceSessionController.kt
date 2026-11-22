package com.jarvis.companion.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.AudioRecord
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import com.jarvis.companion.data.SecureStore
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.net.URI
import java.util.ArrayDeque
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.math.sqrt

enum class MacVoiceLinkState { CONNECTING, LISTENING, PROCESSING, SPEAKING, DISCONNECTED }

/** Authenticated, loopback-only USB-reverse audio session. It never exposes Mac control tools. */
class MacVoiceSessionController(context: Context, private val listener: Listener) : AutoCloseable {
    interface Listener {
        fun onState(state: MacVoiceLinkState) {}
        fun onTranscript(text: String) {}
        fun onResponseText(text: String) {}
        fun onError(message: String) {}
        fun onSessionReady() {}
        fun onInterrupted() {}
    }

    private val app = context.applicationContext
    private val secure = SecureStore(app)
    private val endpointResolver: VoiceSessionEndpointResolver = PairedLoopbackVoiceEndpointResolver(app)
    private val main = Handler(Looper.getMainLooper())
    private val audioManager = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val audioExecutor = Executors.newSingleThreadExecutor { r -> Thread(r, "thozha-voice-playback") }
    private val client = OkHttpClient.Builder()
        .pingInterval(15, TimeUnit.SECONDS)
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(4, TimeUnit.SECONDS)
        .build()
    private val sessionId = UUID.randomUUID().toString()
    @Volatile private var socket: WebSocket? = null
    @Volatile private var closed = false
    @Volatile private var running = false
    @Volatile private var muted = false
    @Volatile private var awaitingResponse = false
    @Volatile private var assistantAudioActive = false
    private var recordThread: Thread? = null
    private var recorder: AudioRecord? = null
    private var echoCanceler: AcousticEchoCanceler? = null
    private var noiseSuppressor: NoiseSuppressor? = null
    private var track: AudioTrack? = null
    private var trackRate = 0
    private val outputSequence = VoiceOutputSequence()
    private var inputSequence = 0L
    private var focusRequest: AudioFocusRequest? = null
    private val focusListener = AudioManager.OnAudioFocusChangeListener { change ->
        if (change <= AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) interrupt()
    }

    fun connect() {
        check(!closed) { "voice session is closed" }
        val token = secure.token()?.takeIf(String::isNotBlank) ?: return fail("This phone is not paired with the Mac.")
        val deviceId = secure.deviceId().takeIf { it.matches(Regex("[0-9a-f]{32}")) }
            ?: return fail("The paired device identity is unavailable.")
        val endpoint = runCatching { endpointResolver.resolve() }.getOrElse { return fail(it.message ?: "The paired Mac voice endpoint is unavailable.") }
        publishState(MacVoiceLinkState.CONNECTING)
        val request = Request.Builder()
            .url(endpoint.url)
            .header("Authorization", "Bearer $token")
            .build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "event=socket_open session=$sessionId transport=${endpoint.transport} elapsed_ms=${SystemClock.elapsedRealtime()}")
                webSocket.send(JSONObject().put("type", "start").put("protocol", 1).put("session_id", sessionId)
                    .put("device_id", deviceId).put("sample_rate", 16_000).put("channels", 1).toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleControl(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                val frame = runCatching { VoicePcmProtocol.decodeMacFrame(bytes.toByteArray()) }.getOrElse {
                    Log.w(TAG, "event=output_frame_rejected reason=${it.message}")
                    interrupt()
                    return
                }
                audioExecutor.execute { playFrame(frame) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "event=socket_failed type=${t.javaClass.simpleName} http=${response?.code ?: 0}")
                stopCapture()
                publishState(MacVoiceLinkState.DISCONNECTED)
                publishError(if (response?.code == 1008) "Mac voice session authentication was refused." else "Mac voice connection failed; phone-local voice is available.")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                stopCapture()
                publishState(MacVoiceLinkState.DISCONNECTED)
                if (!closed) publishError("Mac voice session disconnected.")
            }
        })
    }

    fun respond(speaker: String) {
        if (speaker !in setOf("Ryan", "Aiden", "Vivian", "Serena")) return fail("That Mac voice is unavailable.")
        sendControl("respond", "speaker" to speaker)
    }

    fun speakLocalResult(text: String, speaker: String) {
        if (speaker !in setOf("Ryan", "Aiden", "Vivian", "Serena") || text.isBlank() || text.length > 2_000) {
            return fail("The local response could not be sent to the selected Mac voice.")
        }
        sendControl("local_speech", "speaker" to speaker, "text" to text)
    }

    fun completeLocally() {
        awaitingResponse = false
        sendControl("local_complete")
    }

    fun setMuted(value: Boolean) {
        muted = value
        if (value) {
            awaitingResponse = false
            sendControl("interrupt")
            stopPlayback()
            publishState(MacVoiceLinkState.LISTENING)
        }
    }

    /** Stops current PCM immediately; live capture continues so the next turn can interrupt. */
    fun interrupt() {
        if (closed) return
        awaitingResponse = false
        inputSequence = 0
        sendControl("interrupt")
        stopPlayback()
        publishState(MacVoiceLinkState.LISTENING)
        Log.i(TAG, "event=interrupt elapsed_ms=${SystemClock.elapsedRealtime()}")
    }

    private fun handleControl(raw: String) {
        val json = runCatching { JSONObject(raw) }.getOrNull() ?: return fail("Mac voice sent an invalid control message.")
        if (json.optString("session_id") != sessionId) return fail("Mac voice session identity did not match.")
        when (json.optString("type")) {
            "session_ready" -> {
                val sample = json.optInt("sample_rate")
                if (sample != 16_000) return fail("Mac voice input format was not accepted.")
                if (startCapture()) {
                    Log.i(TAG, "event=session_ready session=$sessionId tts_load_ms=${json.optLong("tts_load_ms", -1)} elapsed_ms=${SystemClock.elapsedRealtime()}")
                    publishState(MacVoiceLinkState.LISTENING)
                    main.post { listener.onSessionReady() }
                }
            }
            "processing" -> publishState(MacVoiceLinkState.PROCESSING)
            "transcript" -> {
                val transcript = json.optString("text")
                awaitingResponse = true
                publishState(MacVoiceLinkState.PROCESSING)
                Log.i(TAG, "event=transcript_received chars=${transcript.length} asr_ms=${json.optLong("asr_ms", -1)} elapsed_ms=${SystemClock.elapsedRealtime()}")
                main.post { listener.onTranscript(transcript) }
            }
            "thinking" -> {
                outputSequence.reset()
                publishState(MacVoiceLinkState.PROCESSING)
            }
            "response_text" -> {
                val text = json.optString("text")
                main.post { listener.onResponseText(text) }
            }
            "speaking" -> {
                assistantAudioActive = true
                publishState(MacVoiceLinkState.SPEAKING)
            }
            "turn_complete" -> {
                awaitingResponse = false
                if (track != null) drainPlayback() else publishState(MacVoiceLinkState.LISTENING)
            }
            "interrupted", "no_match" -> {
                awaitingResponse = false
                assistantAudioActive = false
                stopPlayback()
                publishState(MacVoiceLinkState.LISTENING)
                if (json.optString("type") == "interrupted") main.post { if (!closed) listener.onInterrupted() }
            }
            "error" -> {
                awaitingResponse = false
                val detail = json.optString("detail", "Mac voice failed this turn.")
                publishError(detail)
                publishState(MacVoiceLinkState.LISTENING)
            }
        }
    }

    private fun startCapture(): Boolean {
        if (androidx.core.content.ContextCompat.checkSelfPermission(app, android.Manifest.permission.RECORD_AUDIO) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            fail("Microphone permission is required for Mac voice.")
            return false
        }
        val min = AudioRecord.getMinBufferSize(16_000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (min <= 0 || !requestAudioFocus()) {
            fail("The microphone or media audio route is unavailable.")
            return false
        }
        return try {
            val record = AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                .setAudioFormat(AudioFormat.Builder().setSampleRate(16_000).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build())
                .setBufferSizeInBytes(maxOf(min * 3, 16_000 * 2 / 2))
                .build()
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                record.release()
                fail("Android did not initialize the voice microphone.")
                return false
            }
            recorder = record
            echoCanceler = if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(record.audioSessionId)?.apply { enabled = true } else null
            noiseSuppressor = if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(record.audioSessionId)?.apply { enabled = true } else null
            record.startRecording()
            running = true
            recordThread = Thread({ captureLoop(record) }, "thozha-live-capture").apply { isDaemon = true; start() }
            Log.i(TAG, "event=capture_started sample_rate=16000 frame_ms=20 aec=${echoCanceler?.enabled == true} ns=${noiseSuppressor?.enabled == true} elapsed_ms=${SystemClock.elapsedRealtime()}")
            true
        } catch (t: Throwable) {
            stopCapture()
            fail("Android could not start the microphone (${t.javaClass.simpleName}).")
            false
        }
    }

    private fun captureLoop(record: AudioRecord) {
        val frame = ShortArray(320)
        val preRoll = ArrayDeque<ShortArray>(5)
        var activeVoice = false
        var voicedFrames = 0
        var silenceFrames = 0
        var seq = 0L
        while (running && !closed) {
            val read = record.read(frame, 0, frame.size, AudioRecord.READ_BLOCKING)
            if (read <= 0) continue
            val copy = frame.copyOf(read)
            if (preRoll.size == 5) preRoll.removeFirst()
            preRoll.addLast(copy)
            if (muted) continue
            var sum = 0.0
            for (i in 0 until read) sum += copy[i].toDouble() * copy[i]
            val rms = sqrt(sum / read)
            val voiceDetected = rms > 950.0
            if (!activeVoice) {
                voicedFrames = if (voiceDetected) voicedFrames + 1 else 0
                if (voicedFrames >= 2) {
                    if (awaitingResponse || assistantAudioActive) {
                        awaitingResponse = false
                        stopPlayback()
                        sendControl("interrupt")
                        Log.i(TAG, "event=barge_in_detected rms=${rms.toInt()} elapsed_ms=${SystemClock.elapsedRealtime()}")
                    }
                    activeVoice = true
                    silenceFrames = 0
                    seq = 0
                    inputSequence = 0
                    preRoll.forEach { samples -> sendPcm(samples, samples.size, seq++) }
                }
            } else {
                sendPcm(copy, read, seq++)
                inputSequence = seq
                silenceFrames = if (voiceDetected) 0 else silenceFrames + 1
                if (silenceFrames >= 35) {
                    activeVoice = false
                    voicedFrames = 0
                    sendControl("end_turn", "next_sequence" to seq)
                    awaitingResponse = true
                    inputSequence = 0
                    Log.i(TAG, "event=speech_end frames=$seq elapsed_ms=${SystemClock.elapsedRealtime()}")
                }
            }
        }
    }

    private fun sendPcm(samples: ShortArray, count: Int, sequence: Long) {
        if (count <= 0 || count * 2 + VoicePcmProtocol.HEADER_BYTES > 64 * 1024) return
        val frame = VoicePcmProtocol.encodePhoneFrame(sequence, SystemClock.elapsedRealtime(), samples, count)
        if (socket?.send(ByteString.of(*frame)) != true) Log.w(TAG, "event=input_frame_send_failed seq=$sequence")
    }

    private fun playFrame(frame: VoicePcmFrame) {
        if (closed || !outputSequence.accept(frame.sequence)) {
            Log.w(TAG, "event=output_sequence_mismatch actual=${frame.sequence}")
            interrupt()
            return
        }
        if (!requestAudioFocus()) return
        if (track == null || trackRate != frame.sampleRate) {
            stopPlayback(resetSequence = false, clearSpeaking = false)
            val min = AudioTrack.getMinBufferSize(frame.sampleRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            if (min <= 0) return fail("Android cannot play the Mac voice audio format.")
            val builder = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(AudioFormat.Builder().setSampleRate(frame.sampleRate).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                .setBufferSizeInBytes(maxOf(min * 2, frame.sampleRate * 2 / 2))
                .setTransferMode(AudioTrack.MODE_STREAM)
            track = builder.build().also { if (it.state == AudioTrack.STATE_INITIALIZED) it.play() else { it.release(); track = null; return fail("Android audio output failed to initialize.") } }
            trackRate = frame.sampleRate
            Log.i(TAG, "event=playback_started sample_rate=${frame.sampleRate} first_audio_elapsed_ms=${SystemClock.elapsedRealtime()}")
        }
        val pcm = java.nio.ByteBuffer.wrap(frame.pcm).order(java.nio.ByteOrder.LITTLE_ENDIAN).asShortBuffer()
        val samples = ShortArray(pcm.remaining()).also(pcm::get)
        val written = track?.write(samples, 0, samples.size, AudioTrack.WRITE_BLOCKING) ?: -1
        if (written > 0) outputSamplesWritten += written
        if (written <= 0) Log.w(TAG, "event=playback_write_failed result=$written")
    }

    @Volatile private var outputSamplesWritten = 0

    private fun drainPlayback() {
        audioExecutor.execute {
            val current = track ?: return@execute
            val expected = outputSamplesWritten
            val deadline = SystemClock.elapsedRealtime() + 15_000
            while (!closed && current.playbackHeadPosition < expected && SystemClock.elapsedRealtime() < deadline) {
                try { Thread.sleep(10) } catch (_: InterruptedException) { return@execute }
            }
            if (track === current) {
                stopPlayback()
                publishState(MacVoiceLinkState.LISTENING)
            }
        }
    }

    private fun requestAudioFocus(): Boolean {
        val attributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()
        return if (Build.VERSION.SDK_INT >= 26) {
            val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE).setAudioAttributes(attributes).setOnAudioFocusChangeListener(focusListener).build()
            focusRequest = request
            audioManager.requestAudioFocus(request) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        } else audioManager.requestAudioFocus(focusListener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
    }

    private fun sendControl(type: String, vararg fields: Pair<String, Any>) {
        val json = JSONObject().put("type", type).put("session_id", sessionId)
        fields.forEach { (key, value) -> json.put(key, value) }
        socket?.send(json.toString())
    }

    private fun stopPlayback(resetSequence: Boolean = true, clearSpeaking: Boolean = true) {
        val old = track
        track = null
        trackRate = 0
        if (resetSequence) outputSequence.reset()
        outputSamplesWritten = 0
        if (clearSpeaking) assistantAudioActive = false
        runCatching { old?.pause(); old?.flush(); old?.stop() }
        runCatching { old?.release() }
    }

    private fun stopCapture() {
        running = false
        val old = recorder
        recorder = null
        runCatching { old?.stop() }
        runCatching { old?.release() }
        recordThread?.interrupt()
        recordThread = null
        runCatching { echoCanceler?.release() }; echoCanceler = null
        runCatching { noiseSuppressor?.release() }; noiseSuppressor = null
        abandonAudioFocus()
    }

    private fun abandonAudioFocus() {
        if (Build.VERSION.SDK_INT >= 26) focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        else audioManager.abandonAudioFocus(focusListener)
        focusRequest = null
    }

    private fun publishState(value: MacVoiceLinkState) = main.post { if (!closed) listener.onState(value) }
    private fun publishError(value: String) = main.post { if (!closed) listener.onError(value) }
    private fun fail(message: String) { Log.w(TAG, "event=voice_session_unavailable detail=$message"); publishState(MacVoiceLinkState.DISCONNECTED); publishError(message) }

    override fun close() {
        if (closed) return
        closed = true
        running = false
        sendControl("end_session")
        socket?.close(1000, "call ended")
        stopCapture()
        stopPlayback()
        abandonAudioFocus()
        audioExecutor.shutdownNow()
        client.dispatcher.executorService.shutdown()
        client.connectionPool.evictAll()
        Log.i(TAG, "event=session_released session=$sessionId elapsed_ms=${SystemClock.elapsedRealtime()}")
    }

    companion object { private const val TAG = "ThozhaLive" }
}
