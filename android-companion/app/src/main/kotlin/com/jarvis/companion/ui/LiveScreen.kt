package com.jarvis.companion.ui

import android.Manifest
import android.content.Intent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import android.os.SystemClock
import android.util.Log
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.jarvis.companion.link.LinkMode
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.data.LocalGatewayDiscovery
import com.jarvis.companion.AdvancedActivity
import com.jarvis.companion.telegram.TelegramActivity
import com.jarvis.companion.localai.AndroidDeviceProfileReader
import com.jarvis.companion.presentation.MacLinkPresentation
import com.jarvis.companion.presentation.ConnectionRoutePresentation
import com.jarvis.companion.presentation.LocalWifiPresentation
import com.jarvis.companion.presentation.TaskTone
import com.jarvis.companion.voice.JarvisVoiceController
import com.jarvis.companion.voice.VoiceControllerError
import com.jarvis.companion.voice.VoiceControllerState
import com.jarvis.companion.voice.VoiceRecognitionProvider
import com.jarvis.companion.voice.LocalWeatherClient
import com.jarvis.companion.voice.WeatherIntentRouter
import com.jarvis.companion.voice.WeatherRequestEpoch
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Connection facts from the authenticated Link state, the latest sync, and the optional Telegram transport.
 * A configured route is never treated as proof that the Mac is reachable.
 */
