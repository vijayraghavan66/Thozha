package com.jarvis.companion.data

import android.content.Context
import android.content.ContextWrapper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.jarvis.companion.link.LinkMode
import com.jarvis.companion.link.LinkSettings
import com.jarvis.companion.model.ApiException
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.io.Closeable
import java.net.InetAddress
import java.net.ServerSocket
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.spec.ECGenParameterSpec
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/** Device tests for app-private pairing persistence and the fixed Link cancellation endpoint. */
@RunWith(AndroidJUnit4::class)
class TaskLifecycleInstrumentedTest {
    @Test
    fun encryptedPairingSurvivesStoreRecreationAndHostChangeClearsCredential() {
        val context = isolatedPreferencesContext()
        val preferences = context.getSharedPreferences("jarvis_companion", Context.MODE_PRIVATE)
        preferences.edit().clear().commit()
        val keyPair = KeyPairGenerator.getInstance("EC").apply {
            initialize(ECGenParameterSpec("secp256r1"))
        }.generateKeyPair()
        val fingerprint = "sha256:" + MessageDigest.getInstance("SHA-256")
            .digest(keyPair.public.encoded).joinToString("") { "%02x".format(it) }
        val deviceId = "a1b2c3d4".repeat(4)
        val token = "instrumented-session-token-${UUID.randomUUID()}"

        try {
            SecureStore(context).savePairingWithServerIdentity(
                deviceId = deviceId,
                token = token,
                serverPublicKeyDer = keyPair.public.encoded,
                serverFingerprint = fingerprint,
                baseUrl = "https://thozha-test.local:8443",
            )

            // A new store object is what a recreated activity/process uses to restore the credential.
            val restored = SecureStore(context)
            assertEquals(deviceId, restored.deviceId())
            assertEquals(token, restored.token())
            assertEquals("https://thozha-test.local:8443", restored.baseUrl())
            assertEquals(fingerprint, restored.pinnedServerIdentity()?.fingerprint)
            assertFalse("the token must not be stored as readable SharedPreferences text",
                preferences.all.values.any { it == token })

            // Changing the selected host must invalidate the old bearer and device binding.
            restored.setBaseUrl("https://another-test.local:8443")
            val afterHostChange = SecureStore(context)
            assertEquals("", afterHostChange.deviceId())
            assertEquals(null, afterHostChange.token())
        } finally {
            preferences.edit().clear().commit()
        }
    }

    @Test
    fun interruptedCancellationRequiresExplicitRetryAndUsesAuthenticatedFixedRoute() {
        val context = isolatedPreferencesContext()
        val preferences = context.getSharedPreferences("jarvis_companion", Context.MODE_PRIVATE)
        preferences.edit().clear().commit()
        val store = SecureStore(context)
        val deviceId = "12345678".repeat(4)
        // This mock server is bound to the device's own loopback socket. Use the explicit
        // debug-only Local USB mode so the authenticated cleartext request is allowed only
        // for the reachable test socket; TEST_RELAY must remain fail-closed for loopback.
        LinkSettings(context).mode = LinkMode.LOCAL_USB
        val requestId = "cancel-request-123456"
        val server = OneRequestAtATimeServer(
            actions = listOf(
                ServerAction.CloseWithoutResponse,
                ServerAction.Respond(
                    200,
                    """{"task_id":"${"9".repeat(32)}","state":"CANCELLED","summary":"The Mac confirmed cancellation."}""",
                ),
            ),
        )
        store.setBaseUrl("http://127.0.0.1:${server.port}")
        store.savePairing(deviceId, "instrumented-token")
        val api = JarvisApi(context, store)

        try {
            val first = runCatching { api.cancelLinkTask(requestId) }.exceptionOrNull()
            assertTrue("a dropped connection is surfaced as an API error", first is ApiException)
            assertEquals("the API must not silently replay a cancellation", 1, server.capturedRequests.size)

            val recovered = api.cancelLinkTask(requestId)
            assertEquals("CANCELLED", recovered.state)
            assertTrue(recovered.summary.contains("cancellation"))
            assertTrue("both attempts reach the same fixed cancellation path", server.capturedRequests.all {
                it.requestLine == "POST /v1/jarvis/link/tasks/$requestId/cancel HTTP/1.1"
            })
            assertTrue("the pairing bearer is attached", server.capturedRequests.all {
                it.headers.contains("authorization: bearer instrumented-token")
            })
            assertTrue("the request body is the empty cancel command", server.capturedRequests.all {
                JSONObject(it.body).length() == 0
            })
            assertEquals("usb", store.lastMacConnection().second)
        } finally {
            server.close()
            server.awaitStopped()
            preferences.edit().clear().commit()
        }
    }

