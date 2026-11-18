package com.jarvis.companion.ui

import android.Manifest
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
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
import com.jarvis.companion.presentation.TaskTone
import com.jarvis.companion.presentation.TaskStates
import com.jarvis.companion.voice.JarvisVoiceController
import com.jarvis.companion.voice.VoiceControllerError
import com.jarvis.companion.voice.VoiceControllerState
import com.jarvis.companion.voice.VoiceRecognitionProvider

/**
 * Live connection facts: the Mac pairing, the relay path, the Telegram transport, and work that is running now.
 * Every value is read from the runtime or the Telegram session. "Unknown" is shown as unknown.
 */
@Composable
fun LiveScreen(store: JarvisStore) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val status = store.linkStatus
    val telegram = store.telegram
    val running = store.entries.filter { (it["state"] as? String) in setOf("CREATED", "PLANNING", "RUNNING", "VERIFYING") }
    val waiting = store.entries.filter { (it["state"] as? String) == "WAITING_APPROVAL" }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(start = JarvisSpace.Page, end = JarvisSpace.Page, top = 16.dp, bottom = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                BasicText("Live", style = JarvisType.Title.copy(color = JarvisColor.Text))
                Spacer(Modifier.height(4.dp))
                BasicText("Connection and running work, as reported.", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            }
            PresenceSignal(animating = running.isNotEmpty())
        }

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = JarvisSpace.Page, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item { SectionLabel("Mac link") }
            item {
                FactCard(
                    listOf(
                        "Pairing" to when {
                            status == null -> "Reading…"
                            status.enrollmentPending -> "Enrollment pending"
                            status.certificatesVerified -> "Paired and verified"
                            else -> "Not paired"
                        },
                        "Device" to (status?.deviceId?.takeIf { it.isNotBlank() }?.take(8)?.plus("…") ?: "Unknown"),
                        "Route" to "${modeLabel(LinkSettings(context).mode)} · ${LinkSettings(context).relayUrl}",
                        "Last sync" to if (store.lastSyncMs > 0L) {
                            "${clockTime(store.lastSyncMs)} (${ageLabel(store.now - store.lastSyncMs)})"
                        } else "Not yet",
                        "Sync result" to when {
                            store.lastSyncMs <= 0L -> "No sync has run yet"
                            store.lastSyncError.isBlank() -> "No error reported"
                            else -> store.lastSyncError
                        },
                        "Queued on phone" to "${status?.outboxSize ?: 0} message(s)",
                    ),
                )
            }

            item { SectionLabel("Voice") }
            item { LiveVoiceControls(store) }

            item { SectionLabel("Telegram") }
            item {
                FactCard(
                    listOf(
                        "Connection" to telegramConnectionLabel(telegram?.connection),
                        "Sign-in" to authLabel(telegram?.auth),
                        "Online" to (telegram?.online?.let { if (it) "Yes" else "No" } ?: "Unknown"),
                        "Pings awaiting reply" to (telegram?.pendingPings?.toString() ?: "Unknown"),
                        "Last round trip" to (telegram?.lastRttMs?.let { "$it ms" } ?: "None measured"),
                    ),
                )
            }

            item { SectionLabel("Running now") }
            if (running.isEmpty()) {
                item {
                    SurfaceCard {
                        BasicText(
                            if (waiting.isEmpty()) "Nothing is running on your Mac." else "Nothing is running. ${waiting.size} request(s) wait for your approval.",
                            style = JarvisType.Body.copy(color = JarvisColor.TextMuted),
                        )
                    }
                }
            } else {
                items(running, key = { it["message_id"] as? String ?: it.hashCode().toString() }) { record ->
                    SurfaceCard {
                        Column {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                BasicText(
                                    (record["text"] as? String).orEmpty().ifBlank { "Request" },
                                    style = JarvisType.Section.copy(color = JarvisColor.Text),
                                    maxLines = 2,
                                    modifier = Modifier.weight(1f),
                                )
                                Spacer(Modifier.width(10.dp))
                                StatusPill(TaskStates.label(TaskStates.toneFor(record["state"] as? String)), TaskTone.RUNNING)
                            }
                            (record["summary"] as? String)?.takeIf { it.isNotBlank() }?.let { summary ->
                                Spacer(Modifier.height(8.dp))
                                SelectionContainer {
                                    BasicText(summary, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
                                }
                            }
                        }
                    }
                }
            }
            item { Spacer(Modifier.height(16.dp)) }
        }
    }
}

/** One platform-endpointed voice turn; recognized speech goes to the normal editable draft, never auto-sends. */
@Composable
private fun LiveVoiceControls(store: JarvisStore) {
    val context = LocalContext.current
    val voiceState = remember { mutableStateOf(VoiceControllerState.IDLE) }
    val providerLabel = remember { mutableStateOf("Not used in this session") }
    val outputLabel = remember { mutableStateOf("Installed platform voice") }
    val voice = remember(context, store) {
        JarvisVoiceController(context, object : JarvisVoiceController.Listener {
            override fun onStateChanged(state: VoiceControllerState) { voiceState.value = state }
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
                store.draft = text
                store.notice = "Voice captured. Review the transcript, then tap Send."
                store.selectTab(Tab.HOME)
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
                    VoiceControllerError.SYNTHESIS_UNAVAILABLE, VoiceControllerError.SYNTHESIS_FAILED -> "The installed speech voice is unavailable."
                    VoiceControllerError.MICROPHONE_UNAVAILABLE -> "The microphone could not start. Check whether another app is using it."
                    VoiceControllerError.UNKNOWN -> "Voice turn failed. Use the typed composer."
                }
            }
        })
    }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(voice, lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event -> if (event == Lifecycle.Event.ON_STOP) voice.cancel() }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
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
            BasicText(
                when (state) {
                    VoiceControllerState.LISTENING -> "Listening; Android will finish after a pause."
                    VoiceControllerState.PROCESSING -> "Transcribing speech…"
                    VoiceControllerState.SPEAKING -> "Speaking. You can stop the current response."
                    VoiceControllerState.ERROR -> "Voice is unavailable. Typed requests still work."
                    VoiceControllerState.IDLE -> "Start one spoken turn; review the transcript before sending."
                },
                style = JarvisType.Body.copy(color = JarvisColor.Text),
            )
            BasicText("Recognition · ${providerLabel.value}", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            BasicText("Speech output · ${outputLabel.value}", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted), maxLines = 2)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                VoiceControlButton(
                    label = when (state) {
                        VoiceControllerState.LISTENING, VoiceControllerState.PROCESSING -> "Cancel voice turn"
                        VoiceControllerState.SPEAKING -> "Stop speaking"
                        else -> "Start voice turn"
                    },
                    enabled = true,
                ) {
                    when (state) {
                        VoiceControllerState.LISTENING, VoiceControllerState.PROCESSING -> voice.cancel()
                        VoiceControllerState.SPEAKING -> voice.stopSpeaking()
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
                VoiceControlButton(label = "Speak latest reply", enabled = answer.isNotBlank()) {
                    if (state == VoiceControllerState.LISTENING || state == VoiceControllerState.PROCESSING) voice.cancel()
                    voice.speak(answer)
                }
            }
        }
    }
}

@Composable
private fun VoiceControlButton(label: String, enabled: Boolean, onClick: () -> Unit) {
    Box(
        Modifier
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
