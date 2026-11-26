import SwiftUI
import ThozhaInstallerCore
import Darwin

@main
struct ThozhaSetupApp: App {
    init() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        guard arguments.first == "--ssid" else { return }
        guard let output = GatewaySSIDHelper.output(
            arguments: arguments,
            reader: CoreWLANCurrentWiFiSSIDReader()
        ) else {
            // Fail closed and do not disclose the network name or denial details.
            exit(78)
        }
        FileHandle.standardOutput.write(Data(output.utf8))
        exit(0)
    }

    var body: some Scene { WindowGroup { SetupView() }.windowResizability(.contentSize) }
}

struct SetupView: View {
    @State private var host = HostInspector.snapshot()
    @State private var serviceState = "Not checked"
    @State private var identityMessage = "No new installation identity created"
    @State private var isChecking = false
    @State private var showingPhonePairing = false
    @State private var showingGatewaySetup = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                Image(systemName: "circle.hexagongrid.fill").font(.system(size: 32)).foregroundStyle(.teal)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Thozha · Setup").font(.system(size: 24, weight: .semibold))
                    Text("தோழா · Your assistant, set up safely").font(.subheadline).foregroundStyle(.secondary)
                }
            }
            Text("This staging build does not import an existing Jarvis identity, stop or replace a service, or configure a network listener. Isolated worker provisioning is a separate explicit step.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            GroupBox("Mac requirements") {
                VStack(alignment: .leading, spacing: 9) {
                    ForEach(Array(Preflight.evaluate(host).enumerated()), id: \.offset) { _, item in
                        HStack(alignment: .top, spacing: 9) {
                            Image(systemName: item.state == .pass ? "checkmark.circle.fill" : item.state == .warning ? "exclamationmark.triangle.fill" : "xmark.octagon.fill")
                                .foregroundStyle(item.state == .pass ? .green : item.state == .warning ? .orange : .red)
                            VStack(alignment: .leading, spacing: 2) { Text(item.title).font(.subheadline.weight(.medium)); Text(item.detail).font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                }.padding(.vertical, 4)
            }
            GroupBox("Existing local service") {
                HStack { Text("127.0.0.1:8000"); Spacer(); Text(serviceState).foregroundStyle(.secondary) }
                .font(.callout).padding(.vertical, 4)
                Text("Health check is read-only. It does not start or restart OpenJarvis.").font(.caption).foregroundStyle(.secondary)
            }
            GroupBox("Fresh staging identity") {
                Text(identityMessage).font(.callout).foregroundStyle(.secondary)
                Button("Create new staging identity") { createIdentity() }
                    .disabled(!Preflight.canContinue(host))
                Text("Stores a random installation ID under this macOS user's Application Support. It is not a device credential and is separate from existing Jarvis pairing.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            GroupBox("Phone pairing") {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Connect an Android phone").font(.subheadline.weight(.medium))
                        Text("Create a signed, short-lived pairing QR from the local OpenJarvis service.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Connect Phone") { showingPhonePairing = true }
                }.padding(.vertical, 4)
            }
            GroupBox("Private Wi-Fi gateway") {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Prepare local-network startup").font(.subheadline.weight(.medium))
                        Text("Detect Wi-Fi, verify the exact SSID and review a per-user LaunchAgent plan.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Review gateway setup") { showingGatewaySetup = true }
                }.padding(.vertical, 4)
            }
            HStack {
                Button(isChecking ? "Checking…" : "Run read-only health check") { Task { await healthCheck() } }.disabled(isChecking)
                Spacer()
                Text("Setup assistant prototype · not a consumer installer").font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .padding(24).frame(width: 570)
        .sheet(isPresented: $showingPhonePairing) { ConnectPhoneView().frame(minWidth: 530, minHeight: 650) }
        .sheet(isPresented: $showingGatewaySetup) { GatewaySetupView().frame(minWidth: 620, minHeight: 760) }
    }

    private func createIdentity() {
        do {
            let directory = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/Thozha/Installer-Staging", isDirectory: true)
            let identity = try StagingIdentityStore.createOrLoad(in: directory)
            identityMessage = "Staging ID created: \(identity.id.uuidString)"
        } catch { identityMessage = "Could not create staging ID: \(error.localizedDescription)" }
    }

    @MainActor private func healthCheck() async {
        isChecking = true
        serviceState = await LocalHealth.check() ? "Healthy" : "Not reachable"
        isChecking = false
    }
}
