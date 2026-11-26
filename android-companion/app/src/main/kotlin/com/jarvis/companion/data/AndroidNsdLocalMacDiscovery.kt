package com.jarvis.companion.data

import android.content.Context
import android.content.pm.PackageManager
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import com.jarvis.companion.BuildConfig
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Ongoing Android DNS-SD browse for the private Thozha HTTPS gateway service.
 * DNS-SD data is untrusted: consumers must perform the existing paired certificate-pin and
 * exact-DNS-SAN checks before sending any credential or declaring a Mac identity verified.
 */
class AndroidNsdLocalMacDiscovery(context: Context) : LocalMacDiscoveryProvider {
    private val appContext = context.applicationContext
    private val mainExecutor = Executor { command -> Handler(Looper.getMainLooper()).post(command) }

    override fun start(listener: LocalMacDiscoveryListener): LocalMacDiscoverySession {
        val manager = appContext.getSystemService(Context.NSD_SERVICE) as? NsdManager
            ?: return ImmediateSession(listener, LocalMacDiscoverySnapshot(LocalMacDiscoveryStatus.UNAVAILABLE, diagnosticCode = "NSD_UNAVAILABLE"), mainExecutor)

        if (Build.VERSION.SDK_INT >= 37 && appContext.checkSelfPermission(LOCAL_NETWORK_PERMISSION) != PackageManager.PERMISSION_GRANTED) {
            return ImmediateSession(
                listener,
                LocalMacDiscoverySnapshot(LocalMacDiscoveryStatus.LOCAL_NETWORK_PERMISSION_REQUIRED, diagnosticCode = "LOCAL_NETWORK_PERMISSION_REQUIRED"),
                mainExecutor,
            )
        }

        return BrowseSession(manager, listener, mainExecutor).also { it.start() }
    }

    private class ImmediateSession(
        listener: LocalMacDiscoveryListener,
        snapshot: LocalMacDiscoverySnapshot,
        callbackExecutor: Executor,
    ) : LocalMacDiscoverySession {
        private val closed = AtomicBoolean(false)
        init { callbackExecutor.execute { if (!closed.get()) runCatching { listener.onSnapshot(snapshot) } } }
        override fun close() { closed.set(true) }
    }

    private class BrowseSession(
        private val manager: NsdManager,
        private val observer: LocalMacDiscoveryListener,
        private val callbackExecutor: Executor,
    ) : LocalMacDiscoverySession {
        private val closed = AtomicBoolean(false)
        private val lock = Any()
        private val index = LocalMacDiscoveryIndex()
        private val activeServices = mutableSetOf<String>()
        private val resolvingServices = mutableSetOf<String>()
        private val serviceEpoch = mutableMapOf<String, Long>()
        private var discoveryStarted = false
        @Volatile private var status = LocalMacDiscoveryStatus.IDLE

        private val discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                diagnostic("discovery_started", "type_matches=${isOurServiceType(serviceType)}")
                updateStatus(LocalMacDiscoveryStatus.DISCOVERING)
            }

