# Thozha macOS setup prototype

This is a native SwiftUI setup-assistant prototype with a local staging package. It is intentionally **not a consumer installer** yet: it packages only the setup app and does not install OpenJarvis, create a LaunchAgent, download a VM image, or enroll a device. That boundary prevents this prototype from replacing the working development environment.

## Build and test

From `installer/macos`:

```sh
swift test
swift build -c release
./package-staging.sh "$HOME/Documents/Jarvis-Lab/installer-staging"
```

The script creates a locally testable app and an unsigned `.pkg` at the specified staging path. The app is ad-hoc signed for local inspection; the installer package is not Developer ID signed or notarized. Do not distribute it as a trusted consumer installer.

The staging app:

- checks macOS 14+, Apple silicon, memory, free disk, `uv`, and QEMU ARM64 availability;
- treats missing QEMU as a hard stop for the isolated-worker path;
- probes only `http://127.0.0.1:8000/health` after the user presses the read-only health-check button, and never starts/restarts a service;
- creates a new random staging installation ID only after a deliberate button press, under `~/Library/Application Support/Thozha/Installer-Staging`, with a private directory/file mode;
- does not read, copy, or change existing Jarvis credentials, pairing, task state, LaunchAgents, or VM files.

No internet download, package-manager invocation, privileged operation, firewall change, TCC request, or network listener is performed.

## Remaining installer work

Before this can be called a real product installer, add reviewed, version-pinned backend packaging and an isolated-worker provisioning flow with signed/checksummed artifacts, explicit download and disk-space consent, guest boundary tests, controlled user LaunchAgent configuration, and a complete rollback/uninstall path. Verify those in a disposable macOS account/VM. Add Developer ID signing and notarization through the authorized release account. This prototype does not satisfy fresh-install, worker health, install/uninstall, or consumer distribution acceptance.
