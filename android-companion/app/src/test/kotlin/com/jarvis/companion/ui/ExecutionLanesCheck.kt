package com.jarvis.companion.ui

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Exercises the actual executors used by JarvisStore, with sync held at a network boundary. */
object ExecutionLanesCheck {
    @JvmStatic
    fun main(args: Array<String>) {
        val lanes = ExecutionLanes()
        val syncStarted = CountDownLatch(1)
        val releaseSync = CountDownLatch(1)
        val sendFinished = CountDownLatch(1)
        val localFinished = CountDownLatch(1)
        try {
            lanes.sync.execute {
                syncStarted.countDown()
                releaseSync.await(5, TimeUnit.SECONDS)
            }
            check(syncStarted.await(2, TimeUnit.SECONDS)) { "sync never started" }
            lanes.send.execute { sendFinished.countDown() }
            lanes.local.execute { localFinished.countDown() }
            check(sendFinished.await(2, TimeUnit.SECONDS)) { "send was blocked behind sync" }
            check(localFinished.await(2, TimeUnit.SECONDS)) { "local refresh was blocked behind sync" }
            check(releaseSync.count == 1L) { "sync was not held while send completed" }
            println("Blocked-sync send and local refresh checks passed.")
        } finally {
            releaseSync.countDown()
            lanes.close()
        }
    }
}
