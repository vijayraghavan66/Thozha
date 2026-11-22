package com.jarvis.companion.data

import android.content.Context
import android.content.pm.PackageManager
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.SystemClock
import android.util.Log
import com.jarvis.companion.BuildConfig
import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetAddress
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

data class LocalGatewayEndpoint(
    val hostname: String,
    val port: Int,
    val address: InetAddress,
) {
    /** Connect to the DNS-SD address; retain hostname separately for the pinned TLS identity. */
    fun httpsBaseUrl(): String {
        val literal = (address.hostAddress ?: throw IllegalStateException("Resolved Mac gateway address is empty")).substringBefore('%')
        val urlHost = if (address is Inet6Address) "[$literal]" else literal
        return "https://$urlHost:$port"
    }
}

internal fun isAllowedPrivateGatewayAddress(address: InetAddress): Boolean {
    if (address.isAnyLocalAddress || address.isLoopbackAddress || address.isLinkLocalAddress || address.isMulticastAddress) return false
    return when (address) {
        is Inet4Address -> {
            val bytes = address.address.map { it.toInt() and 0xff }
            bytes[0] == 10 ||
                (bytes[0] == 172 && bytes[1] in 16..31) ||
                (bytes[0] == 192 && bytes[1] == 168)
        }
        is Inet6Address -> (address.address[0].toInt() and 0xfe) == 0xfc // ULA fc00::/7
        else -> false
    }
}

internal fun endpointFromSignedAddress(pin: LocalGatewayPin): LocalGatewayEndpoint? = pin.address?.let { literal ->
    runCatching { InetAddress.getByName(literal) }.getOrNull()
        ?.takeIf(::isAllowedPrivateGatewayAddress)
        ?.let { LocalGatewayEndpoint(pin.hostname, pin.port, it) }
}

internal fun selectLocalGatewayEndpoint(
    loopbackReachable: Boolean,
    cachedOrDiscovered: LocalGatewayEndpoint?,
): LocalGatewayEndpoint? = if (loopbackReachable) null else cachedOrDiscovered

/** Discovers only the previously paired Mac's pinned DNS-SD identity. */
object LocalGatewayDiscovery {
    const val SERVICE_TYPE = "_jarvis-companion._tcp."
    private const val SERVICE_NAME = "Jarvis Companion"
    private const val CACHE_MS = 12_000L
    private const val NEGATIVE_CACHE_MS = 5_000L
    private const val DISCOVERY_TIMEOUT_MS = 2_000L

    @Volatile private var cached: LocalGatewayEndpoint? = null
    @Volatile private var cachedAtMs = 0L
    private val discoveryLock = Any()
    @Volatile private var discoveryInProgress = false
    @Volatile private var discoveryKey = ""
    @Volatile private var discoveryLatch: CountDownLatch? = null
    @Volatile private var failedHostname = ""
    @Volatile private var failedPort = 0
    @Volatile private var failedUntilElapsedMs = 0L

    private fun diagnostic(event: String, detail: String = "") {
        if (BuildConfig.DEBUG) Log.i("JarvisWifi", "event=$event $detail".trim())
    }

    /**
     * Returns a previously verified endpoint immediately and refreshes DNS-SD in the background.
     * A missing local gateway must not add the full two-second discovery timeout to every request.
     */
    fun cachedOrSchedule(context: Context, pin: LocalGatewayPin): LocalGatewayEndpoint? {
        if (!localNetworkPermissionGranted(context)) return null
        val now = SystemClock.elapsedRealtime()
        cached?.let { endpoint ->
            if (now - cachedAtMs < CACHE_MS && endpoint.hostname == pin.hostname && endpoint.port == pin.port) return endpoint
        }
        val session = startDiscovery(context, pin) ?: return null
        if (session.second) {
            Thread({ runDiscoveryAndFinish(context.applicationContext, pin, session.first) }, "jarvis-local-gateway-discovery").apply {
                isDaemon = true
                start()
            }
        }
        return null
    }

    /** Waits only when loopback is unavailable, joining an existing DNS-SD attempt if one is active. */
    fun discoverOrWait(context: Context, pin: LocalGatewayPin): LocalGatewayEndpoint? {
        if (!localNetworkPermissionGranted(context)) return null
        cachedEndpoint(pin)?.let { return it }
        val session = startDiscovery(context, pin) ?: return null
        if (session.second) runDiscoveryAndFinish(context.applicationContext, pin, session.first)
        else runCatching { session.first.await(DISCOVERY_TIMEOUT_MS + 500L, TimeUnit.MILLISECONDS) }
        return cachedEndpoint(pin)
    }

    /** Returns a latch and whether this caller owns the discovery run. */
    private fun startDiscovery(context: Context, pin: LocalGatewayPin): Pair<CountDownLatch, Boolean>? {
        val now = SystemClock.elapsedRealtime()
        return synchronized(discoveryLock) {
            cachedEndpoint(pin)?.let { return@synchronized null }
            if (failedHostname == pin.hostname && failedPort == pin.port && now < failedUntilElapsedMs) return@synchronized null
            val key = "${pin.hostname}:${pin.port}"
            if (discoveryInProgress) {
                if (discoveryKey == key) discoveryLatch?.let { return@synchronized it to false }
                return@synchronized null
            }
            val latch = CountDownLatch(1)
            discoveryInProgress = true
            discoveryKey = key
            discoveryLatch = latch
            latch to true
        }
    }

