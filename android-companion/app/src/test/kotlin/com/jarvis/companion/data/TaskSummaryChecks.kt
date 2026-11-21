package com.jarvis.companion.data

fun main() {
    check(linkTaskSummary("RUNNING", "null", "null") == "Jarvis is processing this task.")
    check(linkTaskSummary("WAITING_APPROVAL", null, null) == "Jarvis is waiting for your approval.")
    check(linkTaskSummary("COMPLETED", null, null) == "Task completed, but the Mac returned no result text.")
    check(linkTaskSummary("COMPLETED", "real result", null) == "real result")
    check(linkTaskSummary("FAILED", "", "actual failure") == "actual failure")
    println("Task result summary checks passed: 5 assertions")
}
