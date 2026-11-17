package com.jarvis.companion.data

import android.content.Context
import android.content.pm.PackageManager
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import java.net.InetAddress
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

data class LocalGatewayEndpoint(
    val hostname: String,
    val port: Int,
    val address: InetAddress,
)

/** Discovers only the previously paired Mac's pinned DNS-SD identity. */
object LocalGatewayDiscovery {
    const val SERVICE_TYPE = "_jarvis-companion._tcp."
    private const val SERVICE_NAME = "Jarvis Companion"
    private const val CACHE_MS = 12_000L
    private const val DISCOVERY_TIMEOUT_MS = 2_000L

    @Volatile private var cached: LocalGatewayEndpoint? = null
    @Volatile private var cachedAtMs = 0L

    fun localNetworkPermissionGranted(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < 37) return true
        return context.checkSelfPermission("android.permission.ACCESS_LOCAL_NETWORK") == PackageManager.PERMISSION_GRANTED
    }

    fun discover(context: Context, pin: LocalGatewayPin): LocalGatewayEndpoint? {
        if (!localNetworkPermissionGranted(context)) return null
        val now = System.currentTimeMillis()
        cached?.let { endpoint ->
            if (now - cachedAtMs < CACHE_MS && endpoint.hostname == pin.hostname && endpoint.port == pin.port) return endpoint
        }
        val manager = context.getSystemService(Context.NSD_SERVICE) as? NsdManager ?: return null
        val latch = CountDownLatch(1)
        val resolved = arrayOfNulls<LocalGatewayEndpoint>(1)
        var resolving = false

        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) = Unit
            override fun onDiscoveryStopped(serviceType: String) = Unit
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) { latch.countDown() }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) = Unit
            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                if (resolved[0]?.hostname == pin.hostname) {
                    resolved[0] = null
                    cached = null
                }
            }
            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                if (resolving || serviceInfo.serviceName != SERVICE_NAME ||
                    serviceInfo.serviceType.trimEnd('.') + "." != SERVICE_TYPE
                ) return
                resolving = true
                manager.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) { resolving = false }
                    override fun onServiceResolved(info: NsdServiceInfo) {
                        resolving = false
                        val hostAttribute = info.attributes["tls-host"]?.toString(Charsets.UTF_8)
                        val address = info.host
                        if (hostAttribute == pin.hostname && info.port == pin.port && address != null) {
                            val answers = runCatching { InetAddress.getAllByName(pin.hostname).toList() }.getOrDefault(emptyList())
                            if (answers.any { it.address.contentEquals(address.address) }) {
                                val endpoint = LocalGatewayEndpoint(pin.hostname, pin.port, address)
                                resolved[0] = endpoint
                                cached = endpoint
                                cachedAtMs = System.currentTimeMillis()
                                latch.countDown()
                            }
                        }
                    }
                })
            }
        }

        return try {
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
    }
}
