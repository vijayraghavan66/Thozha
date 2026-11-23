import SwiftUI
import ThozhaInstallerCore

struct PairedDevicesView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var devices: [PairedDevice] = []
    @State private var isLoading = false
    @State private var actionInFlight = false
    @State private var selectedForRevocation: PairedDevice?
    @State private var showRevokeConfirmation = false
    @State private var errorMessage: String?
    @State private var statusMessage: String?
    @State private var lastUpdated: Date?
    private let service = PairedDeviceManagementService()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Paired phones").font(.system(size: 22, weight: .semibold))
                    Text("Review safe device metadata and revoke a phone only after an explicit confirmation.")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
            }

            HStack {
                Text("Read-only list from this Mac").font(.caption).foregroundStyle(.secondary)
                Spacer()
                if let lastUpdated { Text("Updated \(lastUpdated.formatted(date: .omitted, time: .standard))").font(.caption2).foregroundStyle(.tertiary) }
                Button("Refresh") { Task { await refresh() } }.disabled(isLoading || actionInFlight)
            }

            if let statusMessage {
                Label(statusMessage, systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(.green)
            }
            if let errorMessage {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }

            if devices.isEmpty && isLoading {
                VStack(spacing: 10) { ProgressView(); Text("Loading paired phones…").foregroundStyle(.secondary) }
                    .frame(maxWidth: .infinity, minHeight: 320)
            } else if devices.isEmpty {
                ContentUnavailableView("No paired phones", systemImage: "iphone.gen3", description: Text("The local Mac pairing database contains no devices, or refresh to check again."))
                    .frame(maxWidth: .infinity, minHeight: 320)
            } else {
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(devices) { device in deviceCard(device) }
                    }.padding(.vertical, 4)
                }
            }

            Text("Revocation disables the device token. A 503 may mean access is already revoked while notification-token cleanup remains pending; the UI will report that separately.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(22)
        .frame(width: 660, height: 690)
        .task { await refresh() }
        .confirmationDialog("Revoke this paired phone?", isPresented: $showRevokeConfirmation, titleVisibility: .visible) {
            Button("Revoke device access", role: .destructive) {
                guard let device = selectedForRevocation else { return }
                Task { await revoke(device) }
            }
            Button("Cancel", role: .cancel) { selectedForRevocation = nil }
        } message: {
            if let device = selectedForRevocation {
                Text("Revoke \(device.name) (\(device.id)) with fingerprint \(device.publicKeyFingerprint)? This invalidates its device token. Pairing cannot be restored by dismissing this dialog.")
            }
        }
    }

    private func deviceCard(_ device: PairedDevice) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Text(device.name).font(.headline).textSelection(.enabled)
                Spacer()
                Text(device.status.rawValue.capitalized)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(device.status == .active ? Color.green : Color.secondary)
            }
            LabeledContent("Device ID", value: device.id).font(.system(.caption, design: .monospaced))
            VStack(alignment: .leading, spacing: 3) {
                Text("Public-key fingerprint").font(.caption).foregroundStyle(.secondary)
                Text(device.publicKeyFingerprint).font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            LabeledContent("Paired", value: device.createdAt.formatted(date: .abbreviated, time: .standard))
            LabeledContent("Expires", value: device.expiresAt.formatted(date: .abbreviated, time: .standard))
            if let revokedAt = device.revokedAt {
                LabeledContent("Revoked", value: revokedAt.formatted(date: .abbreviated, time: .standard))
            }
            if device.status == .active {
                HStack {
                    Spacer()
                    Button("Revoke") {
                        selectedForRevocation = device
                        showRevokeConfirmation = true
                    }
                    .buttonStyle(.bordered)
                    .tint(.red)
                    .disabled(isLoading || actionInFlight)
                    .accessibilityLabel("Review revocation for \(device.name)")
                }
            }
        }
        .padding(14)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.quaternary, lineWidth: 1))
    }

    @MainActor private func refresh() async {
        guard !isLoading, !actionInFlight else { return }
        isLoading = true
        errorMessage = nil
        do {
            devices = try await service.listDevices()
            lastUpdated = Date()
        } catch let error as LocalizedError {
            errorMessage = error.errorDescription ?? "Paired-device metadata could not be verified."
        } catch {
            errorMessage = "Paired-device metadata could not be verified."
        }
        isLoading = false
    }

    @MainActor private func revoke(_ selected: PairedDevice) async {
        guard !actionInFlight else { return }
        actionInFlight = true
        errorMessage = nil
        statusMessage = nil
        do {
            try await service.revoke(selected)
            statusMessage = "Device access was revoked. Refresh confirms its server-recorded status."
            devices = try await service.listDevices()
            lastUpdated = Date()
        } catch PairedDeviceManagementError.accessRevokedCleanupPending {
            errorMessage = PairedDeviceManagementError.accessRevokedCleanupPending.localizedDescription
            if let refreshed = try? await service.listDevices() { devices = refreshed; lastUpdated = Date() }
        } catch let error as LocalizedError {
            errorMessage = error.errorDescription ?? "Device revocation did not complete. Refresh the list."
            if let refreshed = try? await service.listDevices() { devices = refreshed; lastUpdated = Date() }
        } catch {
            errorMessage = "Device revocation did not complete. Refresh the list."
        }
        selectedForRevocation = nil
        actionInFlight = false
    }
}
