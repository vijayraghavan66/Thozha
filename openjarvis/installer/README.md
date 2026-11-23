# Thozha macOS setup prototype

This is a native SwiftUI setup-assistant prototype with a local staging package. It is intentionally **not a consumer installer** yet: it does not install/replace OpenJarvis, provision a VM image, or enroll a device. It can create a signed pairing QR from an existing loopback service and, only after the owner reviews a per-user plan and confirms, enable or remove a separate restricted local Wi-Fi gateway LaunchAgent. Nothing starts on app launch.

## Build and test

From `installer/macos`:

```sh
swift test
swift build -c release
./package-staging.sh "$HOME/Documents/Jarvis-Lab/installer-staging"
Tests/test-package-staging-safety.sh
```

The script requires a fresh staging directory whose parent already exists. It refuses existing output directories and symlinks, never recursively deletes a caller-supplied path, and uses no-clobber moves for final artifacts. The app is ad-hoc signed for local inspection. On this host, `pkgbuild` serializes macOS-managed provenance metadata as AppleDouble entries, so the script fails closed and does not publish a `.pkg`; a package can be emitted only when the payload check finds no such entries. Even then, a package is not Developer ID signed or notarized. Do not distribute it as a trusted consumer installer.

The staging app:

- checks macOS 14+, Apple silicon, memory, free disk, `uv`, and QEMU ARM64 availability;
- treats missing QEMU as a hard stop for the isolated-worker path;
- probes only `http://127.0.0.1:8000/health` after the user presses the read-only health-check button; it never starts or restarts the OpenJarvis API/Link/Telegram/VM services;
- creates v1 loopback or opt-in v2 local-Wi-Fi QR invitations only after a user action, validates the Mac signature before displaying a QR, and requires an explicit local confirmation for every enrolled phone;
- requests macOS Location Services only after an explicit user tap when CoreWLAN withholds the current SSID. If SSID or the private interface address cannot be verified, gateway setup remains blocked;
- can generate a mode-0600 per-user gateway LaunchAgent plan and display its exact plist before a separate confirmation. Enabling affects only that gateway job; disabling boots out and removes only its own plist. The live LaunchAgent was not installed or started in development;
- creates a new random staging installation ID only after a deliberate button press, under `~/Library/Application Support/Thozha/Installer-Staging`, with a private directory/file mode;
- does not read, copy, or change existing Jarvis credentials, pairing, task state, OpenJarvis/Link/Telegram LaunchAgents, or VM files. Any new gateway job is separately named, user-scoped and explicitly opt-in.

No internet download, package-manager invocation, privileged operation, firewall change, TCC request, or network listener was performed during these tests.

`Tests/test-package-staging-safety.sh` verifies that existing output directories and symlink targets are left untouched. A package-payload check also refuses AppleDouble paths; the current Mac's `pkgbuild` emits such paths from OS-managed provenance xattrs, which remains a packaging blocker.

## Remaining installer work

Before this can be called a real product installer, add reviewed, version-pinned backend packaging and an isolated-worker provisioning flow with signed/checksummed artifacts, explicit download and disk-space consent, guest boundary tests, controlled user LaunchAgent configuration, and a complete rollback/uninstall path. Verify those in a disposable macOS account/VM. Add Developer ID signing and notarization through the authorized release account. This prototype does not satisfy fresh-install, worker health, install/uninstall, or consumer distribution acceptance.
