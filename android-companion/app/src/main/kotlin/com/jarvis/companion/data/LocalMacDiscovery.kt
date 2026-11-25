package com.jarvis.companion.data

import java.net.InetAddress
import java.util.Locale

/** Discovery state is transport metadata only; it is never an authenticated device identity. */
enum class LocalMacDiscoveryStatus { IDLE, DISCOVERING, LOCAL_NETWORK_PERMISSION_REQUIRED, UNAVAILABLE, ERROR }

/** A DNS-SD record is an untrusted hint until the paired TLS identity is verified by the client. */
enum class LocalMacIdentityStatus { UNVERIFIED }

/** DNS-SD resolution does not test the gateway socket and must not be presented as reachability. */
enum class LocalMacReachability { NOT_CHECKED }

data class DiscoveredMacGateway(
    /** Stable only for this DNS-SD instance/type, not a device ID or credential. */
    val serviceId: String,
    val instanceName: String,
    /** Advertised TLS name. Treat as attacker-controlled input until normal TLS pin checks pass. */
    val advertisedTlsHostname: String?,
    val port: Int?,
    val privateAddresses: List<String>,
    val protocolVersion: String?,
    val lastObservedElapsedMs: Long,
    val identityStatus: LocalMacIdentityStatus = LocalMacIdentityStatus.UNVERIFIED,
    val reachability: LocalMacReachability = LocalMacReachability.NOT_CHECKED,
)

/** A signed address is only a network hint; the normal pinned TLS checks authenticate it. */
internal fun signedAddressGateway(pin: LocalGatewayPin, observedAtElapsedMs: Long): DiscoveredMacGateway? {
    val endpoint = endpointFromSignedAddress(pin) ?: return null
    val address = endpoint.address.hostAddress?.substringBefore('%') ?: return null
    return DiscoveredMacGateway(
        serviceId = "signed-endpoint:${pin.hostname.lowercase(Locale.ROOT)}:${pin.port}",
        instanceName = "Signed paired endpoint",
        advertisedTlsHostname = pin.hostname,
        port = pin.port,
        privateAddresses = listOf(address),
        protocolVersion = ThozhaDnsSdContract.PROTOCOL_VERSION,
        lastObservedElapsedMs = observedAtElapsedMs,
    )
}

data class LocalMacDiscoverySnapshot(
    val status: LocalMacDiscoveryStatus,
    val gateways: List<DiscoveredMacGateway> = emptyList(),
    val diagnosticCode: String? = null,
)

fun interface LocalMacDiscoveryListener {
    fun onSnapshot(snapshot: LocalMacDiscoverySnapshot)
}

fun interface LocalMacDiscoverySession : AutoCloseable {
    override fun close()
}

interface LocalMacDiscoveryProvider {
    /** Starts an ongoing DNS-SD browse. Implementations must not scan address ranges. */
    fun start(listener: LocalMacDiscoveryListener): LocalMacDiscoverySession
}

/** Existing Mac gateway service contract; it is shared with companion_wifi_gateway.py. */
object ThozhaDnsSdContract {
    const val SERVICE_TYPE = "_jarvis-companion._tcp."
    const val PROTOCOL_VERSION = "1"

    private val tlsHostPattern = Regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.local$", RegexOption.IGNORE_CASE)

    internal fun serviceId(instanceName: String, serviceType: String): String =
        "${normalizeServiceType(serviceType)}|${sanitizeInstance(instanceName)}"

    internal fun normalizeServiceType(value: String): String = value.trim().trimEnd('.').lowercase(Locale.ROOT) + "."

    internal fun sanitizeInstance(value: String): String = value
        .filterNot { it.isISOControl() }
        .take(255)
        .ifBlank { "Thozha Mac" }

    internal fun parseResolved(
        instanceName: String,
        serviceType: String,
        tlsHost: String?,
        port: Int,
        protocolVersion: String?,
        address: InetAddress?,
        nowElapsedMs: Long,
    ): DiscoveredMacGateway? {
        if (normalizeServiceType(serviceType) != normalizeServiceType(SERVICE_TYPE)) return null
        val host = tlsHost?.trim()?.takeIf { tlsHostPattern.matches(it) } ?: return null
        if (port !in 1..65535 || protocolVersion != PROTOCOL_VERSION) return null
        val resolvedAddress = address ?: return null
        if (!isRoutablePrivateLanAddress(resolvedAddress)) return null
        val addressText = resolvedAddress.hostAddress?.substringBefore('%') ?: return null
        return DiscoveredMacGateway(
            serviceId = serviceId(instanceName, serviceType),
            instanceName = sanitizeInstance(instanceName),
            advertisedTlsHostname = host.lowercase(Locale.ROOT),
            port = port,
            privateAddresses = listOf(addressText),
            protocolVersion = protocolVersion,
            lastObservedElapsedMs = nowElapsedMs,
        )
    }

    internal fun isRoutablePrivateLanAddress(address: InetAddress): Boolean {
        if (address.isAnyLocalAddress || address.isLoopbackAddress || address.isLinkLocalAddress || address.isMulticastAddress) return false
        val bytes = address.address.map { it.toInt() and 0xff }
        return when (bytes.size) {
            4 -> bytes[0] == 10 || (bytes[0] == 172 && bytes[1] in 16..31) || (bytes[0] == 192 && bytes[1] == 168)
            16 -> (bytes[0] and 0xfe) == 0xfc // IPv6 ULA fc00::/7
            else -> false
        }
    }
}

/** Pure, thread-safe index used by the Android provider and deterministic JVM checks. */
internal class LocalMacDiscoveryIndex {
    private val entries = linkedMapOf<String, DiscoveredMacGateway>()

    @Synchronized
    fun found(instanceName: String, serviceType: String, nowElapsedMs: Long): Boolean {
        if (ThozhaDnsSdContract.normalizeServiceType(serviceType) != ThozhaDnsSdContract.normalizeServiceType(ThozhaDnsSdContract.SERVICE_TYPE)) return false
        val id = ThozhaDnsSdContract.serviceId(instanceName, serviceType)
        val old = entries[id]
        entries[id] = (old ?: DiscoveredMacGateway(
            serviceId = id,
            instanceName = ThozhaDnsSdContract.sanitizeInstance(instanceName),
            advertisedTlsHostname = null,
            port = null,
            privateAddresses = emptyList(),
            protocolVersion = null,
            lastObservedElapsedMs = nowElapsedMs,
        )).copy(lastObservedElapsedMs = nowElapsedMs)
        return old == null
    }

    /** Returns false when a stale callback refers to a service that has already disappeared. */
    @Synchronized
    fun resolved(candidate: DiscoveredMacGateway): Boolean {
        if (!entries.containsKey(candidate.serviceId)) return false
        val old = entries.getValue(candidate.serviceId)
        entries[candidate.serviceId] = candidate.copy(
            // Never promote a DNS-SD advertisement to a verified identity or reachable endpoint.
            identityStatus = LocalMacIdentityStatus.UNVERIFIED,
            reachability = LocalMacReachability.NOT_CHECKED,
        )
        return old != entries[candidate.serviceId]
    }

    @Synchronized
    fun lost(instanceName: String, serviceType: String): Boolean = entries.remove(
        ThozhaDnsSdContract.serviceId(instanceName, serviceType),
    ) != null

    @Synchronized
    fun snapshot(status: LocalMacDiscoveryStatus, diagnosticCode: String? = null): LocalMacDiscoverySnapshot =
        LocalMacDiscoverySnapshot(status, entries.values.sortedBy { it.instanceName.lowercase(Locale.ROOT) }, diagnosticCode)
}
