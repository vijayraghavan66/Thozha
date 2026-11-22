package com.jarvis.companion.ui

import androidx.compose.animation.animateContentSize
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
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.jarvis.companion.presentation.MacLinkPresentation
import com.jarvis.companion.presentation.TimelinePresentation
import com.jarvis.companion.presentation.TaskTone

@Composable
fun HomeOverviewScreen(
    store: JarvisStore,
    onOpenChat: () -> Unit,
    onOpenTasks: () -> Unit,
    onOpenDevices: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    val connection = MacLinkPresentation.state(
        paired = store.macApiPaired || store.linkStatus?.certificatesVerified == true,
        enrollmentPending = store.linkStatus?.enrollmentPending == true,
        syncing = store.syncing || store.macApiSyncing,
        lastSyncMs = maxOf(store.lastSyncMs, store.macApiLastSyncMs),
        lastSyncFailed = if (store.macApiPaired) store.macApiSyncError.isNotBlank() else store.lastSyncError.isNotBlank(),
        nowMs = store.now,
        formatAgo = { ageLabel(it) },
    )
    val updatedAtByRequest = store.entries.associate { row ->
        (row["message_id"] as? String).orEmpty() to ((row["updated_at_unix_ms"] as? Number)?.toLong() ?: 0L)
    }
    val recentRows = TimelinePresentation.sections(store.entries) { clockTime(it) }
        .flatMap { it.rows }
        .sortedByDescending { updatedAtByRequest[it.requestId] ?: 0L }
        .take(3)
    val actionableApprovals = store.approvals.count { it.expiresAtUnixMs > store.now }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(start = JarvisSpace.Page, end = 10.dp, top = 14.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                BasicText("THOZHA  ·  தோழா", style = JarvisType.Label.copy(color = JarvisColor.Sage, letterSpacing = 1.sp))
                Spacer(Modifier.height(5.dp))
                BasicText("Hello, ${store.assistantName}", style = JarvisType.Title.copy(color = JarvisColor.Text))
            }
            IconAction(JarvisIcons.Settings, "Settings", onClick = onOpenSettings)
        }

        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = JarvisSpace.Page, vertical = 10.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            item {
                HeroSurfaceCard {
                    Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Box(
                                Modifier.size(38.dp).clip(CircleShape).background(JarvisColor.SageTint),
                                contentAlignment = Alignment.Center,
                            ) {
                                PresenceSignal(
                                    animating = store.hasRunningWork(),
                                    tint = toneColor(connection.tone),
                                    modifier = Modifier.size(23.dp),
                                )
                            }
                            Spacer(Modifier.width(12.dp))
                            Column(Modifier.weight(1f)) {
                                SectionLabel("Your Mac")
                                BasicText(connection.label, style = JarvisType.Section.copy(color = toneColor(connection.tone)))
                            }
                            IconAction(JarvisIcons.Devices, "View devices", onClick = onOpenDevices)
                        }
                        BasicText(
                            connection.detail,
                            style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                        )
                        if (!connection.paired) {
                            TextAction("Connect a Mac", filled = true, modifier = Modifier.fillMaxWidth(), onClick = onOpenDevices)
                        }
                    }
                }
            }

            item {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    SectionLabel("Start with a thought")
                    QuickRequest(store, onOpenChat)
                }
            }

            if (actionableApprovals > 0) {
                item {
                    SurfaceCard(
                        modifier = Modifier.semantics { contentDescription = "$actionableApprovals approvals waiting for review" },
                        onClick = onOpenTasks,
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                BasicText("Waiting for you", style = JarvisType.Section.copy(color = JarvisColor.Text))
                                Spacer(Modifier.height(3.dp))
                                BasicText(
                                    "$actionableApprovals exact ${if (actionableApprovals == 1) "action" else "actions"} need review",
                                    style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                                )
                            }
                            StatusPill("Review", TaskTone.WAITING)
                        }
                    }
                }
            }

            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    SectionLabel("Recent work", Modifier.weight(1f))
                    BasicText(
                        "See all",
                        modifier = Modifier
                            .clip(RoundedCornerShape(10.dp))
                            .clickable(role = Role.Button, onClick = onOpenTasks)
                            .padding(horizontal = 10.dp, vertical = 8.dp)
                            .semantics { contentDescription = "See all tasks" },
                        style = JarvisType.Caption.copy(color = JarvisColor.Sage),
                    )
                }
            }
            if (recentRows.isEmpty()) {
                item {
                    SurfaceCard {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            PresenceSignal(animating = false, modifier = Modifier.size(22.dp))
                            Spacer(Modifier.width(12.dp))
                            Column {
                                BasicText("A clear start", style = JarvisType.Section.copy(color = JarvisColor.Text))
                                BasicText("Your real task history will appear here.", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
                            }
                        }
                    }
                }
            } else {
                items(items = recentRows, key = { it.requestId }) { row ->
                    SurfaceCard(onClick = onOpenTasks) {
                        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                BasicText(row.title.ifBlank { "Request" }, Modifier.weight(1f), style = JarvisType.Section.copy(color = JarvisColor.Text), maxLines = 2)
                                Spacer(Modifier.width(8.dp))
                                StatusPill(row.statusLabel, row.tone)
                            }
                            if (row.detail.isNotBlank()) {
                                BasicText(row.detail, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted), maxLines = 2)
                            }
                            BasicText(row.timeLabel, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted, fontSize = 12.sp))
                        }
                    }
                }
            }
            item { Spacer(Modifier.height(8.dp)) }
        }
    }
}

@Composable
private fun QuickRequest(store: JarvisStore, onOpenChat: () -> Unit) {
    val send: () -> Unit = {
        if (store.draft.isNotBlank() && !store.submitting) {
            store.submit()
            onOpenChat()
        }
    }
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(24.dp))
            .background(JarvisColor.Surface)
            .animateContentSize(jarvisSpring())
            .padding(start = 18.dp, end = 8.dp, top = 8.dp, bottom = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        BasicTextField(
            value = store.draft,
            onValueChange = { store.draft = it },
            enabled = !store.submitting,
            modifier = Modifier.weight(1f).semantics { contentDescription = "Ask ${store.assistantName}" },
            textStyle = JarvisType.Request.copy(color = JarvisColor.Text),
            cursorBrush = SolidColor(JarvisColor.Sage),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { send() }),
            decorationBox = { inner ->
                Box {
                    if (store.draft.isEmpty()) BasicText("Ask ${store.assistantName} anything…", style = JarvisType.Request.copy(color = JarvisColor.TextMuted))
                    inner()
                }
            },
        )
        IconAction(
            JarvisIcons.Send,
            if (store.submitting) "Sending request" else "Send request",
            enabled = store.draft.isNotBlank() && !store.submitting,
            tint = JarvisColor.Sage,
            onClick = send,
        )
    }
}
