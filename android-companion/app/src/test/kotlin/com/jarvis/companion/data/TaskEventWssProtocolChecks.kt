package com.jarvis.companion.data

import org.json.JSONObject
import java.net.InetAddress
import java.security.MessageDigest

/** Offline protocol/security vectors for the paired local WSS event client. */
object TaskEventWssProtocolChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        val bytes = "fixture-certificate".toByteArray()
        val fingerprint = "sha256:" + MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }
        check(matchesPinnedGatewayCertificate(bytes, fingerprint))
        check(!matchesPinnedGatewayCertificate("different-certificate".toByteArray(), fingerprint))
        check(!matchesPinnedGatewayCertificate(bytes, "sha256:" + "0".repeat(64)))
        check(matchesExactGatewayDnsSan(listOf(listOf(2, "thozha-mac.local")), "thozha-mac.local"))
        check(!matchesExactGatewayDnsSan(listOf(listOf(2, "other.local")), "thozha-mac.local"))
        check(!matchesExactGatewayDnsSan(listOf(listOf(7, "thozha-mac.local")), "thozha-mac.local"))
        check(!matchesExactGatewayDnsSan(listOf(listOf(2, "*.local")), "thozha-mac.local"))

        val pin = LocalGatewayPin("thozha-mac.local", 8443, fingerprint, "192.168.1.2")
        val candidate = DiscoveredMacGateway(
            serviceId = "_jarvis-companion._tcp.|Jarvis Companion",
            instanceName = "Jarvis Companion",
            advertisedTlsHostname = "thozha-mac.local",
            port = 8443,
            privateAddresses = listOf("192.168.1.2", "fd12::2"),
            protocolVersion = "1",
            lastObservedElapsedMs = 1,
        )
        check(validateGatewayAgainstPin(candidate, pin).size == 2)
        check(validateGatewayAgainstPin(candidate.copy(advertisedTlsHostname = "untrusted.local"), pin).isEmpty())
        check(validateGatewayAgainstPin(candidate.copy(port = 8444), pin).isEmpty())
        check(validateGatewayAgainstPin(candidate.copy(privateAddresses = listOf("127.0.0.1")), pin).isEmpty())
        check(candidate.identityStatus == LocalMacIdentityStatus.UNVERIFIED)

        val url = buildTaskEventsWssUrl("thozha-mac.local", 8443, "request-1234567890", 42)
        check(url == "wss://thozha-mac.local:8443/v1/jarvis/link/tasks/request-1234567890/events/ws?after_seq=42")
        check("token" !in url && "Bearer" !in url && "@" !in url)
        check(runCatching { buildTaskEventsWssUrl("public.example.com", 443, "request-1234567890", 0) }.isFailure)
        check(runCatching { buildTaskEventsWssUrl("thozha-mac.local", 8443, "../other", 0) }.isFailure)
        check(reconnectDelayMs(0) == 1_000L)
        check(reconnectDelayMs(1) == 2_000L)
        check(reconnectDelayMs(5) == 30_000L)
        check(reconnectDelayMs(9) == 30_000L)

        val ledger = TaskEventAckLedger(initialCursor = 4, maxPending = 2)
        check(ledger.offer(5) == EventSequenceOffer.ACCEPTED)
        check(ledger.offer(5) == EventSequenceOffer.DUPLICATE)
        check(ledger.offer(7) == EventSequenceOffer.ACCEPTED)
        check(ledger.offer(9) == EventSequenceOffer.BACKPRESSURE)
        check(ledger.offer(6) == EventSequenceOffer.OUT_OF_ORDER)
        check(ledger.markPersisted(7) == null) { "cumulative ack passed an event that was not persisted" }
        check(ledger.acknowledged == 4L)
        check(ledger.markPersisted(5) == 7L) { "persisted events were not cumulatively acknowledged in order" }
        check(ledger.markPersisted(7) == 7L) { "duplicate persistence ACK should be idempotent" }

        val taskId = "a".repeat(32)
        val requestKey = "request-1234567890"
        val ready = parseTaskEventReady(
            JSONObject().put("type", "ready").put("protocol", 1).put("after_seq", 42).put("latest_seq", 44),
        ) ?: error("exact server ready frame rejected")
        check(ready.afterSequence == 42L && ready.latestSequence == 44L)
        check(parseTaskEventReady(JSONObject().put("type", "ready").put("protocol", 1).put("after_seq", 5).put("latest_seq", 4)) == null)

        // Exact server event shape: the durable cursor is nested event.seq; there is no event_seq alias.
        val eventFrame = JSONObject()
            .put("type", "event")
            .put("protocol", 1)
            .put("task_id", taskId)
            .put(
                "event",
                JSONObject()
                    .put("seq", 9)
                    .put("event_type", "link_task.waiting_approval")
                    .put("created_at", "2026-11-25T04:00:00Z")
                    .put("payload", JSONObject().put("state", "WAITING_APPROVAL").put("approval_id", "secret-ish-id").put("operation", "mcp:tool")),
            )
        val event = parseTaskEvent(eventFrame, requestKey) ?: error("valid exact server event frame rejected")
        check(event.supervisorTaskId == taskId && event.idempotencyKey == requestKey && event.sequence == 9L)
        check(event.taskState == "WAITING_APPROVAL")
        check(event.event.id == "9")
        check(event.event.state == "link_task.waiting_approval")
        check(event.event.message == "Task is waiting for your approval")
        check("secret-ish-id" !in event.event.message && "mcp:tool" !in event.event.message)
        check("event_seq" !in eventFrame.toString())

        check(parseTaskEvent(JSONObject(eventFrame.toString()).put("task_id", "bad-id"), requestKey) == null) {
            "malformed task ID was accepted"
        }
        val privateFrame = JSONObject(eventFrame.toString()).put(
            "event",
            JSONObject().put("seq", 10).put("event_type", "private.reasoning").put("created_at", "now"),
        )
        check(parseTaskEvent(privateFrame, requestKey) == null) { "non-allowlisted/private event type was accepted" }
        val completedFrame = JSONObject(eventFrame.toString()).put(
            "event",
            JSONObject().put("seq", 11).put("event_type", "link_task.completed").put("created_at", "now")
                .put("payload", JSONObject().put("result_text", "private model output")),
        )
        check(parseTaskEvent(completedFrame, requestKey)?.event?.message == "Task completed") {
            "event adapter surfaced content instead of safe lifecycle metadata"
        }

        val checkpoint = parseTaskEventCheckpoint(
            JSONObject().put("type", "checkpoint").put("protocol", 1).put("task_id", taskId).put("seq", 12),
            requestKey,
        ) ?: error("exact server checkpoint frame rejected")
        check(checkpoint.supervisorTaskId == taskId && checkpoint.idempotencyKey == requestKey && checkpoint.sequence == 12L)
        check(
            parseTaskEventCheckpoint(
                JSONObject().put("type", "checkpoint").put("protocol", 1).put("task_id", "bad-id").put("seq", 12), requestKey,
            ) == null,
        ) { "malformed/mismatched task ID checkpoint accepted" }

        val actionOrder = mutableListOf<String>()
        check(persistBeforeTaskEventAck({ actionOrder += "persist"; true }) { actionOrder += "ack" })
        check(actionOrder == listOf("persist", "ack")) { "ACK did not follow durable persistence" }
        actionOrder.clear()
        check(!persistBeforeTaskEventAck({ actionOrder += "persist"; false }) { actionOrder += "ack" })
        check(actionOrder == listOf("persist")) { "failed persistence was acknowledged" }

        val ack = JSONObject(buildTaskEventAck(taskId, 12L))
        check(ack.optString("type") == "ack" && ack.optInt("protocol") == 1)
        check(ack.optString("task_id") == taskId && ack.optLong("seq") == 12L)
        check(!ack.has("event_seq") && !ack.has("token") && !ack.has("authorization"))
        check(runCatching { buildTaskEventAck("bad-id", 1L) }.isFailure)

        // Ensure the builder has no username/password component, query auth, or fragment.
        val uri = java.net.URI(url)
        check(uri.userInfo == null && uri.fragment == null && uri.query == "after_seq=42")
        check(InetAddress.getByName(candidate.privateAddresses.first()).isSiteLocalAddress)

        println("TaskEventWssProtocolChecks: passed")
    }
}
