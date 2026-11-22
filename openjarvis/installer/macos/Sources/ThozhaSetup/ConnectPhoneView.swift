import SwiftUI
import ThozhaInstallerCore

struct ConnectPhoneView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var invitation: PairingInvitation?
    @State private var selectedTransport: PairingTransport = .wifi
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var gatewayDisabled = false
    @State private var showRefreshConfirmation = false
    @State private var showPendingDevices = false
    private let service = PairingInvitationService()

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Connect your phone").font(.system(size: 23, weight: .semibold))
                    Text("Pairing is initiated on this Mac and confirmed on the phone.")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Pending devices") { showPendingDevices = true }
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
            }

            GroupBox("Connection security") {
                VStack(alignment: .leading, spacing: 8) {
                    Label("A short-lived invitation signed by this Mac", systemImage: "checkmark.shield")
                    Label("Server key fingerprint is shown for identity review", systemImage: "key.horizontal")
                    Label("The server identity private key never leaves OpenJarvis", systemImage: "lock.fill")
                }.font(.callout).padding(.vertical, 4)
            }

            if invitation == nil {
                GroupBox("Choose a connection") {
                    Picker("Transport", selection: $selectedTransport) {
                        ForEach(PairingTransport.allCases) { transport in
                            Text(transport.title).tag(transport)
                        }
                    }.pickerStyle(.segmented)
                    if selectedTransport == .wifi {
                        Text("Wi-Fi mode requests an explicitly signed HTTPS gateway invitation. If the gateway is disabled or unhealthy, no QR is shown; you can switch to USB / ADB.")
                            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).padding(.top, 6)
                    } else {
                        Text("USB mode creates the legacy loopback invitation. The phone needs the existing USB/ADB reverse route to reach this Mac.")
                            .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).padding(.top, 6)
                    }
                }
            }

            if let invitation {
                TimelineView(.periodic(from: .now, by: 1)) { timeline in
                    invitationCard(invitation, now: timeline.date)
                }
            } else if isLoading {
                VStack(spacing: 10) { ProgressView(); Text("Requesting a signed invitation from this Mac…").font(.callout).foregroundStyle(.secondary) }
                    .frame(maxWidth: .infinity, minHeight: 250)
            } else {
                ContentUnavailableView("No invitation created", systemImage: "qrcode", description: Text("Create a fresh QR only when the phone is ready to scan it."))
                    .frame(maxWidth: .infinity, minHeight: 250)
            }

            if let errorMessage {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange).font(.callout).fixedSize(horizontal: false, vertical: true)
                if gatewayDisabled {
                    Button("Use USB / ADB fallback") {
                        selectedTransport = .usb
                        createInvitation()
                    }.buttonStyle(.bordered)
                }
            }

            HStack {
                if invitation == nil {
                    Button(selectedTransport == .wifi ? "Create signed Wi-Fi QR" : "Create signed USB QR") { createInvitation() }
                        .buttonStyle(.borderedProminent).disabled(isLoading)
                } else {
                    Button("Refresh QR") {
                        if isInvitationActive { showRefreshConfirmation = true } else { createInvitation() }
                    }
                    .disabled(isLoading)
                    .confirmationDialog("Create another invitation?", isPresented: $showRefreshConfirmation, titleVisibility: .visible) {
                        Button("Create another signed invitation") { createInvitation() }
                        Button("Cancel", role: .cancel) { }
                    } message: {
                        Text("The current invitation remains valid until its shown expiry. The server has no revoke endpoint in this UI, so both QR codes may be usable during that overlap.")
                    }
                }
                Spacer()
                if invitation != nil { Text("No automatic retries").font(.caption).foregroundStyle(.tertiary) }
            }
        }
        .padding(24)
        .frame(width: 560, height: 690)
        .sheet(isPresented: $showPendingDevices) {
            PendingDeviceConfirmationView()
        }
    }

    @ViewBuilder private func invitationCard(_ invitation: PairingInvitation, now: Date) -> some View {
        let seconds = max(0, Int(ceil(invitation.expiresAt.timeIntervalSince(now))))
        GroupBox("Signed pairing QR") {
            HStack(alignment: .top, spacing: 20) {
                if seconds > 0 {
                    PairingQRCode(text: invitation.qrText)
                        .frame(width: 205, height: 205)
                        .accessibilityLabel("Signed Thozha device pairing QR code")
                } else {
                    VStack(spacing: 8) {
                        Image(systemName: "qrcode").font(.system(size: 62)).foregroundStyle(.secondary)
                        Text("Invitation expired").font(.headline)
                        Text("This QR can no longer be used.").font(.caption).foregroundStyle(.secondary)
                    }.frame(width: 205, height: 205)
                }
                VStack(alignment: .leading, spacing: 12) {
                    LabeledContent("Status", value: seconds > 0 ? "Expires in \(seconds)s" : "Expired")
                    LabeledContent("Expires", value: invitation.expiresAt.formatted(date: .omitted, time: .standard))
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Mac server identity").font(.caption).foregroundStyle(.secondary)
                        Text(invitation.serverFingerprint).font(.system(.caption, design: .monospaced)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        Text("QR endpoint").font(.caption).foregroundStyle(.secondary)
                        Text(invitation.serverURL.absoluteString).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.padding(.vertical, 5)
            if invitation.supportsWiFi {
                VStack(alignment: .leading, spacing: 6) {
                    LabeledContent("Signed gateway address", value: "\(invitation.gatewayAddress ?? ""):\(invitation.gatewayPort ?? 8443)")
                    VStack(alignment: .leading, spacing: 3) {
                        Text("TLS certificate pin").font(.caption).foregroundStyle(.secondary)
                        Text(invitation.gatewayCertificateSHA256 ?? "").font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    }
                    Text("The phone connects to the signed private IPv4 address and validates HTTPS using this certificate pin.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }.padding(.top, 6)
            } else {
                Label("This v1 QR uses loopback and needs USB / ADB reverse; it is not a Wi-Fi invitation.", systemImage: "wifi.slash")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).padding(.top, 6)
            }
            Text("Do not share this QR image or screenshot. It contains a one-time pairing challenge. Close only hides it; the invitation remains valid until expiry.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    private var isInvitationActive: Bool {
        guard let invitation else { return false }
        return invitation.expiresAt > Date()
    }

    private func createInvitation() {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        gatewayDisabled = false
        Task {
            do {
                let created = try await service.createInvitation(transport: selectedTransport)
                await MainActor.run {
                    invitation = created
                    isLoading = false
                }
            } catch let error as LocalizedError {
                await MainActor.run {
                    errorMessage = error.errorDescription ?? "The pairing invitation could not be created."
                    if error as? PairingInvitationError == .unavailable {
                        errorMessage! += " If the request timed out, wait for the five-minute invitation window to pass before trying again; the Mac may have created it before the connection failed."
                    }
                    gatewayDisabled = error as? PairingInvitationError == .gatewayDisabled
                    isLoading = false
                }
            } catch {
                await MainActor.run {
                    errorMessage = "The invitation could not be verified. No QR was displayed."
                    isLoading = false
                }
            }
        }
    }
}

private struct PairingQRCode: View {
    let text: String

    var body: some View {
        Group {
            if let cgImage = PairingQRCodeGenerator.image(payload: text) {
                Image(decorative: cgImage, scale: 1)
                    .interpolation(.none)
                    .resizable().scaledToFit()
                    .padding(10).background(.white, in: RoundedRectangle(cornerRadius: 12))
            } else {
                ContentUnavailableView("QR unavailable", systemImage: "qrcode")
            }
        }
    }
}
