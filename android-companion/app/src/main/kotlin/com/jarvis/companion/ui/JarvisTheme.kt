package com.jarvis.companion.ui

import androidx.compose.animation.core.FiniteAnimationSpec
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.spring
import androidx.compose.runtime.Composable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** Jarvis 3.0 palette: near-black and charcoal surfaces with restrained sage accents. */
object JarvisColor {
    val Background = Color(0xFF0A0C0B)
    val Surface = Color(0xFF121614)
    val SurfaceRaised = Color(0xFF1A201D)
    val Border = Color(0xFF263029)
    val Text = Color(0xFFEBEFEA)
    val TextMuted = Color(0xFF98A49E)
    val Sage = Color(0xFF9CB99A)
    val SageMuted = Color(0xFF5E7459)
    val SageTint = Color(0xFF1E2A21)
    val Waiting = Color(0xFFD6B77C)
    val Failed = Color(0xFFD28A80)
    val Running = Color(0xFFA7BCC2)
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

/** Spacing: page 20 dp, card 16 dp, touch target 48 dp minimum (nav items 56 dp). */
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
