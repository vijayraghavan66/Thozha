package com.jarvis.companion.presentation

/** Separates verified Mac identity from actual route reachability. */
data class LocalWifiPresentation(
    val identityLabel: String,
    val latestRouteLabel: String,
    val canVerifyIdentity: Boolean,
) {
    companion object {
        fun state(
            paired: Boolean,
            localNetworkPermission: Boolean,
            gatewayIdentityPinned: Boolean,
            lastSuccessfulRoute: String,
        ): LocalWifiPresentation {
            val identity = when {
                !paired -> "Pair this phone with the Mac first"
                !localNetworkPermission -> "Local network access is off; USB remains available"
                !gatewayIdentityPinned -> "Mac Wi-Fi identity has not been verified"
                else -> "Mac identity pinned; Wi-Fi reachability is not yet confirmed"
            }
            val route = when (lastSuccessfulRoute.lowercase()) {
                "wifi" -> "Last confirmed request used Wi-Fi"
                "usb" -> "Last confirmed request used USB"
                else -> "No successful Mac route recorded yet"
            }
            return LocalWifiPresentation(identity, route, paired && localNetworkPermission)
        }
    }
}

object ConnectionRoutePresentation {
    fun label(lastSuccessfulMacRoute: String, fallback: String): String = when (lastSuccessfulMacRoute.lowercase()) {
        "usb" -> "Local USB"
        "wifi" -> "Local Wi-Fi"
        "tailscale" -> "Private Tailscale"
        else -> fallback.ifBlank { "Not checked yet" }
    }
}
