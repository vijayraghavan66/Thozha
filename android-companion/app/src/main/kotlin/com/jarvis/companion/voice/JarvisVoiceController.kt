package com.jarvis.companion.voice

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import androidx.core.content.ContextCompat
import java.util.Locale
import java.util.UUID
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

private enum class CaptureMode { PUSH_TO_TALK, AUTOMATIC_ENDPOINTER, BARGE_IN }

/** Stable, UI-agnostic voice states for a lead-owned Compose or Activity control. */
enum class VoiceControllerState { IDLE, LISTENING, PROCESSING, SPEAKING, ERROR }

enum class VoiceControllerError {
    PERMISSION_REQUIRED,
    RECOGNIZER_UNAVAILABLE,
    MICROPHONE_UNAVAILABLE,
    NO_MATCH,
    NETWORK_OR_SERVICE,
    RECOGNIZER_BUSY,
    SPEECH_TIMEOUT,
    LANGUAGE_UNAVAILABLE,
    SYNTHESIS_UNAVAILABLE,
    SYNTHESIS_FAILED,
    UNKNOWN,
}

/**
 * Voice boundary owned independently of the chat UI. Call [startPushToTalk] on press and
 * [releasePushToTalk] on release. [Listener.onFinalText] returns recognized text to the caller;
 * this class never submits it or executes a command itself.
 *
 * All control methods must be called on Android's main thread. Recognition uses the platform
 * speech service (preferring on-device recognition when available) and audio is never logged.
 */
