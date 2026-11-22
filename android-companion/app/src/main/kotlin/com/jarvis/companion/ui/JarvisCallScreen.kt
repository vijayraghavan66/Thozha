package com.jarvis.companion.ui

import android.Manifest
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
import com.jarvis.companion.voice.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.Locale

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
    val settings = remember(context) { context.getSharedPreferences("jarvis_voice", Context.MODE_PRIVATE) }
    var sid by remember { mutableIntStateOf(settings.getInt("kokoro_sid", 5)) }
    val voiceName = when(sid) { 1 -> "af_bella"; 6 -> "am_michael"; 9 -> "bm_george"; 10 -> "bm_lewis"; else -> "am_adam" }
    var controller by remember { mutableStateOf<JarvisVoiceController?>(null) }
    val created = remember(context) { JarvisVoiceController(context, object : JarvisVoiceController.Listener {
        override fun onStateChanged(value: VoiceControllerState) { state = value }
        override fun onPartialText(text: String) { transcript = text }
        override fun onBargeInDetected() { response = "Interrupted"; pending = false }
        override fun onFinalText(text: String) {
            transcript = text; pending = true
            android.util.Log.i("JarvisCall", "event=transcript_final chars=${text.length} elapsed_ms=${SystemClock.elapsedRealtime()}")
            if (WeatherIntentRouter.isWeatherRequest(text)) {
                val city = Regex("(?i)\\b(?:in|for)\\s+([\\p{L} .'-]+?)(?:\\s+(?:today|now|currently|should)\\b|[?.!,]|$)").find(text)?.groupValues?.get(1)?.trim()
                if (city.isNullOrBlank()) { response = "Which city should I check?"; pending = false; controller?.speakNeural(response, sid, voiceName); return }
                response = "Checking weather in $city…"
                scope.launch {
                    val t = SystemClock.elapsedRealtime()
                    val result = runCatching { withContext(Dispatchers.IO) { LocalWeatherClient().current(city).response() } }
                    android.util.Log.i("JarvisCall", "event=weather_complete duration_ms=${SystemClock.elapsedRealtime()-t} success=${result.isSuccess}")
                    response = result.getOrElse { "Weather unavailable: ${it.message ?: "check connection"}" }
                    pending = false
                    if (result.isSuccess && active) controller?.speakNeural(response, sid, voiceName)
                }
            } else {
                store.draft = text
                store.submit()
                response = "Sent to Jarvis. Waiting for the Mac response."
                pending = false
            }
        }
        override fun onError(error: VoiceControllerError) { response = "Voice error: $error"; pending = false }
    }) }
    SideEffect { controller = created }
    DisposableEffect(created) { onDispose { active = false; created.cancel(); created.close() } }
    LaunchedEffect(active) { while(active) { seconds = ((SystemClock.elapsedRealtime()-started)/1000).toInt(); delay(1000) } }
    LaunchedEffect(state, pending, muted, active) {
        if (active && !muted && !pending && state == VoiceControllerState.IDLE) {
            delay(350)
            if (active && !muted && !pending && state == VoiceControllerState.IDLE) created.startVoiceTurn()
        }
    }
    Column(Modifier.fillMaxSize().background(JarvisColor.Background).padding(24.dp), horizontalAlignment=Alignment.CenterHorizontally, verticalArrangement=Arrangement.SpaceBetween) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement=Arrangement.End) { BasicText("End", Modifier.clickable { active=false; created.cancel(); onEnd() }.padding(12.dp), style=JarvisType.Section.copy(color=JarvisColor.Text)) }
        Column(horizontalAlignment=Alignment.CenterHorizontally, verticalArrangement=Arrangement.spacedBy(20.dp)) {
            Box(Modifier.size(150.dp).clip(CircleShape).background(JarvisColor.SageTint), contentAlignment=Alignment.Center) {
                BasicText("J", style=JarvisType.Title.copy(color=JarvisColor.Sage))
            }
            BasicText("Jarvis Live", style=JarvisType.Title.copy(color=JarvisColor.Text))
            BasicText("%02d:%02d".format(seconds/60, seconds%60), style=JarvisType.Caption.copy(color=JarvisColor.TextMuted))
            BasicText(when(state){ VoiceControllerState.LISTENING->"Listening"; VoiceControllerState.PROCESSING->"Thinking"; VoiceControllerState.SPEAKING->"Speaking"; VoiceControllerState.ERROR->"Needs attention"; VoiceControllerState.IDLE->"Connecting" }, style=JarvisType.Section.copy(color=JarvisColor.Sage))
            BasicText(if(transcript.isNotBlank()) transcript else "Your conversation stays in this call.", style=JarvisType.Body.copy(color=JarvisColor.TextMuted))
            if(response.isNotBlank()) BasicText(response, style=JarvisType.Body.copy(color=JarvisColor.Text))
        }
        Column(horizontalAlignment=Alignment.CenterHorizontally, verticalArrangement=Arrangement.spacedBy(12.dp)) {
            BasicText("Voice · $voiceName", Modifier.clip(RoundedCornerShape(20.dp)).background(JarvisColor.Surface).clickable {
                sid=if(sid in setOf(1,9,10)) 5 else 1; settings.edit().putInt("kokoro_sid",sid).apply()
            }.padding(14.dp), style=JarvisType.Caption.copy(color=JarvisColor.Text))
            Row(horizontalArrangement=Arrangement.spacedBy(12.dp)) {
                CallAction(if(muted) "Unmute" else "Mute") { muted=!muted; if(muted) created.cancel() else created.startVoiceTurn() }
                if (state == VoiceControllerState.SPEAKING) CallAction("Interrupt") { created.startBargeInTurn() }
            }
            BasicText("Audio follows the active Android speaker or Bluetooth route", style = JarvisType.Caption.copy(color=JarvisColor.TextMuted))
            BasicText("End call", Modifier.fillMaxWidth().clip(RoundedCornerShape(24.dp)).background(JarvisColor.SageTint).clickable { active=false; created.cancel(); onEnd() }.padding(18.dp), style=JarvisType.Section.copy(color=JarvisColor.Text))
        }
    }
}

@Composable private fun CallAction(label:String,onClick:()->Unit) { BasicText(label, Modifier.clip(RoundedCornerShape(20.dp)).background(JarvisColor.Surface).semantics { contentDescription=label; role=Role.Button }.clickable(onClick=onClick).padding(14.dp), style=JarvisType.Caption.copy(color=JarvisColor.Text)) }
