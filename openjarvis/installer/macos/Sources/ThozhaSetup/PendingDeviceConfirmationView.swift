import SwiftUI
import ThozhaInstallerCore

struct PendingDeviceConfirmationView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var requests: [PendingDeviceRequest] = []
    @State private var isRefreshing = false
    @State private var refreshInFlight = false
    @State private var actingOnID: String?
    @State private var errorMessage: String?
    @State private var statusMessage: String?
    @State private var lastUpdated: Date?
    private let service = PendingDeviceConfirmationService()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Confirm a phone").font(.system(size: 22, weight: .semibold))
                    Text("Only confirm a device you can physically identify and intended to pair.")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
            }

            HStack {
                Label(isRefreshing ? "Refreshing…" : "Read-only pending requests", systemImage: "arrow.triangle.2.circlepath")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                if let lastUpdated { Text("Updated \(lastUpdated.formatted(date: .omitted, time: .standard))").font(.caption2).foregroundStyle(.tertiary) }
                Button("Refresh") { Task { await refresh(showSpinner: true) } }.disabled(refreshInFlight || actingOnID != nil)
            }

            if let statusMessage {
                Label(statusMessage, systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.callout)
            }
            if let errorMessage {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange).font(.callout).fixedSize(horizontal: false, vertical: true)
            }

            if requests.isEmpty && isRefreshing {
                VStack(spacing: 10) { ProgressView(); Text("Loading pending phone requests…").foregroundStyle(.secondary) }
                    .frame(maxWidth: .infinity, minHeight: 280)
            } else if requests.isEmpty {
                ContentUnavailableView("No pending devices", systemImage: "iphone.gen3", description: Text("This screen checks the local Mac queue every five seconds while open."))
                    .frame(maxWidth: .infinity, minHeight: 280)
            } else {
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(requests) { request in requestCard(request) }
                    }.padding(.vertical, 4)
                }
            }

            Text("Confirming authorizes this device to finish the existing pairing flow; it does not bypass device-token claim or any task approval policy. Reject prevents that request from completing.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(22)
        .frame(width: 640, height: 660)
        .task { await pollWhileVisible() }
    }

    private func requestCard(_ request: PendingDeviceRequest) -> some View {
        let expired = request.isExpired()
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(request.deviceName).font(.headline).textSelection(.enabled)
                Spacer()
                Text(expired ? "Expired" : "Pending confirmation")
                    .font(.caption.weight(.medium)).foregroundStyle(expired ? Color.gray : Color.orange)
            }
            LabeledContent("Request received", value: request.requestedAt.formatted(date: .abbreviated, time: .standard))
            LabeledContent("Expires", value: request.expiresAt.formatted(date: .abbreviated, time: .standard))
            VStack(alignment: .leading, spacing: 3) {
                Text("Phone public-key fingerprint").font(.caption).foregroundStyle(.secondary)
                Text(request.publicKeyFingerprint).font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Reject", role: .destructive) { Task { await decide(request, decision: .reject) } }
                    .disabled(expired || actingOnID != nil || refreshInFlight)
                Button(actingOnID == request.id ? "Recording…" : "Confirm") { Task { await decide(request, decision: .confirm) } }
                    .buttonStyle(.borderedProminent)
                    .disabled(expired || actingOnID != nil || refreshInFlight)
            }
        }
        .padding(14)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.quaternary, lineWidth: 1))
    }

    @MainActor private func pollWhileVisible() async {
        await refresh(showSpinner: true)
        while !Task.isCancelled {
            do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
            if Task.isCancelled { return }
            await refresh(showSpinner: false)
        }
    }

    @MainActor private func refresh(showSpinner: Bool) async {
        guard actingOnID == nil, !refreshInFlight else { return }
        refreshInFlight = true
        if showSpinner { isRefreshing = true }
        do {
            requests = try await service.listPending()
            lastUpdated = Date()
            errorMessage = nil
        } catch let error as LocalizedError {
            errorMessage = error.errorDescription ?? "Pending requests could not be refreshed."
        } catch {
            errorMessage = "Pending requests could not be refreshed."
        }
        isRefreshing = false
        refreshInFlight = false
    }

    @MainActor private func decide(_ displayedRequest: PendingDeviceRequest, decision: PendingDeviceDecision) async {
        guard actingOnID == nil, !displayedRequest.isExpired() else { return }
        actingOnID = displayedRequest.id
        errorMessage = nil
        statusMessage = nil
        do {
            // Re-read immediately after the explicit button tap. A stale row
            // never authorizes a POST; the backend still enforces expiry/state.
            let latest = try await service.listPending()
            guard let current = latest.first(where: { $0.id == displayedRequest.id }),
                  current.deviceName == displayedRequest.deviceName,
                  current.publicKeyFingerprint == displayedRequest.publicKeyFingerprint,
                  current.expiresAt == displayedRequest.expiresAt else {
                requests = latest
                throw PendingDeviceError.changed
            }
            guard !current.isExpired() else {
                requests = latest
                throw PendingDeviceError.expired
            }

            let result = try await service.decide(current, decision: decision)
            switch result {
            case .confirmed: statusMessage = "Device confirmed. It must still claim its one-time pairing token."
            case .rejected: statusMessage = "Device request rejected. It cannot claim a pairing token."
            case .expired: statusMessage = "The request expired before the server could apply the decision."
            }
            requests = try await service.listPending()
            lastUpdated = Date()
        } catch let error as LocalizedError {
            errorMessage = error.errorDescription ?? "The device decision was not recorded."
            if let refreshed = try? await service.listPending() { requests = refreshed; lastUpdated = Date() }
        } catch {
            errorMessage = "The device decision was not recorded. Refresh the list and check its status."
        }
        actingOnID = nil
    }
}
