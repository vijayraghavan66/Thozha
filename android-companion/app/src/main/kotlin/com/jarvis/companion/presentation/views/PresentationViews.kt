package com.jarvis.companion.presentation.views

import android.content.Context
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import com.jarvis.companion.R
import com.jarvis.companion.presentation.ApprovalCard
import com.jarvis.companion.presentation.ApprovalPhase
import com.jarvis.companion.presentation.ConnectionSummary
import com.jarvis.companion.presentation.ConversationItem
import com.jarvis.companion.presentation.Speaker
import com.jarvis.companion.presentation.TaskTone
import com.jarvis.companion.presentation.TimelineRow

/*
 * Reusable presentation builders. Each takes presentation models and optional callbacks. None of them calls
 * the network, decides an approval, or cancels a task: the caller wires callbacks to the existing runtime.
 */

/** Spacing, radii and type scale shared by every Jarvis screen. Changing a value here changes all screens. */
object JarvisTokens {
    const val SPACE_XS = 4
    const val SPACE_S = 8
    const val SPACE_M = 12
    const val SPACE_L = 16
    const val SPACE_XL = 24
    const val RADIUS_BUBBLE = 20
    const val RADIUS_CARD = 16
    const val RADIUS_CONTROL = 14
    const val TEXT_BODY = 16f
    const val TEXT_CAPTION = 13f
    const val TEXT_TITLE = 20f
}

internal fun Context.dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

internal fun Context.color(res: Int): Int = ContextCompat.getColor(this, res)

internal fun Context.card(fill: Int, radius: Int): GradientDrawable = GradientDrawable().apply {
    shape = GradientDrawable.RECTANGLE
    cornerRadius = dp(radius).toFloat()
    setColor(fill)
}

internal fun Context.text(value: String, size: Float, colorRes: Int, bold: Boolean = false): TextView =
    TextView(this).apply {
        text = value
        textSize = size
        setTextColor(color(colorRes))
        if (bold) typeface = Typeface.DEFAULT_BOLD
        setLineSpacing(0f, 1.12f)
    }

internal fun toneColor(tone: TaskTone?): Int = when (tone) {
    TaskTone.COMPLETED -> R.color.ok
    TaskTone.WAITING -> R.color.warn
    TaskTone.FAILED -> R.color.alert
    TaskTone.RUNNING -> R.color.accent
    TaskTone.CANCELLED, TaskTone.UNKNOWN, null -> R.color.label_secondary
}

/** A message bubble. The user's message sits on the right; Jarvis's replies sit on the left with a small identity line. */
fun Context.conversationBubble(item: ConversationItem): LinearLayout {
    val isUser = item.speaker == Speaker.USER
    return LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        gravity = if (isUser) Gravity.END else Gravity.START
        layoutParams = LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(JarvisTokens.SPACE_S) }
        if (!isUser) addView(text("JARVIS", 11f, R.color.accent, bold = true).apply { setPadding(dp(4), 0, 0, dp(4)) })
        addView(LinearLayout(this@conversationBubble).apply {
            orientation = LinearLayout.VERTICAL
            background = card(
                fill = if (isUser) color(R.color.surface_strong) else color(R.color.surface),
                radius = JarvisTokens.RADIUS_BUBBLE,
            )
            setPadding(dp(JarvisTokens.SPACE_M + 4), dp(JarvisTokens.SPACE_S + 2), dp(JarvisTokens.SPACE_M + 4), dp(JarvisTokens.SPACE_S + 2))
            addView(text(item.text, JarvisTokens.TEXT_BODY, R.color.label).apply {
                // Long messages wrap inside a bounded column instead of running to the screen edge.
                maxWidth = (resources.displayMetrics.widthPixels * 0.78f).toInt()
                if (item.isProgress) setTextColor(color(R.color.label_secondary))
                // Replies keep the same formatting as before (lists, code, emphasis); user text stays literal.
                if (!isUser) {
                    setText(com.jarvis.companion.AssistantTextFormatter.render(item.text), TextView.BufferType.SPANNABLE)
                    setTextIsSelectable(true)
                }
                setLineSpacing(dp(3).toFloat(), 1f)
            })
            item.statusLabel?.let { addView(text(it, JarvisTokens.TEXT_CAPTION, R.color.label_secondary).apply { setPadding(0, dp(4), 0, 0) }) }
        }, LinearLayout.LayoutParams(-2, -2).apply { gravity = if (isUser) Gravity.END else Gravity.START })
        if (item.timeLabel.isNotBlank()) {
            addView(text(item.timeLabel, 11f, R.color.label_secondary).apply { setPadding(dp(4), dp(2), dp(4), 0) })
        }
    }
}

/** The honest three-part connection card: Telegram, Mac (last seen) and AI (verified by a real answer). */
fun Context.connectionCard(summary: ConnectionSummary): LinearLayout = LinearLayout(this).apply {
    orientation = LinearLayout.VERTICAL
    background = card(color(R.color.surface), JarvisTokens.RADIUS_CARD)
    setPadding(dp(JarvisTokens.SPACE_L), dp(JarvisTokens.SPACE_L), dp(JarvisTokens.SPACE_L), dp(JarvisTokens.SPACE_L))
    addView(text(summary.headline, JarvisTokens.TEXT_TITLE, R.color.label, bold = true))
    addView(text(summary.subline, JarvisTokens.TEXT_CAPTION, R.color.label_secondary).apply { setPadding(0, dp(4), 0, dp(JarvisTokens.SPACE_M)) })
    summary.rows.forEach { row ->
        addView(LinearLayout(this@connectionCard).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(JarvisTokens.SPACE_XS), 0, dp(JarvisTokens.SPACE_XS))
            addView(text(row.label, JarvisTokens.TEXT_CAPTION, R.color.label_secondary), LinearLayout.LayoutParams(dp(56), -2))
            addView(text(row.value, JarvisTokens.TEXT_CAPTION, toneColor(row.tone), bold = true), LinearLayout.LayoutParams(0, -2, 1f))
        })
    }
}

