# Thozha Android

Native Kotlin and Jetpack Compose companion for the Mac-side OpenJarvis supervisor. Thozha is local-first: the raw Mac API stays on loopback, and the phone connects through a narrow HTTPS gateway on the same private network. The existing USB/ADB-reverse route remains a recovery path. Telegram is optional remote access.

**Current state:** The signed local-Wi-Fi invitation parser, TLS pin validation, local pairing client and four-destination UI are implemented on the Android branch. The matching Mac gateway v2 implementation is isolated for review and has not yet been integrated into the canonical OpenJarvis checkout or enabled in production. Do not claim first-pair Wi-Fi is end-to-end until those server changes are integrated and a disposable pairing test passes.

## Build and install

Build the debug APK:

    ./gradlew :app:assembleDebug

APK output:

    app/build/outputs/apk/debug/app-debug.apk

Install in place only after checking package identity and signer:

    adb install -r app/build/outputs/apk/debug/app-debug.apk

Do not clear app data or uninstall the app to update it. The package remains `com.jarvis.companion.debug` with the existing signing identity.

## Main destinations

- **Home:** assistant overview, real connection state, active task/progress summaries, and request composer.
- **Chat:** persistent assistant conversation with actual task results.
- **Tasks:** Activity and exact-operation approval review.
- **Devices:** paired Mac status, local Wi-Fi state, and optional Telegram remote access.
- **Settings:** appearance, accessibility, notifications, local model, privacy, and advanced setup.

Progress is rendered only from persisted task events. The app does not synthesize token streaming or percentages.

## Pairing protocol

The Mac creates invitations through its loopback owner UI. The QR contains no persistent device token or Mac API key. The phone signs the short-lived challenge with a device key held in Android Keystore. Scanning alone never grants access: the Mac's local pending-device screen must confirm the exact device name and public-key fingerprint before the phone can claim its one-time expiring credential.

- **v1 — USB compatibility:** signed URL must be loopback (`127.0.0.1`/`localhost`) and uses the existing USB reverse path. The app and server retain v1 compatibility.
- **v2 — direct local Wi-Fi:** the Mac signs `server_url=https://<mac-name>.local:8443`, the private gateway address, port, exact TLS certificate SHA-256 pin, server P-256 identity, invitation challenge and expiry. The app validates all signed fields and the private address before connecting. It pins the HTTPS certificate and exact DNS SAN before sending the phone's signed pairing proof. mDNS is an optimization; the invitation's signed private IP supports initial connection when DNS-SD is unavailable.
- The opt-in Wi-Fi gateway exposes only invitation-ID-scoped completion, status and claim routes before pairing. It does not expose invitation creation, pending-device enumeration, Mac decisions, raw API routes, documentation or arbitrary proxy paths.
- The Mac retains the final local Confirm/Reject step. Only the approved one-time claim can return a device token; token and pins are encrypted with Android Keystore.

The v2 backend is not yet live or enabled. See the current checkpoint in `~/Documents/Jarvis-Lab/PROGRESS.md` before testing new-device pairing.

## Local and optional remote operation

- **Local mode:** the device-to-Mac connection does not require an internet relay, paid service, Telegram, port forwarding, router credentials, USB debugging, or manual IP entry. Both devices must be reachable on the same local network and the Mac gateway must be explicitly enabled for the selected interface/SSID. The configured AI engine may itself need internet access to generate answers.
- The Mac raw API remains `127.0.0.1:8000`. The restricted companion gateway binds only to a configured private interface address and serves HTTPS; it is never Tailscale Funnel or a public listener.
- Android targeting API 37+ requests `ACCESS_LOCAL_NETWORK` only when the user chooses a Wi-Fi operation. Camera permission is requested only when the user taps Scan. The QR decoder runs on-device; camera frames are not saved or uploaded. If local-network access is denied, USB remains available.
- **Telegram:** optional remote access. A fresh install defaults to local-only; an existing authenticated Telegram session is preserved as the prior opt-in until the user disables it. Turning it off stops the transport without deleting TDLib sign-in data or its database. Telegram bots are not Secret Chats; privileged actions still require native approval.
- The existing Tailscale identity-migration code remains a separate, manually requested option. It is not required for local mode and does not configure VPN or relay services.

## Verification

Focused Android protocol and identity checks:

    ./gradlew :app:verifyPairingInvitationProtocol :app:verifyMigrationIdentity :app:verifyLocalGatewayRouting

Other checks include `:app:verifyPresentation`, `:app:verifyLinkProtocol`, `:app:testDebugUnitTest`, and `:app:assembleDebug`. The current project has JVM assertion harnesses; the Gradle JUnit task may report zero discovered JUnit cases. See `PROGRESS.md` for actual test counts and device evidence.

The Mac supervisor remains authoritative for pairing expiry, replay protection, device revocation, task ownership, approval digest matching and privileged tool authorization.
