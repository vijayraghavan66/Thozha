package com.jarvis.companion.ui

import android.content.Context
import android.os.SystemClock
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.jarvis.companion.link.LinkService
import com.jarvis.companion.voice.*
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun JarvisCallScreen(store: JarvisStore, onEnd: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var state by remember { mutableStateOf(VoiceControllerState.IDLE) }
    var transcript by remember { mutableStateOf("") }
    var response by remember { mutableStateOf("") }
    var muted by remember { mutableStateOf(false) }
    var active by remember { mutableStateOf(true) }
    var started by remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }
    var seconds by remember { mutableIntStateOf(0) }
    var pending by remember { mutableStateOf(false) }
    var missedUtterances by remember { mutableIntStateOf(0) }
    var retryJob by remember { mutableStateOf<Job?>(null) }
    var macRetryJob by remember { mutableStateOf<Job?>(null) }
    var weatherJob by remember { mutableStateOf<Job?>(null) }
    var reconnectAttempts by remember { mutableIntStateOf(0) }
    var macReady by remember { mutableStateOf(false) }
    var phoneFallback by remember { mutableStateOf(false) }
    var macStatus by remember { mutableStateOf("Connecting to paired Mac…") }
    val settings = remember(context) { context.getSharedPreferences("jarvis_voice", Context.MODE_PRIVATE) }
    var sid by remember { mutableIntStateOf(settings.getInt("kokoro_sid", 5)) }
    val kokoroVoice = when (sid) { 1 -> "af_bella"; 6 -> "am_michael"; 9 -> "bm_george"; 10 -> "bm_lewis"; else -> "am_adam" }
    val qwenSpeaker = if (sid == 1) "Vivian" else "Ryan"
    val transcriptRouter = remember { mutableStateOf<(String, Boolean) -> Unit>({ _, _ -> }) }
    var controller by remember { mutableStateOf<JarvisVoiceController?>(null) }
    var macController by remember { mutableStateOf<MacVoiceSessionController?>(null) }

    val localVoice = remember(context) {
        JarvisVoiceController(context, object : JarvisVoiceController.Listener {
            override fun onStateChanged(value: VoiceControllerState) { state = value }
            override fun onPartialText(text: String) { transcript = text; missedUtterances = 0; retryJob?.cancel(); response = "" }
            override fun onBargeInDetected() { response = "Interrupted"; pending = false }
            override fun onFinalText(text: String) { transcriptRouter.value(text, false) }
            override fun onError(error: VoiceControllerError) {
                pending = false
                if (VoiceRecognitionRetryPolicy.isRecoverable(error) && active && !muted && phoneFallback) {
                    missedUtterances += 1
                    val wait = VoiceRecognitionRetryPolicy.delayMs(missedUtterances)
                    response = if (missedUtterances <= 2) "Didn't catch that. Listening again…" else "Still here. Listening again…"
                    android.util.Log.i("JarvisVoiceSession", "event=recoverable_recognition_error error=$error retry=$missedUtterances delay_ms=$wait")
                    retryJob?.cancel()
                    retryJob = scope.launch {
                        delay(wait)
                        if (active && !muted && state == VoiceControllerState.ERROR) controller?.startVoiceTurn()
                    }
                } else {
                    response = when (error) {
                        VoiceControllerError.PERMISSION_REQUIRED -> "Microphone permission is needed to continue this call."
                        VoiceControllerError.RECOGNIZER_UNAVAILABLE -> "Speech recognition is unavailable on this device."
                        VoiceControllerError.MICROPHONE_UNAVAILABLE -> "The microphone could not start. Check whether another app is using it."
                        VoiceControllerError.NETWORK_OR_SERVICE -> "The speech service is unavailable right now."
                        else -> "Voice needs attention. You can keep the call open or hang up."
                    }
                    android.util.Log.w("JarvisVoiceSession", "event=recognition_unrecoverable error=$error elapsed_ms=${SystemClock.elapsedRealtime()}")
                }
            }
        })
    }
    SideEffect { controller = localVoice }

    val macVoice = remember(context) {
        MacVoiceSessionController(context, object : MacVoiceSessionController.Listener {
            override fun onSessionReady() {
                macRetryJob?.cancel()
                reconnectAttempts = 0
                localVoice.cancel()
                macReady = true
                phoneFallback = false
                macStatus = "Secure USB voice session · Mac processing"
                response = "Listening on the paired Mac voice lane."
            }
            override fun onState(value: MacVoiceLinkState) {
                state = when (value) {
                    MacVoiceLinkState.CONNECTING -> VoiceControllerState.IDLE
                    MacVoiceLinkState.LISTENING -> VoiceControllerState.LISTENING
                    MacVoiceLinkState.PROCESSING -> VoiceControllerState.PROCESSING
                    MacVoiceLinkState.SPEAKING -> VoiceControllerState.SPEAKING
                    MacVoiceLinkState.DISCONNECTED -> VoiceControllerState.ERROR
                }
                if (value == MacVoiceLinkState.DISCONNECTED) macReady = false
            }
            override fun onTranscript(text: String) { transcriptRouter.value(text, true) }
            override fun onResponseText(text: String) { response += text }
            override fun onInterrupted() {
                weatherJob?.cancel()
                weatherJob = null
                pending = false
                response = "Interrupted. Listening again."
            }
            override fun onError(message: String) {
                response = message
                if (!macReady && active) {
                    phoneFallback = true
                    macStatus = "Mac voice unavailable · phone-local recognition"
                    state = VoiceControllerState.IDLE
                    if (reconnectAttempts < 5) {
                        val waitMs = (1_000L shl reconnectAttempts.coerceAtMost(4)).coerceAtMost(16_000L)
                        macRetryJob?.cancel()
                        macRetryJob = scope.launch {
                            delay(waitMs)
                            if (active && !macReady) {
                                reconnectAttempts += 1
                                macStatus = "Phone-local voice · retrying paired Mac"
                                macController?.connect()
                            }
                        }
                    }
                } else if (macReady) {
                    macStatus = "Mac voice disconnected · reconnecting is required"
                }
            }
        })
    }
    SideEffect { macController = macVoice }
    SideEffect {
        transcriptRouter.value = { text, fromMac ->
            retryJob?.cancel()
            macRetryJob?.cancel()
            weatherJob?.cancel()
            weatherJob = null
            missedUtterances = 0
            transcript = text
            response = ""
            pending = true
            android.util.Log.i("ThozhaLive", "event=transcript_final source=${if (fromMac) "MAC_ASR" else "PHONE_ASR"} chars=${text.length} elapsed_ms=${SystemClock.elapsedRealtime()}")
            val weather = WeatherIntentRouter.isWeatherRequest(text)
            if (weather) {
                val city = Regex("(?i)\\b(?:in|for)\\s+([\\p{L} .'-]+?)(?:\\s+(?:today|now|currently|should)\\b|[?.!,]|$)")
                    .find(text)?.groupValues?.get(1)?.trim()
                if (city.isNullOrBlank()) {
                    response = "Which city should I check?"
                    pending = false
                    if (fromMac) macVoice.speakLocalResult(response, qwenSpeaker) else localVoice.speakNeural(response, sid, kokoroVoice)
                } else {
                    response = "Checking weather in $city…"
                    weatherJob = scope.launch {
                        val taskId = UUID.randomUUID().toString().replace("-", "")
                        val startedAt = SystemClock.elapsedRealtime()
                        val result = try {
                            Result.success(withContext(Dispatchers.IO) {
                                val runtime = LinkService.runtime(context)
                                runtime.recordLocalTaskRequest(taskId, text, transport = "api", lookupLinkStatus = false, executionTarget = "PHONE")
                                runtime.recordTaskLifecycle(taskId, "", "RUNNING", "Fetching current weather on this phone", "PHONE", "$taskId:weather-running")
                                val answer = LocalWeatherClient().current(city).response()
                                runtime.recordTaskLifecycle(taskId, "", "COMPLETED", answer.take(400), "PHONE", "$taskId:weather-completed")
                                answer
                            })
                        } catch (cancelled: CancellationException) {
                            withContext(NonCancellable + Dispatchers.IO) {
                                runCatching { LinkService.runtime(context).recordTaskLifecycle(taskId, "", "CANCELLED", "Weather lookup was interrupted", "PHONE", "$taskId:weather-cancelled") }
                            }
                            throw cancelled
                        } catch (failure: Exception) {
                            Result.failure(failure)
                        }
                        if (!active || !currentCoroutineContext().isActive) return@launch
                        android.util.Log.i("ThozhaLive", "event=weather_complete duration_ms=${SystemClock.elapsedRealtime()-startedAt} success=${result.isSuccess}")
                        store.refreshLocal()
                        response = result.getOrElse { "Weather unavailable: ${it.message ?: "check connection"}" }
                        if (result.isFailure) {
                            withContext(Dispatchers.IO) {
                                runCatching { LinkService.runtime(context).recordTaskLifecycle(taskId, "", "FAILED", response.take(300), "PHONE", "$taskId:weather-failed") }
                            }
                            store.refreshLocal()
                        }
                        pending = false
                        if (active && result.isSuccess) {
                            if (fromMac && macReady) {
                                response = ""
                                macVoice.speakLocalResult(result.getOrThrow(), qwenSpeaker)
                            }
                            else localVoice.speakNeural(response, sid, kokoroVoice)
                        } else if (fromMac && result.isFailure) macVoice.speakLocalResult(response, qwenSpeaker)
                        else if (fromMac) macVoice.completeLocally()
                        else if (result.isFailure) localVoice.speakNeural(response, sid, kokoroVoice)
                    }
                }
            } else if (fromMac && MacVoiceRequestRouting.shouldSubmitThroughSupervisor(text)) {
                store.draft = text
                store.submit()
                response = "Sent to the Mac's authorization-controlled task flow. Check Tasks for its real status or approval."
                macVoice.speakLocalResult(response, qwenSpeaker)
            } else if (fromMac) {
                macVoice.respond(qwenSpeaker)
            } else {
                store.draft = text
                store.submit()
                response = "Sent to the paired Mac task flow. Check Tasks for status or approval."
                pending = false
                localVoice.speakNeural(response, sid, kokoroVoice)
            }
        }
    }

    LaunchedEffect(macVoice) { macVoice.connect() }
    DisposableEffect(localVoice, macVoice) {
        onDispose {
            active = false
            retryJob?.cancel()
            localVoice.cancel()
            localVoice.close()
            macVoice.close()
        }
    }
    LaunchedEffect(active) {
        while (active) {
            seconds = ((SystemClock.elapsedRealtime() - started) / 1000).toInt()
            delay(1000)
        }
    }
    LaunchedEffect(state, pending, muted, active, phoneFallback) {
        if (active && phoneFallback && !muted && !pending && state == VoiceControllerState.IDLE) {
            delay(300)
            if (active && phoneFallback && !muted && !pending && state == VoiceControllerState.IDLE) localVoice.startVoiceTurn()
        }
    }

    fun endCall() {
        active = false
        localVoice.cancel()
        macVoice.close()
        onEnd()
    }

    Column(Modifier.fillMaxSize().background(JarvisColor.Background).padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.SpaceBetween) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            BasicText("End", Modifier.clickable { endCall() }.padding(12.dp), style = JarvisType.Section.copy(color = JarvisColor.Text))
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(20.dp)) {
            Box(Modifier.size(150.dp).clip(CircleShape).background(JarvisColor.SageTint), contentAlignment = Alignment.Center) {
                BasicText("J", style = JarvisType.Title.copy(color = JarvisColor.Sage))
            }
            BasicText("Jarvis Live", style = JarvisType.Title.copy(color = JarvisColor.Text))
            BasicText("%02d:%02d".format(seconds / 60, seconds % 60), style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            BasicText(when (state) {
                VoiceControllerState.LISTENING -> "Listening"
                VoiceControllerState.PROCESSING -> "Thinking"
                VoiceControllerState.SPEAKING -> "Speaking"
                VoiceControllerState.ERROR -> if (phoneFallback) "Listening · phone mode" else "Reconnecting"
                VoiceControllerState.IDLE -> "Connecting"
            }, style = JarvisType.Section.copy(color = JarvisColor.Sage))
            BasicText(macStatus, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            BasicText(if (transcript.isNotBlank()) transcript else "Speak naturally; Jarvis listens for the end of your turn.", style = JarvisType.Body.copy(color = JarvisColor.TextMuted))
            if (response.isNotBlank()) BasicText(response, style = JarvisType.Body.copy(color = JarvisColor.Text))
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            BasicText("Mac voice · $qwenSpeaker  /  Phone fallback · $kokoroVoice", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            BasicText("Voice · $kokoroVoice", Modifier.clip(RoundedCornerShape(20.dp)).background(JarvisColor.Surface).clickable {
                sid = if (sid in setOf(1, 9, 10)) 5 else 1
                settings.edit().putInt("kokoro_sid", sid).apply()
            }.padding(14.dp), style = JarvisType.Caption.copy(color = JarvisColor.Text))
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                CallAction(if (muted) "Unmute" else "Mute") {
                    muted = !muted
                    if (macReady) macVoice.setMuted(muted)
                    if (!macReady) { if (muted) localVoice.cancel() else localVoice.startVoiceTurn() }
                }
                if (state == VoiceControllerState.SPEAKING) CallAction("Interrupt") {
                    if (macReady) macVoice.interrupt() else localVoice.startBargeInTurn()
                }
            }
            BasicText("Audio follows the active Android speaker or Bluetooth route", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            BasicText("End call", Modifier.fillMaxWidth().clip(RoundedCornerShape(24.dp)).background(JarvisColor.SageTint).clickable { endCall() }.padding(18.dp), style = JarvisType.Section.copy(color = JarvisColor.Text))
        }
    }
}

@Composable
private fun CallAction(label: String, onClick: () -> Unit) {
    BasicText(label, Modifier.clip(RoundedCornerShape(20.dp)).background(JarvisColor.Surface)
        .semantics { contentDescription = label; role = Role.Button }
        .clickable(onClick = onClick).padding(14.dp), style = JarvisType.Caption.copy(color = JarvisColor.Text))
}
