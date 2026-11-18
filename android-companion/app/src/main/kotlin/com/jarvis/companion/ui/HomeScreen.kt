package com.jarvis.companion.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.vector.rememberVectorPainter
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import android.Manifest
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import com.jarvis.companion.voice.JarvisVoiceController
import com.jarvis.companion.voice.VoiceControllerError
import com.jarvis.companion.voice.VoiceControllerState
import com.jarvis.companion.voice.VoiceRecognitionProvider
import com.jarvis.companion.presentation.AiEvidence
import com.jarvis.companion.presentation.ConnectionPresentation
import com.jarvis.companion.presentation.ConversationPresentation
import com.jarvis.companion.presentation.Speaker
import com.jarvis.companion.presentation.TaskTone
import com.jarvis.companion.presentation.TransportState
import com.jarvis.companion.presentation.ConversationItem

@Composable
fun HomeScreen(store: JarvisStore, onOpenSettings: () -> Unit) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val voiceState = remember { mutableStateOf(VoiceControllerState.IDLE) }
    val voicePreview = remember { mutableStateOf("") }
    val voice = remember(context, store) {
        JarvisVoiceController(context, object : JarvisVoiceController.Listener {
            override fun onStateChanged(state: VoiceControllerState) { voiceState.value = state }
            override fun onRecognitionProvider(provider: VoiceRecognitionProvider) {
                store.notice = if (provider == VoiceRecognitionProvider.ON_DEVICE) {
                    "Speech is being recognized on this device."
                } else {
                    "Android's system speech service is active and may use network processing."
                }
            }
            override fun onPartialText(text: String) { voicePreview.value = text }
            override fun onFinalText(text: String) {
                voicePreview.value = ""
                store.draft = text
                store.notice = "Voice captured. Review the text, then tap Send."
            }
            override fun onError(error: VoiceControllerError) {
                voicePreview.value = ""
                store.notice = when (error) {
                    VoiceControllerError.PERMISSION_REQUIRED -> "Microphone permission is required. Tap the mic to allow it."
                    VoiceControllerError.RECOGNIZER_UNAVAILABLE -> "Speech recognition is unavailable. You can type your request."
                    VoiceControllerError.NO_MATCH -> "I didn't catch that. Try again or type your request."
                    VoiceControllerError.SPEECH_TIMEOUT -> "No speech was detected. Hold the mic and try again."
                    VoiceControllerError.NETWORK_OR_SERVICE -> "Speech recognition couldn't connect. Try again or type your request."
                    VoiceControllerError.RECOGNIZER_BUSY -> "Speech recognition is busy. Try again in a moment."
                    VoiceControllerError.LANGUAGE_UNAVAILABLE -> "Speech recognition doesn't support this language."
                    VoiceControllerError.SYNTHESIS_UNAVAILABLE, VoiceControllerError.SYNTHESIS_FAILED -> "Text-to-speech is unavailable right now."
                    VoiceControllerError.MICROPHONE_UNAVAILABLE -> "The microphone couldn't start. Check another app isn't using it."
                    VoiceControllerError.UNKNOWN -> "Voice input failed. You can type your request."
                }
            }
        })
    }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(voice, lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP) voice.cancel()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            voice.close()
        }
    }
    val haptics = LocalHapticFeedback.current
    val now = store.now
    val entries = store.entriesInSession()
    val items = remember(entries, now) { ConversationPresentation.items(entries) { clockTime(it) } }
    val transport = transportState(store.telegram?.connection)
    val header = connectionHeader(store.entries, transport, now)

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(start = JarvisSpace.Page, end = 8.dp, top = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                BasicText(store.session.title.ifBlank { "New chat" }, style = JarvisType.Title.copy(color = JarvisColor.Text), maxLines = 1)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    PresenceSignal(animating = store.hasRunningWork())
                    Spacer(Modifier.width(8.dp))
                    BasicText(header.headline, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted), maxLines = 1)
                }
            }
            IconAction(JarvisIcons.History, "Chat history") { store.openOverlay(Overlay.HISTORY) }
            IconAction(JarvisIcons.NewChat, "New chat") { store.newChat() }
            IconAction(JarvisIcons.Settings, "Settings", onClick = onOpenSettings)
        }

        if (store.notice.isNotEmpty()) {
            Notice(store.notice) { store.clearNotice() }
        }

        if (items.isEmpty()) {
            Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                EmptyNote(
                    "Ask Jarvis",
                    "Requests travel to your Mac over the encrypted link. Replies appear here as the Mac reports them, and nothing is shown until it does.",
                )
            }
        } else {
            val listState = rememberLazyListState()
            val newest = items.asReversed()
            LaunchedEffect(items.size) { if (items.isNotEmpty()) listState.animateScrollToItem(0) }
            LazyColumn(
                state = listState,
                reverseLayout = true,
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = JarvisSpace.Page, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                itemsIndexed(newest, key = { _, item -> item.id }) { _, item ->
                    MessageRow(item, onSpeak = if (item.speaker == Speaker.JARVIS) ({ voice.speak(item.text) }) else null)
                }
            }
        }

        Composer(store, voice, voiceState.value, voicePreview.value, haptics)
    }
}

