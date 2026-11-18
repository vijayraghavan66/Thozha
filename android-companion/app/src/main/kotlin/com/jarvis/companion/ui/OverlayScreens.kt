package com.jarvis.companion.ui

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import androidx.compose.animation.core.animateDpAsState
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
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.jarvis.companion.AdvancedActivity
import com.jarvis.companion.link.LinkAlerts
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.telegram.TelegramActivity

/** Settings: real connection diagnostics, a real sync action, real preferences, and the existing setup screens. */
@Composable
fun SettingsScreen(store: JarvisStore, onClose: () -> Unit) {
    val context = LocalContext.current
    val settings = remember(context) { LinkSettings(context) }
    val telegram = store.telegram
    Column(Modifier.fillMaxSize().statusBarsPadding()) {
        Row(Modifier.fillMaxWidth().padding(start = 8.dp, end = 8.dp, top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            IconAction(JarvisIcons.Back, "Close settings", onClick = onClose)
            BasicText("Settings", style = JarvisType.Section.copy(color = JarvisColor.Text))
        }
        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = JarvisSpace.Page, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            item { SectionLabel("Connection") }
            item {
                FactCard(
                    listOf(
                        "Route" to modeLabel(settings.mode),
                        "Relay" to settings.relayUrl,
                        "Telegram" to telegramConnectionLabel(telegram?.connection),
                        "Last sync" to if (store.lastSyncMs > 0L) "${clockTime(store.lastSyncMs)} (${ageLabel(store.now - store.lastSyncMs)})" else "Not yet",
                        "Sync result" to when {
                            store.lastSyncMs <= 0L -> "No sync has run yet"
                            store.lastSyncError.isBlank() -> "No error reported"
                            else -> store.lastSyncError
                        },
                    ),
                )
            }
            item {
                TextAction(
                    label = if (store.syncing) "Syncing with the Mac…" else "Sync now",
                    filled = false,
                    enabled = !store.syncing,
                    modifier = Modifier.fillMaxWidth(),
                ) { store.syncNow() }
            }

            item { SectionLabel("AI routing") }
            item {
                SurfaceCard(
                    onClick = {
                        val modes = com.jarvis.companion.localai.LocalAiRequestMode.entries
                        val next = modes[(modes.indexOf(store.aiRequestMode) + 1) % modes.size]
                        store.updateAiRequestMode(next)
                    },
                ) {
                    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                        BasicText("${aiModeLabel(store.aiRequestMode)} · tap to change", style = JarvisType.Section.copy(color = JarvisColor.Text))
                        BasicText(
                            when (store.aiRequestMode) {
                                com.jarvis.companion.localai.LocalAiRequestMode.AUTO -> "Exact supported commands use their typed path. Other requests use a loaded local model only when one is verified; otherwise they go to the Mac. No local model is loaded in this build."
                                com.jarvis.companion.localai.LocalAiRequestMode.MAC_ONLY -> "Requests use the paired Mac AI. Exact supported commands still use their authenticated typed path."
                                com.jarvis.companion.localai.LocalAiRequestMode.LOCAL_ONLY -> "No local model is installed or loaded. Jarvis will keep requests on this phone and refuse them until local inference is ready."
                            },
                            style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                        )
                    }
                }
            }

            item { SectionLabel("Appearance") }
            item {
                SurfaceCard {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            BasicText("Reduce motion", style = JarvisType.Section.copy(color = JarvisColor.Text))
                            Spacer(Modifier.height(2.dp))
                            BasicText(
                                "Turns off spring and slide transitions. The system animation setting also applies.",
                                style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                            )
                        }
                        Toggle(on = store.reduceMotion) { store.updateReduceMotion(!store.reduceMotion) }
                    }
                }
            }

            item { SectionLabel("Notifications") }
            item {
                FactCard(listOf("Permission" to notificationPermissionLabel(context)))
            }
            item {
                val granted = Build.VERSION.SDK_INT < 33 ||
                    ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
                if (granted) {
                    TextAction("Open notification settings", filled = false, modifier = Modifier.fillMaxWidth()) {
                        openAppNotificationSettings(context)
                    }
                } else {
                    TextAction("Allow notifications", filled = true, modifier = Modifier.fillMaxWidth()) {
                        store.permissionRequester?.invoke(Manifest.permission.POST_NOTIFICATIONS) ?: openAppNotificationSettings(context)
                    }
                }
            }
            if (Build.VERSION.SDK_INT >= 37) {
                item {
                    val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_LOCAL_NETWORK) == PackageManager.PERMISSION_GRANTED
                    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                        FactCard(listOf("Local Wi-Fi access" to if (granted) "Allowed" else "Not allowed. USB stays available."))
                        if (!granted) {
                            TextAction("Allow local Wi-Fi access", filled = false, modifier = Modifier.fillMaxWidth()) {
                                store.permissionRequester?.invoke(Manifest.permission.ACCESS_LOCAL_NETWORK)
                            }
                        }
                    }
                }
            }

            item { SectionLabel("Setup") }
            item {
                TextAction("Mac pairing and advanced setup", filled = false, modifier = Modifier.fillMaxWidth()) {
                    context.startActivity(Intent(context, AdvancedActivity::class.java))
                }
            }
            item {
                TextAction("Telegram setup", filled = false, modifier = Modifier.fillMaxWidth()) {
                    context.startActivity(Intent(context, TelegramActivity::class.java))
                }
            }

            item { SectionLabel("Privacy") }
            item {
                SurfaceCard {
                    BasicText(
                        "Requests travel as encrypted Jarvis Link envelopes. The relay and Telegram carry only those envelopes. Pairing keys stay in Android Keystore on this phone.",
                        style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                    )
                }
            }
            item { Spacer(Modifier.height(24.dp)) }
        }
    }
}