@Composable
fun LiveScreen(
    store: JarvisStore,
    showHeader: Boolean = true,
    showPhoneDetails: Boolean = true,
    showVoice: Boolean = true,
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val status = store.linkStatus
    val telegram = store.telegram
    val settings = remember(context) { LinkSettings(context) }
    val phone = remember(context) { AndroidDeviceProfileReader.read(context) }
    val localWifiPermission = LocalGatewayDiscovery.localNetworkPermissionGranted(context)
    val localWifi = LocalWifiPresentation.state(
        paired = store.macApiPaired,
        localNetworkPermission = localWifiPermission,
        gatewayIdentityPinned = store.localWifiPin != null,
        lastSuccessfulRoute = store.lastMacApiTransport,
    )
    val connection = MacLinkPresentation.state(
        paired = store.macApiPaired || status?.certificatesVerified == true,
        enrollmentPending = status?.enrollmentPending == true,
        syncing = store.syncing || store.macApiSyncing,
        lastSyncMs = maxOf(store.lastSyncMs, store.macApiLastSyncMs),
        lastSyncFailed = if (store.macApiPaired) store.macApiSyncError.isNotBlank() else store.lastSyncError.isNotBlank(),
        nowMs = store.now,
        formatAgo = { ageLabel(it) },
    )
    var diagnosticsExpanded by remember { mutableStateOf(false) }

    Column(Modifier.fillMaxSize()) {
        if (showHeader) ScreenHeader("Devices", "Paired computers and their real connection state.")

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = JarvisSpace.Page, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                HeroSurfaceCard {
                    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(
                                Modifier.size(46.dp).clip(CircleShape).background(JarvisColor.SageTint),
                                contentAlignment = Alignment.Center,
                            ) {
                                PresenceSignal(
                                    animating = store.hasRunningWork(),
                                    tint = toneColor(connection.tone),
                                    modifier = Modifier.size(26.dp),
                                )
                            }
                            Spacer(Modifier.width(14.dp))
                            Column(Modifier.weight(1f)) {
                                SectionLabel("Mac link")
                                BasicText(connection.label, style = JarvisType.Section.copy(color = toneColor(connection.tone)))
                            }
                        }
                        BasicText(connection.detail, style = JarvisType.Body.copy(color = JarvisColor.TextMuted))
                        TextAction(
                            label = connection.actionLabel,
                            filled = !connection.paired,
                        enabled = !store.syncing && !store.macApiSyncing,
                            modifier = Modifier.fillMaxWidth(),
                        ) {
                            if (connection.paired || connection.pendingEnrollment) store.syncNow(forcePermanentTelegramRetry = true)
                            else context.startActivity(
                                Intent(context, AdvancedActivity::class.java).putExtra("open_device_pairing", true),
                            )
                        }
                        if (!connection.paired || connection.pendingEnrollment) {
                            BasicText(
                                "A new phone is not paired until the Mac confirms its signed invitation.",
                                style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                            )
                        }
                    }
                }
            }

            item { SectionLabel("Transport") }
            item {
                val telegramRetry = telegramRetryStatus(telegram, store.now)
                FactCard(
                    listOf(
                        "Route" to ConnectionRoutePresentation.label(store.lastMacApiTransport, modeLabel(settings.mode)),
                        "Telegram" to telegramConnectionLabel(telegram?.connection),
                        "Last successful sync" to if (maxOf(store.lastSyncMs, store.macApiLastSyncMs) > 0L && (store.macApiSyncError.isBlank() || !store.macApiPaired)) {
                            val latest = maxOf(store.lastSyncMs, store.macApiLastSyncMs)
                            "${clockTime(latest)} · ${ageLabel((store.now - latest).coerceAtLeast(0L))}"
                        } else "Not confirmed yet",
                        "Queued on phone" to "${status?.outboxSize ?: 0} messages",
                    ) + listOfNotNull(telegramRetry?.let { "Telegram delivery" to it }),
                )
            }
            item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    SectionLabel("Optional remote access")
                    SurfaceCard {
                        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            BasicText(
                                if (store.telegramRemoteEnabled) "Telegram remote access is on" else "Local-only mode",
                                style = JarvisType.Section.copy(color = JarvisColor.Text),
                            )
                            BasicText(
                                if (store.telegramRemoteEnabled)
                                    "Telegram is optional. Turning it off keeps its saved sign-in on this phone and uses only local Mac routes."
                                else
                                    "This phone uses the Mac's local Wi-Fi or USB route. No Telegram account or cloud relay is required.",
                                style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                            )
                            TextAction(
                                label = if (store.telegramRemoteEnabled) "Turn off Telegram remote access" else "Enable Telegram remote access",
                                filled = store.telegramRemoteEnabled,
                                modifier = Modifier.fillMaxWidth(),
                            ) { store.updateTelegramRemoteEnabled(!store.telegramRemoteEnabled) }
                            TextAction(
                                label = "Telegram account setup",
                                filled = false,
                                enabled = store.telegramRemoteEnabled,
                                modifier = Modifier.fillMaxWidth(),
                            ) { context.startActivity(Intent(context, TelegramActivity::class.java)) }
                        }
                    }
                }
            }
            item {
                TextAction(
                    label = if (store.syncing || store.macApiSyncing) "Syncing with the Mac…" else "Check connection",
                    filled = false,
                    enabled = !store.syncing && !store.macApiSyncing,
                    modifier = Modifier.fillMaxWidth(),
                        onClick = { store.syncNow(forcePermanentTelegramRetry = true) },
                )
            }

            item { SectionLabel("Local Wi-Fi") }
            item {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    FactCard(
                        listOf(
                            "Identity" to localWifi.identityLabel,
                            "Last route" to localWifi.latestRouteLabel,
                            "LAN events" to store.lanTransportStatus,
                        ),
                    )
                    TextAction(
                        label = when {
                            store.localWifiSetupRunning -> "Verifying Mac identity…"
                            android.os.Build.VERSION.SDK_INT >= 37 && !localWifiPermission -> "Allow local network access"
                            store.localWifiPin == null -> "Set up secure Wi-Fi"
                            else -> "Refresh Mac Wi-Fi identity"
                        },
                        filled = store.localWifiPin == null,
                        enabled = !store.localWifiSetupRunning && store.macApiPaired,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        if (android.os.Build.VERSION.SDK_INT >= 37 && !localWifiPermission) {
                            store.permissionRequester?.invoke(Manifest.permission.ACCESS_LOCAL_NETWORK)
                        } else {
                            store.verifyLocalWifiGateway()
                        }
                    }
                    BasicText(
                        store.localWifiSetupMessage.ifBlank {
                            "Initial certificate pinning needs the existing USB/ADB-reverse connection. Afterward, the app can discover the pinned Mac on Wi-Fi. A saved pin alone does not prove Wi-Fi is reachable."
                        },
                        style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                    )
                }
            }

            if (showPhoneDetails) {
                item { SectionLabel("This phone") }
                item {
                    FactCard(
                        listOf(
                            "Device" to listOf(phone.manufacturer, phone.model).filter(String::isNotBlank).joinToString(" "),
                            "Android" to "${phone.androidApi} · ${phone.supportedAbis.firstOrNull() ?: "Unknown processor"}",
                            "Memory" to "${memoryLabel(phone.totalRamBytes)} total · ${memoryLabel(phone.availableRamBytes)} available",
                        ),
                    )
                }
            }

            item {
                SurfaceCard(onClick = { diagnosticsExpanded = !diagnosticsExpanded }) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            SectionLabel("Advanced diagnostics")
                            BasicText("Route and sync details", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
                        }
                        BasicText(if (diagnosticsExpanded) "Hide" else "Show", style = JarvisType.Caption.copy(color = JarvisColor.Sage))
                    }
                }
            }
            if (diagnosticsExpanded) {
                item {
                    FactCard(
                        listOf(
                            "Relay endpoint" to settings.relayUrl,
                            "Last sync result" to when {
                                store.lastSyncMs <= 0L -> "No sync has run yet"
                                store.lastSyncError.isBlank() -> "No error reported"
                                else -> store.lastSyncError
                            },
                            "Mac API result" to when {
                                !store.macApiPaired -> "Not paired"
                                store.macApiSyncError.isBlank() -> "No error reported"
                                else -> store.macApiSyncError.take(200)
                            },
                            "Telegram sign-in" to authLabel(telegram?.auth),
                            "Pending Telegram pings" to (telegram?.pendingPings?.toString() ?: "Unknown"),
                        ),
                    )
                }
            }

            if (showVoice) {
                item { SectionLabel("Voice") }
                item { LiveVoiceControls(store) }
            }
            item { Spacer(Modifier.height(16.dp)) }
        }
    }
}