            override fun onDiscoveryStopped(serviceType: String) {
                if (!closed.get()) updateStatus(LocalMacDiscoveryStatus.IDLE)
            }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                discoveryStarted = false
                publish(LocalMacDiscoveryStatus.ERROR, "NSD_START_FAILED_$errorCode")
                stopQuietly()
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
                diagnostic("stop_failed", "code=$errorCode")
            }

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                if (closed.get()) return
                if (!isOurServiceType(serviceInfo.serviceType)) {
                    diagnostic("service_ignored", "type_matches=false")
                    return
                }
                val id = ThozhaDnsSdContract.serviceId(serviceInfo.serviceName, serviceInfo.serviceType)
                val epoch: Long
                val shouldAnnounce: Boolean
                synchronized(lock) {
                    shouldAnnounce = activeServices.add(id)
                    if (shouldAnnounce) serviceEpoch[id] = (serviceEpoch[id] ?: 0L) + 1L
                    epoch = serviceEpoch[id] ?: 0L
                }
                if (shouldAnnounce) {
                    index.found(serviceInfo.serviceName, serviceInfo.serviceType, SystemClock.elapsedRealtime())
                    publish(status, null)
                    diagnostic("service_found")
                }
                resolve(serviceInfo, id, epoch)
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                val id = ThozhaDnsSdContract.serviceId(serviceInfo.serviceName, serviceInfo.serviceType)
                synchronized(lock) {
                    activeServices.remove(id)
                    resolvingServices.remove(id)
                    serviceEpoch[id] = (serviceEpoch[id] ?: 0L) + 1L
                }
                if (index.lost(serviceInfo.serviceName, serviceInfo.serviceType)) {
                    diagnostic("service_lost")
                    publish(status, null)
                }
            }
        }

        fun start() {
            if (closed.get()) return
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    manager.discoverServices(
                        ThozhaDnsSdContract.SERVICE_TYPE,
                        NsdManager.PROTOCOL_DNS_SD,
                        null, // null selects Android's current default local network; no interface is forced.
                        callbackExecutor,
                        discoveryListener,
                    )
                } else {
                    manager.discoverServices(ThozhaDnsSdContract.SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
                }
                discoveryStarted = true
                updateStatus(LocalMacDiscoveryStatus.DISCOVERING)
            } catch (_: SecurityException) {
                publish(LocalMacDiscoveryStatus.LOCAL_NETWORK_PERMISSION_REQUIRED, "LOCAL_NETWORK_PERMISSION_REQUIRED")
            } catch (_: IllegalArgumentException) {
                publish(LocalMacDiscoveryStatus.ERROR, "NSD_INVALID_REQUEST")
            } catch (_: RuntimeException) {
                publish(LocalMacDiscoveryStatus.ERROR, "NSD_START_FAILED")
            }
        }

        private fun resolve(info: NsdServiceInfo, id: String, epoch: Long) {
            val shouldResolve = synchronized(lock) {
                activeServices.contains(id) && resolvingServices.add(id)
            }
            if (!shouldResolve) return
            try {
                @Suppress("DEPRECATION")
                manager.resolveService(info, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                        synchronized(lock) { resolvingServices.remove(id) }
                        if (isCurrent(id, epoch)) {
                            diagnostic("resolve_failed", "code=$errorCode")
                            publish(status, "NSD_RESOLVE_FAILED_$errorCode")
                        }
                    }

                    override fun onServiceResolved(resolved: NsdServiceInfo) {
                        synchronized(lock) { resolvingServices.remove(id) }
                        if (!isCurrent(id, epoch)) return
                        val attributes = resolved.attributes
                        val host = attributes["tls-host"]?.toString(Charsets.UTF_8)
                        val protocol = attributes["protocol"]?.toString(Charsets.UTF_8)
                        val candidate = ThozhaDnsSdContract.parseResolved(
                            instanceName = resolved.serviceName,
                            serviceType = resolved.serviceType,
                            tlsHost = host,
                            port = resolved.port,
                            protocolVersion = protocol,
                            address = resolved.host,
                            nowElapsedMs = SystemClock.elapsedRealtime(),
                        )
                        if (candidate == null) {
                            diagnostic("service_record_rejected")
                            publish(status, "DNS_SD_RECORD_UNSUPPORTED")
                            return
                        }
                        if (index.resolved(candidate)) {
                            diagnostic("service_resolved_unverified")
                            publish(status, null)
                        }
                    }
                })
            } catch (_: SecurityException) {
                synchronized(lock) { resolvingServices.remove(id) }
                publish(LocalMacDiscoveryStatus.LOCAL_NETWORK_PERMISSION_REQUIRED, "LOCAL_NETWORK_PERMISSION_REQUIRED")
            } catch (_: RuntimeException) {
                synchronized(lock) { resolvingServices.remove(id) }
                if (isCurrent(id, epoch)) publish(status, "NSD_RESOLVE_FAILED")
            }
        }

        private fun isCurrent(id: String, epoch: Long): Boolean = synchronized(lock) {
            !closed.get() && activeServices.contains(id) && serviceEpoch[id] == epoch
        }

        private fun isOurServiceType(value: String): Boolean =
            ThozhaDnsSdContract.normalizeServiceType(value) == ThozhaDnsSdContract.normalizeServiceType(ThozhaDnsSdContract.SERVICE_TYPE)

        private fun updateStatus(next: LocalMacDiscoveryStatus) {
            status = next
            publish(next, null)
        }

        private fun publish(next: LocalMacDiscoveryStatus, diagnosticCode: String?) {
            if (closed.get()) return
            status = next
            val snapshot = index.snapshot(next, diagnosticCode)
            callbackExecutor.execute {
                if (!closed.get()) runCatching { observer.onSnapshot(snapshot) }
            }
        }

        private fun diagnostic(event: String, detail: String = "") {
            if (BuildConfig.DEBUG) Log.i("ThozhaDiscovery", "event=$event $detail".trim())
        }

        private fun stopQuietly() {
            if (!discoveryStarted) return
            discoveryStarted = false
            try {
                manager.stopServiceDiscovery(discoveryListener)
            } catch (_: RuntimeException) {
                // The platform may report a concurrent stop through onStopDiscoveryFailed.
            }
        }

        override fun close() {
            if (!closed.compareAndSet(false, true)) return
            stopQuietly()
            synchronized(lock) {
                activeServices.clear()
                resolvingServices.clear()
                serviceEpoch.clear()
            }
        }
    }

    private companion object {
        const val LOCAL_NETWORK_PERMISSION = "android.permission.ACCESS_LOCAL_NETWORK"
    }
}
