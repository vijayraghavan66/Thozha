package com.jarvis.companion.ui

import android.provider.Settings
import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import com.jarvis.companion.link.LinkSettings
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
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
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
import androidx.core.view.WindowCompat
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.foundation.layout.size
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

internal enum class ThozhaDestination(val label: String) {
    HOME("Home"), CHAT("Chat"), TASKS("Tasks"), DEVICES("Devices"),
}

/** Root of Thozha: four labeled destinations, spring transitions, and focused secondary overlays. */
@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
fun JarvisApp(store: JarvisStore) {
    val context = LocalContext.current
    val systemScale = remember { Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) }
    val reduced = reducedMotion(systemScale, store.reduceMotion)
    var lightTheme by remember(context) { mutableStateOf(AppearancePreference.isLight(context)) }
    JarvisColor.useLightMode(lightTheme)
    var destination by remember { mutableStateOf(ThozhaDestination.HOME) }
    var callActive by remember { mutableStateOf(false) }
    val imeVisible = WindowInsets.isImeVisible
    SideEffect {
        context.findActivity()?.window?.let { window ->
            WindowCompat.getInsetsController(window, window.decorView).apply {
                isAppearanceLightStatusBars = lightTheme
                isAppearanceLightNavigationBars = lightTheme
            }
        }
    }

    // Telegram carries each sync as a user message and a bot response. Five-second polling caused
    // TDLib flood waits when send and sync share the same bot chat. Each sync is a Telegram message
    // and each response is another; keep active work at 10s with the per-chat send gate below.
    LaunchedEffect(Unit) {
        store.refreshLocal()
        while (true) {
            if (store.shouldRunLinkSync()) store.syncNow() else store.refreshLocal()
            val telegramTunnelActive = com.jarvis.companion.telegram.TelegramSession.activeTunnel() != null
            val telegramCooldown = if (LinkSettings(context).mode == com.jarvis.companion.link.LinkMode.TEST_RELAY &&
                telegramTunnelActive
            ) com.jarvis.companion.telegram.TelegramSession.retryRemainingMs() else 0L
            delay(LinkSyncPolicy.intervalMs(store.hasActiveWork(), telegramCooldown, telegramTunnelActive))
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

    // Leaving Chat must not leave its composer focused behind another destination or overlay.
    val focusManager = LocalFocusManager.current
    val keyboard = LocalSoftwareKeyboardController.current
    LaunchedEffect(store.overlay, destination) {
        if (store.overlay != null || destination != ThozhaDestination.CHAT) {
            focusManager.clearFocus(force = true)
            keyboard?.hide()
        }
    }

    BackHandler(enabled = callActive || store.overlay != null || destination != ThozhaDestination.HOME) {
        if (callActive) callActive = false else if (store.overlay != null) store.openOverlay(null) else destination = ThozhaDestination.HOME
    }

    CompositionLocalProvider(LocalReducedMotion provides reduced) {
        Box(Modifier.fillMaxSize().background(JarvisColor.Background)) {
            if (callActive) {
                JarvisCallScreen(store) { callActive = false }
            } else Column(Modifier.fillMaxSize().statusBarsPadding()) {
                Box(
                    Modifier
                        .weight(1f)
                        .widthIn(max = 720.dp)
                        .fillMaxWidth()
                        .align(Alignment.CenterHorizontally),
                ) {
                    AnimatedContent(
                        targetState = destination,
                        transitionSpec = {
                            if (reduced) {
                                EnterTransition.None togetherWith ExitTransition.None
                            } else {
                                val direction = if (targetState.ordinal > initialState.ordinal) 1 else -1
                                (fadeIn(motionSpring<Float>(reduced)) +
                                    slideInHorizontally(motionSpring<IntOffset>(reduced)) { full -> full / 10 * direction }) togetherWith
                                    (fadeOut(motionSpring<Float>(reduced)) +
                                        slideOutHorizontally(motionSpring<IntOffset>(reduced)) { full -> -full / 10 * direction })
                            }
                        },
                        label = "tab-content",
                        modifier = Modifier.fillMaxSize(),
                    ) { target ->
                        when (target) {
                            ThozhaDestination.HOME -> HomeOverviewScreen(
                                store,
                                onOpenChat = { destination = ThozhaDestination.CHAT },
                                onOpenTasks = { destination = ThozhaDestination.TASKS },
                                onOpenDevices = { destination = ThozhaDestination.DEVICES },
                                onOpenSettings = { store.openOverlay(Overlay.SETTINGS) },
                                onCallJarvis = { callActive = true },
                            )
                            ThozhaDestination.CHAT -> HomeScreen(
                                store,
                                onOpenSettings = { store.openOverlay(Overlay.SETTINGS) },
                                onOpenConnection = { destination = ThozhaDestination.DEVICES },
                            )
                            ThozhaDestination.TASKS -> TasksScreen(store)
                            ThozhaDestination.DEVICES -> DevicesScreen(
                                store,
                                onOpenSettings = { store.openOverlay(Overlay.SETTINGS) },
                            )
                        }
                    }
                }
                AnimatedVisibility(
                    visible = !imeVisible,
                    enter = fadeIn(motionSpring<Float>(reduced)),
                    exit = fadeOut(motionSpring<Float>(reduced)),
                ) {
                    BottomTabs(selected = destination, onSelect = { destination = it })
                }
            }

            OverlayHost(
                store = store,
                reduced = reduced,
                lightTheme = lightTheme,
                onLightThemeChange = { enabled ->
                    lightTheme = enabled
                    AppearancePreference.setLight(context, enabled)
                    JarvisColor.useLightMode(enabled)
                },
            )
        }
    }
}

@Composable
private fun OverlayHost(
    store: JarvisStore,
    reduced: Boolean,
    lightTheme: Boolean,
    onLightThemeChange: (Boolean) -> Unit,
) {
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
                Overlay.SETTINGS -> SettingsScreen(
                    store,
                    onClose = { store.openOverlay(null) },
                    lightTheme = lightTheme,
                    onLightThemeChange = onLightThemeChange,
                )
                Overlay.HISTORY -> HistoryScreen(store, onClose = { store.openOverlay(null) })
            }
        }
    }
}

