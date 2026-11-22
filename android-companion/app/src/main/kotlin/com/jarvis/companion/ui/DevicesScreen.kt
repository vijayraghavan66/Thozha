package com.jarvis.companion.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun DevicesScreen(store: JarvisStore, onOpenSettings: () -> Unit) {
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(start = JarvisSpace.Page, end = 8.dp, top = 14.dp, bottom = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                BasicText("Devices", style = JarvisType.Title.copy(color = JarvisColor.Text))
                BasicText("Your paired computers and connection options", style = JarvisType.Caption.copy(color = JarvisColor.TextMuted))
            }
            IconAction(JarvisIcons.Settings, "Settings", onClick = onOpenSettings)
        }
        Box(Modifier.weight(1f)) {
            LiveScreen(store, showHeader = false, showPhoneDetails = false, showVoice = true)
        }
    }
}