/** One platform-endpointed voice turn; recognized speech goes to the normal editable draft, never auto-sends. */
@Composable
private fun LiveVoiceControls(store: JarvisStore) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var weatherReply by remember { mutableStateOf("") }
    var weatherBusy by remember { mutableStateOf(false) }
    var weatherJob by remember { mutableStateOf<Job?>(null) }
    val weatherEpoch = remember { WeatherRequestEpoch() }
    val voiceState = remember { mutableStateOf(VoiceControllerState.IDLE) }
    val savedVoice = remember(context) { context.getSharedPreferences("jarvis_voice", android.content.Context.MODE_PRIVATE).getInt("kokoro_sid", 5) }
    val selectedSpeaker = remember { mutableStateOf(if (savedVoice in setOf(1, 5, 6, 9, 10)) savedVoice else 5) }
    val selectedSpeakerName = when (selectedSpeaker.value) { 1 -> "af_bella"; 6 -> "am_michael"; 9 -> "bm_george"; 10 -> "bm_lewis"; else -> "am_adam" }
    val providerLabel = remember { mutableStateOf("Not used in this session") }
    val outputLabel = remember { mutableStateOf("Installed platform voice") }
    var voiceRef by remember { mutableStateOf<JarvisVoiceController?>(null) }
    val voice = remember(context, store) {
        JarvisVoiceController(context, object : JarvisVoiceController.Listener {
            override fun onStateChanged(state: VoiceControllerState) {
                voiceState.value = state
                if (state == VoiceControllerState.SPEAKING) Log.i("JarvisVoice", "event=tts_playback_started elapsed_ms=${SystemClock.elapsedRealtime()}")
            }
            override fun onRecognitionProvider(provider: VoiceRecognitionProvider) {
                providerLabel.value = if (provider == VoiceRecognitionProvider.ON_DEVICE) {
                    "On-device recognition"
                } else {
                    "System recognition; provider may use network"
                }
            }
            override fun onOutputVoiceSelected(profile: com.jarvis.companion.voice.VoiceOutputProfile) {
                outputLabel.value = "${profile.name} · ${profile.localeTag}" + if (profile.offline) " · offline" else " · system"
            }
            override fun onFinalText(text: String) {
                val speechEnd = SystemClock.elapsedRealtime()
                Log.i("JarvisVoice", "event=recognition_final elapsed_ms=$speechEnd chars=${text.length}")
                if (WeatherIntentRouter.isWeatherRequest(text)) {
                    Log.i("JarvisWeather", "event=local_route_selected elapsed_ms=${SystemClock.elapsedRealtime()}")
                    val city = Regex("(?i)\\b(?:in|for)\\s+([\\p{L} .'-]+?)(?:\\s+(?:today|now|currently|should)\\b|[?.!,]|$)")
                        .find(text)?.groupValues?.get(1)?.trim()
                    weatherJob?.cancel()
                    val requestToken = weatherEpoch.begin()
                    if (city.isNullOrBlank()) {
                        weatherReply = "Choose a city in your request (for example, ‘weather in Chennai’). I won’t guess your location."
                        store.notice = weatherReply
                        return
                    }
                    weatherBusy = true
                    weatherReply = "Checking current weather for $city…"
                    weatherJob = scope.launch {
                        val fetchStart = SystemClock.elapsedRealtime()
                        Log.i("JarvisWeather", "event=fetch_started elapsed_ms=$fetchStart")
                        val result = runCatching { withContext(Dispatchers.IO) { LocalWeatherClient().current(city).response() } }
                        if (!isActive || !weatherEpoch.isCurrent(requestToken)) return@launch
                        Log.i("JarvisWeather", "event=fetch_finished elapsed_ms=${SystemClock.elapsedRealtime()} duration_ms=${SystemClock.elapsedRealtime() - fetchStart} success=${result.isSuccess}")
                        weatherBusy = false
                        weatherReply = result.getOrElse { "Weather unavailable: ${it.message ?: "check your connection and retry."}" }
                        store.notice = weatherReply
                        if (result.isSuccess) voiceRef?.speakNeural(weatherReply, selectedSpeaker.value, selectedSpeakerName)
                    }
                } else {
                    store.draft = text
                    store.notice = "Voice captured. Review the transcript, then tap Send."
                    store.selectTab(Tab.HOME)
                }
            }
            override fun onBargeInDetected() {
                store.notice = "I heard your interruption. Playback stopped; review the transcript before sending."
            }
            override fun onError(error: VoiceControllerError) {
                store.notice = when (error) {
                    VoiceControllerError.PERMISSION_REQUIRED -> "Microphone permission is required. Tap Start voice turn to allow it."
                    VoiceControllerError.RECOGNIZER_UNAVAILABLE -> "Speech recognition is unavailable. Use the typed composer."
                    VoiceControllerError.NO_MATCH -> "I didn't catch that. Try another voice turn or type your request."
                    VoiceControllerError.SPEECH_TIMEOUT -> "No speech was detected. Start a new voice turn when ready."
                    VoiceControllerError.NETWORK_OR_SERVICE -> "The system speech service couldn't complete recognition."
                    VoiceControllerError.RECOGNIZER_BUSY -> "Speech recognition is busy. Try again shortly."
                    VoiceControllerError.LANGUAGE_UNAVAILABLE -> "Speech recognition doesn't support the current language."
                    VoiceControllerError.SYNTHESIS_UNAVAILABLE -> "System speech voice unavailable."
                    VoiceControllerError.SYNTHESIS_FAILED -> "Kokoro neural voice failed; system TTS fallback is being used."
                    VoiceControllerError.MICROPHONE_UNAVAILABLE -> "The microphone could not start. Check whether another app is using it."
                    VoiceControllerError.UNKNOWN -> "Voice turn failed. Use the typed composer."
                }
            }
        })
    }
    voiceRef = voice
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(voice, lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_STOP) voice.cancel() }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            weatherJob?.cancel()
            weatherEpoch.cancel()
            voice.close()
        }
    }
    val state = voiceState.value
    val answer = remember(store.entries, store.session.id) {
        store.entriesInSession().lastOrNull {
            (it["state"] as? String) == "COMPLETED" && it["body_integrity_failed"] != true
        }?.let { entry ->
            (entry["body"] as? String)?.takeIf(String::isNotBlank)
                ?: (entry["summary"] as? String).orEmpty()
        }.orEmpty()
    }
    SurfaceCard {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (weatherReply.isNotBlank()) BasicText(weatherReply, style = JarvisType.Body.copy(color = JarvisColor.Text))
            if (weatherBusy) BasicText("Using local weather routing · no Mac request", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            BasicText(
                when (state) {
                    VoiceControllerState.LISTENING -> "Listening; Android will finish after a pause."
                    VoiceControllerState.PROCESSING -> "Transcribing speech…"
                    VoiceControllerState.SPEAKING -> "Speaking. Tap Interrupt and listen to speak over this response."
                    VoiceControllerState.ERROR -> "Voice is unavailable. Typed requests still work."
                    VoiceControllerState.IDLE -> "Start one spoken turn; review the transcript before sending."
                },
                style = JarvisType.Body.copy(color = JarvisColor.Text),
            )
            BasicText("Recognition · ${providerLabel.value}", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            BasicText("Speech output · ${outputLabel.value}", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted), maxLines = 2)
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                VoiceControlButton(
                    label = when (state) {
                        VoiceControllerState.LISTENING, VoiceControllerState.PROCESSING -> "Cancel voice turn"
                        VoiceControllerState.SPEAKING -> "Interrupt and listen"
                        else -> "Start voice turn"
                    },
                    enabled = true,
                ) {
                    when (state) {
                        VoiceControllerState.LISTENING, VoiceControllerState.PROCESSING -> voice.cancel()
                        VoiceControllerState.SPEAKING -> {
                            if (!JarvisVoiceController.hasMicrophonePermission(context)) {
                                store.notice = "Allow microphone access before using voice interruption."
                                store.permissionRequester?.invoke(Manifest.permission.RECORD_AUDIO)
                            } else if (!voice.startBargeInTurn()) {
                                store.notice = "The recognizer could not start an interruption. Tap Stop/Cancel or type your request."
                            }
                        }
                        else -> {
                            if (!JarvisVoiceController.hasMicrophonePermission(context)) {
                                store.notice = "Allow microphone access, then start a voice turn."
                                store.permissionRequester?.invoke(Manifest.permission.RECORD_AUDIO)
                            } else {
                                voice.startVoiceTurn()
                            }
                        }
                    }
                }
                VoiceControlButton(label = if (selectedSpeaker.value == 1 || selectedSpeaker.value == 9 || selectedSpeaker.value == 10) "Voice: Female · $selectedSpeakerName" else "Voice: Male · $selectedSpeakerName", enabled = true) {
                    val next = if (selectedSpeaker.value in setOf(1, 9, 10)) 5 else 1
                    selectedSpeaker.value = next
                    context.getSharedPreferences("jarvis_voice", android.content.Context.MODE_PRIVATE).edit().putInt("kokoro_sid", next).apply()
                    store.notice = "Selected ${if (next == 5) "male am_adam" else "female af_bella"} neural voice."
                }
                VoiceControlButton(label = "Test neural ${if (selectedSpeaker.value in setOf(1, 9, 10)) "female" else "male"} voice", enabled = true) {
                    voice.speakNeural("Good evening. I'm Jarvis. Your assistant is ready. What would you like to do today?", selectedSpeaker.value, selectedSpeakerName)
                }
                VoiceControlButton(label = "Test speaker", enabled = true) { voice.speak("Jarvis speaker test. If you hear this, audio output is working.") }
                VoiceControlButton(label = "Speak latest reply", enabled = answer.isNotBlank()) {
                    if (state == VoiceControllerState.LISTENING || state == VoiceControllerState.PROCESSING) voice.cancel()
                    voice.speakNeural(answer, selectedSpeaker.value, selectedSpeakerName)
                }
            }
        }
    }
}

