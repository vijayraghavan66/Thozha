package com.jarvis.companion.link

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.jarvis.link.InboxItem
import com.jarvis.link.LinkCrypto
import com.jarvis.link.LinkJson
import com.jarvis.link.RelayHttpException
import com.jarvis.link.RelayInvitation
import com.jarvis.link.SafeCapability
import com.jarvis.link.TaskSubmission
import com.jarvis.link.TrustedHostRoot
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.SecureRandom
import java.util.concurrent.Executors

/**
 * DEBUG BUILDS ONLY. Lets a developer drive Jarvis Link checks with `adb shell am broadcast` and read
 * structured results from logcat (tag JarvisLinkDebug). It never logs keys, signatures over secrets, or
 * plaintext task content; invitation secrets are passed as extras and are not echoed.
 */
class LinkDebugReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val app = context.applicationContext
        val action = intent.action ?: return
        val pending = goAsync()
        executor.execute {
            try {
                val result = when (action) {
                    "com.jarvis.link.debug.SELFTEST" -> selfTest()
                    "com.jarvis.link.debug.STATUS" -> status(app)
                    "com.jarvis.link.debug.ENROLL" -> enroll(app, intent)
                    "com.jarvis.link.debug.SYNC" -> LinkService.runtime(app).sync(relayOf(intent)).toMutableMap<String, Any>()
                    "com.jarvis.link.debug.SUBMIT" -> submit(app, intent)
                    "com.jarvis.link.debug.POLL" -> poll(app, intent)
                    "com.jarvis.link.debug.FLUSH" -> LinkService.runtime(app).flush(relayOf(intent)).toMutableMap<String, Any>()
                    "com.jarvis.link.debug.FORGET" -> forget(app)
                    "com.jarvis.link.debug.APPROVALS" -> mutableMapOf<String, Any>(
                        "approvals" to LinkService.runtime(app).pendingApprovals().map { it.approvalId to it.operation }.toString(),
                        "approval_count" to LinkService.runtime(app).pendingApprovals().size,
                    )
                    "com.jarvis.link.debug.APPROVE", "com.jarvis.link.debug.REJECT" -> LinkService.runtime(app).decideApproval(
                        relayOf(intent),
                        intent.getStringExtra("approval_id") ?: error("missing approval_id"),
                        if (action.endsWith("APPROVE")) "approve" else "reject",
                    ).toMutableMap()
                    else -> mapOf("error" to "unknown_action")
                }
                log("OK", action, result)
            } catch (failure: RelayHttpException) {
                log("FAIL", action, mapOf("error" to failure.code, "http_status" to failure.status))
            } catch (failure: Exception) {
                log("FAIL", action, mapOf("error" to (failure.javaClass.simpleName), "message" to safeMessage(failure)))
            } finally {
                pending.finish()
            }
        }
    }

    private fun relayOf(intent: Intent): String = intent.getStringExtra("relay") ?: SAFE_DEFAULT_RELAY

    private fun enroll(app: Context, intent: Intent): Map<String, Any> = LinkService.runtime(app).enroll(
        relayUrl = relayOf(intent),
        invitation = RelayInvitation(
            tenantId = intent.getStringExtra("tenant") ?: error("missing tenant"),
            invitationId = intent.getStringExtra("invitation_id") ?: error("missing invitation_id"),
            invitationSecretBase64Url = intent.getStringExtra("invitation_secret") ?: error("missing invitation_secret"),
            expiresAtUnixMs = intent.getLongExtra("invitation_expires_ms", 0L),
        ),
        deviceName = intent.getStringExtra("name") ?: "Debug Companion",
        root = TrustedHostRoot(
            deviceId = intent.getStringExtra("root_device_id") ?: error("missing root_device_id"),
            signingPublicKeyBase64Url = intent.getStringExtra("root_public_key") ?: error("missing root_public_key"),
        ),
    ).toMutableMap()

    private fun submit(app: Context, intent: Intent): Map<String, Any> {
        // Use objective_b64 from ADB/tooling: shell argument parsing can split plaintext at spaces.
        // Keep the old extra for backward-compatible local checks, but do not log either value.
        val objective = intent.getStringExtra("objective_b64")?.let(DebugTaskPayload::decode)
            ?: intent.getStringExtra("objective")
            ?: "Harmless debug status check"
        val scope = (intent.getStringExtra("scope") ?: "read_only").split(",").map { wire ->
            SafeCapability.entries.firstOrNull { it.wireName == wire } ?: error("unknown capability")
        }.toSet()
        val result = LinkService.runtime(app).submit(
            relayOf(intent),
            TaskSubmission(objective, scope),
            if (intent.hasExtra("ttl_ms")) intent.getLongExtra("ttl_ms", 0L) else null,
        )
        return mutableMapOf("message_id" to result.messageId, "queued_offline" to result.queuedOffline)
    }

    private fun poll(app: Context, intent: Intent): Map<String, Any> {
        val page = LinkService.runtime(app).poll(relayOf(intent))
        return mutableMapOf(
            "events" to page.events.map { it.toMap() },
            "event_count" to page.events.size,
        )
    }

    private fun forget(app: Context): Map<String, Any> {
        LinkKeystoreKeys.deleteIdentity()
        SharedPrefsLinkStateStore(app).clear()
        app.getSharedPreferences("jarvis_link_settings", Context.MODE_PRIVATE).edit().clear().apply()
        java.io.File(app.filesDir, "jarvis_link").deleteRecursively()
        return mutableMapOf("forgotten" to true, "keystore_identity_present" to LinkKeystoreKeys.hasKeys())
    }

    private fun status(app: Context): Map<String, Any> {
        val s = LinkService.runtime(app).status()
        return mutableMapOf(
            "keystore_supported" to LinkKeystoreKeys.isSupported(),
            "keystore_keys_present" to LinkKeystoreKeys.hasKeys(),
            "device_id" to (s.deviceId ?: ""),
            "tenant_id" to (s.tenantId ?: ""),
            "enrollment_pending" to s.enrollmentPending,
            "certificates_verified" to s.certificatesVerified,
            "outbox_size" to s.outboxSize,
        )
    }

    /**
     * Keystore self-test on the real device:
     *  1. keys exist and the device ID derives from the Keystore signing public key;
     *  2. Keystore ECDH with a software peer yields the same secret in both directions;
     *  3. a Keystore ECDSA signature over a fixed message is returned (verified off-device by the host tests).
     */
    private fun selfTest(): Map<String, Any> {
        val (signing, encryption) = LinkKeystoreKeys.identities()
        val (signingAgain, _) = LinkKeystoreKeys.identities()
        val peer: KeyPair = KeyPairGenerator.getInstance("EC").apply { initialize(256, SecureRandom()) }.generateKeyPair()
        val fromKeystore = LinkCrypto.ecdh(encryption.keyPair.private, peer.public, encryption.agreementProvider)
        val fromPeer = LinkCrypto.ecdh(peer.private, encryption.keyPair.public)
        val message = "JARVIS-LINK-SELFTEST-V1\n${signing.deviceId}".toByteArray(Charsets.UTF_8)
        val signatureB64 = LinkCrypto.sign(signing.keyPair.private, message)
        val verifies = LinkCrypto.verify(signing.keyPair.public, message, signatureB64)
        return mutableMapOf(
            "keystore_supported" to true,
            "device_id" to signing.deviceId,
            "stable_across_calls" to (signing.deviceId == signingAgain.deviceId),
            "ecdh_matches_software_peer" to fromKeystore.contentEquals(fromPeer),
            "ecdh_secret_bytes" to fromKeystore.size,
            "signature_verifies_on_device" to verifies,
            "signing_public_key_b64url" to LinkCrypto.b64url(signing.keyPair.public.encoded),
            "agreement_public_key_b64url" to LinkCrypto.b64url(encryption.keyPair.public.encoded),
            "signature_b64url" to signatureB64,
            "signed_message" to String(message, Charsets.UTF_8),
        )
    }

    private fun safeMessage(failure: Exception): String =
        // Only the class and our own short, non-secret messages; never the full cause chain.
        (failure.message ?: "").take(120).replace(Regex("[A-Za-z0-9_-]{40,}"), "<redacted>")

    private fun log(outcome: String, action: String, values: Map<String, Any>) {
        val json = LinkJson.encode(mapOf("outcome" to outcome, "action" to action.substringAfterLast('.')) + values)
        Log.i(TAG, json)
    }

    companion object {
        private const val TAG = "JarvisLinkDebug"
        private val executor = Executors.newSingleThreadExecutor()
        /** Default test relay; the phone reaches the Mac loopback relay through adb reverse. */
        private const val SAFE_DEFAULT_RELAY = "http://127.0.0.1:8787"
    }
}