private fun openAppNotificationSettings(context: Context) {
    context.startActivity(
        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
    )
}

private fun notificationPermissionLabel(context: Context): String {
    if (Build.VERSION.SDK_INT < 33) return "Allowed by this Android version"
    return if (ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) {
        "Allowed"
    } else {
        "Not allowed. Approvals still appear in Approvals."
    }
}

private fun modeLabel(mode: com.jarvis.companion.link.LinkMode): String = when (mode) {
    com.jarvis.companion.link.LinkMode.LOCAL_USB -> "Local USB"
    com.jarvis.companion.link.LinkMode.TEST_RELAY -> "Test relay"
}

private fun aiModeLabel(mode: com.jarvis.companion.localai.LocalAiRequestMode): String = when (mode) {
    com.jarvis.companion.localai.LocalAiRequestMode.AUTO -> "AI route: Auto"
    com.jarvis.companion.localai.LocalAiRequestMode.MAC_ONLY -> "AI route: Mac only"
    com.jarvis.companion.localai.LocalAiRequestMode.LOCAL_ONLY -> "AI route: Local only"
}

/** A native switch drawn with Compose; the knob moves on a spring. */
@Composable
private fun Toggle(on: Boolean, onToggle: () -> Unit) {
    val knob by animateDpAsState(targetValue = if (on) 22.dp else 2.dp, animationSpec = jarvisSpring())
    Box(
        Modifier
            .size(width = 50.dp, height = 30.dp)
            .clip(RoundedCornerShape(50))
            .background(if (on) JarvisColor.Sage else JarvisColor.Border)
            .clickable(role = androidx.compose.ui.semantics.Role.Switch, onClick = onToggle),
    ) {
        Box(
            Modifier
                .offset(x = knob, y = 4.dp)
                .size(22.dp)
                .clip(CircleShape)
                .background(if (on) JarvisColor.Background else JarvisColor.Text),
        )
    }
}

/** Chat history: every conversation kept on this phone, newest first. Opening one makes it the active chat. */
@Composable
fun HistoryScreen(store: JarvisStore, onClose: () -> Unit) {
    val sessions = store.sessionList.sortedByDescending { it.createdMs }
    Column(Modifier.fillMaxSize().statusBarsPadding()) {
        Row(Modifier.fillMaxWidth().padding(start = 8.dp, end = 8.dp, top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            IconAction(JarvisIcons.Back, "Close chat history", onClick = onClose)
            BasicText("Chat history", style = JarvisType.Section.copy(color = JarvisColor.Text))
        }
        if (sessions.isEmpty()) {
            EmptyNote("No conversations yet", "Start a chat from Home. Past conversations are kept on this phone.")
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = JarvisSpace.Page, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                items(sessions, key = { it.id }) { session ->
                    val count = store.requestCount(session.id)
                    val preview = store.latestRequestText(session.id)
                    SurfaceCard(raised = session.id == store.session.id, onClick = { store.openSession(session.id) }) {
                        Column {
                            BasicText(
                                session.title.ifBlank { "New chat" },
                                style = JarvisType.Section.copy(color = JarvisColor.Text),
                                maxLines = 1,
                            )
                            Spacer(Modifier.height(4.dp))
                            BasicText(
                                if (preview.isBlank()) "No messages yet" else preview,
                                style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                                maxLines = 2,
                            )
                            Spacer(Modifier.height(8.dp))
                            BasicText(
                                "$count request(s) · started ${clockTime(session.createdMs)}",
                                style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                            )
                        }
                    }
                }
            }
        }
    }
}