/** Telegram transport, the Mac's last observed reply, and the AI answer evidence, kept as separate facts. */
private fun connectionHeader(entries: List<Map<String, Any>>, transport: TransportState, now: Long): ConnectionHeader {
    val answered = entries.lastOrNull { (it["state"] as? String) == "COMPLETED" && !(it["summary"] as? String).isNullOrBlank() }
    val ai = when {
        answered == null -> AiEvidence.NOT_VERIFIED
        now - ((answered["updated_at_unix_ms"] as? Number)?.toLong() ?: 0L) < 30 * 60_000L -> AiEvidence.ANSWERED
        else -> AiEvidence.NO_RECENT_ANSWER
    }
    val lastMacUpdate = entries.filter { (it["state"] as? String) !in setOf("QUEUED", "SENT") }
        .mapNotNull { (it["updated_at_unix_ms"] as? Number)?.toLong() }
        .maxOrNull()
    val summary = ConnectionPresentation.summary(transport, lastMacUpdate, ai, now) { ageLabel(it) }
    return ConnectionHeader(summary.headline, summary.subline)
}

private data class ConnectionHeader(val headline: String, val subline: String)

@Composable
private fun MessageRow(item: ConversationItem, onSpeak: (() -> Unit)?) {
    val isUser = item.speaker == Speaker.USER
    Column(
        Modifier.fillMaxWidth(),
        horizontalAlignment = if (isUser) Alignment.End else Alignment.Start,
    ) {
        if (isUser) {
            Box(
                Modifier
                    .clip(RoundedCornerShape(20.dp, 20.dp, 6.dp, 20.dp))
                    .background(JarvisColor.SurfaceRaised)
                    .padding(horizontal = 16.dp, vertical = 12.dp),
            ) {
                SelectionContainer {
                    BasicText(item.text, style = JarvisType.Request.copy(color = JarvisColor.Text))
                }
            }
        } else {
            Column(Modifier.fillMaxWidth()) {
                SectionLabel("Jarvis", Modifier.padding(bottom = 6.dp))
                SelectionContainer {
                    BasicText(
                        item.text.ifBlank { "No text was returned for this request." },
                        style = JarvisType.Body.copy(color = if (item.text.isBlank()) JarvisColor.TextMuted else JarvisColor.Text),
                    )
                }
                if (onSpeak != null && item.text.isNotBlank()) {
                    Row(
                        Modifier
                            .padding(top = 8.dp)
                            .clip(RoundedCornerShape(16.dp))
                            .clickable(role = Role.Button, onClick = onSpeak)
                            .semantics { contentDescription = "Read answer aloud" }
                            .padding(horizontal = 10.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Image(
                            painter = rememberVectorPainter(JarvisIcons.Speaker),
                            contentDescription = null,
                            colorFilter = ColorFilter.tint(JarvisColor.Sage),
                            modifier = Modifier.size(18.dp),
                        )
                        Spacer(Modifier.width(6.dp))
                        BasicText("Listen", style = JarvisType.Caption.copy(color = JarvisColor.Sage))
                    }
                }
            }
        }
        item.statusLabel?.let { label ->
            Spacer(Modifier.height(6.dp))
            StatusPill(label, item.tone ?: TaskTone.UNKNOWN)
        }
        if (item.timeLabel.isNotBlank()) {
            Spacer(Modifier.height(4.dp))
            BasicText(item.timeLabel, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted, fontSize = 12.sp))
        }
    }
}