    @Test
    fun expiredApprovalLikeIdentifierFailsBeforeAnyNetworkRequest() {
        val context = isolatedPreferencesContext()
        val preferences = context.getSharedPreferences("jarvis_companion", Context.MODE_PRIVATE)
        preferences.edit().clear().commit()
        val store = SecureStore(context)
        val unusedPort = ServerSocket(0).use { it.localPort }
        store.setBaseUrl("http://127.0.0.1:$unusedPort")
        store.savePairing("12345678".repeat(4), "instrumented-token")
        val api = JarvisApi(context, store)
        try {
            val failure = runCatching { api.cancelLinkTask("short") }.exceptionOrNull()
            assertTrue(failure is ApiException)
            assertTrue("invalid IDs fail at validation before network access", failure?.message?.contains("could not be identified safely") == true)
        } finally {
            preferences.edit().clear().commit()
        }
    }

    private fun isolatedPreferencesContext(): Context {
        val base = InstrumentationRegistry.getInstrumentation().context
        val suffix = "_instrumented_test"
        return object : ContextWrapper(base) {
            override fun getSharedPreferences(name: String, mode: Int) =
                base.getSharedPreferences(name + suffix, mode)
        }
    }

    private sealed interface ServerAction {
        data object CloseWithoutResponse : ServerAction
        data class Respond(val status: Int, val body: String) : ServerAction
    }

    private data class CapturedRequest(val requestLine: String, val headers: String, val body: String)

    private class OneRequestAtATimeServer(private val actions: List<ServerAction>) : Closeable {
        private val socket = ServerSocket(0, actions.size, InetAddress.getByName("127.0.0.1"))
        val port: Int = socket.localPort
        val capturedRequests = java.util.Collections.synchronizedList(mutableListOf<CapturedRequest>())
        private val stopped = CountDownLatch(1)
        private val worker = thread(name = "thozha-instrumented-http", isDaemon = true) {
            try {
                var actionIndex = 0
                while (actionIndex < actions.size) {
                    val action = actions[actionIndex]
                    socket.accept().use { client ->
                        client.soTimeout = 4_000
                        val request = readRequest(client.getInputStream())
                        // LOCAL_USB performs an explicit loopback reachability probe before
                        // sending its authenticated request. That probe closes without HTTP
                        // headers; ignore it and keep the same scripted response.
                        if (request == null) return@use
                        actionIndex++
                        capturedRequests += request
                        when (action) {
                            ServerAction.CloseWithoutResponse -> Unit
                            is ServerAction.Respond -> {
                                val bytes = action.body.toByteArray(Charsets.UTF_8)
                                val reason = if (action.status in 200..299) "OK" else "Service Unavailable"
                                val header = "HTTP/1.1 ${action.status} $reason\r\n" +
                                    "Content-Type: application/json\r\n" +
                                    "Content-Length: ${bytes.size}\r\n" +
                                    "Connection: close\r\n\r\n"
                                client.getOutputStream().apply {
                                    write(header.toByteArray(Charsets.US_ASCII))
                                    write(bytes)
                                    flush()
                                }
                            }
                        }
                    }
                }
            } catch (failure: java.net.SocketException) {
                if (!socket.isClosed) throw failure
            } finally {
                stopped.countDown()
            }
        }

        override fun close() {
            runCatching { socket.close() }
            if (worker.isAlive) worker.interrupt()
        }

        fun awaitStopped() {
            assertTrue("test HTTP server should stop", stopped.await(5, TimeUnit.SECONDS))
        }

        private fun readRequest(input: java.io.InputStream): CapturedRequest? {
            val bytes = ByteArrayOutputStream()
            val ending = byteArrayOf(13, 10, 13, 10)
            while (bytes.size() < 32_768) {
                val next = input.read()
                if (next < 0) {
                    if (bytes.size() == 0) return null
                    throw AssertionError("HTTP request ended before headers")
                }
                bytes.write(next)
                val header = bytes.toByteArray()
                if (header.size >= ending.size && ending.indices.all { header[header.size - ending.size + it] == ending[it] }) break
            }
            val headerText = bytes.toString(Charsets.US_ASCII.name())
            val lines = headerText.removeSuffix("\r\n\r\n").split("\r\n")
            val contentLength = lines.drop(1).firstOrNull { it.startsWith("content-length:", ignoreCase = true) }
                ?.substringAfter(':')?.trim()?.toIntOrNull() ?: 0
            val body = ByteArray(contentLength)
            var offset = 0
            while (offset < body.size) {
                val count = input.read(body, offset, body.size - offset)
                if (count < 0) throw AssertionError("HTTP request body was truncated")
                offset += count
            }
            return CapturedRequest(
                requestLine = lines.firstOrNull().orEmpty(),
                headers = lines.drop(1).joinToString("\n").lowercase(),
                body = body.toString(Charsets.UTF_8),
            )
        }
    }
}
