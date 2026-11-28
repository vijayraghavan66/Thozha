package com.jarvis.companion.ui

import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.painter.Painter
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.rememberVectorPainter
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.text.BasicText
import com.jarvis.companion.presentation.TaskTone
import androidx.compose.ui.graphics.Color
import androidx.compose.runtime.withFrameNanos

/** Icon-only control with a 48 dp minimum target, a spoken label, and a light haptic tick. */
@Composable
fun IconAction(
    icon: ImageVector,
    label: String,
    tint: Color = JarvisColor.Text,
    size: androidx.compose.ui.unit.Dp = 48.dp,
    enabled: Boolean = true,
    onClick: () -> Unit,
) {
    val haptics = LocalHapticFeedback.current
    Box(
        modifier = Modifier
            .size(size)
            .clip(CircleShape)
            .semantics { contentDescription = label }
            .clickable(enabled = enabled, role = Role.Button) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                onClick()
            },
        contentAlignment = Alignment.Center,
    ) {
        Image(
            painter = rememberVectorPainter(icon),
            contentDescription = null,
            colorFilter = ColorFilter.tint(if (enabled) tint else JarvisColor.TextMuted.copy(alpha = 0.4f)),
            modifier = Modifier.size(22.dp),
        )
    }
}

/** A surface card. Surfaces carry the hierarchy; there are no shadows or ornamental borders. */
@Composable
fun SurfaceCard(
    modifier: Modifier = Modifier,
    raised: Boolean = false,
    onClick: (() -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    val haptics = LocalHapticFeedback.current
    val base = modifier
        .fillMaxWidth()
        .clip(RoundedCornerShape(JarvisSpace.Radius))
        .background(if (raised) JarvisColor.SurfaceRaised else JarvisColor.Surface)
        .let { if (onClick != null) it.clickable(role = Role.Button) { haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove); onClick() } else it }
        .padding(JarvisSpace.Card)
    Box(base) { content() }
}

/** A single, restrained hero surface for the current task or Mac connection state. */
@Composable
fun HeroSurfaceCard(
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit,
) {
    val shape = RoundedCornerShape(24.dp)
    Box(
        modifier
            .fillMaxWidth()
            .animateContentSize(jarvisSpring())
            .clip(shape)
            .background(JarvisColor.SurfaceRaised)
            .border(1.dp, JarvisColor.Border.copy(alpha = 0.72f), shape)
            .padding(20.dp),
    ) {
        content()
    }
}

/** Quiet section heading; sentence case is easier to scan than repeated dashboard-style capitals. */
@Composable
fun SectionLabel(text: String, modifier: Modifier = Modifier) {
    BasicText(
        text = text,
        modifier = modifier.semantics { contentDescription = text },
        style = JarvisType.Label.copy(color = JarvisColor.TextMuted, letterSpacing = 0.15.sp),
    )
}

/** The visible status for a task or connection. Always shows the words, so colour is never the only signal. */
@Composable
fun StatusPill(label: String, tone: TaskTone) {
    val color = toneColor(tone)
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(50))
            .background(color.copy(alpha = 0.14f))
            .padding(horizontal = 10.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(6.dp).clip(CircleShape).background(color))
        Spacer(Modifier.width(7.dp))
        BasicText(label, style = JarvisType.Caption.copy(color = color, fontWeight = androidx.compose.ui.text.font.FontWeight.Medium))
    }
}

fun toneColor(tone: TaskTone): Color = when (tone) {
    TaskTone.COMPLETED -> JarvisColor.Sage
    TaskTone.WAITING -> JarvisColor.Waiting
    TaskTone.FAILED -> JarvisColor.Failed
    TaskTone.CANCELLED -> JarvisColor.TextMuted
    TaskTone.REJECTED -> JarvisColor.Waiting
    TaskTone.RUNNING -> JarvisColor.Running
    TaskTone.UNKNOWN -> JarvisColor.TextMuted
}

/**
 * Three-stroke presence mark. It moves only while a real task is running; waiting, done, failed and idle
 * states are static and distinct. Frames are requested only while [animating] is true.
 */
