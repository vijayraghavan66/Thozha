package com.jarvis.companion.ui

import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/** Independent serial lanes keep ordered sends and refreshes moving while a network sync is blocked. */
internal class ExecutionLanes {
    val local: ExecutorService = lane("jarvis-ui-local")
    val send: ExecutorService = lane("jarvis-ui-send")
    val sync: ExecutorService = lane("jarvis-ui-sync")
    val approval: ExecutorService = lane("jarvis-ui-approval")

    fun close() {
        local.shutdownNow()
        send.shutdownNow()
        sync.shutdownNow()
        approval.shutdownNow()
    }

    private fun lane(name: String): ExecutorService = Executors.newSingleThreadExecutor { action ->
        Thread(action, name).apply { isDaemon = true }
    }
}
