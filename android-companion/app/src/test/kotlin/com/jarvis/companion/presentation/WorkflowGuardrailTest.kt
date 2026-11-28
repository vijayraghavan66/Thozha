package com.jarvis.companion.presentation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import com.jarvis.companion.link.TaskNotificationPolicy
import com.jarvis.companion.ui.LinkSyncPolicy

class WorkflowGuardrailTest {
    @Test
    fun relayReceiptDoesNotApproveAndMacRejectionIsTerminal() {
        val now = 1_800_000_000_000L
        val request = ApprovalDecisionPresentation.submitted(
            approvalId = "approval-1234567890",
            taskId = "task-123456789012",
            decision = "approve",
            queuedOffline = false,
            messageId = "message-1",
            nowUnixMs = now,
        )!!

        assertEquals(ApprovalDecisionPhase.DELIVERED, request.phase)
        assertTrue(ApprovalDecisionPresentation.label(request).contains("Waiting for the Mac supervisor"))
        assertTrue(ApprovalDecisionPresentation.blocksAnotherTap(request))
        assertNull("a result for another device task cannot decide this approval", ApprovalDecisionPresentation.authoritative(
            request,
            mapOf(
                "kind" to "approval.decision_result",
                "approval_id" to request.approvalId,
                "task_id" to "different-task",
                "decision" to "approve",
                "status" to "APPROVED",
                "execution_state" to "EXECUTION_PENDING",
            ),
            now + 1,
        ))

        val rejectionRequest = ApprovalDecisionPresentation.submitted(
            approvalId = request.approvalId,
            taskId = request.taskId,
            decision = "reject",
            queuedOffline = false,
            messageId = "message-2",
            nowUnixMs = now + 2,
        )!!
        val rejected = ApprovalDecisionPresentation.authoritative(
            rejectionRequest,
            mapOf(
                "kind" to "approval.decision_result",
                "approval_id" to request.approvalId,
                "task_id" to request.taskId,
                "decision" to "reject",
                "status" to "REJECTED",
                "execution_state" to "REJECTED",
                "executed" to false,
                "success" to false,
            ),
            now + 3,
        )!!
        assertEquals(ApprovalDecisionPhase.REJECTED, rejected.phase)
        assertEquals("REJECTED", ApprovalDecisionPresentation.resultingTaskState("reject", null))
        assertEquals("PAUSED", ApprovalDecisionPresentation.resultingTaskState("reject", " paused "))
        assertEquals("Rejected", TaskStates.statusLabel("REJECTED"))
        assertEquals(TaskTone.FAILED, TaskStates.toneFor("REJECTED"))
        assertNull("a rejected task cannot later be promoted to running", ApprovalDecisionPresentation.taskState(rejected, "RUNNING", now + 4))
        assertTrue(ApprovalDecisionPresentation.blocksAnotherTap(rejected))
    }

    @Test
    fun taskActivityExposesCancellationOnlyForOpenApiRequests() {
        fun row(state: String, transport: String = "api"): TimelineRow =
            TimelinePresentation.sections(
                listOf(
                    mapOf(
                        "message_id" to "request-id-123456",
                        "task_id" to "task-123456789012",
                        "text" to "Long task",
                        "state" to state,
                        "transport" to transport,
                    ),
                ),
            ) { it.toString() }.flatMap { it.rows }.single()

        for (state in listOf("CREATED", "RUNNING", "PLANNING", "WAITING_APPROVAL", "PAUSED")) {
            assertTrue("$state task should expose cancel", row(state).canCancel)
        }
        for (state in listOf("COMPLETED", "FAILED", "CANCELLED", "REJECTED", "EXPIRED")) {
            assertFalse("$state task must not expose cancel", row(state).canCancel)
        }
        assertFalse("a read-only supervisor snapshot is not cancellable from this phone control", row("RUNNING", "mac_snapshot").canCancel && row("RUNNING", "mac_snapshot").transport == "api")
    }

