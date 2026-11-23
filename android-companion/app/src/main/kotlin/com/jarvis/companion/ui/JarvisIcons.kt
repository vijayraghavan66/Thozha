package com.jarvis.companion.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.addPathNodes
import androidx.compose.ui.unit.dp

/** Stroke icons drawn as native vectors. They are tinted at draw time, so one shape serves every state. */
object JarvisIcons {
    private fun icon(name: String, vararg paths: String): ImageVector {
        val builder = ImageVector.Builder(name, 24.dp, 24.dp, 24f, 24f)
        paths.forEach { data ->
            builder.addPath(
                pathData = addPathNodes(data),
                fill = null,
                stroke = SolidColor(Color.White),
                strokeLineWidth = 1.8f,
                strokeLineCap = StrokeCap.Round,
                strokeLineJoin = StrokeJoin.Round,
            )
        }
        return builder.build()
    }

    val Home by lazy {
        icon("Home", "M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 3.5V17H6.5A2.5 2.5 0 0 1 4 14.5z")
    }
    val Chat by lazy {
        icon("Chat", "M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-5 4v-4.5a2.5 2.5 0 0 1-1-2z", "M8 8h8", "M8 11h5")
    }
    val Activity by lazy { icon("Activity", "M3 12h4l2.5-6 4 12 2.5-6H21") }
    val Tasks by lazy {
        icon("Tasks", "M9 5h10", "M9 12h10", "M9 19h10", "M4.5 5h.01", "M4.5 12h.01", "M4.5 19h.01")
    }
    val Devices by lazy {
        icon("Devices", "M3 6.5A2.5 2.5 0 0 1 5.5 4h13A2.5 2.5 0 0 1 21 6.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 15.5z", "M8 20h8", "M12 18v2")
    }
    val Approvals by lazy {
        icon("Approvals", "M12 3l7 3v5.5c0 4.3-2.9 7.9-7 9.5-4.1-1.6-7-5.2-7-9.5V6z", "M9 12l2.2 2.2L15.5 10")
    }
    val Live by lazy {
        icon("Live", "M8.5 8.5a5 5 0 0 0 0 7", "M15.5 8.5a5 5 0 0 1 0 7", "M5.5 5.5a9.5 9.5 0 0 0 0 13", "M18.5 5.5a9.5 9.5 0 0 1 0 13", "M12 11v2")
    }
    val Settings by lazy {
        icon("Settings", "M4 7h9", "M17 7h3", "M4 17h3", "M11 17h9", "M15 5v4", "M9 15v4")
    }
    val Send by lazy { icon("Send", "M12 19V5", "M6 11l6-6 6 6") }
    val Microphone by lazy { icon("Microphone", "M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3z", "M19 11a7 7 0 0 1-14 0", "M12 18v3", "M8 21h8") }
    val Speaker by lazy { icon("Speaker", "M3 10v4h4l5 4V6l-5 4z", "M16 9a5 5 0 0 1 0 6", "M18 6a9 9 0 0 1 0 12") }
    val NewChat by lazy { icon("NewChat", "M12 5v14", "M5 12h14") }
    val History by lazy { icon("History", "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "M12 7v5l3 2") }
    val Back by lazy { icon("Back", "M15 6l-6 6 6 6") }
    val Close by lazy { icon("Close", "M6 6l12 12", "M18 6L6 18") }
}
