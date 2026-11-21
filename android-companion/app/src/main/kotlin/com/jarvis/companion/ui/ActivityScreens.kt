package com.jarvis.companion.ui

import android.content.Intent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.layout.size
import com.jarvis.companion.presentation.TaskTone
import com.jarvis.companion.presentation.TaskProgressEvent
import com.jarvis.companion.presentation.TaskProgressPresentation
import com.jarvis.companion.presentation.TimelinePresentation
import com.jarvis.companion.presentation.TimelineRow
import com.jarvis.companion.presentation.ApprovalDecisionPresentation
import com.jarvis.companion.presentation.ApprovalDecisionStatus
import com.jarvis.link.ApprovalReview
import com.jarvis.link.DevelopmentPlanReview
import com.jarvis.companion.model.TaskArtifact

@Composable
fun ScreenHeader(title: String, subtitle: String) {
    Column(Modifier.fillMaxWidth().padding(start = JarvisSpace.Page, end = JarvisSpace.Page, top = 16.dp, bottom = 12.dp)) {
        BasicText(title, style = JarvisType.Title.copy(color = JarvisColor.Text))
        Spacer(Modifier.height(4.dp))
        BasicText(subtitle, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
    }
}

/** Task records from the Mac, grouped by real state. Nothing is shown as done until the Mac reports it. */
@Composable
fun ActivityScreen(store: JarvisStore) {
    var pendingArtifact by remember { mutableStateOf<TaskArtifact?>(null) }
    val saveArchive = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/zip")) { uri ->
        val artifact = pendingArtifact
        pendingArtifact = null
        if (uri != null && artifact != null) store.saveTaskArtifact(artifact, uri)
    }
    val sections = remember(store.entries, store.now) { TimelinePresentation.sections(store.entries) { clockTime(it) } }
    Column(Modifier.fillMaxSize()) {
        ScreenHeader("Activity", "Tasks as the Mac has reported them. Unconfirmed requests stay marked as such.")
        if (sections.isEmpty()) {
            EmptyNote("No tasks yet", "Requests you send and the Mac's real results appear here, with the state the Mac reports.")
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = JarvisSpace.Page, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                sections.forEach { section ->
                    item(key = "header-${section.title}") {
                        Column(Modifier.padding(top = 8.dp, bottom = 2.dp)) { SectionLabel(section.title) }
                    }
                    items(section.rows, key = { it.requestId }) { row ->
                        val progress = remember(row.taskId, row.requestId, store.taskUpdates) {
                            TaskProgressPresentation.eventsFor(row.taskId, store.taskUpdates) { clockTime(it) }
                        }
                        TaskRow(row, progress, store) { artifact ->
                            pendingArtifact = artifact
                            saveArchive.launch(artifact.fileName)
                        }
                    }
                }
                item { Spacer(Modifier.height(16.dp)) }
            }
        }
    }
}