    @Test
    fun authenticatedProgressIsOrderedDeduplicatedAndHonest() {
        val events = TaskProgressPresentation.eventsFor(
            taskId = "task-1",
            updates = listOf(
                mapOf("task_id" to "task-1", "event_seq" to 2L, "state" to "RUNNING", "summary" to "Unit tests are running"),
                mapOf("task_id" to "task-1", "event_seq" to 1L, "state" to "PLANNING", "summary" to "Preparing the approved task"),
                mapOf("task_id" to "task-1", "event_seq" to 2L, "state" to "RUNNING", "summary" to "duplicate update"),
                mapOf("task_id" to "other-task", "event_seq" to 3L, "state" to "FAILED", "summary" to "unrelated"),
            ),
        ) { "t$it" }

        assertEquals(listOf("seq:1", "seq:2"), events.map { it.id })
        assertEquals("Preparing the approved task", events[0].detail)
        assertEquals("Unit tests are running", events[1].detail)
        assertTrue("only records associated with the active task are rendered", events.none { it.detail == "unrelated" })
    }

    @Test
    fun restoredPollingUsesCachedActivityInsteadOfBlockingOnRuntimeDiskLock() {
        assertTrue("unknown startup snapshot conservatively keeps active polling until background hydration", LinkSyncPolicy.hasActiveWorkFromSnapshot(emptyList(), null))
        assertTrue("a durable active task remains visible even before Compose hydration", LinkSyncPolicy.hasActiveWorkFromSnapshot(emptyList(), true))
        assertTrue("a still-visible active row is not hidden by an older idle cache", LinkSyncPolicy.hasActiveWorkFromSnapshot(
            listOf(mapOf("transport" to "api", "state" to "RUNNING")), false,
        ))
        assertFalse("hydrated idle runtime and idle UI can use the normal cadence", LinkSyncPolicy.hasActiveWorkFromSnapshot(emptyList(), false))
        assertTrue("unknown Telegram fallback state does not incorrectly skip retry work", LinkSyncPolicy.shouldRunLinkSyncFromSnapshot(true, null))
        assertFalse("healthy LAN with a hydrated empty Telegram queue skips fallback polling", LinkSyncPolicy.shouldRunLinkSyncFromSnapshot(true, false to 0))
    }

    @Test
    fun largeNotificationHistoryRebaselinesAndDoesNotReplayOrFloodOnRefresh() {
        val history = (0 until 700).map { index ->
            mapOf<String, Any>("task_id" to "task-$index", "state" to "COMPLETED", "event_seq" to 1L)
        }
        val baseline = TaskNotificationPolicy.notificationBatch(history, emptySet(), baselineInitialized = false)
        assertTrue("upgrading the notification ledger establishes a quiet bounded baseline", baseline.eventsToNotify.isEmpty())
        assertEquals(TaskNotificationPolicy.MAX_TRACKED_EVENTS, baseline.seenKeys.size)

        val newEvents = (700 until 720).map { index ->
            mapOf<String, Any>("task_id" to "task-$index", "state" to "FAILED", "event_seq" to 2L)
        }
        val expanded = history + newEvents
        val firstRefresh = TaskNotificationPolicy.notificationBatch(expanded, baseline.seenKeys, baselineInitialized = true)
        assertEquals("one bounded refresh cannot enqueue a large backfill burst", TaskNotificationPolicy.MAX_NOTIFICATIONS_PER_REFRESH, firstRefresh.eventsToNotify.size)
        assertEquals(TaskNotificationPolicy.MAX_TRACKED_EVENTS, firstRefresh.seenKeys.size)
        val nextRefresh = TaskNotificationPolicy.notificationBatch(expanded, firstRefresh.seenKeys, baselineInitialized = true)
        assertTrue("events suppressed by the burst cap are recorded too, so they are not replayed on each refresh", nextRefresh.eventsToNotify.isEmpty())

        val progression = TaskNotificationPolicy.notificationBatch(
            listOf(
                mapOf("task_id" to "task-running", "state" to "RUNNING", "event_seq" to 1L),
                mapOf("task_id" to "task-running", "state" to "COMPLETED", "event_seq" to 2L),
            ),
            emptySet(),
            baselineInitialized = true,
        )
        assertEquals(1, progression.eventsToNotify.size)
        assertEquals("COMPLETED", progression.eventsToNotify.single().state)
    }
}