@Composable
private fun VoiceControlButton(label: String, enabled: Boolean, onClick: () -> Unit) {
    Box(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(18.dp))
            .background(if (enabled) JarvisColor.SurfaceRaised else JarvisColor.Surface)
            .semantics { contentDescription = label; role = Role.Button }
            .clickable(enabled = enabled, role = Role.Button, onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 10.dp),
        contentAlignment = Alignment.Center,
    ) {
        BasicText(label, style = JarvisType.Caption.copy(color = if (enabled) JarvisColor.Text else JarvisColor.TextMuted))
    }
}

private fun authLabel(step: com.jarvis.companion.telegram.AuthStep?): String = when (step) {
    null -> "Unknown"
    com.jarvis.companion.telegram.AuthStep.WaitParameters -> "Starting"
    com.jarvis.companion.telegram.AuthStep.WaitPhone -> "Phone number needed"
    com.jarvis.companion.telegram.AuthStep.WaitCode -> "Login code needed"
    is com.jarvis.companion.telegram.AuthStep.WaitPassword -> "Two-step password needed"
    com.jarvis.companion.telegram.AuthStep.Ready -> "Signed in"
    com.jarvis.companion.telegram.AuthStep.Closed -> "Closed"
}

private fun modeLabel(mode: LinkMode): String = when (mode) {
    LinkMode.LOCAL_USB -> "Local USB"
    LinkMode.TEST_RELAY -> "Test relay"
}

/** Label/value rows inside one card. Values are selectable so diagnostics can be copied. */
@Composable
fun FactCard(rows: List<Pair<String, String>>) {
    SurfaceCard {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            rows.forEach { (label, value) ->
                Row(Modifier.fillMaxWidth()) {
                    BasicText(label, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted), modifier = Modifier.width(118.dp))
                    SelectionContainer(Modifier.weight(1f)) {
                        BasicText(value, style = JarvisType.Body.copy(color = JarvisColor.Text))
                    }
                }
            }
        }
    }
}
