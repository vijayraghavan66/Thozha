package com.jarvis.companion.link

fun main() {
    val progress = mapOf<String, Any>("task_id" to "t1", "state" to "RUNNING", "event_seq" to 4L)
    check(TaskNotificationPolicy.eventKey(progress) == "t1:RUNNING:4")
    check(TaskNotificationPolicy.eventKey(progress) == TaskNotificationPolicy.eventKey(progress))
    check(TaskNotificationPolicy.eventKey(progress + ("event_seq" to 5L)) != TaskNotificationPolicy.eventKey(progress))
    val rejected = mapOf<String, Any>("task_id" to "t2", "state" to "REJECTED", "event_seq" to 8L)
    check(TaskNotificationPolicy.eventKey(rejected) == "t2:REJECTED:8")
    check(TaskNotificationPolicy.eventKey(mapOf("task_id" to "t1", "state" to "WAITING_APPROVAL", "event_seq" to 5L)) == null)
    check(TaskNotificationPolicy.eventKey(mapOf("state" to "COMPLETED")) == null)
    val largeHistory = (0 until 700).map { index ->
        mapOf<String, Any>("task_id" to "task-$index", "state" to "COMPLETED", "event_seq" to 1L)
    }
    val baseline = TaskNotificationPolicy.notificationBatch(largeHistory, emptySet(), baselineInitialized = false)
    check(baseline.eventsToNotify.isEmpty())
    check(baseline.seenKeys.size == TaskNotificationPolicy.MAX_TRACKED_EVENTS)
    val newEvents = (700 until 720).map { index ->
        mapOf<String, Any>("task_id" to "task-$index", "state" to "FAILED", "event_seq" to 2L)
    }
    val expanded = largeHistory + newEvents
    val firstRefresh = TaskNotificationPolicy.notificationBatch(expanded, baseline.seenKeys, baselineInitialized = true)
    check(firstRefresh.eventsToNotify.size == TaskNotificationPolicy.MAX_NOTIFICATIONS_PER_REFRESH)
    check(firstRefresh.seenKeys.size == TaskNotificationPolicy.MAX_TRACKED_EVENTS)
    check(TaskNotificationPolicy.notificationBatch(expanded, firstRefresh.seenKeys, baselineInitialized = true).eventsToNotify.isEmpty())
    val collapsed = TaskNotificationPolicy.notificationBatch(
        listOf(
            mapOf("task_id" to "one-task", "state" to "RUNNING", "event_seq" to 1L),
            mapOf("task_id" to "one-task", "state" to "COMPLETED", "event_seq" to 2L),
        ),
        emptySet(),
        baselineInitialized = true,
    )
    check(collapsed.eventsToNotify.single().state == "COMPLETED")
    val rejection = TaskNotificationPolicy.notificationBatch(listOf(rejected), emptySet(), baselineInitialized = true)
    check(rejection.eventsToNotify.single().state == "REJECTED")
    println("Task notification policy checks passed: 13 assertions")
}
