package com.jarvis.companion.presentation

import com.jarvis.companion.link.AssistantNamePolicy

/** Run with ./gradlew :app:verifyLocalWifiPresentation. */
object LocalWifiPresentationChecks {
    @JvmStatic
    fun main(args: Array<String>) {
        val unpaired = LocalWifiPresentation.state(false, true, true, "wifi")
        check(!unpaired.canVerifyIdentity)
        check(unpaired.identityLabel.contains("Pair this phone"))
        check(unpaired.latestRouteLabel == "Last confirmed request used Wi-Fi")

        val permissionMissing = LocalWifiPresentation.state(true, false, false, "usb")
        check(!permissionMissing.canVerifyIdentity)
        check(permissionMissing.identityLabel.contains("USB remains available"))

        val pinnedButUnreachable = LocalWifiPresentation.state(true, true, true, "usb")
        check(pinnedButUnreachable.canVerifyIdentity)
        check(pinnedButUnreachable.identityLabel.contains("not yet confirmed"))
        check(pinnedButUnreachable.latestRouteLabel == "Last confirmed request used USB")

        val actualWifiRequest = LocalWifiPresentation.state(true, true, true, "wifi")
        check(actualWifiRequest.latestRouteLabel == "Last confirmed request used Wi-Fi")

        check(ConnectionRoutePresentation.label("wifi", "Legacy fallback") == "Local Wi-Fi")
        check(ConnectionRoutePresentation.label("usb", "Legacy fallback") == "Local USB")
        check(ConnectionRoutePresentation.label("tailscale", "Legacy fallback") == "Private Tailscale")
        check(ConnectionRoutePresentation.label("", "Legacy fallback") == "Legacy fallback")

        val unknownRoute = LocalWifiPresentation.state(true, true, false, "")
        check(unknownRoute.latestRouteLabel == "No successful Mac route recorded yet")

        check(AssistantNamePolicy.normalize("  Ada  ") == "Ada")
        check(AssistantNamePolicy.normalize("   ") == null)
        check(AssistantNamePolicy.normalize("a".repeat(25)) == null)
        check(AssistantNamePolicy.normalize("a\u0000b") == null)
        check(AssistantNamePolicy.normalize("கவி") == "கவி")
        println("Local Wi-Fi and assistant-name presentation checks passed.")
    }
}
