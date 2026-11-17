package com.jarvis.companion

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.view.View
import android.view.animation.LinearInterpolator
import kotlin.math.PI
import kotlin.math.sin

/** A compact three-stroke signal that mirrors the most recent real task state. */
internal class JarvisPresenceView(
    context: Context,
    private val reduceMotion: Boolean,
) : View(context) {
    enum class State { IDLE, LISTENING, THINKING, EXECUTING, WAITING_APPROVAL, PAUSED, RESPONDING, COMPLETE, FAILED }

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { strokeCap = Paint.Cap.ROUND }
    private var phase = 0f
    private var animator: ValueAnimator? = null
    var state: State = State.IDLE
        set(value) {
            if (field == value) return
            field = value
            contentDescription = when (value) {
                State.IDLE -> "Jarvis is ready"
                State.LISTENING -> "Jarvis is listening"
                State.THINKING -> "Jarvis is thinking"
                State.EXECUTING -> "Jarvis is working"
                State.WAITING_APPROVAL -> "Jarvis is waiting for your approval"
                State.PAUSED -> "The Jarvis task is paused"
                State.RESPONDING -> "Jarvis is checking the result"
                State.COMPLETE -> "Jarvis finished the last task"
                State.FAILED -> "The last Jarvis task failed"
            }
            updateAnimation()
            invalidate()
        }

    init {
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_YES
        contentDescription = "Jarvis is ready"
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        updateAnimation()
    }

    override fun onDetachedFromWindow() {
        animator?.cancel()
        animator = null
        super.onDetachedFromWindow()
    }

    private fun updateAnimation() {
        val animated = !reduceMotion && state in setOf(State.LISTENING, State.THINKING, State.EXECUTING, State.RESPONDING)
        if (!animated) {
            animator?.cancel()
            animator = null
            phase = 0f
            return
        }
        if (animator != null || !isAttachedToWindow) return
        animator = ValueAnimator.ofFloat(0f, (2 * PI).toFloat()).apply {
            duration = if (state == State.LISTENING) 1100L else 1500L
            repeatCount = ValueAnimator.INFINITE
            interpolator = LinearInterpolator()
            addUpdateListener {
                phase = it.animatedValue as Float
                invalidate()
            }
            start()
        }
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val tint = when (state) {
            State.WAITING_APPROVAL, State.PAUSED -> R.color.warn
            State.COMPLETE -> R.color.ok
            State.FAILED -> R.color.alert
            else -> R.color.accent
        }
        paint.color = context.getColor(tint)
        paint.strokeWidth = dp(3.5f)
        val centerX = width / 2f
        val centerY = height / 2f
        val spacing = dp(8f)
        val minHeight = dp(7f)
        val maxHeight = dp(23f)
        for (index in 0..2) {
            val staticHeight = when (state) {
                State.IDLE -> if (index == 1) dp(16f) else dp(8f)
                State.WAITING_APPROVAL -> if (index == 1) dp(22f) else dp(10f)
                State.PAUSED -> if (index == 1) dp(13f) else dp(8f)
                State.COMPLETE -> if (index == 1) dp(18f) else dp(8f)
                State.FAILED -> dp(9f)
                else -> dp(13f)
            }
            val wave = ((sin(phase + index * 1.2f) + 1f) / 2f)
            val barHeight = if (!reduceMotion && animator != null) minHeight + (maxHeight - minHeight) * wave else staticHeight
            val x = centerX + (index - 1) * spacing
            canvas.drawLine(x, centerY - barHeight / 2f, x, centerY + barHeight / 2f, paint)
        }
    }

    private fun dp(value: Float): Float = value * resources.displayMetrics.density
}
