package com.jarvis.companion.data

import java.net.InetAddress

/** Run with ./gradlew :app:verifyLocalMacDiscovery. No LAN or device is used by this harness. */
object LocalMacDiscoveryChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        val index = LocalMacDiscoveryIndex()
        check(index.found("Jarvis Companion", "_jarvis-companion._tcp.", 10L))
        check(!index.found("Jarvis Companion", "_jarvis-companion._tcp", 11L)) { "repeated browse callback duplicated the same service" }
        check(index.found("Jarvis Companion (2)", "_jarvis-companion._tcp.", 12L)) { "distinct DNS-SD instance was merged" }
        check(index.snapshot(LocalMacDiscoveryStatus.DISCOVERING).gateways.size == 2)

        val resolved = ThozhaDnsSdContract.parseResolved(
            instanceName = "Jarvis Companion",
            serviceType = "_jarvis-companion._tcp.",
            tlsHost = "thozha-mac.local",
            port = 8443,
            protocolVersion = "1",
            address = InetAddress.getByName("192.168.1.2"),
            nowElapsedMs = 20L,
        ) ?: error("valid local DNS-SD record rejected")
        check(index.resolved(resolved))
        val host = index.snapshot(LocalMacDiscoveryStatus.DISCOVERING).gateways.single { it.instanceName == "Jarvis Companion" }
        check(host.advertisedTlsHostname == "thozha-mac.local")
        check(host.port == 8443)
        check(host.privateAddresses == listOf("192.168.1.2"))
        check(host.identityStatus == LocalMacIdentityStatus.UNVERIFIED)
        check(host.reachability == LocalMacReachability.NOT_CHECKED)

        val addressChanged = ThozhaDnsSdContract.parseResolved(
            instanceName = "Jarvis Companion",
            serviceType = "_jarvis-companion._tcp.",
            tlsHost = "thozha-mac.local",
            port = 8443,
            protocolVersion = "1",
            address = InetAddress.getByName("10.0.0.8"),
            nowElapsedMs = 30L,
        ) ?: error("private address update rejected")
        check(index.resolved(addressChanged))
        check(index.snapshot(LocalMacDiscoveryStatus.DISCOVERING).gateways.single { it.instanceName == "Jarvis Companion" }.privateAddresses == listOf("10.0.0.8"))

        check(index.lost("Jarvis Companion", "_jarvis-companion._tcp."))
        check(!index.resolved(resolved)) { "late resolve callback resurrected a lost service" }
        check(!index.lost("Jarvis Companion", "_jarvis-companion._tcp."))
        check(index.snapshot(LocalMacDiscoveryStatus.DISCOVERING).gateways.map { it.instanceName } == listOf("Jarvis Companion (2)"))

        val invalidRecords = listOf(
            Triple("_other._tcp.", "thozha-mac.local", "192.168.1.2"),
            Triple("_jarvis-companion._tcp.", "attacker.example.com", "192.168.1.2"),
            Triple("_jarvis-companion._tcp.", "thozha-mac.local", "127.0.0.1"),
            Triple("_jarvis-companion._tcp.", "thozha-mac.local", "169.254.1.2"),
            Triple("_jarvis-companion._tcp.", "thozha-mac.local", "224.0.0.251"),
        )
        invalidRecords.forEach { (type, tlsHost, address) ->
            check(
                ThozhaDnsSdContract.parseResolved(
                    instanceName = "Jarvis Companion",
                    serviceType = type,
                    tlsHost = tlsHost,
                    port = 8443,
                    protocolVersion = "1",
                    address = InetAddress.getByName(address),
                    nowElapsedMs = 40L,
                ) == null,
            ) { "invalid discovery input accepted: $type / $tlsHost / $address" }
        }
        check(
            ThozhaDnsSdContract.parseResolved(
                "Jarvis Companion", "_jarvis-companion._tcp.", "thozha-mac.local", 8443, "2",
                InetAddress.getByName("192.168.1.2"), 50L,
            ) == null,
        ) { "unsupported service protocol accepted" }
        check(
            ThozhaDnsSdContract.parseResolved(
                "Jarvis Companion", "_jarvis-companion._tcp.", "thozha-mac.local", 0, "1",
                InetAddress.getByName("192.168.1.2"), 50L,
            ) == null,
        ) { "invalid port accepted" }

        println("LocalMacDiscoveryChecks: passed")
    }
}