@Composable
private fun TaskRow(row: TimelineRow, progress: List<TaskProgressEvent>, store: JarvisStore, onSaveArtifact: (TaskArtifact) -> Unit) {
    val context = androidx.compose.ui.platform.LocalContext.current
    var showAllProgress by remember(row.requestId) { mutableStateOf(false) }
    val visibleProgress = if (showAllProgress) progress else progress.takeLast(1)
    SurfaceCard {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                BasicText(
                    row.title.ifBlank { "Request" },
                    style = JarvisType.Section.copy(color = JarvisColor.Text),
                    maxLines = 2,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(10.dp))
                StatusPill(row.statusLabel, row.tone)
            }
            if (row.detail.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                SelectionContainer {
                    BasicText(row.detail, style = JarvisType.Body.copy(color = JarvisColor.TextMuted))
                }
            }
            val footer = listOfNotNull(
                row.timeLabel.takeIf { it.isNotBlank() },
                row.taskId.takeIf { it.isNotBlank() }?.let { "Task ${it.take(8)}" },
            ).joinToString("  ·  ")
            if (footer.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                BasicText(footer, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            }
            if (row.canCancel && row.transport == "api") {
                Spacer(Modifier.height(10.dp))
                TextAction(
                    if (row.requestId in store.cancellingRequests) "Requesting cancellation…" else "Cancel task",
                    filled = false,
                    enabled = row.requestId !in store.cancellingRequests,
                    modifier = Modifier.fillMaxWidth(),
                ) { store.cancelLinkTask(row.requestId) }
            }
            if (row.transport == "api" && row.tone == TaskTone.FAILED && row.taskId.isBlank() &&
                row.detail.startsWith("The Mac no longer has this task record.")) {
                Spacer(Modifier.height(10.dp))
                TextAction("Check task status", filled = false, modifier = Modifier.fillMaxWidth()) {
                    store.reconcileLinkTask(row.requestId)
                }
            }
            if (row.tone == TaskTone.COMPLETED && row.capabilityScope == "development" && row.taskId.matches(Regex("[0-9a-f]{32}"))) {
                Spacer(Modifier.height(10.dp))
                val artifacts = store.taskArtifacts[row.taskId]
                if (artifacts.isNullOrEmpty()) {
                    TextAction(
                        if (row.taskId in store.artifactLoadingTasks) "Checking project files…" else "Find project files",
                        filled = false,
                        enabled = row.taskId !in store.artifactLoadingTasks,
                        modifier = Modifier.fillMaxWidth(),
                    ) { store.loadTaskArtifacts(row.taskId) }
                } else {
                    SectionLabel("Project files")
                    Spacer(Modifier.height(6.dp))
                    artifacts.forEach { artifact ->
                        BasicText(
                            "${artifact.fileName} · ${artifact.sizeBytes} bytes · SHA-256 ${artifact.archiveSha256.take(16)}…",
                            style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
                        )
                        Spacer(Modifier.height(6.dp))
                        TextAction("Download verified ZIP", filled = true, modifier = Modifier.fillMaxWidth()) {
                            onSaveArtifact(artifact)
                        }
                        store.savedArtifacts[artifact.artifactId]?.let { uri ->
                            Spacer(Modifier.height(6.dp))
                            TextAction("Open or share ZIP", filled = false, modifier = Modifier.fillMaxWidth()) {
                                val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/zip")
                                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                                runCatching { context.startActivity(Intent.createChooser(view, "Open project archive")) }
                                    .onFailure { store.notice = "The archive is saved and verified. Open it from My Files or share it from the selected folder." }
                            }
                        }
                    }
                }
            }
            if (progress.isNotEmpty()) {
                Spacer(Modifier.height(12.dp))
                SectionLabel("Mac updates")
                Spacer(Modifier.height(8.dp))
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    visibleProgress.forEachIndexed { index, event ->
                        Row(verticalAlignment = Alignment.Top) {
                            Column(
                                Modifier.width(16.dp),
                                horizontalAlignment = Alignment.CenterHorizontally,
                            ) {
                                androidx.compose.foundation.layout.Box(
                                    Modifier.size(8.dp).clip(androidx.compose.foundation.shape.CircleShape)
                                        .background(toneColor(event.tone)),
                                )
                                if (index < visibleProgress.lastIndex) {
                                    Spacer(Modifier.height(3.dp))
                                    androidx.compose.foundation.layout.Box(
                                        Modifier.width(1.dp).height(24.dp).background(JarvisColor.Border),
                                    )
                                }
                            }
                            Spacer(Modifier.width(8.dp))
                            Column(Modifier.weight(1f)) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    BasicText(event.label, style = JarvisType.Caption.copy(color = toneColor(event.tone), fontWeight = androidx.compose.ui.text.font.FontWeight.Medium), modifier = Modifier.weight(1f))
                                    if (event.timeLabel.isNotBlank()) {
                                        BasicText(event.timeLabel, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
                                    }
                                }
                                Spacer(Modifier.height(3.dp))
                                SelectionContainer {
                                    BasicText(event.detail, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
                                }
                            }
                        }
                    }
                }
                if (progress.size > 1) {
                    Spacer(Modifier.height(6.dp))
                    TextAction(
                        if (showAllProgress) "Show latest update" else "Show ${progress.size} Mac updates",
                        filled = false,
                        modifier = Modifier.fillMaxWidth(),
                    ) { showAllProgress = !showAllProgress }
                }
            }
        }
    }
}

/**
 * Exact operations waiting for a decision. Each card shows the operation, target, the Mac's redacted arguments,
 * the digest and the expiry. The decision controls appear only on the detail page, and only while not expired.
 */
@Composable
fun ApprovalsScreen(store: JarvisStore) {
    var selectedId by remember { mutableStateOf<String?>(null) }
    val selected = store.approvals.firstOrNull { it.approvalId == selectedId }
    if (selectedId != null && selected == null) selectedId = null
    BackHandler(enabled = selected != null) { selectedId = null }

    val reduced = LocalReducedMotion.current
    AnimatedContent(
        targetState = selected,
        transitionSpec = { fadeIn(motionSpring<Float>(reduced)) togetherWith fadeOut(motionSpring<Float>(reduced)) },
        label = "approval-detail",
        modifier = Modifier.fillMaxSize(),
    ) { current ->
        if (current == null) {
            ApprovalList(store) { selectedId = it.approvalId }
        } else {
            ApprovalDetail(store, current) { selectedId = null }
        }
    }
}

