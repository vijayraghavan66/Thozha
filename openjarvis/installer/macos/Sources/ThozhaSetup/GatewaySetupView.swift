import SwiftUI
import ThozhaInstallerCore

struct GatewaySetupView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var locationPermission = GatewayLocationPermissionRequester()
    @State private var snapshot: GatewayWiFiSnapshot?
    @State private var configuration: GatewayNetworkConfiguration?
    @State private var runtime: GatewayRuntime?
    @State private var enablePlan: GatewayEnablePlan?
    @State private var disablePlan: GatewayDisablePlan?
    @State private var activeEnablePlan: GatewayEnablePlan?
    @State private var activeDisablePlan: GatewayDisablePlan?
    @State private var showEnableConfirmation = false
    @State private var showDisableConfirmation = false
    @State private var ownerOptIn = false
    @State private var proposedInstallationID = UUID()
    @State private var isChecking = false
    @State private var isRequestingLocation = false
    @State private var errorMessage: String?
    @State private var statusMessage: String?
    private let wifiProvider = CoreWLANWiFiSnapshotProvider()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Private Wi-Fi gateway").font(.system(size: 22, weight: .semibold))
                    Text("A restricted local gateway for paired phones. It never exposes the raw OpenJarvis API.")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
            }

            GroupBox("Network checks") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Thozha detects the active Wi-Fi interface, current SSID and private IPv4 address automatically. It never asks you to type an IP or network name.")
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                    if let configuration {
                        LabeledContent("Wi-Fi interface", value: configuration.interfaceName)
                        LabeledContent("Current SSID", value: configuration.allowedSSID)
                        LabeledContent("Private IPv4", value: configuration.bindIPv4)
                        LabeledContent("TLS hostname", value: "https://\(configuration.hostname):8443")
                    } else if let snapshot, snapshot.ssid == nil {
                        Label("macOS is withholding the SSID. Gateway setup remains blocked.", systemImage: "lock.fill")
                            .font(.callout).foregroundStyle(.orange)
                    }
                    Button(isChecking ? "Checking Wi-Fi…" : "Check Wi-Fi and runtime") { Task { await checkReadiness() } }
                        .disabled(isChecking || isRequestingLocation)
                }.padding(.vertical, 4)
            }

            if snapshot?.interfaceName != nil && snapshot?.ssid == nil && locationPermission.authorization == .notDetermined {
                GroupBox("Location Services permission") {
                    Text("macOS may protect the Wi-Fi network name as location-related data. Thozha needs the current SSID only to compare it exactly before a gateway can listen. No GPS location is requested or stored, and setup stays blocked if the name remains unavailable.")
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                    Button(isRequestingLocation ? "Waiting for macOS…" : "Allow SSID verification") {
                        Task { await requestLocationPermission() }
                    }
                    .disabled(isRequestingLocation)
                    .padding(.top, 6)
                }
            } else if snapshot?.ssid == nil && (locationPermission.authorization == .denied || locationPermission.authorization == .restricted) {
                Label("Location Services is denied or restricted. Thozha will not use a manually entered SSID as a substitute.", systemImage: "exclamationmark.shield")
                    .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }

            if let errorMessage {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
            if let statusMessage {
                Label(statusMessage, systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(.green)
            }

            if let configuration, let runtime {
                GroupBox("Owner opt-in and startup plan") {
                    Toggle("Use \(configuration.allowedSSID) as the exact Wi-Fi allowlist for this gateway", isOn: $ownerOptIn)
                        .toggleStyle(.checkbox)
                    Text("The LaunchAgent contains only the gateway opt-in, interface, SSID allowlist, private bind address, fixed port, TLS hostname, verified interpreter paths and log paths. The SSID is stored in a user-readable-only plist; no API keys, pairing credentials or tokens are included.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).padding(.top, 6)
                    Button("Review per-user enable plan") { makeEnablePlan(configuration, runtime) }
                        .disabled(!ownerOptIn)
                        .padding(.top, 8)
                }
            }

            if let enablePlan {
                GroupBox("Enable plan preview — not applied") {
                    Text("Destination: \(enablePlan.plistURL.path)").font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    Text("Plist mode: 0600 (owner-only). The SSID is a local network identifier, not a password; it is stored only in this user-scoped file.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    Text("Review the complete plist. Enabling writes it owner-only (0600) and loads only this user's job after the explicit confirmation below.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    ScrollView { Text(enablePlan.plistXML).font(.system(.caption2, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                        .frame(height: 170)
                        .padding(8).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
                    Text("Planned load: \(enablePlan.bootstrapCommand.joined(separator: " "))")
                        .font(.system(.caption2, design: .monospaced)).textSelection(.enabled)
                    Button("Enable gateway") {
                        activeEnablePlan = enablePlan
                        showEnableConfirmation = true
                    }
                    .buttonStyle(.borderedProminent)
                }
            }

            GroupBox("Disable and removal plan") {
                Button("Preview disable/removal") { makeDisablePlan() }
                if let disablePlan {
                    Text(disablePlan.bootoutCommand.joined(separator: " ")).font(.system(.caption2, design: .monospaced)).textSelection(.enabled)
                    Text(disablePlan.removalDescription).font(.caption).fixedSize(horizontal: false, vertical: true)
                    Text(disablePlan.preservedStateDescription).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    Button("Disable gateway") {
                        activeDisablePlan = disablePlan
                        showDisableConfirmation = true
                    }
                    .buttonStyle(.bordered)
                }
            }

            Spacer(minLength: 0)
        }
        .padding(22)
        .frame(width: 640, height: 780)
        .onChange(of: ownerOptIn) { _, optedIn in
            if !optedIn { enablePlan = nil }
        }
        .confirmationDialog("Enable Thozha's private Wi-Fi gateway?", isPresented: $showEnableConfirmation, titleVisibility: .visible) {
            Button("Enable this user's gateway") {
                guard let plan = activeEnablePlan else { return }
                Task { await applyEnable(plan) }
            }
            Button("Cancel", role: .cancel) { activeEnablePlan = nil }
        } message: {
            if let activeEnablePlan {
                Text("This writes \(activeEnablePlan.plistURL.path) with mode 0600, then loads the per-user gateway job. It is bound only to the verified private Wi-Fi address and SSID shown in the reviewed plist. No router or firewall changes are made.")
            } else { Text("Review the complete plist before enabling.") }
        }
        .confirmationDialog("Disable Thozha's private Wi-Fi gateway?", isPresented: $showDisableConfirmation, titleVisibility: .visible) {
            Button("Stop and remove this user's gateway", role: .destructive) {
                guard let plan = activeDisablePlan else { return }
                Task { await applyDisable(plan) }
            }
            Button("Cancel", role: .cancel) { activeDisablePlan = nil }
        } message: {
            if let activeDisablePlan {
                Text("This stops only \(GatewayLaunchAgentPlanner.label) for the current macOS user and removes \(activeDisablePlan.plistURL.path). Pairing, task history, credentials, TLS identity and gateway logs are preserved.")
            } else { Text("Review the disable plan before stopping the gateway.") }
        }
    }

    @MainActor private func checkReadiness() async {
        isChecking = true
        errorMessage = nil
        statusMessage = nil
        enablePlan = nil
        configuration = nil
        let detectedSnapshot = wifiProvider.snapshot()
        snapshot = detectedSnapshot
        switch GatewayPreflight.evaluate(snapshot: detectedSnapshot, installationID: proposedInstallationID) {
        case .failure(let failure):
            errorMessage = failure.localizedDescription
        case .success(let detectedConfiguration):
            do {
                let requiredBundle = URL(fileURLWithPath: "/Applications/Thozha Setup.app", isDirectory: true).standardizedFileURL
                guard Bundle.main.bundleURL.standardizedFileURL == requiredBundle else {
                    throw GatewayRuntimeError.setupAppMustBeInstalled
                }
                let shutdownSupervisor = Bundle.main.bundleURL
                    .appendingPathComponent("Contents/Helpers/ThozhaGatewaySupervisor")
                let detectedRuntime = try await Task.detached(priority: .utility) {
                    try GatewayRuntimeDetector().detect(shutdownSupervisor: shutdownSupervisor)
                }.value
                configuration = detectedConfiguration
                runtime = detectedRuntime
                statusMessage = "Wi-Fi, exact SSID and isolated gateway runtime checks passed. No gateway was started."
            } catch let error as LocalizedError {
                errorMessage = error.errorDescription ?? "The OpenJarvis runtime could not be verified."
            } catch {
                errorMessage = "The OpenJarvis runtime could not be verified."
            }
        }
        isChecking = false
    }

    @MainActor private func requestLocationPermission() async {
        isRequestingLocation = true
        errorMessage = nil
        _ = await locationPermission.requestWhenInUseIfNeeded()
        isRequestingLocation = false
        await checkReadiness()
    }

    private func makeEnablePlan(_ configuration: GatewayNetworkConfiguration, _ runtime: GatewayRuntime) {
        guard ownerOptIn else { return }
        do {
            let freshSnapshot = wifiProvider.snapshot()
            let freshConfiguration = try GatewayPreflight.evaluate(snapshot: freshSnapshot, installationID: proposedInstallationID).get()
            guard freshConfiguration.interfaceName == configuration.interfaceName,
                  freshConfiguration.allowedSSID == configuration.allowedSSID,
                  freshConfiguration.bindIPv4 == configuration.bindIPv4 else {
                throw GatewayPreflightError.networkChanged
            }
            // Stable host naming uses the separate staging ID; it is not a credential.
            let directory = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/Thozha/Installer-Staging", isDirectory: true)
            let identity = try StagingIdentityStore.createOrLoad(in: directory)
            let finalConfiguration = GatewayNetworkConfiguration(
                interfaceName: freshConfiguration.interfaceName,
                allowedSSID: freshConfiguration.allowedSSID,
                bindIPv4: freshConfiguration.bindIPv4,
                hostname: "thozha-\(identity.id.uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(8)).local"
            )
            self.configuration = finalConfiguration
            enablePlan = try GatewayLaunchAgentPlanner.enablePlan(
                configuration: finalConfiguration, runtime: runtime,
                homeDirectory: FileManager.default.homeDirectoryForCurrentUser,
                userID: getuid(), ownerOptIn: true
            )
            errorMessage = nil
        } catch let error as LocalizedError {
            errorMessage = error.errorDescription ?? "The enable plan could not be created."
        } catch { errorMessage = "The enable plan could not be created." }
    }

    private func makeDisablePlan() {
        do {
            disablePlan = try GatewayLaunchAgentPlanner.disablePlan(
                homeDirectory: FileManager.default.homeDirectoryForCurrentUser, userID: getuid()
            )
            errorMessage = nil
        } catch let error as LocalizedError {
            errorMessage = error.errorDescription ?? "The disable plan could not be created."
        } catch { errorMessage = "The disable plan could not be created." }
    }

    @MainActor private func applyEnable(_ plan: GatewayEnablePlan) async {
        guard ownerOptIn, let original = configuration else { return }
        errorMessage = nil
        statusMessage = nil
        isChecking = true
        let freshSnapshot = wifiProvider.snapshot()
        let freshConfiguration = try? GatewayPreflight.evaluate(snapshot: freshSnapshot, installationID: proposedInstallationID).get()
        guard let freshConfiguration,
              freshConfiguration.interfaceName == original.interfaceName,
              freshConfiguration.allowedSSID == original.allowedSSID,
              freshConfiguration.bindIPv4 == original.bindIPv4 else {
            errorMessage = GatewayPreflightError.networkChanged.localizedDescription
            isChecking = false
            activeEnablePlan = nil
            return
        }
        do {
            try await Task.detached(priority: .utility) {
                try GatewayLaunchAgentManager().enable(plan, userConfirmed: true)
            }.value
            statusMessage = "The per-user gateway LaunchAgent was loaded. It will start at login and stop itself if the SSID or bind IP changes."
            enablePlan = nil
        } catch let error as LocalizedError {
            errorMessage = error.errorDescription ?? "The gateway did not start. Check the LaunchAgent status."
        } catch {
            errorMessage = "The gateway did not start. Check the LaunchAgent status."
        }
        isChecking = false
        activeEnablePlan = nil
    }

    @MainActor private func applyDisable(_ plan: GatewayDisablePlan) async {
        errorMessage = nil
        statusMessage = nil
        isChecking = true
        do {
            try await Task.detached(priority: .utility) {
                try GatewayLaunchAgentManager().disable(plan, userConfirmed: true)
            }.value
            statusMessage = "The gateway job is stopped and its LaunchAgent plist was removed. Pairing, TLS identity and logs were preserved."
            enablePlan = nil
            disablePlan = nil
        } catch let error as LocalizedError {
            errorMessage = error.errorDescription ?? "The gateway could not be disabled safely. The plist was preserved."
        } catch {
            errorMessage = "The gateway could not be disabled safely. The plist was preserved."
        }
        isChecking = false
        activeDisablePlan = nil
    }
}
