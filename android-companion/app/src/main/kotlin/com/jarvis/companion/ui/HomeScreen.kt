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
import com.jarvis.companion.presentation.ConversationPresentation
import com.jarvis.companion.presentation.MacLinkPresentation
import com.jarvis.companion.presentation.Speaker
import com.jarvis.companion.presentation.TaskTone
import com.jarvis.companion.presentation.ConversationItem

@Composable
fun HomeScreen(store: JarvisStore, onOpenSettings: () -> Unit, onOpenConnection: () -> Unit) {
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
    val lastApiTerminal = entries.lastOrNull { row ->
        row["transport"] == "api" && (row["state"] as? String)?.uppercase() in setOf("COMPLETED", "FAILED", "CANCELLED")
    }
    LaunchedEffect(lastApiTerminal?.get("message_id"), lastApiTerminal?.get("state")) {
        val requestId = lastApiTerminal?.get("message_id") as? String
        val state = lastApiTerminal?.get("state") as? String
        if (!requestId.isNullOrBlank() && !state.isNullOrBlank()) {
            PerformanceTrace.mark(PerformanceTrace.requestId(requestId), "compose_terminal_render", detail = "state=${state.uppercase()}")
        }
    }
    val connection = MacLinkPresentation.state(
        paired = store.macApiPaired || store.linkStatus?.certificatesVerified == true,
        enrollmentPending = store.linkStatus?.enrollmentPending == true,
        syncing = store.syncing || store.macApiSyncing,
        lastSyncMs = maxOf(store.lastSyncMs, store.macApiLastSyncMs),
        lastSyncFailed = if (store.macApiPaired) store.macApiSyncError.isNotBlank() else store.lastSyncError.isNotBlank(),
        nowMs = store.now,
        formatAgo = { ageLabel(it) },
    )

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(start = JarvisSpace.Page, end = 8.dp, top = 10.dp, bottom = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                BasicText("JARVIS", style = JarvisType.Label.copy(color = JarvisColor.Sage, letterSpacing = 1.2.sp))
                Spacer(Modifier.height(2.dp))
                BasicText(
                    store.session.title.ifBlank { "Your assistant" },
                    style = JarvisType.Title.copy(color = JarvisColor.Text),
                    maxLines = 1,
                )
            }
            IconAction(JarvisIcons.History, "Chat history") { store.openOverlay(Overlay.HISTORY) }
            IconAction(JarvisIcons.NewChat, "New chat") { store.newChat() }
            IconAction(JarvisIcons.Settings, "Settings", onClick = onOpenSettings)
        }
        Row(
            Modifier
                .padding(start = JarvisSpace.Page, bottom = 8.dp)
                .semantics { contentDescription = "Mac connection: ${connection.label}" }
                .clip(RoundedCornerShape(50))
                .background(toneColor(connection.tone).copy(alpha = 0.12f))
                .clickable(role = Role.Button, onClick = onOpenConnection)
                .padding(horizontal = 11.dp, vertical = 7.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            PresenceSignal(animating = store.hasRunningWork(), tint = toneColor(connection.tone), modifier = Modifier.size(18.dp))
            Spacer(Modifier.width(7.dp))
            BasicText(connection.label, style = JarvisType.Caption.copy(color = toneColor(connection.tone)), maxLines = 1)
        }

        if (store.notice.isNotEmpty()) {
            Notice(store.notice) { store.clearNotice() }
        }

        if (items.isEmpty()) {
            Box(
                Modifier.weight(1f).fillMaxWidth(),
                contentAlignment = Alignment.Center,
            ) {
                if (!connection.paired || connection.tone == TaskTone.FAILED || connection.tone == TaskTone.WAITING) {
                    HeroSurfaceCard(Modifier.padding(horizontal = JarvisSpace.Page)) {
                        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Box(
                                    Modifier
                                        .size(42.dp)
                                        .clip(CircleShape)
                                        .background(JarvisColor.SageTint),
                                    contentAlignment = Alignment.Center,
                                ) {
                                    PresenceSignal(animating = false, modifier = Modifier.size(24.dp))
                                }
                                Spacer(Modifier.width(12.dp))
                                Column {
                                    SectionLabel(if (connection.pendingEnrollment) "Pairing" else "Mac connection")
                                    BasicText(connection.label, style = JarvisType.Section.copy(color = JarvisColor.Text))
                                }
                            }
                            BasicText(
                                connection.detail,
                                style = JarvisType.Body.copy(color = JarvisColor.TextMuted),
                            )
                            TextAction(
                                "View connection options",
                                filled = true,
                                enabled = !store.syncing,
                                modifier = Modifier.fillMaxWidth(),
                            ) {
                                if (connection.paired) store.syncNow() else onOpenConnection()
                            }
                        }
                    }
                } else {
                    EmptyNote(
                        "Ready when you are",
                        "Ask Jarvis a question. The Mac's real response and task state will appear here.",
                    )
                }
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
                .semantics { contentDescription = if (store.localAiRequestRunning) "Cancel on-device response" else "Send" }
                .clickable(enabled = canSend || store.localAiRequestRunning, role = Role.Button) {
                    if (store.localAiRequestRunning) store.cancelLocalAi() else store.submit()
                },
            contentAlignment = Alignment.Center,
        ) {
            if (store.localAiRequestRunning) {
                Image(
                    painter = rememberVectorPainter(JarvisIcons.Close),
                    contentDescription = null,
                    colorFilter = ColorFilter.tint(JarvisColor.Background),
                    modifier = Modifier.size(22.dp),
                )
            } else if (store.submitting) {
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
