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
        fun onPartialText(text: String) {}
        fun onFinalText(text: String) {}
        fun onError(error: VoiceControllerError) {}
    }

    private val appContext = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val audioManager = appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var recognizer: SpeechRecognizer? = null
    private var textToSpeech: TextToSpeech? = null
    private var ttsReady = false
    private var pendingSpeech: String? = null
    private var state = VoiceControllerState.IDLE
    private var closed = false
    private var focusRequest: AudioFocusRequest? = null
    private val focusListener = AudioManager.OnAudioFocusChangeListener { change ->
        if (change <= AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
            // Do not continue recording or speaking after another app takes exclusive audio focus.
            cancel()
        }
    }

    init {
        checkMainThread()
        textToSpeech = TextToSpeech(appContext) { status ->
            main.post {
                if (closed) return@post
                val engine = textToSpeech
                ttsReady = status == TextToSpeech.SUCCESS && engine != null && configureTts(engine)
                if (!ttsReady) {
                    pendingSpeech = null
                    fail(VoiceControllerError.SYNTHESIS_UNAVAILABLE)
                } else {
                    pendingSpeech?.also { pendingSpeech = null; speakNow(it) }
                }
            }
        }
    }

    val currentState: VoiceControllerState get() = state

    /** Call only after the Activity/Compose permission launcher has granted RECORD_AUDIO. */
    fun startPushToTalk(): Boolean {
        checkMainThread()
        if (closed) return false
        if (ContextCompat.checkSelfPermission(appContext, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            fail(VoiceControllerError.PERMISSION_REQUIRED)
            return false
        }
        if (state == VoiceControllerState.LISTENING) return true
        if (!SpeechRecognizer.isRecognitionAvailable(appContext)) {
            fail(VoiceControllerError.RECOGNIZER_UNAVAILABLE)
            return false
        }
        if (state == VoiceControllerState.SPEAKING) stopSpeaking()
        if (state == VoiceControllerState.LISTENING) cancel()
        if (!requestFocus()) {
            fail(VoiceControllerError.MICROPHONE_UNAVAILABLE)
            return false
        }
        return try {
            val speechRecognizer = createRecognizer()
            recognizer = speechRecognizer
            speechRecognizer.setRecognitionListener(recognitionListener)
            speechRecognizer.startListening(recognitionIntent())
            setState(VoiceControllerState.LISTENING)
            true
        } catch (_: SecurityException) {
            abandonFocus()
            fail(VoiceControllerError.PERMISSION_REQUIRED)
            false
        } catch (_: RuntimeException) {
            abandonFocus()
            destroyRecognizer()
            fail(VoiceControllerError.MICROPHONE_UNAVAILABLE)
            false
        }
    }

    /** Release event for hold-to-talk; the speech service finalizes the current utterance. */
    fun releasePushToTalk() {
        checkMainThread()
        if (closed || state != VoiceControllerState.LISTENING) return
        setState(VoiceControllerState.PROCESSING)
        try {
            recognizer?.stopListening()
        } catch (_: RuntimeException) {
            abandonFocus()
            fail(VoiceControllerError.MICROPHONE_UNAVAILABLE)
        }
    }

    /** Interrupts either recording or speech output without submitting recognized content. */
    fun cancel() {
        checkMainThread()
        if (closed) return
        try { recognizer?.cancel() } catch (_: RuntimeException) { }
        destroyRecognizer()
        try { textToSpeech?.stop() } catch (_: RuntimeException) { }
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
        if (!ttsReady) {
            pendingSpeech = safeText
            return
        }
        speakNow(safeText)
    }

    fun stopSpeaking() {
        checkMainThread()
        if (closed) return
        pendingSpeech = null
        try { textToSpeech?.stop() } catch (_: RuntimeException) { }
        abandonFocus()
        if (state == VoiceControllerState.SPEAKING) setState(VoiceControllerState.IDLE)
    }

    override fun close() {
        checkMainThread()
        if (closed) return
        closed = true
        try { recognizer?.cancel() } catch (_: RuntimeException) { }
        try { recognizer?.destroy() } catch (_: RuntimeException) { }
        recognizer = null
        try { textToSpeech?.stop(); textToSpeech?.shutdown() } catch (_: RuntimeException) { }
        textToSpeech = null
        pendingSpeech = null
        abandonFocus()
    }

    private fun createRecognizer(): SpeechRecognizer =
        if (android.os.Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(appContext)) {
            SpeechRecognizer.createOnDeviceSpeechRecognizer(appContext)
        } else {
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

    private val recognitionListener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) { }
        override fun onBeginningOfSpeech() { }
        override fun onRmsChanged(rmsdB: Float) { }
        override fun onBufferReceived(buffer: ByteArray?) { }
        override fun onEndOfSpeech() { setState(VoiceControllerState.PROCESSING) }
        override fun onEvent(eventType: Int, params: Bundle?) { }

        override fun onPartialResults(partialResults: Bundle?) {
            partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
                ?.takeIf { it.isNotBlank() }?.let { post { listener.onPartialText(it) } }
        }

        override fun onResults(results: Bundle?) {
            val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim()
            abandonFocus()
            if (text.isNullOrEmpty()) {
                fail(VoiceControllerError.NO_MATCH)
            } else {
                setState(VoiceControllerState.IDLE)
                post { listener.onFinalText(text) }
            }
            destroyRecognizer()
        }

        override fun onError(error: Int) {
            abandonFocus()
            fail(mapRecognitionError(error))
            destroyRecognizer()
        }
    }

    private fun configureTts(engine: TextToSpeech): Boolean {
        val language = engine.setLanguage(locale)
        if (language == TextToSpeech.LANG_MISSING_DATA || language == TextToSpeech.LANG_NOT_SUPPORTED) return false
        engine.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build(),
        )
        engine.setSpeechRate(1.0f)
        engine.setPitch(1.0f)
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = post { setState(VoiceControllerState.SPEAKING) }
            override fun onDone(utteranceId: String?) = post {
                abandonFocus()
                setState(VoiceControllerState.IDLE)
            }
            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) = post {
                abandonFocus()
                fail(VoiceControllerError.SYNTHESIS_FAILED)
            }
            override fun onError(utteranceId: String?, errorCode: Int) = post {
                abandonFocus()
                fail(VoiceControllerError.SYNTHESIS_FAILED)
            }
        })
        return true
    }

    private fun speakNow(text: String) {
        val engine = textToSpeech ?: run { fail(VoiceControllerError.SYNTHESIS_UNAVAILABLE); return }
        if (!requestFocus()) { fail(VoiceControllerError.SYNTHESIS_FAILED); return }
        val result = try {
            engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, "jarvis-${UUID.randomUUID()}")
        } catch (_: RuntimeException) { TextToSpeech.ERROR }
        if (result == TextToSpeech.ERROR) {
            abandonFocus()
            fail(VoiceControllerError.SYNTHESIS_FAILED)
        } else {
            setState(VoiceControllerState.SPEAKING)
        }
    }

    private fun requestFocus(): Boolean {
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_EXCLUSIVE)
            .setAudioAttributes(
                AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ASSISTANT)
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

    private fun destroyRecognizer() {
        val old = recognizer ?: return
        recognizer = null
        try { old.destroy() } catch (_: RuntimeException) { }
    }

    private fun setState(next: VoiceControllerState) {
        if (closed || state == next) return
        state = next
        post { listener.onStateChanged(next) }
    }

    private fun fail(error: VoiceControllerError) {
        if (closed) return
        setState(VoiceControllerState.ERROR)
        post { listener.onError(error) }
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
