package com.jarvis.companion.ui

import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.jarvis.companion.link.LinkMode
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.presentation.TaskTone
import com.jarvis.companion.presentation.TaskStates

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
