package com.jarvis.companion.data

import android.util.Log
import com.jarvis.companion.BuildConfig
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.SSLSession
import javax.net.ssl.X509TrustManager

/** TLS trust manager that accepts only the certificate pinned over paired USB. */
object LocalGatewayTls {
    private val localHostnamePattern = Regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.local$", RegexOption.IGNORE_CASE)

    fun socketFactory(expectedFingerprint: String): SSLSocketFactory {
        require(expectedFingerprint.matches(Regex("sha256:[0-9a-f]{64}"))) { "invalid local gateway certificate pin" }
        val expected = expectedFingerprint.removePrefix("sha256:")
        val trustManager = object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
            override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                throw CertificateException("Client certificates are not used by the companion gateway")
            }
            override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                if (BuildConfig.DEBUG) Log.i("JarvisWifi", "tls_peer_received chain_size=${chain?.size ?: 0}")
                val leaf = chain?.firstOrNull() ?: throw CertificateException("Gateway did not present a certificate")
                try {
                    leaf.checkValidity()
                } catch (_: Exception) {
                    throw CertificateException("Gateway certificate is outside its validity period")
                }
                val actual = MessageDigest.getInstance("SHA-256").digest(leaf.encoded)
                    .joinToString("") { "%02x".format(it.toInt() and 0xff) }
                if (!MessageDigest.isEqual(actual.toByteArray(Charsets.US_ASCII), expected.toByteArray(Charsets.US_ASCII))) {
                    if (BuildConfig.DEBUG) Log.w("JarvisWifi", "tls_pin_match=false")
                    throw CertificateException("Gateway certificate does not match the Mac pin")
                }
                if (BuildConfig.DEBUG) Log.i("JarvisWifi", "tls_pin_match=true")
            }
        }
        return SSLContext.getInstance("TLS").apply { init(null, arrayOf(trustManager), null) }.socketFactory
    }

    fun hostnameVerifier(expectedHostname: String): HostnameVerifier {
        require(localHostnamePattern.matches(expectedHostname)) { "invalid local gateway hostname" }
        val canonicalHost = expectedHostname.lowercase(java.util.Locale.ROOT)
        return HostnameVerifier { _, session ->
            val matches = matchesPinnedDnsName(session, canonicalHost)
            if (BuildConfig.DEBUG) Log.i("JarvisWifi", "tls_dns_san_match=$matches")
            matches
        }
    }

    fun configure(connection: HttpsURLConnection, pin: LocalGatewayPin) {
        connection.sslSocketFactory = socketFactory(pin.certificateSha256)
        connection.hostnameVerifier = hostnameVerifier(pin.hostname)
        // The TCP URL targets the resolved private IP, while the TLS certificate and HTTP authority
        // remain bound to the hostname learned over the paired route.
        connection.setRequestProperty("Host", "${pin.hostname}:${pin.port}")
    }

    internal fun matchesPinnedDnsName(session: SSLSession, expectedHostname: String): Boolean {
        val certificate = session.peerCertificates.firstOrNull() as? X509Certificate ?: return false
        return matchesDnsSubjectAlternativeName(certificate.subjectAlternativeNames, expectedHostname)
    }

    internal fun matchesDnsSubjectAlternativeName(names: Collection<List<*>>?, expectedHostname: String): Boolean {
        if (names == null || !localHostnamePattern.matches(expectedHostname)) return false
        return names.any { entry ->
            entry.size >= 2 && entry[0] == 2 &&
                (entry[1] as? String)?.equals(expectedHostname, ignoreCase = true) == true
        }
    }
}
