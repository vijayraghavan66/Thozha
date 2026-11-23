package com.jarvis.companion.data

import java.net.InetAddress

object LocalGatewayRoutingChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        fun check(value: Boolean, message: String) {
            if (!value) error(message)
        }

        val endpoint = LocalGatewayEndpoint("thozha-mac.local", 8443, InetAddress.getByName("192.168.1.8"))
        check(endpoint.httpsBaseUrl() == "https://192.168.1.8:8443", "Wi-Fi must connect to the DNS-SD resolved private IP")
        check(selectLocalGatewayEndpoint(loopbackReachable = true, cachedOrDiscovered = endpoint) == null, "Reachable USB loopback must keep priority over cached Wi-Fi")
        check(selectLocalGatewayEndpoint(loopbackReachable = false, cachedOrDiscovered = endpoint) == endpoint, "Pinned Wi-Fi endpoint should be used when USB is unavailable")
        val signedFallback = endpointFromSignedAddress(
            LocalGatewayPin("thozha-mac.local", 8443, "sha256:" + "a".repeat(64), "192.168.1.8"),
        )
        check(signedFallback?.httpsBaseUrl() == "https://192.168.1.8:8443", "Signed loopback-provided IP should be usable when mDNS is blocked")
        check(
            endpointFromSignedAddress(LocalGatewayPin("thozha-mac.local", 8443, "sha256:" + "a".repeat(64), "8.8.8.8")) == null,
            "A public address must never be used as the local gateway fallback",
        )
        check(isAllowedPrivateGatewayAddress(InetAddress.getByName("192.168.1.8")), "RFC1918 gateway address should be accepted")
        check(isAllowedPrivateGatewayAddress(InetAddress.getByName("fd12::1")), "ULA gateway address should be accepted")
        listOf("127.0.0.1", "8.8.8.8", "169.254.1.1", "100.64.0.1", "224.0.0.1", "::1", "fe80::1").forEach { host ->
            check(!isAllowedPrivateGatewayAddress(InetAddress.getByName(host)), "Non-private address $host must be rejected")
        }
        check(
            LocalGatewayTls.matchesDnsSubjectAlternativeName(listOf(listOf(2, "thozha-mac.local")), "thozha-mac.local"),
            "Exact DNS SAN should be accepted",
        )
        check(
            !LocalGatewayTls.matchesDnsSubjectAlternativeName(listOf(listOf(2, "attacker.local")), "thozha-mac.local"),
            "Mismatched DNS SAN must be rejected",
        )
        check(
            !LocalGatewayTls.matchesDnsSubjectAlternativeName(listOf(listOf(2, "*.local")), "thozha-mac.local"),
            "Wildcard DNS SAN must be rejected",
        )
        check(
            !LocalGatewayTls.matchesDnsSubjectAlternativeName(listOf(listOf(1, "thozha-mac.local")), "thozha-mac.local"),
            "Common-name/RFC822 SAN must not be treated as a DNS SAN",
        )
        println("Local Wi-Fi routing and pinned DNS-name checks passed.")
    }
}