@Composable
private fun ApprovalList(store: JarvisStore, onSelect: (ApprovalReview) -> Unit) {
    Column(Modifier.fillMaxSize()) {
        ScreenHeader("Approvals", "Exact operations the Mac is waiting on. Nothing runs until you approve it once.")
        if (store.approvals.isEmpty() && store.approvalDecisions.isEmpty()) {
            EmptyNote(
                "Nothing waiting",
                "When the Mac needs your approval for an exact operation, it appears here until you decide or it expires.",
            )
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = JarvisSpace.Page, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                // Keep the current approval ahead of historical decisions so stale history cannot bury it.
                if (store.approvals.isNotEmpty()) item(key = "pending-approvals-heading") { SectionLabel("Waiting for you") }
                items(store.approvals, key = { it.approvalId }) { review ->
                    SurfaceCard(onClick = { onSelect(review) }) {
                        Column {
                            BasicText(review.operation, style = JarvisType.Section.copy(color = JarvisColor.Text), maxLines = 2)
                            Spacer(Modifier.height(4.dp))
                            BasicText(review.target, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted), maxLines = 2)
                            Spacer(Modifier.height(10.dp))
                            StatusPill(remainingLabel(review.expiresAtUnixMs, store.now), if (review.expiresAtUnixMs > store.now) TaskTone.WAITING else TaskTone.FAILED)
                        }
                    }
                }
                if (store.approvalDecisions.isNotEmpty()) {
                    item(key = "decision-history-heading") { SectionLabel("Decision status") }
                    items(store.approvalDecisions.values.sortedByDescending { it.updatedAtUnixMs }, key = { "decision-${it.approvalId}" }) { status ->
                        ApprovalDecisionCard(status, store)
                    }
                }
            }
        }
    }
}

@Composable
private fun ApprovalDecisionCard(status: ApprovalDecisionStatus, store: JarvisStore) {
    SurfaceCard {
        Column {
            BasicText(status.operation.ifBlank { "Approval decision" }, style = JarvisType.Section.copy(color = JarvisColor.Text), maxLines = 2)
            if (status.target.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                BasicText(status.target, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted), maxLines = 2)
            }
            Spacer(Modifier.height(8.dp))
            BasicText(ApprovalDecisionPresentation.label(status), style = JarvisType.Body.copy(color = JarvisColor.Text))
            Spacer(Modifier.height(4.dp))
            BasicText("Task ${status.taskId.take(8)}", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            if (status.phase !in setOf(
                    com.jarvis.companion.presentation.ApprovalDecisionPhase.COMPLETED,
                    com.jarvis.companion.presentation.ApprovalDecisionPhase.REJECTED,
                    com.jarvis.companion.presentation.ApprovalDecisionPhase.EXPIRED,
                    com.jarvis.companion.presentation.ApprovalDecisionPhase.CANCELLED,
                    com.jarvis.companion.presentation.ApprovalDecisionPhase.FAILED,
                )) {
                Spacer(Modifier.height(8.dp))
                TextAction("Check status", filled = false) { store.refreshApprovalDecisionStatus() }
            }
        }
    }
}