/**
 * Approval confirmation. Shows the exact operation, target and digest. The decision controls are enabled only
 * when the presentation says the request is still decidable; callbacks forward the choice to the existing path.
 */
fun Context.approvalCard(
    card: ApprovalCard,
    onApprove: (String) -> Unit,
    onReject: (String) -> Unit,
): LinearLayout = LinearLayout(this).apply {
    orientation = LinearLayout.VERTICAL
    background = card(color(R.color.surface), JarvisTokens.RADIUS_CARD)
    setPadding(dp(JarvisTokens.SPACE_L), dp(JarvisTokens.SPACE_L), dp(JarvisTokens.SPACE_L), dp(JarvisTokens.SPACE_L))
    addView(text(card.title, JarvisTokens.TEXT_TITLE, R.color.label, bold = true))
    addView(text("Action", JarvisTokens.TEXT_CAPTION, R.color.label_secondary).apply { setPadding(0, dp(JarvisTokens.SPACE_M), 0, 0) })
    addView(text(card.operation, JarvisTokens.TEXT_BODY, R.color.label, bold = true))
    addView(text("Target", JarvisTokens.TEXT_CAPTION, R.color.label_secondary).apply { setPadding(0, dp(JarvisTokens.SPACE_S), 0, 0) })
    addView(text(card.target, JarvisTokens.TEXT_BODY, R.color.label))
    addView(text("Request fingerprint ${card.digestPrefix}", 11f, R.color.label_secondary).apply { setPadding(0, dp(JarvisTokens.SPACE_S), 0, 0) })
    addView(text(card.expiryLabel, JarvisTokens.TEXT_CAPTION, if (card.phase == ApprovalPhase.EXPIRED) R.color.alert else R.color.warn).apply {
        setPadding(0, dp(JarvisTokens.SPACE_S), 0, 0)
    })
    if (card.canDecide) {
        addView(LinearLayout(this@approvalCard).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(JarvisTokens.SPACE_M), 0, 0)
            addView(decisionButton(R.string.jarvis_reject, R.color.surface_strong, R.color.label) { onReject(card.approvalId) }, LinearLayout.LayoutParams(0, dp(48), 1f).apply { rightMargin = dp(JarvisTokens.SPACE_S) })
            addView(decisionButton(R.string.jarvis_approve, R.color.accent, R.color.on_accent) { onApprove(card.approvalId) }, LinearLayout.LayoutParams(0, dp(48), 1f))
        })
    }
}

private fun Context.decisionButton(labelRes: Int, fill: Int, textColor: Int, onClick: () -> Unit): Button =
    Button(this).apply {
        text = getString(labelRes)
        isAllCaps = false
        setTextColor(color(textColor))
        background = card(color(fill), JarvisTokens.RADIUS_CONTROL)
        setOnClickListener { onClick() }
    }

/** A timeline row. Cancel is shown only when the presentation says the task can still be cancelled. */
fun Context.timelineRow(row: TimelineRow, onCancel: (String) -> Unit): LinearLayout = LinearLayout(this).apply {
    orientation = LinearLayout.VERTICAL
    background = card(color(R.color.surface), JarvisTokens.RADIUS_CARD)
    setPadding(dp(JarvisTokens.SPACE_L), dp(JarvisTokens.SPACE_M), dp(JarvisTokens.SPACE_L), dp(JarvisTokens.SPACE_M))
    layoutParams = LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(JarvisTokens.SPACE_S) }
    addView(text(row.statusLabel.uppercase(), 11f, toneColor(row.tone), bold = true))
    addView(text(row.title, JarvisTokens.TEXT_BODY, R.color.label, bold = true).apply { setPadding(0, dp(2), 0, 0) })
    if (row.detail.isNotBlank()) addView(text(row.detail, JarvisTokens.TEXT_CAPTION, R.color.label_secondary).apply { setPadding(0, dp(JarvisTokens.SPACE_XS), 0, 0) })
    addView(LinearLayout(this@timelineRow).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setPadding(0, dp(JarvisTokens.SPACE_S), 0, 0)
        addView(text(row.timeLabel, 11f, R.color.label_secondary), LinearLayout.LayoutParams(0, -2, 1f))
        if (row.canCancel) {
            addView(TextView(this@timelineRow).apply {
                text = getString(R.string.jarvis_cancel_task)
                textSize = JarvisTokens.TEXT_CAPTION
                setTextColor(color(R.color.alert))
                setPadding(dp(JarvisTokens.SPACE_S), dp(JarvisTokens.SPACE_XS), dp(JarvisTokens.SPACE_S), dp(JarvisTokens.SPACE_XS))
                setOnClickListener { onCancel(row.requestId) }
            })
        }
    })
}

/** Empty state for a list. Shown only when the underlying records are genuinely empty. */
fun Context.emptyState(messageRes: Int): TextView = text(getString(messageRes), JarvisTokens.TEXT_BODY, R.color.label_secondary).apply {
    gravity = Gravity.CENTER
    setPadding(dp(JarvisTokens.SPACE_XL), dp(JarvisTokens.SPACE_XL), dp(JarvisTokens.SPACE_XL), dp(JarvisTokens.SPACE_XL))
    visibility = View.VISIBLE
}