@Composable
fun PresenceSignal(animating: Boolean, tint: Color = JarvisColor.Sage, modifier: Modifier = Modifier) {
    var phase by remember { mutableFloatStateOf(0f) }
    val reduced = LocalReducedMotion.current
    LaunchedEffect(animating, reduced) {
        if (!animating || reduced) {
            phase = 0f
            return@LaunchedEffect
        }
        val start = withFrameNanos { it }
        // Cancelled with the effect when the signal stops animating or leaves composition.
        while (true) {
            withFrameNanos { frame -> phase = ((frame - start) / 1_000_000_000f) % 1f }
        }
    }
    Canvas(modifier.size(22.dp)) {
        val strokeWidth = 2.2.dp.toPx()
        val heights = if (animating && !reduced) {
            val wave = { offset: Float -> 0.55f + 0.45f * kotlin.math.sin((phase + offset) * 2f * Math.PI.toFloat()) }
            floatArrayOf(wave(0f), wave(0.33f), wave(0.66f))
        } else floatArrayOf(0.6f, 1f, 0.6f)
        val xs = floatArrayOf(size.width * 0.22f, size.width * 0.5f, size.width * 0.78f)
        for (index in 0 until 3) {
            val half = size.height * 0.42f * heights[index]
            drawLine(
                color = tint,
                start = Offset(xs[index], size.height / 2f - half),
                end = Offset(xs[index], size.height / 2f + half),
                strokeWidth = strokeWidth,
                cap = StrokeCap.Round,
            )
        }
    }
}

/** Small rotating arc for an operation with unknown duration. No percentage is ever shown. */
@Composable
fun Spinner(color: Color = JarvisColor.Text, size: androidx.compose.ui.unit.Dp = 18.dp) {
    val transition = rememberInfiniteTransition(label = "spinner")
    val angle by transition.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(900, easing = LinearEasing)),
        label = "spinner-angle",
    )
    Canvas(Modifier.size(size)) {
        val stroke = 2.dp.toPx()
        drawArc(
            color = color,
            startAngle = angle,
            sweepAngle = 270f,
            useCenter = false,
            style = Stroke(width = stroke, cap = StrokeCap.Round),
            size = androidx.compose.ui.geometry.Size(this.size.width - stroke, this.size.height - stroke),
            topLeft = Offset(stroke / 2f, stroke / 2f),
        )
    }
}

/** A labelled text button with a full-width 48 dp target. Filled uses the sage accent; outline is for reject. */
@Composable
fun TextAction(label: String, filled: Boolean, enabled: Boolean = true, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val haptics = LocalHapticFeedback.current
    val shape = RoundedCornerShape(14.dp)
    Box(
        modifier = modifier
            .height(52.dp)
            .clip(shape)
            .background(if (filled) JarvisColor.Sage else Color.Transparent)
            .then(if (filled) Modifier else Modifier.border(1.dp, JarvisColor.Border, shape))
            .clickable(enabled = enabled, role = Role.Button) {
                haptics.performHapticFeedback(if (filled) HapticFeedbackType.Confirm else HapticFeedbackType.TextHandleMove)
                onClick()
            },
        contentAlignment = Alignment.Center,
    ) {
        BasicText(
            text = label,
            style = JarvisType.Section.copy(
                color = when {
                    !enabled -> JarvisColor.TextMuted
                    filled -> JarvisColor.Background
                    else -> JarvisColor.Text
                },
                textAlign = TextAlign.Center,
            ),
        )
    }
}

/** Empty state: a calm line that says what will appear here and why it is empty. */
@Composable
fun EmptyNote(title: String, body: String) {
    Column(Modifier.fillMaxWidth().padding(horizontal = JarvisSpace.Page, vertical = 48.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        BasicText(title, style = JarvisType.Section.copy(color = JarvisColor.Text, textAlign = TextAlign.Center))
        Spacer(Modifier.height(8.dp))
        BasicText(body, style = JarvisType.Caption.copy(color = JarvisColor.TextMuted, textAlign = TextAlign.Center, fontSize = 14.sp))
    }
}