@Composable
private fun Composer(
    store: JarvisStore,
    voice: JarvisVoiceController,
    voiceState: VoiceControllerState,
    voicePreview: String,
    haptics: androidx.compose.ui.hapticfeedback.HapticFeedback,
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val canSend = store.draft.isNotBlank() && !store.submitting
    val micPermission = JarvisVoiceController.hasMicrophonePermission(context)
    val micDescription = when (voiceState) {
        VoiceControllerState.LISTENING -> "Listening. Release to finish voice input."
        VoiceControllerState.PROCESSING -> "Processing voice input"
        VoiceControllerState.SPEAKING -> "Speaking. Tap mic to stop."
        VoiceControllerState.ERROR -> "Voice input error. Tap mic to retry."
        VoiceControllerState.IDLE -> if (micPermission) "Hold to dictate" else "Allow microphone, then hold to dictate"
    }
    Row(
        Modifier
            .imePadding()
            .padding(start = 12.dp, end = 12.dp, top = 8.dp, bottom = 12.dp)
            .fillMaxWidth()
            .clip(RoundedCornerShape(26.dp))
            .background(JarvisColor.Surface)
            .border(1.dp, JarvisColor.Border, RoundedCornerShape(26.dp))
            .padding(start = 18.dp, end = 6.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.Bottom,
    ) {
        Box(Modifier.weight(1f).padding(vertical = 10.dp)) {
            if (store.draft.isEmpty()) {
                BasicText(
                    voicePreview.takeIf { it.isNotBlank() } ?: when (voiceState) {
                        VoiceControllerState.LISTENING -> "Listening… release to finish"
                        VoiceControllerState.PROCESSING -> "Transcribing…"
                        else -> "Ask Jarvis"
                    },
                    style = JarvisType.Request.copy(color = if (voicePreview.isNotBlank()) JarvisColor.Text else JarvisColor.TextMuted),
                )
            }
            BasicTextField(
                value = store.draft,
                onValueChange = { store.draft = it },
                textStyle = JarvisType.Request.copy(color = JarvisColor.Text),
                cursorBrush = SolidColor(JarvisColor.Sage),
                maxLines = 5,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { if (canSend) store.submit() }),
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Spacer(Modifier.width(6.dp))
        Box(
            Modifier
                .size(44.dp)
                .clip(CircleShape)
                .background(if (voiceState == VoiceControllerState.LISTENING) JarvisColor.SageTint else JarvisColor.SurfaceRaised)
                .pointerInput(voice, micPermission) {
                    detectTapGestures(onPress = {
                        if (!micPermission) {
                            store.notice = "Allow microphone access, then hold the mic to dictate."
                            store.permissionRequester?.invoke(Manifest.permission.RECORD_AUDIO)
                            return@detectTapGestures
                        }
                        if (voiceState == VoiceControllerState.SPEAKING) {
                            voice.stopSpeaking()
                            return@detectTapGestures
                        }
                        if (!voice.startPushToTalk()) return@detectTapGestures
                        haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                        val released = tryAwaitRelease()
                        if (released) voice.releasePushToTalk() else voice.cancel()
                    })
                }
                .semantics {
                    contentDescription = micDescription
                    role = Role.Button
                    stateDescription = micDescription
                    onClick(label = if (voiceState == VoiceControllerState.LISTENING) "Finish voice input" else "Start voice input") {
                        if (!micPermission) {
                            store.permissionRequester?.invoke(Manifest.permission.RECORD_AUDIO)
                        } else if (voiceState == VoiceControllerState.LISTENING) {
                            voice.releasePushToTalk()
                        } else if (voiceState == VoiceControllerState.SPEAKING) {
                            voice.stopSpeaking()
                        } else {
                            voice.startPushToTalk()
                        }
                        true
                    }
                },
            contentAlignment = Alignment.Center,
        ) {
            Image(
                painter = rememberVectorPainter(JarvisIcons.Microphone),
                contentDescription = null,
                colorFilter = ColorFilter.tint(if (voiceState == VoiceControllerState.LISTENING) JarvisColor.Sage else JarvisColor.TextMuted),
                modifier = Modifier.size(21.dp),
            )
        }
        Spacer(Modifier.width(4.dp))
        Box(
            Modifier
                .size(48.dp)
                .clip(CircleShape)
                .background(if (canSend || store.submitting) JarvisColor.Sage else JarvisColor.SurfaceRaised)
                .semantics { contentDescription = "Send" }
                .clickable(enabled = canSend, role = Role.Button) { store.submit() },
            contentAlignment = Alignment.Center,
        ) {
            if (store.submitting) {
                Spinner(color = JarvisColor.Background)
            } else {
                Image(
                    painter = rememberVectorPainter(JarvisIcons.Send),
                    contentDescription = null,
                    colorFilter = ColorFilter.tint(if (canSend) JarvisColor.Background else JarvisColor.TextMuted),
                    modifier = Modifier.size(22.dp),
                )
            }
        }
    }
}

@Composable
private fun Notice(text: String, onDismiss: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = JarvisSpace.Page)
            .clip(RoundedCornerShape(14.dp))
            .background(JarvisColor.SageTint)
            .clickable(role = androidx.compose.ui.semantics.Role.Button, onClick = onDismiss)
            .padding(horizontal = 14.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        BasicText(text, style = JarvisType.Caption.copy(color = JarvisColor.Text), modifier = Modifier.weight(1f))
        BasicText("Dismiss", style = JarvisType.Caption.copy(color = JarvisColor.Sage))
    }
}
