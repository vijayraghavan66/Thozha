package com.jarvis.companion.link

import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.ComponentName
import android.content.Context
import com.jarvis.link.SyncReport

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

    fun run(context: Context): SyncReport? {
        val settings = LinkSettings(context)
        if (!canRun(context)) return null
        val report = LinkService.runtime(context).syncNow(settings.relayUrl)
        report.newApprovals.forEach { LinkAlerts.notifyApproval(context, it) }
        return report
    }
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
