package com.jarvis.companion.link

fun main() {
    val progress = mapOf<String, Any>("task_id" to "t1", "state" to "RUNNING", "event_seq" to 4L)
    check(TaskNotificationPolicy.eventKey(progress) == "t1:RUNNING:4")
    check(TaskNotificationPolicy.eventKey(progress) == TaskNotificationPolicy.eventKey(progress))
    check(TaskNotificationPolicy.eventKey(progress + ("event_seq" to 5L)) != TaskNotificationPolicy.eventKey(progress))
    check(TaskNotificationPolicy.eventKey(mapOf("task_id" to "t1", "state" to "WAITING_APPROVAL", "event_seq" to 5L)) == null)
    check(TaskNotificationPolicy.eventKey(mapOf("state" to "COMPLETED")) == null)
    println("Task notification policy checks passed: 6 assertions")
}
