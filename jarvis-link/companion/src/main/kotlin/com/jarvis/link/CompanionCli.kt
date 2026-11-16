package com.jarvis.link

import java.io.File
import kotlin.system.exitProcess

/**
 * Command-line driver for the production Kotlin companion code (LinkRuntime + HttpJarvisLinkRelay).
 * Used by the Python integration tests to exercise the Kotlin code against a live relay.
 *
 * TEST ONLY: keys come from SoftwareTestKeyProvider in the state directory. The Android app uses
 * Keystore-backed keys through the same LinkRuntime.
 *
 * Output is one JSON object on stdout. Errors are one JSON object on stderr with a stable code and never
 * contain key material, signatures, or plaintext.
 */
object CompanionCli {
    @JvmStatic
    fun main(args: Array<String>) {
        val command = args.firstOrNull() ?: usage()
        val options = parseOptions(args.drop(1))
        try {
            val stateFile = File(need(options, "state"))
            val dir = stateFile.absoluteFile.parentFile
            val runtime = LinkRuntime(
                keys = SoftwareTestKeyProvider(File(dir, "test-keys.json")),
                state = FileLinkStateStore(stateFile),
                storeDir = dir,
            )
            val result: Map<String, Any> = when (command) {
                "enroll" -> runtime.enroll(
                    relayUrl = need(options, "relay"),
                    invitation = RelayInvitation(
                        tenantId = need(options, "tenant"),
                        invitationId = need(options, "invitation-id"),
                        invitationSecretBase64Url = need(options, "invitation-secret"),
                        expiresAtUnixMs = need(options, "invitation-expires-ms").toLong(),
                    ),
                    deviceName = need(options, "name"),
                    root = TrustedHostRoot(need(options, "root-device-id"), need(options, "root-public-key")),
                )
                "sync" -> runtime.sync(need(options, "relay"))
                "submit" -> {
                    val scope = need(options, "scope").split(",").map { wire ->
                        SafeCapability.entries.firstOrNull { it.wireName == wire } ?: throw IllegalArgumentException("unknown capability")
                    }.toSet()
                    val result = runtime.submit(
                        need(options, "relay"),
                        TaskSubmission(need(options, "objective"), scope),
                        options["ttl-ms"]?.toLong(),
                    )
                    mapOf(
                        "message_id" to result.messageId,
                        "queued_offline" to result.queuedOffline,
                        "relay_status" to (result.receipt?.status ?: "queued_locally"),
                    )
                }
                "poll" -> {
                    val page = runtime.poll(need(options, "relay"), options["limit"]?.toInt() ?: 20)
                    mapOf("events" to page.events.map { it.toMap() }, "cursor" to (page.cursor ?: ""))
                }
                "flush" -> runtime.flush(need(options, "relay"))
                "approvals" -> mapOf("approvals" to runtime.pendingApprovals().map { review ->
                    mapOf(
                        "approval_id" to review.approvalId, "task_id" to review.taskId, "operation" to review.operation,
                        "target" to review.target, "digest" to review.canonicalArgumentsDigest,
                        "expires_at_unix_ms" to review.expiresAtUnixMs, "summary" to review.redactedSummary,
                    )
                })
                "approve", "reject" -> runtime.decideApproval(need(options, "relay"), need(options, "approval-id"), command)
                "status" -> runtime.status().let {
                    mapOf(
                        "device_id" to (it.deviceId ?: ""),
                        "tenant_id" to (it.tenantId ?: ""),
                        "enrollment_pending" to it.enrollmentPending,
                        "certificates_verified" to it.certificatesVerified,
                        "outbox_size" to it.outboxSize,
                    )
                }
                else -> usage()
            }
            println(LinkJson.encode(result))
        } catch (failure: Exception) {
            val code = when (failure) {
                is RelayHttpException -> failure.code
                is SecurityException -> "AUTH_FAILED"
                is java.io.IOException -> "NETWORK_UNAVAILABLE"
                else -> "PROTOCOL_ERROR"
            }
            System.err.println(LinkJson.encode(mapOf("error" to code, "exception" to failure.javaClass.simpleName)))
            exitProcess(if (code == "NETWORK_UNAVAILABLE") 3 else 2)
        }
    }

    private fun usage(): Nothing {
        System.err.println("usage: CompanionCli enroll|sync|submit|poll|flush|status --state FILE [--relay URL] ...")
        exitProcess(64)
    }

    private fun parseOptions(args: List<String>): Map<String, String> {
        val out = linkedMapOf<String, String>()
        var i = 0
        while (i < args.size) {
            val key = args[i]
            require(key.startsWith("--") && i + 1 < args.size) { "options are --name value pairs" }
            out[key.removePrefix("--")] = args[i + 1]
            i += 2
        }
        return out
    }

    private fun need(options: Map<String, String>, key: String): String =
        options[key] ?: throw IllegalArgumentException("missing --$key")
}
