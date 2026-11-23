package com.jarvis.companion.data

fun main() {
    check(linkTaskSummary("CREATED", null, "Queued for execution.") == "Queued for execution.")
    check(linkTaskSummary("RUNNING", null, "Working on the task.") == "Working on the task.")
    check(linkTaskSummary("VERIFYING", null, "Checking the result.") == "Checking the result.")
    check(linkTaskSummary("RUNNING", "null", "null") == "Jarvis is processing this task.")
    check(linkTaskSummary("WAITING_APPROVAL", null, null) == "Jarvis is waiting for your approval.")
    check(linkTaskSummary("COMPLETED", null, null) == "Task completed, but the Mac returned no result text.")
    check(linkTaskSummary("COMPLETED", "real result", null) == "real result")
    check(linkTaskSummary("FAILED", "", "actual failure") == "actual failure")
    println("Task result summary checks passed: 8 assertions")
}
