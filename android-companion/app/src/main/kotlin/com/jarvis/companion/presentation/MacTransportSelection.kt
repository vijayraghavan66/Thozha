package com.jarvis.companion.presentation

/** Selects direct paired transport only when its required private route is configured. */
object MacTransportSelection {
    fun usePairedMacApi(
        hasMacApiPairing: Boolean,
        pairedLocalRouteReady: Boolean,
        directReadOnlyCommand: Boolean,
        routeIsMac: Boolean,
    ): Boolean = hasMacApiPairing && pairedLocalRouteReady && (directReadOnlyCommand || routeIsMac)
}
