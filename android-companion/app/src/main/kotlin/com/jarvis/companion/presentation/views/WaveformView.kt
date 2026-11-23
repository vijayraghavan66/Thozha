package com.jarvis.companion.presentation.views

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.provider.Settings
import android.util.AttributeSet
import android.view.View
import com.jarvis.companion.R
import com.jarvis.companion.presentation.MotionPolicy
import androidx.core.content.ContextCompat

/**
 * Voice-level waveform. Presentation only: it draws the levels it is given and never reads a microphone.
 * Bars ease toward new levels; with reduced motion the bars jump directly to each new level.
 */
class WaveformView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {
    private val barCount = 28
    private val levels = FloatArray(barCount)
    private val targets = FloatArray(barCount)
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = ContextCompat.getColor(context, R.color.accent) }
    private val rect = RectF()
    private val density = resources.displayMetrics.density
    private var animator: ValueAnimator? = null

    init {
        contentDescription = context.getString(R.string.jarvis_waveform_description)
    }

    /** Sets the target level for each bar, 0..1. Extra values are ignored; missing values fall to 0. */
    fun setLevels(values: FloatArray) {
        for (i in 0 until barCount) targets[i] = values.getOrElse(i) { 0f }.coerceIn(0f, 1f)
        if (reducedMotion()) {
            targets.copyInto(levels)
            invalidate()
            return
        }
        animator?.cancel()
        val from = levels.copyOf()
        animator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = MotionPolicy.durationMs(120L, false)
            addUpdateListener { anim ->
                val t = anim.animatedFraction
                for (i in 0 until barCount) levels[i] = from[i] + (targets[i] - from[i]) * t
                invalidate()
            }
            start()
        }
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val gap = 3 * density
        val barWidth = (width - gap * (barCount - 1)) / barCount
        val minHeight = 4 * density
        val centre = height / 2f
        for (i in 0 until barCount) {
            val h = minHeight + (height - minHeight) * levels[i]
            val left = i * (barWidth + gap)
            rect.set(left, centre - h / 2f, left + barWidth, centre + h / 2f)
            canvas.drawRoundRect(rect, barWidth / 2f, barWidth / 2f, paint)
        }
    }

    override fun onDetachedFromWindow() {
        animator?.cancel()
        super.onDetachedFromWindow()
    }

    private fun reducedMotion(): Boolean {
        val scale = Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f)
        return MotionPolicy.reduced(scale, userRequestedReduced = false)
    }
}