    private fun runDiscoveryAndFinish(context: Context, pin: LocalGatewayPin, latch: CountDownLatch) {
        val result = runCatching { discover(context, pin) }.getOrNull()
        synchronized(discoveryLock) {
            if (result == null) {
                failedHostname = pin.hostname
                failedPort = pin.port
                failedUntilElapsedMs = SystemClock.elapsedRealtime() + NEGATIVE_CACHE_MS
            } else {
                failedHostname = ""
                failedPort = 0
                failedUntilElapsedMs = 0L
            }
            discoveryInProgress = false
            discoveryKey = ""
            discoveryLatch = null
            latch.countDown()
        }
    }

    private fun cachedEndpoint(pin: LocalGatewayPin): LocalGatewayEndpoint? {
        val now = SystemClock.elapsedRealtime()
        return cached?.takeIf {
            now - cachedAtMs < CACHE_MS && it.hostname == pin.hostname && it.port == pin.port
        }
    }

    fun localNetworkPermissionGranted(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < 37) return true
        return context.checkSelfPermission("android.permission.ACCESS_LOCAL_NETWORK") == PackageManager.PERMISSION_GRANTED
    }

    fun discover(context: Context, pin: LocalGatewayPin): LocalGatewayEndpoint? {
        if (!localNetworkPermissionGranted(context)) return null
        val now = SystemClock.elapsedRealtime()
        cached?.let { endpoint ->
            if (now - cachedAtMs < CACHE_MS && endpoint.hostname == pin.hostname && endpoint.port == pin.port) return endpoint
        }
        if (failedHostname == pin.hostname && failedPort == pin.port && now < failedUntilElapsedMs) return null
        val manager = context.getSystemService(Context.NSD_SERVICE) as? NsdManager ?: return null
        val latch = CountDownLatch(1)
        val resolved = arrayOfNulls<LocalGatewayEndpoint>(1)
        var resolving = false

        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) { diagnostic("discovery_started") }
            override fun onDiscoveryStopped(serviceType: String) = Unit
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                diagnostic("discovery_start_failed", "code=$errorCode")
                latch.countDown()
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) = Unit
            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                diagnostic("service_lost")
                if (resolved[0]?.hostname == pin.hostname) {
                    resolved[0] = null
                    cached = null
                }
            }
            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                val nameMatches = serviceInfo.serviceName == SERVICE_NAME
                val typeMatches = serviceInfo.serviceType.trimEnd('.') + "." == SERVICE_TYPE
                diagnostic("service_found", "name_matches=$nameMatches type_matches=$typeMatches resolving=$resolving")
                if (resolving || !nameMatches || !typeMatches) return
                resolving = true
                manager.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                        diagnostic("service_resolve_failed", "code=$errorCode")
                        resolving = false
                    }
                    override fun onServiceResolved(info: NsdServiceInfo) {
                        resolving = false
                        val hostAttribute = info.attributes["tls-host"]?.toString(Charsets.UTF_8)
                        val address = info.host
                        val hostMatches = hostAttribute == pin.hostname
                        val portMatches = info.port == pin.port
                        val privateAddress = address?.let(::isAllowedPrivateGatewayAddress) == true
                        diagnostic("service_resolved", "host_matches=$hostMatches port_matches=$portMatches private_address=$privateAddress")
                        if (hostMatches && portMatches && address != null) {
                            // Android's JVM resolver does not resolve .local reliably. The DNS-SD SRV
                            // address is usable only on private LAN ranges; TLS pin + exact DNS SAN
                            // validation below establishes the Mac identity before any token is sent.
                            if (isAllowedPrivateGatewayAddress(address)) {
                                val endpoint = LocalGatewayEndpoint(pin.hostname, pin.port, address)
                                resolved[0] = endpoint
                                cached = endpoint
                                cachedAtMs = SystemClock.elapsedRealtime()
                                latch.countDown()
                            }
                        }
                    }
                })
            }
        }

        val discoveredEndpoint = try {
            manager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
            latch.await(DISCOVERY_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            resolved[0]
        } catch (_: SecurityException) {
            null
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
            null
        } finally {
            runCatching { manager.stopServiceDiscovery(listener) }
        }
        val signedAddressFallback = if (discoveredEndpoint == null) endpointFromSignedAddress(pin) else null
        val endpoint = discoveredEndpoint ?: signedAddressFallback
        if (discoveredEndpoint == null && signedAddressFallback != null) diagnostic("discovery_signed_address_fallback")
        if (endpoint == null) diagnostic("discovery_no_endpoint")
        else diagnostic("discovery_endpoint_accepted")
        endpoint?.let {
            cached = it
            cachedAtMs = SystemClock.elapsedRealtime()
        }
        if (endpoint == null) {
            failedHostname = pin.hostname
            failedPort = pin.port
            failedUntilElapsedMs = SystemClock.elapsedRealtime() + NEGATIVE_CACHE_MS
        } else {
            failedHostname = ""
            failedPort = 0
            failedUntilElapsedMs = 0L
        }
        return endpoint
    }
}
