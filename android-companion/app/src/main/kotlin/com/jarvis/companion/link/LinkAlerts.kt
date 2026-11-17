package com.jarvis.companion.link

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.jarvis.companion.MainActivity
import com.jarvis.companion.R
import com.jarvis.link.ApprovalReview
import com.jarvis.companion.model.Approval

/**
 * Notifications for Jarvis Link. Channel sound and vibration are set here as defaults; the user can change
 * sound, vibration, and importance per channel in Android settings, and Android enforces those choices.
 * Nothing is sent to a notification except the approval ID; details are read from the app after a tap.
 */
object LinkAlerts {
    const val CHANNEL_APPROVALS = "jarvis_link_approvals"
    const val CHANNEL_UPDATES = "jarvis_link_updates"
    const val EXTRA_APPROVAL_ID = "jarvis_link_approval_id"
    const val EXTRA_APPROVAL_SOURCE = "jarvis_approval_source"
    private val approvalVibration = longArrayOf(0L, 350L, 200L, 350L)

    fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        manager.createNotificationChannel(NotificationChannel(
            CHANNEL_APPROVALS, "Approval requests", NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = "A task on your Mac is waiting for your approval"
            enableVibration(true)
            vibrationPattern = approvalVibration
            setShowBadge(true)
        })
        manager.createNotificationChannel(NotificationChannel(
            CHANNEL_UPDATES, "Task updates", NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = "Progress and completion of tasks you approved"
            enableVibration(false)
        })
    }

    /** POST_NOTIFICATIONS is a runtime permission on Android 13 (API 33) and later. */
    fun canPost(context: Context): Boolean {
        val permitted = Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        return permitted && NotificationManagerCompat.from(context).areNotificationsEnabled()
    }

    fun notifyApproval(context: Context, review: ApprovalReview): Boolean {
        return notifyApprovalPointer(context, review.approvalId, "link")
    }

    fun notifySupervisorApproval(context: Context, approval: Approval): Boolean {
        return notifyApprovalPointer(context, approval.id, "supervisor")
    }

    private fun notifyApprovalPointer(context: Context, approvalId: String, source: String): Boolean {
        if (!canPost(context)) return false
        ensureChannels(context)
        val open = Intent(context, MainActivity::class.java)
            .setAction("com.jarvis.link.OPEN_APPROVAL")
            .putExtra(EXTRA_APPROVAL_ID, approvalId)
            .putExtra(EXTRA_APPROVAL_SOURCE, source)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        val pending = PendingIntent.getActivity(
            context, stableId(approvalId), open, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(context, CHANNEL_APPROVALS)
        } else {
            @Suppress("DEPRECATION") Notification.Builder(context).setPriority(Notification.PRIORITY_HIGH)
        }
        val notification = builder
            .setSmallIcon(R.drawable.ic_stat_jarvis)
            .setContentTitle("Jarvis needs your approval")
            .setContentText("A task on your Mac is waiting for your review. Tap to inspect the exact action.")
            .setContentIntent(pending)
            .setAutoCancel(true)
            .setCategory(Notification.CATEGORY_MESSAGE)
            .build()
        // Same approval ID -> same notification ID: a repeated sync updates the alert instead of duplicating it.
        NotificationManagerCompat.from(context).notify(stableId(approvalId), notification)
        return true
    }

    private fun stableId(approvalId: String): Int = approvalId.hashCode() and 0x7fffffff
}