@Composable
private fun ApprovalDetail(store: JarvisStore, review: ApprovalReview, onBack: () -> Unit) {
    val expired = review.expiresAtUnixMs <= store.now
    val decisionStatus = store.approvalDecisionStatus(review.approvalId)
    val decisionBlocksTap = ApprovalDecisionPresentation.blocksAnotherTap(decisionStatus)
    Column(Modifier.fillMaxSize().padding(horizontal = JarvisSpace.Page)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconAction(JarvisIcons.Back, "Back to approvals", onClick = onBack)
            BasicText("Approval", style = JarvisType.Section.copy(color = JarvisColor.Text))
        }
        Spacer(Modifier.height(12.dp))
        Column(Modifier.fillMaxWidth().weight(1f, fill = true).verticalScroll(rememberScrollState())) {
            DetailField("Operation", review.operation)
            DetailField("Target", review.target)
            val plan = review.developmentPlan
            if (plan == null) {
                DetailField("Arguments (redacted by the Mac)", review.redactedSummary.ifBlank { "None reported" })
            } else {
                DetailField("Reason", review.redactedSummary.ifBlank { "Review the complete plan below." })
                DevelopmentPlanDetails(plan)
            }
            DetailField("Supervisor operation digest", review.canonicalArgumentsDigest)
            DetailField(
                "Expires",
                "${expiryClock(review.expiresAtUnixMs)} · ${remainingLabel(review.expiresAtUnixMs, store.now)}",
            )
            Spacer(Modifier.height(8.dp))
            BasicText(
                "Approving runs this exact operation once on the Mac. A changed or expired request is refused.",
                style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
            )
        }
        if (decisionStatus != null) {
            SurfaceCard {
                Column {
                    BasicText(ApprovalDecisionPresentation.label(decisionStatus), style = JarvisType.Section.copy(color = JarvisColor.Text))
                    Spacer(Modifier.height(8.dp))
                    TextAction("Check status", filled = false) { store.refreshApprovalDecisionStatus() }
                }
            }
            Spacer(Modifier.height(12.dp))
        }
        if (expired) {
            BasicText("This approval expired. The Mac will not run it.", style = JarvisType.Section.copy(color = JarvisColor.Failed))
            Spacer(Modifier.height(16.dp))
        } else if (decisionStatus?.phase == com.jarvis.companion.presentation.ApprovalDecisionPhase.DELIVERY_FAILED) {
            BasicText(
                "Only the same decision can be retried. The supervisor did not confirm this submission.",
                style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
            )
            Spacer(Modifier.height(10.dp))
            TextAction(
                if (decisionStatus.decision == "approve") "Retry approval delivery" else "Retry rejection delivery",
                filled = true,
                enabled = !store.decisionInFlight,
                modifier = Modifier.fillMaxWidth(),
            ) {
                store.decide(review, decisionStatus.decision)
            }
            Spacer(Modifier.height(16.dp))
        } else if (decisionBlocksTap) {
            BasicText("This approval decision has already been submitted. Wait for the Mac's authoritative result; do not send it again.", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            Spacer(Modifier.height(16.dp))
        } else {
            if (store.decisionInFlight) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Spinner(color = JarvisColor.Sage)
                    Spacer(Modifier.width(10.dp))
                    BasicText(store.decisionNote, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
                }
                Spacer(Modifier.height(12.dp))
            }
            TextAction("Approve once", filled = true, enabled = !store.decisionInFlight, modifier = Modifier.fillMaxWidth()) {
                store.decide(review, "approve")
            }
            Spacer(Modifier.height(10.dp))
            TextAction("Reject", filled = false, enabled = !store.decisionInFlight, modifier = Modifier.fillMaxWidth()) {
                store.decide(review, "reject")
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun DevelopmentPlanDetails(plan: DevelopmentPlanReview) {
    SectionLabel("Complete approved scope")
    Spacer(Modifier.height(4.dp))
    DetailField("Plan ID", plan.planId)
    DetailField("Workspace", plan.workspaceRoot)
    DetailField("Paired device", plan.deviceId)
    DetailField("User binding", plan.userId)
    DetailField("Permitted operations", plan.allowedOps.joinToString("\n"))
    DetailField("Permitted paths", if (plan.allowedPaths.isEmpty() || "" in plan.allowedPaths) "Entire workspace" else plan.allowedPaths.joinToString("\n"))
    DetailField("Maximum file size", "${plan.maxFileBytes} bytes")
    DetailField("Maximum total write bytes", "${plan.maxTotalBytes} bytes")
    DetailField("Write count", plan.maxWrites.toString())
    DetailField("Test runs", plan.maxTestRuns.toString())
    DetailField("Repair attempts", plan.maxRepairs.toString())
    DetailField("Maximum task duration", "${plan.maxDurationSeconds} seconds")
    DetailField("Capability lifetime", "${plan.ttlSeconds} seconds")
    DetailField("Plan digest", plan.planDigest)
    BasicText(
        "Approval authorizes only these operations and paths within these limits. Codex shell and independent sandbox approvals remain separate.",
        style = JarvisType.Caption.copy(color = JarvisColor.TextMuted),
    )
    Spacer(Modifier.height(16.dp))
}

@Composable
private fun DetailField(label: String, value: String) {
    Column(Modifier.fillMaxWidth().padding(bottom = 16.dp)) {
        SectionLabel(label)
        Spacer(Modifier.height(4.dp))
        SelectionContainer {
            BasicText(value, style = JarvisType.Body.copy(color = JarvisColor.Text))
        }
    }
}
