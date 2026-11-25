package com.jarvis.companion.presentation

/** Keeps a paired USB API as the default, while honoring an explicit Link test-relay selection. */
object MacTransportSelection {
    fun usePairedMacApi(
        hasMacApiPairing: Boolean,
        testRelaySelected: Boolean,
        directReadOnlyCommand: Boolean,
        routeIsMac: Boolean,
    ): Boolean = hasMacApiPairing && !testRelaySelected && (directReadOnlyCommand || routeIsMac)
}
