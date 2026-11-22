package com.jarvis.companion.ui

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.foundation.text.BasicText
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

private enum class TaskPanel(val label: String) { ACTIVITY("Activity"), APPROVALS("Approvals") }

@Composable
fun TasksScreen(store: JarvisStore) {
    var panel by remember { mutableStateOf(TaskPanel.ACTIVITY) }
    val reduced = LocalReducedMotion.current
    val actionableApprovals = store.approvals.count { it.expiresAtUnixMs > store.now }
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = JarvisSpace.Page, vertical = 14.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                BasicText("Tasks", style = JarvisType.Title.copy(color = JarvisColor.Text))
                BasicText("Progress and decisions from your Mac", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            }
            if (actionableApprovals > 0) StatusPill("$actionableApprovals waiting", com.jarvis.companion.presentation.TaskTone.WAITING)
        }
        Row(
            Modifier.fillMaxWidth().padding(horizontal = JarvisSpace.Page, vertical = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            TaskPanel.entries.forEach { item ->
                val isSelected = panel == item
                TextAction(
                    label = if (item == TaskPanel.APPROVALS && actionableApprovals > 0) {
                        "${item.label} · $actionableApprovals"
                    } else item.label,
                    filled = isSelected,
                    modifier = Modifier.weight(1f).semantics { selected = isSelected },
                ) { panel = item }
            }
        }
        AnimatedContent(
            targetState = panel,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            transitionSpec = { fadeIn(motionSpring<Float>(reduced)) togetherWith fadeOut(motionSpring<Float>(reduced)) },
            label = "task-panel",
        ) { selected ->
            when (selected) {
                TaskPanel.ACTIVITY -> ActivityScreen(store, showHeader = false)
                TaskPanel.APPROVALS -> ApprovalsScreen(store, showHeader = false)
            }
        }
    }
}