@Composable
private fun BottomTabs(selected: ThozhaDestination, onSelect: (ThozhaDestination) -> Unit) {
    val haptics = LocalHapticFeedback.current
    val tabs = ThozhaDestination.entries
    BoxWithConstraints(
        Modifier
            .fillMaxWidth()
            .background(JarvisColor.Surface)
            .navigationBarsPadding()
            .padding(horizontal = 12.dp, vertical = 8.dp),
    ) {
        val itemWidth = maxWidth / tabs.size
        val position by animateFloatAsState(targetValue = selected.ordinal.toFloat(), animationSpec = jarvisSpring())
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
                val isSelected = tab == selected
                Box(
                    Modifier
                        .weight(1f)
                        .height(60.dp)
                        .semantics {
                            contentDescription = tab.label
                            this.selected = isSelected
                            role = Role.Tab
                        }
                        .clickable(role = Role.Tab) {
                            if (!isSelected) haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            onSelect(tab)
                        },
                    contentAlignment = Alignment.Center,
                ) {
                    Column(
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(2.dp),
                    ) {
                        Image(
                            painter = rememberVectorPainter(iconFor(tab)),
                            contentDescription = null,
                            colorFilter = ColorFilter.tint(if (isSelected) JarvisColor.Sage else JarvisColor.TextMuted),
                            modifier = Modifier.size(21.dp),
                        )
                        BasicText(
                            tab.label,
                            style = JarvisType.Caption.copy(
                                color = if (isSelected) JarvisColor.Sage else JarvisColor.TextMuted,
                                fontSize = 11.sp,
                                fontWeight = if (isSelected) FontWeight.SemiBold else FontWeight.Normal,
                                textAlign = TextAlign.Center,
                            ),
                            maxLines = 1,
                        )
                    }
                }
            }
        }
    }
}

private fun iconFor(tab: ThozhaDestination): ImageVector = when (tab) {
    ThozhaDestination.HOME -> JarvisIcons.Home
    ThozhaDestination.CHAT -> JarvisIcons.Chat
    ThozhaDestination.TASKS -> JarvisIcons.Tasks
    ThozhaDestination.DEVICES -> JarvisIcons.Devices
}

private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}
