package com.jarvis.companion.data

import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.X509TrustManager

/** TLS trust manager that accepts only the certificate pinned over paired USB. */
object LocalGatewayTls {
    fun socketFactory(expectedFingerprint: String): SSLSocketFactory {
        require(expectedFingerprint.matches(Regex("sha256:[0-9a-f]{64}"))) { "invalid local gateway certificate pin" }
        val expected = expectedFingerprint.removePrefix("sha256:")
        val trustManager = object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
            override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                throw CertificateException("Client certificates are not used by the companion gateway")
            }
            override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                val leaf = chain?.firstOrNull() ?: throw CertificateException("Gateway did not present a certificate")
                try {
                    leaf.checkValidity()
                } catch (_: Exception) {
                    throw CertificateException("Gateway certificate is outside its validity period")
                }
                val actual = MessageDigest.getInstance("SHA-256").digest(leaf.encoded)
                    .joinToString("") { "%02x".format(it.toInt() and 0xff) }
                if (!MessageDigest.isEqual(actual.toByteArray(Charsets.US_ASCII), expected.toByteArray(Charsets.US_ASCII))) {
                    throw CertificateException("Gateway certificate does not match the Mac pin")
                }
            }
        }
        return SSLContext.getInstance("TLS").apply { init(null, arrayOf(trustManager), null) }.socketFactory
    }
}