class JarvisVoiceController(
    context: Context,
    private val listener: Listener,
    private val locale: Locale = Locale.getDefault(),
) : AutoCloseable {
    interface Listener {
        fun onStateChanged(state: VoiceControllerState) {}
        fun onRecognitionProvider(provider: VoiceRecognitionProvider) {}
        fun onOutputVoiceSelected(profile: VoiceOutputProfile) {}
        fun onPartialText(text: String) {}
        /** The platform recognizer's speech-start/VAD event interrupted active TTS playback. */
        fun onBargeInDetected() {}
        fun onFinalText(text: String) {}
        fun onError(error: VoiceControllerError) {}
    }

    private val appContext = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val audioManager = appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var recognizer: SpeechRecognizer? = null
    private var textToSpeech: TextToSpeech? = null
    private var neuralTts: KokoroNeuralTts? = null
    @Volatile private var neuralSpeaking = false
    private val neuralCancelled = AtomicBoolean(false)
    private var ttsInitialized = false
    private var ttsReady = false
    private var pendingSpeech: String? = null
    private var state = VoiceControllerState.IDLE
    private var closed = false
    private var activeRecognitionProvider = VoiceRecognitionProvider.SYSTEM_FALLBACK
    private val sessionGeneration = VoiceSessionGeneration()
    private val bargeInGate = VoiceBargeInGate()
    private var captureMode: CaptureMode? = null
    private var activeUtteranceId: String? = null
    private var focusRequest: AudioFocusRequest? = null
    private val focusListener = AudioManager.OnAudioFocusChangeListener { change ->
        if (VoiceFocusPolicy.shouldStop(change)) {
            // Stop on transient, duck, and permanent loss; do not leave the mic/speaker active.
            cancel()
        }
    }

    init {
        checkMainThread()
        textToSpeech = TextToSpeech(appContext) { status ->
            main.post {
                if (closed) return@post
                val engine = textToSpeech
                ttsInitialized = true
                ttsReady = status == TextToSpeech.SUCCESS && engine != null && configureTts(engine)
                val queued = pendingSpeech
                pendingSpeech = null
                if (queued != null) {
                    if (ttsReady) speakNow(queued) else fail(VoiceControllerError.SYNTHESIS_UNAVAILABLE)
                }
            }
        }
    }

    val currentState: VoiceControllerState get() = state

    /** Call only after the Activity/Compose permission launcher has granted RECORD_AUDIO. */
    fun startPushToTalk(): Boolean = startCapture(CaptureMode.PUSH_TO_TALK)

    /** One natural speech turn; the platform endpointer closes the utterance at a pause. Still never auto-submits. */
    fun startVoiceTurn(): Boolean = startCapture(CaptureMode.AUTOMATIC_ENDPOINTER)

    /**
     * Start live platform ASR while the current TTS utterance is still playing. Android's
     * recognizer owns microphone streaming and supplies its speech-start VAD event; on that
     * event Jarvis stops its own TTS but leaves recognition running to capture the interruption.
     * This depends on the installed recognition service and is not guaranteed to suppress speaker
     * echo on every device. It never submits recognized text.
     */
    fun startBargeInTurn(): Boolean {
        checkMainThread()
        if (state != VoiceControllerState.SPEAKING || (activeUtteranceId == null && !neuralSpeaking)) return false
        return startCapture(CaptureMode.BARGE_IN)
    }

    private fun startCapture(mode: CaptureMode): Boolean {
        checkMainThread()
        if (closed) return false
        if (ContextCompat.checkSelfPermission(appContext, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            fail(VoiceControllerError.PERMISSION_REQUIRED)
            return false
        }
        if (state == VoiceControllerState.LISTENING) return captureMode == mode
        val outputWasSpeaking = state == VoiceControllerState.SPEAKING && activeUtteranceId != null
        if (mode == CaptureMode.BARGE_IN && !outputWasSpeaking) return false
        if (!SpeechRecognizer.isRecognitionAvailable(appContext)) {
            fail(VoiceControllerError.RECOGNIZER_UNAVAILABLE)
            return false
        }
        if (outputWasSpeaking && mode != CaptureMode.BARGE_IN) stopSpeaking()
        if (recognizer != null) cancelRecognition()
        // Reuse the focus already held for speech output while starting a barge-in recognizer.
        if (!outputWasSpeaking && !requestFocus()) {
            fail(VoiceControllerError.MICROPHONE_UNAVAILABLE)
            return false
        }
        return try {
            val speechRecognizer = createRecognizer()
            val generation = sessionGeneration.begin()
            recognizer = speechRecognizer
            captureMode = mode
            if (mode == CaptureMode.BARGE_IN) bargeInGate.arm()
            speechRecognizer.setRecognitionListener(recognitionListener(speechRecognizer, generation))
            post { if (sessionGeneration.isCurrent(generation)) listener.onRecognitionProvider(activeRecognitionProvider) }
            speechRecognizer.startListening(recognitionIntent())
            setState(VoiceControllerState.LISTENING, generation)
            true
        } catch (_: SecurityException) {
            cancelRecognition()
            if (outputWasSpeaking) stopSpeaking() else abandonFocus()
            fail(VoiceControllerError.PERMISSION_REQUIRED)
            false
        } catch (_: RuntimeException) {
            cancelRecognition()
            if (outputWasSpeaking) stopSpeaking() else abandonFocus()
            fail(VoiceControllerError.MICROPHONE_UNAVAILABLE)
            false
        }
    }

    /** Release event for hold-to-talk; the speech service finalizes the current utterance. */
    fun releasePushToTalk() {
        checkMainThread()
        if (closed || state != VoiceControllerState.LISTENING || captureMode != CaptureMode.PUSH_TO_TALK) return
        val generation = sessionGeneration.value
        setState(VoiceControllerState.PROCESSING, generation)
        try {
            recognizer?.stopListening()
        } catch (_: RuntimeException) {
            val failedGeneration = sessionGeneration.invalidate()
            val old = recognizer
            recognizer = null
            captureMode = null
            try { old?.cancel(); old?.destroy() } catch (_: RuntimeException) { }
            abandonFocus()
            fail(VoiceControllerError.MICROPHONE_UNAVAILABLE, failedGeneration)
        }
    }

    /** Interrupts either recording or speech output without submitting recognized content. */
    fun cancel() {
        checkMainThread()
        if (closed) return
        cancelRecognition()
        neuralCancelled.set(true)
        try { textToSpeech?.stop() } catch (_: RuntimeException) { }
        activeUtteranceId = null
        pendingSpeech = null
        abandonFocus()
        setState(VoiceControllerState.IDLE)
    }

    /** Speak assistant output. This is presentation only and cannot execute or approve a task. */
    fun speak(text: String) {
        checkMainThread()
        if (closed) return
        val safeText = text.trim().take(MAX_SPEECH_CHARS)
        if (safeText.isEmpty()) return
        if (!ttsInitialized) {
            pendingSpeech = safeText
            return
        }
        if (!ttsReady) { fail(VoiceControllerError.SYNTHESIS_UNAVAILABLE); return }
        speakNow(safeText)
    }

    fun speakNeuralMale(text: String) = speakNeural(text, 5, "am_adam")

    fun speakNeural(text: String, sid: Int, voiceName: String) {
        checkMainThread()
        if (closed || text.isBlank()) return
        neuralCancelled.set(false)
        neuralSpeaking = true
        try { textToSpeech?.stop() } catch (_: RuntimeException) { }
        activeUtteranceId = null
        if (!requestFocus()) { fail(VoiceControllerError.SYNTHESIS_FAILED); return }
        android.util.Log.i("JarvisNeuralTts", "event=button_callback_enter voice=$voiceName sid=$sid")
        setState(VoiceControllerState.SPEAKING)
        Thread {
            val started = android.os.SystemClock.elapsedRealtime()
            try {
                val engine = neuralTts ?: run {
                    val privateModel = KokoroAssets.provision(appContext)
                    KokoroNeuralTts(privateModel).also { val loadMs = it.load(); neuralTts = it; android.util.Log.i("JarvisNeuralTts", "event=model_loaded model=kokoro-en-v0_19 load_ms=$loadMs speakers=${it.speakerCount()}") }
                }
                val synthMs = engine.speak(text, sid) { neuralCancelled.get() || closed }
                android.util.Log.i("JarvisNeuralTts", "event=pcm_playback_finished voice=$voiceName sid=$sid synth_play_ms=$synthMs cancelled=${neuralCancelled.get()} elapsed_ms=${android.os.SystemClock.elapsedRealtime()}")
                main.post { neuralSpeaking = false; if (!closed && !neuralCancelled.get()) { abandonFocus(); setState(VoiceControllerState.IDLE) } }
            } catch (failure: Throwable) {
                android.util.Log.e("JarvisNeuralTts", "event=neural_failed voice=$voiceName sid=$sid elapsed_ms=${android.os.SystemClock.elapsedRealtime() - started} type=${failure.javaClass.simpleName} detail=${failure.message?.take(300)}")
                main.post { neuralSpeaking = false; if (!closed && !neuralCancelled.get()) { listener.onError(VoiceControllerError.SYNTHESIS_FAILED); abandonFocus(); speak(text) } }
            }
        }.start()
    }

    fun stopSpeaking() {
        checkMainThread()
        if (closed) return
        stopTts(preserveCapture = captureMode == CaptureMode.BARGE_IN && state == VoiceControllerState.LISTENING)
    }

    override fun close() {
        checkMainThread()
        if (closed) return
        closed = true
        sessionGeneration.invalidate()
        try { recognizer?.cancel() } catch (_: RuntimeException) { }
        try { recognizer?.destroy() } catch (_: RuntimeException) { }
        recognizer = null
        captureMode = null
        bargeInGate.disarm()
        neuralCancelled.set(true)
        try { neuralTts?.close() } catch (_: RuntimeException) { }
        neuralTts = null
        try { textToSpeech?.stop(); textToSpeech?.shutdown() } catch (_: RuntimeException) { }
        textToSpeech = null
        activeUtteranceId = null
        pendingSpeech = null
        abandonFocus()
    }

    private fun createRecognizer(): SpeechRecognizer =
        if (android.os.Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(appContext)) {
            activeRecognitionProvider = VoiceRecognitionProvider.ON_DEVICE
            SpeechRecognizer.createOnDeviceSpeechRecognizer(appContext)
        } else {
            activeRecognitionProvider = VoiceRecognitionProvider.SYSTEM_FALLBACK
            SpeechRecognizer.createSpeechRecognizer(appContext)
        }

    private fun recognitionIntent(): Intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        putExtra(RecognizerIntent.EXTRA_LANGUAGE, locale.toLanguageTag())
        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
        putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, appContext.packageName)
    }

    private fun recognitionListener(owner: SpeechRecognizer, generation: Long) = object : RecognitionListener {
        private fun current() = !closed && recognizer === owner && sessionGeneration.isCurrent(generation)

        override fun onReadyForSpeech(params: Bundle?) { }
        override fun onBeginningOfSpeech() {
            if (current() && captureMode == CaptureMode.BARGE_IN && bargeInGate.onSpeechStarted()) {
                stopTts(preserveCapture = true)
                postIfCurrent(generation) { listener.onBargeInDetected() }
            }
        }
        override fun onRmsChanged(rmsdB: Float) { }
        override fun onBufferReceived(buffer: ByteArray?) { }
        override fun onEndOfSpeech() { if (current()) setState(VoiceControllerState.PROCESSING, generation) }
        override fun onEvent(eventType: Int, params: Bundle?) { }

        override fun onPartialResults(partialResults: Bundle?) {
            val text = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
                ?.takeIf { it.isNotBlank() } ?: return
            postIfCurrent(generation) { listener.onPartialText(text) }
        }

        override fun onResults(results: Bundle?) {
            if (!current()) return
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim()
            val completedGeneration = sessionGeneration.invalidate()
            recognizer = null
            captureMode = null
            bargeInGate.disarm()
            try { owner.destroy() } catch (_: RuntimeException) { }
            abandonFocus()
            if (text.isNullOrEmpty()) {
                fail(VoiceControllerError.NO_MATCH, completedGeneration)
            } else {
                setState(VoiceControllerState.IDLE, completedGeneration)
                postIfCurrent(completedGeneration) { listener.onFinalText(text) }
            }
        }

        override fun onError(error: Int) {
            if (!current()) return
            val completedGeneration = sessionGeneration.invalidate()
            recognizer = null
            captureMode = null
            bargeInGate.disarm()
            try { owner.destroy() } catch (_: RuntimeException) { }
            abandonFocus()
            fail(mapRecognitionError(error), completedGeneration)
        }
    }

    private fun configureTts(engine: TextToSpeech): Boolean {
        val availableVoices = engine.voices.orEmpty()
        val selected = VoicePolicy.bestOfflineVoice(
            availableVoices.map { voice ->
                VoiceCandidate(
                    name = voice.name,
                    localeTag = voice.locale.toLanguageTag(),
                    quality = voice.quality,
                    latency = voice.latency,
                    networkRequired = voice.isNetworkConnectionRequired,
                    dataInstalled = TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED !in voice.features.orEmpty(),
                )
            },
            locale,
        ) ?: return false
        val voice = availableVoices.firstOrNull { it.name == selected.name } ?: return false
        if (engine.setVoice(voice) != TextToSpeech.SUCCESS) return false
        val profile = selected.toProfile()
        post { if (!closed) listener.onOutputVoiceSelected(profile) }
        engine.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build(),
        )
        engine.setSpeechRate(1.0f)
        engine.setPitch(1.0f)
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = post {
                if (!closed && utteranceId != null && utteranceId == activeUtteranceId) setState(VoiceControllerState.SPEAKING)
            }
            override fun onDone(utteranceId: String?) = post {
                if (!closed && utteranceId != null && utteranceId == activeUtteranceId) {
                    activeUtteranceId = null
                    if (captureMode == CaptureMode.BARGE_IN && state == VoiceControllerState.LISTENING) {
                        bargeInGate.disarm()
                        return@post
                    }
                    abandonFocus()
                    setState(VoiceControllerState.IDLE)
                }
            }
            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) = post {
                if (!closed && utteranceId != null && utteranceId == activeUtteranceId) {
                    activeUtteranceId = null
                    if (captureMode == CaptureMode.BARGE_IN && state == VoiceControllerState.LISTENING) {
                        bargeInGate.disarm()
                        return@post
                    }
                    abandonFocus()
                    fail(VoiceControllerError.SYNTHESIS_FAILED)
                }
            }
            override fun onError(utteranceId: String?, errorCode: Int) = post {
                if (!closed && utteranceId != null && utteranceId == activeUtteranceId) {
                    activeUtteranceId = null
                    if (captureMode == CaptureMode.BARGE_IN && state == VoiceControllerState.LISTENING) {
                        bargeInGate.disarm()
                        return@post
                    }
                    abandonFocus()
                    fail(VoiceControllerError.SYNTHESIS_FAILED)
                }
            }
        })
        return true
    }

    private fun speakNow(text: String) {
        val engine = textToSpeech ?: run { fail(VoiceControllerError.SYNTHESIS_UNAVAILABLE); return }
        if (!requestFocus()) { fail(VoiceControllerError.SYNTHESIS_FAILED); return }
        val utteranceId = "jarvis-${UUID.randomUUID()}"
        activeUtteranceId = utteranceId
        val result = try {
            engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, utteranceId)
        } catch (_: RuntimeException) { TextToSpeech.ERROR }
        if (result == TextToSpeech.ERROR) {
            activeUtteranceId = null
            abandonFocus()
            fail(VoiceControllerError.SYNTHESIS_FAILED)
        } else {
            setState(VoiceControllerState.SPEAKING)
        }
    }

    private fun requestFocus(): Boolean {
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
            .setAudioAttributes(
                AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build(),
            )
            .setAcceptsDelayedFocusGain(false)
            .setOnAudioFocusChangeListener(focusListener, main)
            .build()
        focusRequest = request
        val granted = audioManager.requestAudioFocus(request) == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        if (!granted) focusRequest = null
        return granted
    }

    private fun abandonFocus() {
        focusRequest?.let { runCatching { audioManager.abandonAudioFocusRequest(it) } }
        focusRequest = null
    }

    private fun mapRecognitionError(code: Int): VoiceControllerError = when (code) {
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> VoiceControllerError.PERMISSION_REQUIRED
        SpeechRecognizer.ERROR_NO_MATCH -> VoiceControllerError.NO_MATCH
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> VoiceControllerError.SPEECH_TIMEOUT
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> VoiceControllerError.RECOGNIZER_BUSY
        SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED, SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE -> VoiceControllerError.LANGUAGE_UNAVAILABLE
        SpeechRecognizer.ERROR_AUDIO -> VoiceControllerError.MICROPHONE_UNAVAILABLE
        SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT, SpeechRecognizer.ERROR_SERVER,
        SpeechRecognizer.ERROR_SERVER_DISCONNECTED -> VoiceControllerError.NETWORK_OR_SERVICE
        else -> VoiceControllerError.UNKNOWN
    }

    private fun cancelRecognition() {
        sessionGeneration.invalidate()
        bargeInGate.disarm()
        val old = recognizer
        recognizer = null
        captureMode = null
        if (old != null) {
            try { old.cancel() } catch (_: RuntimeException) { }
            try { old.destroy() } catch (_: RuntimeException) { }
        }
    }

    private fun stopTts(preserveCapture: Boolean) {
        pendingSpeech = null
        activeUtteranceId = null
        if (neuralSpeaking) neuralCancelled.set(true)
        bargeInGate.disarm()
        try { textToSpeech?.stop() } catch (_: RuntimeException) { }
        if (!preserveCapture) {
            abandonFocus()
            if (state == VoiceControllerState.SPEAKING) setState(VoiceControllerState.IDLE)
        }
    }

    private fun setState(next: VoiceControllerState, generation: Long? = null) {
        if (closed || state == next) return
        state = next
        post {
            if (!closed && (generation == null || sessionGeneration.isCurrent(generation))) listener.onStateChanged(next)
        }
    }

    private fun fail(error: VoiceControllerError, generation: Long? = null) {
        if (closed || (generation != null && !sessionGeneration.isCurrent(generation))) return
        setState(VoiceControllerState.ERROR, generation)
        post {
            if (!closed && (generation == null || sessionGeneration.isCurrent(generation))) listener.onError(error)
        }
    }

    private fun postIfCurrent(generation: Long, block: () -> Unit) {
        post { if (!closed && sessionGeneration.isCurrent(generation)) block() }
    }

    private fun post(block: () -> Unit) {
        if (Looper.myLooper() == Looper.getMainLooper()) block() else main.post(block)
    }

    private fun checkMainThread() {
        check(Looper.myLooper() == Looper.getMainLooper()) { "JarvisVoiceController must be called on the main thread" }
    }

    companion object {
        private const val MAX_SPEECH_CHARS = 12_000

        /** Safe for the calling Activity/Composable to use before requesting runtime permission. */
        fun hasMicrophonePermission(context: Context): Boolean =
            ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

        fun isRecognitionAvailable(context: Context): Boolean = SpeechRecognizer.isRecognitionAvailable(context)
    }
}
