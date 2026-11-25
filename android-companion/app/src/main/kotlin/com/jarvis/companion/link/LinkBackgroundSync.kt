package com.jarvis.companion.link

import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.ComponentName
import android.content.Context
import com.jarvis.link.SyncReport
import com.jarvis.companion.telegram.TelegramSession

/**
 * One Link sync: retries queued sends, polls for replies and approval requests, and alerts once per new
 * request. Uses the existing loopback/test route or the authenticated Telegram tunnel when that tunnel is ready.
 */
object LinkBackgroundSync {
    /** True when the explicit test relay, an authenticated Telegram tunnel, or loopback Link relay is usable. */
    fun canRun(context: Context): Boolean {
        if (!LinkKeystoreKeys.hasKeys()) return false
        val settings = LinkSettings(context)
        val loopback = runCatching {
            val uri = java.net.URI(settings.relayUrl)
            uri.scheme == "http" && uri.host in setOf("127.0.0.1", "localhost", "::1", "[::1]")
        }.getOrDefault(false)
        val routeReady = settings.mode == LinkMode.TEST_RELAY ||
            com.jarvis.companion.telegram.TelegramSession.activeTunnel() != null ||
            (settings.mode == LinkMode.LOCAL_USB && loopback)
        if (!routeReady) return false
        return runCatching { LinkService.runtime(context).status().certificatesVerified }.getOrDefault(false)
    }

    fun run(context: Context, forcePermanentRetry: Boolean = false): SyncReport? {
        val settings = LinkSettings(context)
        if (!canRun(context)) return null
        val runtime = LinkService.runtime(context)
        val usesTelegramTunnel = settings.mode == LinkMode.TEST_RELAY && TelegramSession.activeTunnel() != null
        if (usesTelegramTunnel) {
            var retryState = TelegramSession.retryState()
            if (forcePermanentRetry && retryState.blockedCode != null) {
                retryState = TelegramSession.clearPermanentSendBlock()
            }
            if (retryState.blockedCode != null) {
                android.util.Log.w(
                    "JarvisDelivery",
                    "event=telegram_sync_deferred reason=permanent_failure code=${retryState.blockedCode}",
                )
                return SyncReport(emptyList(), runtime.recentUpdates(), 0, "TELEGRAM_AUTH_BLOCKED")
            }
            val remaining = TelegramSession.retryRemainingMs()
            if (remaining > 0L) {
                android.util.Log.i(
                    "JarvisDelivery",
                    "event=telegram_sync_deferred reason=retry_after remaining_s=${(remaining + 999L) / 1_000L}",
                )
                return SyncReport(emptyList(), runtime.recentUpdates(), 0, "TELEGRAM_RETRY_AFTER")
            }
        }
        val startedAtMs = System.currentTimeMillis()
        val startedNs = android.os.SystemClock.elapsedRealtimeNanos()
        val report = runtime.syncNow(settings.relayUrl)
        if (usesTelegramTunnel) {
            when {
                report.error in PERMANENT_RELAY_FAILURES -> TelegramSession.blockAutomaticRetry(permanentReason(report))
                report.errorStatus in 400..499 && report.errorStatus != 408 && report.errorStatus != 429 ->
                    TelegramSession.blockAutomaticRetry(permanentReason(report))
                report.errorStatus == 408 || report.errorStatus == 429 || report.errorStatus in 500..599 ||
                    report.error in TRANSIENT_RELAY_FAILURES -> TelegramSession.deferTransientRetry()
                report.error.isBlank() -> TelegramSession.recordTransportSuccess()
            }
        }
        val newlyPersisted = report.updates.filter { row ->
            val receivedAt = (row["received_at_unix_ms"] as? Number)?.toLong() ?: 0L
            receivedAt >= startedAtMs
        }
        val newlyPersistedTerminal = newlyPersisted.count { row ->
            (row["state"] as? String)?.uppercase() in TERMINAL_TASK_STATES
        }
        val latestReceiptAt = newlyPersisted.maxOfOrNull { row ->
            (row["received_at_unix_ms"] as? Number)?.toLong() ?: 0L
        } ?: 0L
        val safeError = report.error.takeIf { it.matches(Regex("[A-Z0-9_]{1,48}")) } ?: if (report.error.isBlank()) "none" else "other"
        android.util.Log.i(
            "JarvisDelivery",
            "event=link_sync_persisted mode=${settings.mode.wire} elapsed_ms=${android.os.SystemClock.elapsedRealtimeNanos().minus(startedNs) / 1_000_000} new_updates=${newlyPersisted.size} new_terminal=$newlyPersistedTerminal receipt_to_sync_end_ms=${if (latestReceiptAt > 0) (System.currentTimeMillis() - latestReceiptAt).coerceAtLeast(0) else -1} error=$safeError",
        )
        report.newApprovals.forEach { LinkAlerts.notifyApproval(context, it) }
        return report
    }

