package com.jarvis.companion.ui

import android.provider.Settings
import androidx.activity.compose.BackHandler
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.EnterTransition
import androidx.compose.animation.ExitTransition
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.slideOutVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.isImeVisible
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.rememberVectorPainter
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.selected
import androidx.compose.foundation.layout.size
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

/** Root of the Jarvis 3.0 interface: four icon-only tabs, spring transitions, and full-screen overlays. */
@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
fun JarvisApp(store: JarvisStore) {
    val context = LocalContext.current
    val systemScale = remember { Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) }
    val reduced = reducedMotion(systemScale, store.reduceMotion)
    val imeVisible = WindowInsets.isImeVisible

    // Telegram carries each sync as a user message and a bot response. Five-second polling caused
    // TDLib flood waits when send and sync share the same bot chat. Each sync is a Telegram message
    // and each response is another; keep active work at 10s with the per-chat send gate below.
    LaunchedEffect(Unit) {
        store.refreshLocal()
        while (true) {
            store.syncNow()
            delay(if (store.hasActiveWork()) 10_000L else 30_000L)
        }
    }
    LaunchedEffect(Unit) {
        while (true) {
            store.tick()
            delay(15_000L)
        }
    }
    // Local-only refresh while work is open, so replies appear as soon as they are stored, not only after a sync.
    LaunchedEffect(Unit) {
        while (true) {
            delay(3_000L)
            if (store.hasActiveWork() || store.submitting) store.refreshLocal()
        }
    }

    // Opening an overlay or changing tab must not leave the composer focused behind it with the keyboard up.
    val focusManager = LocalFocusManager.current
    val keyboard = LocalSoftwareKeyboardController.current
    LaunchedEffect(store.overlay, store.tab) {
        if (store.overlay != null || store.tab != Tab.HOME) {
            focusManager.clearFocus(force = true)
            keyboard?.hide()
        }
    }

    BackHandler(enabled = store.overlay != null || store.tab != Tab.HOME) {
        if (store.overlay != null) store.openOverlay(null) else store.selectTab(Tab.HOME)
    }

    CompositionLocalProvider(LocalReducedMotion provides reduced) {
        Box(Modifier.fillMaxSize().background(JarvisColor.Background)) {
            Column(Modifier.fillMaxSize().statusBarsPadding()) {
                Box(
                    Modifier
                        .weight(1f)
                        .widthIn(max = 720.dp)
                        .fillMaxWidth()
                        .align(Alignment.CenterHorizontally),
                ) {
                    AnimatedContent(
                        targetState = store.tab,
                        transitionSpec = {
                            if (reduced) {
                                EnterTransition.None togetherWith ExitTransition.None
                            } else {
                                val direction = if (targetState.ordinal > initialState.ordinal) 1 else -1
                                (fadeIn(motionSpring<Float>(false)) +
                                    slideInHorizontally(motionSpring<IntOffset>(false)) { full -> full / 10 * direction }) togetherWith
                                    (fadeOut(motionSpring<Float>(false)) +
                                        slideOutHorizontally(motionSpring<IntOffset>(false)) { full -> -full / 10 * direction })
                            }
                        },
                        label = "tab-content",
                        modifier = Modifier.fillMaxSize(),
                    ) { tab ->
                        when (tab) {
                            Tab.HOME -> HomeScreen(
                                store,
                                onOpenSettings = { store.openOverlay(Overlay.SETTINGS) },
                                onOpenConnection = { store.selectTab(Tab.LIVE) },
                            )
                            Tab.ACTIVITY -> ActivityScreen(store)
                            Tab.APPROVALS -> ApprovalsScreen(store)
                            Tab.LIVE -> LiveScreen(store)
                        }
                    }
                }
                AnimatedVisibility(
                    visible = !imeVisible,
                    enter = fadeIn(motionSpring<Float>(reduced)),
                    exit = fadeOut(motionSpring<Float>(reduced)),
                ) {
                    BottomTabs(store)
                }
            }

            OverlayHost(store, reduced)
        }
    }
}

@Composable
private fun OverlayHost(store: JarvisStore, reduced: Boolean) {
    // Keep the last overlay while it animates out, so its content does not disappear mid-transition.
    var shown by remember { mutableStateOf(Overlay.SETTINGS) }
    store.overlay?.let { shown = it }
    AnimatedVisibility(
        visible = store.overlay != null,
        enter = fadeIn(motionSpring<Float>(reduced)) + slideInVertically(motionSpring<IntOffset>(reduced)) { full -> full / 8 },
        exit = fadeOut(motionSpring<Float>(reduced)) + slideOutVertically(motionSpring<IntOffset>(reduced)) { full -> full / 8 },
    ) {
        Box(Modifier.fillMaxSize().background(JarvisColor.Background)) {
            when (shown) {
                Overlay.SETTINGS -> SettingsScreen(store, onClose = { store.openOverlay(null) })
                Overlay.HISTORY -> HistoryScreen(store, onClose = { store.openOverlay(null) })
            }
        }
    }
}

@Composable
private fun BottomTabs(store: JarvisStore) {
    val haptics = LocalHapticFeedback.current
    val tabs = Tab.entries
    BoxWithConstraints(
        Modifier
            .fillMaxWidth()
            .background(JarvisColor.Surface)
            .navigationBarsPadding()
            .padding(horizontal = 12.dp, vertical = 8.dp),
    ) {
        val itemWidth = maxWidth / tabs.size
        val position by animateFloatAsState(targetValue = store.tab.ordinal.toFloat(), animationSpec = jarvisSpring())
        Box(
            Modifier
                .offset(x = itemWidth * position)
                .width(itemWidth)
                .height(44.dp)
                .clip(RoundedCornerShape(22.dp))
                .background(JarvisColor.SageTint),
        )
        Row(Modifier.fillMaxWidth()) {
            tabs.forEach { tab ->
                val selected = tab == store.tab
                Box(
                    Modifier
                        .weight(1f)
                        .height(56.dp)
                        .semantics {
                            contentDescription = tab.label
                            this.selected = selected
                            role = Role.Tab
                        }
                        .clickable(role = Role.Tab) {
                            if (!selected) haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            store.selectTab(tab)
                        },
                    contentAlignment = Alignment.Center,
                ) {
                    Image(
                        painter = rememberVectorPainter(iconFor(tab)),
                        contentDescription = null,
                        colorFilter = ColorFilter.tint(if (selected) JarvisColor.Sage else JarvisColor.TextMuted),
                        modifier = Modifier.size(24.dp),
                    )
                }
            }
        }
    }
}

private fun iconFor(tab: Tab): ImageVector = when (tab) {
    Tab.HOME -> JarvisIcons.Home
    Tab.ACTIVITY -> JarvisIcons.Activity
    Tab.APPROVALS -> JarvisIcons.Approvals
    Tab.LIVE -> JarvisIcons.Live
}
