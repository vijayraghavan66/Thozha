package com.jarvis.companion.ui

import androidx.compose.animation.core.FiniteAnimationSpec
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.spring
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** Immutable ARGB tokens keep palette/contrast checks independent of Android or Compose runtime. */
data class ThozhaPalette(
    val background: Long,
    val surface: Long,
    val surfaceRaised: Long,
    val border: Long,
    val text: Long,
    val textMuted: Long,
    val sage: Long,
    val sageMuted: Long,
    val sageTint: Long,
    val waiting: Long,
    val failed: Long,
    val running: Long,
) {
    fun contrast(foreground: Long, background: Long): Double {
        val a = luminance(foreground)
        val b = luminance(background)
        return (maxOf(a, b) + 0.05) / (minOf(a, b) + 0.05)
    }

    private fun luminance(argb: Long): Double {
        fun channel(shift: Int): Double {
            val value = ((argb shr shift) and 0xff).toDouble() / 255.0
            return if (value <= 0.04045) value / 12.92 else Math.pow((value + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(16) + 0.7152 * channel(8) + 0.0722 * channel(0)
    }

    companion object {
        val Dark = ThozhaPalette(
            0xFF0A0C0B, 0xFF121614, 0xFF1A201D, 0xFF263029, 0xFFEBEFEA, 0xFF98A49E,
            0xFF9CB99A, 0xFF5E7459, 0xFF1E2A21, 0xFFD6B77C, 0xFFD28A80, 0xFFA7BCC2,
        )
        val Light = ThozhaPalette(
            0xFFF3F2EC, 0xFFFBFAF6, 0xFFE9EEE8, 0xFFD5DDD5, 0xFF18211B, 0xFF5B685F,
            0xFF31583B, 0xFF49664F, 0xFFDDE9DD, 0xFF83540F, 0xFF9B342A, 0xFF315A65,
        )
    }
}

/** Thozha's ink-and-sage dark palette and warm paper-and-forest light palette. */
object JarvisColor {
    private var lightMode = mutableStateOf(false)
    private val current: ThozhaPalette get() = if (lightMode.value) ThozhaPalette.Light else ThozhaPalette.Dark
    fun useLightMode(enabled: Boolean) { if (lightMode.value != enabled) lightMode.value = enabled }

    val Background: Color get() = Color(current.background)
    val Surface: Color get() = Color(current.surface)
    val SurfaceRaised: Color get() = Color(current.surfaceRaised)
    val Border: Color get() = Color(current.border)
    val Text: Color get() = Color(current.text)
    val TextMuted: Color get() = Color(current.textMuted)
    val Sage: Color get() = Color(current.sage)
    val SageMuted: Color get() = Color(current.sageMuted)
    val SageTint: Color get() = Color(current.sageTint)
    val Waiting: Color get() = Color(current.waiting)
    val Failed: Color get() = Color(current.failed)
    val Running: Color get() = Color(current.running)
}

/** Type scale. System sans-serif (Roboto on the supported device) keeps font scaling standard. */
object JarvisType {
    val Title = TextStyle(fontSize = 28.sp, lineHeight = 34.sp, fontWeight = FontWeight.Medium, letterSpacing = (-0.4).sp)
    val Section = TextStyle(fontSize = 15.sp, lineHeight = 20.sp, fontWeight = FontWeight.Medium)
    val Request = TextStyle(fontSize = 17.sp, lineHeight = 24.sp, fontWeight = FontWeight.Normal)
    val Body = TextStyle(fontSize = 16.sp, lineHeight = 25.sp, fontWeight = FontWeight.Normal)
    val Caption = TextStyle(fontSize = 13.sp, lineHeight = 18.sp, fontWeight = FontWeight.Normal)
    val Label = TextStyle(fontSize = 12.sp, lineHeight = 16.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 0.8.sp)
}

/** Spacing: page 20 dp, card 16 dp, touch target 48 dp minimum (main destinations 60 dp). */
object JarvisSpace {
    val Page: Dp = 20.dp
    val Card: Dp = 16.dp
    val Radius: Dp = 18.dp
    val Touch: Dp = 48.dp
}

/** True when the user or the system asks for reduced motion. Derived in JarvisApp from the animator scale. */
val LocalReducedMotion = staticCompositionLocalOf { false }

/** Spring used for every transition, so motion feels physical rather than timed. Snaps when motion is reduced. */
fun <T> motionSpring(reduced: Boolean): FiniteAnimationSpec<T> =
    if (reduced) snap() else spring(dampingRatio = 0.86f, stiffness = Spring.StiffnessMediumLow)

@Composable
fun <T> jarvisSpring(): FiniteAnimationSpec<T> = motionSpring(LocalReducedMotion.current)

/** Reduced motion applies when the system animator scale is zero or the user asked for it in Settings. */
fun reducedMotion(systemAnimatorScale: Float, userRequested: Boolean): Boolean =
    userRequested || systemAnimatorScale == 0f