    private fun permanentReason(report: SyncReport): String = if (report.errorStatus == 401 || report.errorStatus == 403) "AUTH_REQUIRED" else
        report.error.takeIf { it.matches(Regex("[A-Z0-9_]{1,48}")) } ?: "SEND_REJECTED"

    private val PERMANENT_RELAY_FAILURES = setOf(
        "AUTH_FAILED", "DEVICE_REVOKED", "DEVICE_NOT_AUTHORIZED", "CERTIFICATE_REVOKED", "CERTIFICATE_INVALID",
        "RELAY_AUTH_FAILED", "PERMISSION_DENIED",
    )

    private val TERMINAL_TASK_STATES = setOf("COMPLETED", "FAILED", "CANCELLED", "REJECTED", "EXPIRED", "INDETERMINATE")
    private val TRANSIENT_RELAY_FAILURES = setOf("NETWORK_UNAVAILABLE", "PROTOCOL_ERROR", "REQUEST_STALE_RETRY", "RATE_LIMITED")
}

/**
 * Periodic background sync through the platform JobScheduler. 15 minutes is the minimum period Android
 * allows; the system decides when it runs and respects battery policies (no wake locks, no loops).
 * Push wake-ups via FCM are not wired: no Firebase project or google-services.json is configured.
 */
class LinkSyncJob : JobService() {
    override fun onStartJob(params: JobParameters): Boolean {
        Thread {
            com.jarvis.companion.telegram.TelegramBootstrap.ensureRunning(applicationContext)
            val deadline = android.os.SystemClock.elapsedRealtime() + 20_000L
            while (!LinkBackgroundSync.canRun(applicationContext) && android.os.SystemClock.elapsedRealtime() < deadline) {
                try { Thread.sleep(250L) } catch (_: InterruptedException) { break }
            }
            runCatching { LinkBackgroundSync.run(applicationContext) }
            jobFinished(params, false)
        }.start()
        return true
    }

    override fun onStopJob(params: JobParameters): Boolean = true

    companion object {
        private const val JOB_ID = 4117
        private const val PERIOD_MS = 15 * 60 * 1000L

        fun ensureScheduled(context: Context) {
            val scheduler = context.getSystemService(JobScheduler::class.java) ?: return
            val telegramConfigured = runCatching {
                com.jarvis.companion.telegram.TelegramCredentialStore(context).load() != null
            }.getOrDefault(false)
            if (LinkSettings(context).mode != LinkMode.TEST_RELAY && !telegramConfigured) {
                scheduler.cancel(JOB_ID)
                return
            }
            if (scheduler.getPendingJob(JOB_ID) != null) return
            val job = JobInfo.Builder(JOB_ID, ComponentName(context, LinkSyncJob::class.java))
                .setPeriodic(PERIOD_MS)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .build()
            // Not persisted across reboots: persistence needs RECEIVE_BOOT_COMPLETED, which this app does not
            // declare. The job is re-scheduled on every app open, and the failure code is checked, not ignored.
            check(scheduler.schedule(job) == JobScheduler.RESULT_SUCCESS) { "job scheduler refused the Link sync job" }
        }
    }
}
