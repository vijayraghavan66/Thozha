package com.jarvis.companion.data

import com.jarvis.companion.model.ApiException
import com.jarvis.companion.model.Approval
fun main() {
    val plan = mapOf<String, Any>(
        "plan_id" to "jarvis-expense-20261121",
        "device_id" to "d".repeat(32),
        "user_id" to "device:" + "d".repeat(32),
        "workspace_root" to "/Users/REDACTED/Documents/Jarvis-Lab/agent-evaluation/taskflow",
        "allowed_ops" to listOf("artifact_export", "artifact_manifest", "run_tests", "write_file"),
        "allowed_paths" to listOf("README.md", "src", "tests"),
        "max_file_bytes" to 65_536L,
        "max_total_bytes" to 262_144L,
        "max_writes" to 20L,
        "max_test_runs" to 5L,
        "max_repairs" to 2L,
        "max_duration_s" to 1_800L,
        "ttl_s" to 1_800L,
    )
    val planDigest = "7fa780c010332ef3f18985c8b4abf3c6c3745311251daec14cc0c4b146e7acc5"
    val operationDigest = "12be6b00fd5eba1b10aa88c47aded2654e7915ec448936d1179b65c46f945d19"
    val approvalPayload = mapOf(
        "canonical_arguments" to mapOf("plan" to plan, "plan_digest" to planDigest),
    )
    val approval = Approval(
        id = "a".repeat(32),
        taskId = "b".repeat(32),
        operation = "mcp:jarvis_dev:approve_plan",
        targetResource = "taskflow:plan:jarvis-expense-20261121",
        reason = "Review the exact bounded development plan.",
        risk = "requires_user_approval",
        operationDigest = operationDigest,
        createdAt = "2026-11-21T10:00:00+00:00",
        expiresAt = "2026-11-21T10:30:00+00:00",
        parametersSummary = "bounded development plan",
    )
    val verified = parseSupervisorApprovalReview(approvalPayload, approval)
    check(verified.developmentPlan?.planId == "jarvis-expense-20261121")
    check(verified.developmentPlan?.workspaceRoot == plan["workspace_root"])
    check(verified.developmentPlan?.planDigest == planDigest)
    check(verified.canonicalArgumentsDigest == operationDigest)
    check(verified.developmentPlan?.allowedPaths == listOf("README.md", "src", "tests"))
    check("artifact_export" in verified.developmentPlan!!.allowedOps)

    val missingPlan = mapOf("canonical_arguments" to emptyMap<String, Any>())
    check(runCatching { parseSupervisorApprovalReview(missingPlan, approval) }.exceptionOrNull() is ApiException)
    val changedPlan = plan.toMutableMap().apply { put("max_writes", 21L) }
    val tampered = mapOf("canonical_arguments" to mapOf("plan" to changedPlan, "plan_digest" to planDigest))
    check(runCatching { parseSupervisorApprovalReview(tampered, approval) }.exceptionOrNull() is ApiException)

    println("OpenJarvis approval review checks passed: 8 assertions")
}
