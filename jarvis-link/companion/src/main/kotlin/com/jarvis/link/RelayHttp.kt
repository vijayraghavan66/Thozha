package com.jarvis.link

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.nio.charset.StandardCharsets.UTF_8

/** A relay HTTP rejection. [code] is the relay's stable error code; relay text is never surfaced. */
class RelayHttpException(val status: Int, val code: String) : IOException("relay HTTP $status ($code)")

/** Raw result of one relay exchange, independent of how the bytes travelled. */
data class RelayRawResponse(val status: Int, val text: String)

/**
 * How signed relay requests reach the relay. The default is HTTPS (or loopback HTTP). The Telegram tunnel
 * implements the same contract by carrying the identical request over Telegram. Requests always carry the
 * exact bytes that were signed; a transport must not modify headers or body.
 */
interface RelayTransport {
    fun exchange(method: String, pathAndQuery: String, headers: Map<String, String>, body: ByteArray): RelayRawResponse
}

/** HTTPS transport using the platform's own HTTP stack (and therefore the device's configured network path). */
class HttpUrlRelayTransport(private val base: String, private val timeoutMs: Int) : RelayTransport {
    override fun exchange(method: String, pathAndQuery: String, headers: Map<String, String>, body: ByteArray): RelayRawResponse {
        val connection = (URI(base + pathAndQuery).toURL().openConnection() as HttpURLConnection).apply {
            requestMethod = method
            instanceFollowRedirects = false
            connectTimeout = timeoutMs
            readTimeout = timeoutMs
            useCaches = false
            headers.forEach { (name, value) -> setRequestProperty(name, value) }
            if (method == "POST" || method == "PUT") {
                doOutput = true
                // Buffered output, not fixed-length streaming: HttpURLConnection cannot retry a POST after a
                // 401 in streaming mode and then discards the relay's error body (verified with a Java probe).
            }
        }
        try {
            if (method == "POST" || method == "PUT") connection.outputStream.use { it.write(body) }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            return RelayRawResponse(status, stream?.use { it.readBytes().toString(UTF_8) }.orEmpty())
        } finally {
            connection.disconnect()
        }
    }
}

/**
 * Outbound client for the relay. Requests carry exactly the bytes that were signed, so no JSON is
 * re-serialised between signing and sending. Plain HTTP is accepted only for loopback development.
 */
class HttpJarvisLinkRelay(
    baseUrl: String,
    private val tenantId: String,
    timeoutMs: Int = 10_000,
    transport: RelayTransport? = null,
) : JarvisLinkRelayApi {
    private val base: String
    private val transport: RelayTransport

    init {
        val uri = URI(baseUrl.trimEnd('/'))
        val loopback = uri.host in setOf("127.0.0.1", "localhost", "::1", "[::1]")
        require(uri.scheme == "https" || (uri.scheme == "http" && loopback)) { "relay URL must be HTTPS (HTTP only for loopback)" }
        require(uri.userInfo == null && uri.query == null && uri.fragment == null) { "relay URL must not carry credentials or query" }
        require(uri.path.isNullOrEmpty()) { "relay URL must not include a path" }
        require(tenantId.matches(Regex("^[A-Za-z0-9_-]{8,64}$"))) { "invalid tenant ID" }
        base = uri.toString().trimEnd('/')
        this.transport = transport ?: HttpUrlRelayTransport(base, timeoutMs)
    }

    override fun postMessage(envelopeJson: String, headers: Map<String, String>): RelayReceipt {
        val value = call("POST", "/v1/messages", envelopeJson.toByteArray(UTF_8), headers)
        return RelayReceipt(value.string("message_id"), value.string("status"), value.long("expires_at_unix_ms"))
    }

    override fun inbox(pathAndQuery: String, headers: Map<String, String>): InboxPage {
        val value = call("GET", pathAndQuery, ByteArray(0), headers)
        @Suppress("UNCHECKED_CAST")
        val items = value["messages"] as? List<Map<String, Any>> ?: error("relay inbox has an invalid shape")
        val messages = items.map { item ->
            @Suppress("UNCHECKED_CAST")
            val envelope = item["envelope"] as? Map<String, Any> ?: error("inbox item without envelope")
            @Suppress("UNCHECKED_CAST")
            val cert = item["sender_certificate"] as? Map<String, Any> ?: error("inbox item without sender certificate")
            InboxItem(LinkEnvelope.parse(LinkJson.encode(envelope)), certificateFrom(cert))
        }
        return InboxPage(messages, value.optionalString("next_cursor"))
    }

    override fun acknowledge(messageId: String, bodyJson: String, headers: Map<String, String>) {
        call("POST", "/v1/messages/$messageId/ack", bodyJson.toByteArray(UTF_8), headers)
    }

    override fun deviceCertificate(deviceId: String, headers: Map<String, String>): DeviceCertificate {
        val value = call("GET", "/v1/devices/$deviceId/certificate", ByteArray(0), headers)
        @Suppress("UNCHECKED_CAST")
        return certificateFrom(value["certificate"] as? Map<String, Any> ?: error("relay returned no certificate"))
    }

    override fun enrollmentChallenge(bodyJson: String): Map<String, Any> =
        call("POST", "/v1/enrollment/challenge", bodyJson.toByteArray(UTF_8), emptyMap())

    override fun enrollmentComplete(bodyJson: String): Map<String, Any> =
        call("POST", "/v1/enrollment/complete", bodyJson.toByteArray(UTF_8), emptyMap())

    /** Device-signed push-token registration. The body is the exact signed bytes. */
    fun setPushToken(deviceId: String, tokenJson: String, headers: Map<String, String>) {
        call("PUT", "/v1/devices/$deviceId/push-token", tokenJson.toByteArray(UTF_8), headers)
    }

    fun clearPushToken(deviceId: String, headers: Map<String, String>) {
        call("DELETE", "/v1/devices/$deviceId/push-token", ByteArray(0), headers)
    }

    private fun call(method: String, pathAndQuery: String, body: ByteArray, signed: Map<String, String>): Map<String, Any> {
        require(pathAndQuery.startsWith("/") && '\n' !in pathAndQuery && '\r' !in pathAndQuery)
        val headers = linkedMapOf("Accept" to "application/json", "X-Jarvis-Link-Tenant" to tenantId)
        headers.putAll(signed)
        if (method == "POST" || method == "PUT") headers["Content-Type"] = "application/json"
        val response = transport.exchange(method, pathAndQuery, headers, body)
        if (response.status !in 200..299) {
            val code = runCatching {
                @Suppress("UNCHECKED_CAST")
                ((LinkJson.parseObject(response.text)["error"] as? Map<String, Any>)?.get("code") as? String)
            }.getOrNull() ?: "relay_error"
            throw RelayHttpException(response.status, code)
        }
        return LinkJson.parseObject(response.text)
    }

    private fun certificateFrom(fields: Map<String, Any>): DeviceCertificate = DeviceCertificate(
        version = fields["v"]?.toString() ?: error("certificate missing version"),
        tenantId = fields.string("tenant_id"),
        deviceId = fields.string("device_id"),
        deviceRole = fields.string("device_role"),
        signingPublicKeyBase64Url = fields.string("signing_public_key_b64url"),
        encryptionPublicKeyBase64Url = fields.string("encryption_public_key_b64url"),
        notBeforeUnixMs = fields.long("not_before_unix_ms"),
        expiresAtUnixMs = fields.long("expires_at_unix_ms"),
        certificateId = fields.string("certificate_id"),
        issuerDeviceId = fields.string("issuer_device_id"),
        signatureBase64Url = fields.string("signature_b64url"),
    )
}
