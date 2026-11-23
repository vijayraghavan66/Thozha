import CryptoKit
import Foundation

public struct PairingInvitation: Equatable, Sendable {
    public let transport: PairingTransport
    public let invitationID: String
    public let serverURL: URL
    public let serverFingerprint: String
    public let gatewayAddress: String?
    public let gatewayPort: Int?
    public let gatewayCertificateSHA256: String?
    public let expiresAt: Date
    public let qrText: String
    public let remainingSeconds: Int

    public var supportsWiFi: Bool { transport == .wifi }
}

public enum PairingTransport: String, CaseIterable, Identifiable, Equatable, Sendable {
    case wifi
    case usb

    public var id: String { rawValue }
    public var title: String { self == .wifi ? "Wi-Fi" : "USB / ADB" }
}

public enum PairingInvitationError: Error, Equatable, LocalizedError {
    case unavailable
    case refused
    case httpStatus(Int)
    case malformed
    case unsupportedProtocol
    case invalidServerURL
    case invalidFingerprint
    case invalidSignature
    case invalidGatewayAddress
    case invalidGatewayHostname
    case invalidGatewayPort
    case invalidCertificatePin
    case gatewayDisabled
    case expired
    case tooLong

    public var errorDescription: String? {
        switch self {
        case .unavailable: "OpenJarvis is not reachable on this Mac. Check that its local service is running."
        case .refused: "The local pairing endpoint refused this app's loopback or origin check."
        case .httpStatus: "OpenJarvis could not create a pairing invitation. No QR was displayed."
        case .malformed: "OpenJarvis returned an incomplete or malformed pairing invitation."
        case .unsupportedProtocol: "This Mac returned an unsupported pairing protocol. Update Thozha and try again."
        case .invalidServerURL: "The invitation server URL does not match the selected transport. It was not displayed."
        case .invalidFingerprint: "The server key fingerprint does not match the invitation. It was not displayed."
        case .invalidSignature: "The Mac signature did not verify. The invitation was not displayed."
        case .invalidGatewayAddress: "The Wi-Fi invitation does not contain a private IPv4 gateway address. It was not displayed."
        case .invalidGatewayHostname: "The Wi-Fi invitation hostname is not a single-label .local name. It was not displayed."
        case .invalidGatewayPort: "The Wi-Fi gateway must use signed HTTPS port 8443. It was not displayed."
        case .invalidCertificatePin: "The Wi-Fi certificate pin is malformed. It was not displayed."
        case .gatewayDisabled: "Secure Wi-Fi pairing is disabled or not ready on this Mac. No Wi-Fi QR was created. You can explicitly switch to USB / ADB pairing."
        case .expired: "The invitation has already expired. Create a fresh QR."
        case .tooLong: "The invitation is too large to safely render as a QR."
        }
    }
}

public protocol PairingInvitationTransport: Sendable {
    func createInvitation(transport: PairingTransport) async throws -> Data
}

/// Calls the existing local-only `/v1/jarvis/pair/invitations` endpoint.
/// The endpoint's admin boundary is loopback peer + loopback Host + the
/// explicitly allowed local UI Origin; it does not use an API-key bearer.
public struct LoopbackPairingTransport: PairingInvitationTransport {
    private let endpoint: URL
    private let healthEndpoint: URL
    private let session: URLSession

    public init(endpoint: URL = URL(string: "http://127.0.0.1:8000/v1/jarvis/pair/invitations")!,
                healthEndpoint: URL = URL(string: "http://127.0.0.1:8000/health")!, session: URLSession = .shared) {
        self.endpoint = endpoint
        self.healthEndpoint = healthEndpoint
        self.session = session
    }

    public func createInvitation(transport: PairingTransport) async throws -> Data {
        guard await LocalHealth.check(url: healthEndpoint) else { throw PairingInvitationError.unavailable }
        let request = Self.invitationRequest(endpoint: endpoint, transport: transport)
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw PairingInvitationError.unavailable }
            guard (200..<300).contains(http.statusCode) else {
                throw Self.invitationError(statusCode: http.statusCode, transport: transport)
            }
            return data
        } catch let error as PairingInvitationError {
            throw error
        } catch {
            throw PairingInvitationError.unavailable
        }
    }

    public static func invitationRequest(endpoint: URL = URL(string: "http://127.0.0.1:8000/v1/jarvis/pair/invitations")!,
                                          transport: PairingTransport) -> URLRequest {
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("http://tauri.localhost", forHTTPHeaderField: "Origin")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if transport == .wifi {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = Data("{\"transport\":\"wifi\"}".utf8)
        }
        // No bearer token is needed by this route. The service itself checks
        // loopback peer, Host and the allowlisted local UI origin.
        return request
    }

    public static func invitationError(statusCode: Int, transport: PairingTransport) -> PairingInvitationError {
        if statusCode == 403 { return .refused }
        if statusCode == 409 && transport == .wifi { return .gatewayDisabled }
        return .httpStatus(statusCode)
    }
}

public struct PairingInvitationValidator: Sendable {
    private let now: @Sendable () -> Date

    public init(now: @escaping @Sendable () -> Date = Date.init) { self.now = now }

    public func validate(_ response: Data, requestedTransport: PairingTransport) throws -> PairingInvitation {
        guard response.count <= 16_384,
              let object = try? JSONSerialization.jsonObject(with: response) as? [String: Any],
              let payload = object["qr_payload"] as? [String: Any]
        else { throw PairingInvitationError.malformed }

        guard let protocolName = payload["protocol"] as? String else { throw PairingInvitationError.malformed }
        let v1Keys: Set<String> = ["protocol", "server_url", "server_fingerprint", "server_public_key", "invitation_id", "challenge", "expires_at", "server_signature"]
        let v2Keys: Set<String> = ["protocol", "server_url", "gateway_address", "gateway_port", "gateway_certificate_sha256", "server_fingerprint", "server_public_key", "invitation_id", "challenge", "expires_at", "server_signature"]
        let keys: Set<String>
        switch protocolName {
        case "openjarvis-pairing-v1": keys = v1Keys
        case "openjarvis-pairing-v2": keys = v2Keys
        default: throw PairingInvitationError.unsupportedProtocol
        }
        guard Set(payload.keys) == keys,
              let serverURLText = payload["server_url"] as? String,
              let fingerprint = payload["server_fingerprint"] as? String,
              let publicKeyBase64 = payload["server_public_key"] as? String,
              let invitationID = payload["invitation_id"] as? String,
              let challenge = payload["challenge"] as? String,
              let expiryText = payload["expires_at"] as? String,
              let signatureBase64 = payload["server_signature"] as? String,
              !invitationID.isEmpty, invitationID.utf8.count <= 256,
              !challenge.isEmpty, challenge.utf8.count <= 1024
        else { throw PairingInvitationError.malformed }

        let isWiFi = protocolName == "openjarvis-pairing-v2"
        guard requestedTransport == (isWiFi ? .wifi : .usb) else { throw PairingInvitationError.malformed }

        guard let serverURL = URL(string: serverURLText),
              serverURL.user == nil, serverURL.password == nil, serverURL.query == nil, serverURL.fragment == nil
        else { throw PairingInvitationError.invalidServerURL }
        var gatewayAddress: String?
        var gatewayPort: Int?
        var certificatePin: String?
        if isWiFi {
            guard serverURL.scheme?.lowercased() == "https",
                  let host = serverURL.host?.lowercased(), Self.isValidLocalHostname(host),
                  serverURL.port == 8443, serverURL.path.isEmpty,
                  serverURL.absoluteString == "https://\(host):8443"
            else { throw PairingInvitationError.invalidGatewayHostname }
            guard let address = payload["gateway_address"] as? String, Self.isRFC1918IPv4(address) else {
                throw PairingInvitationError.invalidGatewayAddress
            }
            guard let portText = payload["gateway_port"] as? String, portText == "8443" else {
                throw PairingInvitationError.invalidGatewayPort
            }
            guard let pin = payload["gateway_certificate_sha256"] as? String,
                  pin.range(of: #"^sha256:[0-9a-f]{64}$"#, options: .regularExpression) != nil
            else { throw PairingInvitationError.invalidCertificatePin }
            gatewayAddress = address
            gatewayPort = 8443
            certificatePin = pin
        } else {
            guard let scheme = serverURL.scheme?.lowercased(), scheme == "http" || scheme == "https",
                  let host = serverURL.host?.lowercased(), ["localhost", "127.0.0.1", "::1"].contains(host)
            else { throw PairingInvitationError.invalidServerURL }
        }

        guard let publicDER = Data(base64Encoded: publicKeyBase64),
              let signatureDER = Data(base64Encoded: signatureBase64),
              let point = p256Point(fromSubjectPublicKeyInfo: publicDER)
        else { throw PairingInvitationError.invalidSignature }
        let computedFingerprint = "sha256:" + SHA256.hash(data: publicDER).map { String(format: "%02x", $0) }.joined()
        guard computedFingerprint == fingerprint else { throw PairingInvitationError.invalidFingerprint }

        var signedFields: [String: String] = [:]
        for (field, value) in payload where field != "server_signature" {
            guard let string = value as? String else { throw PairingInvitationError.malformed }
            signedFields[field] = string
        }
        guard let key = try? P256.Signing.PublicKey(x963Representation: point),
              let signature = try? P256.Signing.ECDSASignature(derRepresentation: signatureDER),
              key.isValidSignature(signature, for: Self.canonicalJSON(signedFields))
        else { throw PairingInvitationError.invalidSignature }

        guard let expiry = Self.parseDate(expiryText) else { throw PairingInvitationError.malformed }
        let remaining = Int(floor(expiry.timeIntervalSince(now())))
        guard remaining > 0 else { throw PairingInvitationError.expired }
        guard remaining <= 600 else { throw PairingInvitationError.malformed }

        let qrData = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys, .fragmentsAllowed])
        guard qrData.count <= 8192, let qrText = String(data: qrData, encoding: .utf8) else { throw PairingInvitationError.tooLong }
        // Prevent a malformed wrapper from displaying one payload while its
        // duplicated top-level fields claim a different identity.
        guard Set(object.keys) == keys.union(["qr_payload"]) else { throw PairingInvitationError.malformed }
        for key in keys where object[key] as? String != payload[key] as? String {
            throw PairingInvitationError.malformed
        }
        return PairingInvitation(transport: isWiFi ? .wifi : .usb,
                                 invitationID: invitationID, serverURL: serverURL,
                                 serverFingerprint: fingerprint, gatewayAddress: gatewayAddress,
                                 gatewayPort: gatewayPort, gatewayCertificateSHA256: certificatePin,
                                 expiresAt: expiry,
                                 qrText: qrText, remainingSeconds: remaining)
    }

    public static func canonicalJSON(_ fields: [String: String]) -> Data {
        let entries = fields.keys.sorted().map { "\(quote($0)):\(quote(fields[$0] ?? ""))" }
        return Data(("{" + entries.joined(separator: ",") + "}").utf8)
    }

    private static func quote(_ input: String) -> String {
        var output = "\""
        for scalar in input.unicodeScalars {
            switch scalar.value {
            case 0x22: output += "\\\""
            case 0x5c: output += "\\\\"
            case 0x08: output += "\\b"
            case 0x09: output += "\\t"
            case 0x0a: output += "\\n"
            case 0x0c: output += "\\f"
            case 0x0d: output += "\\r"
            case 0x00...0x1f: output += String(format: "\\u%04x", scalar.value)
            default: output.unicodeScalars.append(scalar)
            }
        }
        output += "\""
        return output
    }

    private static func parseDate(_ string: String) -> Date? {
        let fractional = ISO8601DateFormatter(); fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let parsed = fractional.date(from: string) { return parsed }
        let plain = ISO8601DateFormatter(); plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: string)
    }

    private static func isValidLocalHostname(_ host: String) -> Bool {
        host.range(of: #"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local$"#, options: .regularExpression) != nil
    }

    private static func isRFC1918IPv4(_ address: String) -> Bool {
        let parts = address.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        var octets: [Int] = []
        for part in parts {
            guard !part.isEmpty, part.allSatisfy(\.isNumber),
                  let value = Int(part), (0...255).contains(value), String(value) == part else { return false }
            octets.append(value)
        }
        return octets[0] == 10
            || (octets[0] == 172 && (16...31).contains(octets[1]))
            || (octets[0] == 192 && octets[1] == 168)
    }

    private func p256Point(fromSubjectPublicKeyInfo data: Data) -> Data? {
        // DER SubjectPublicKeyInfo for id-ecPublicKey / prime256v1, followed by
        // an uncompressed 65-byte P-256 point. Reject alternate key encodings.
        let prefix: [UInt8] = [0x30,0x59,0x30,0x13,0x06,0x07,0x2a,0x86,0x48,0xce,0x3d,0x02,0x01,0x06,0x08,0x2a,0x86,0x48,0xce,0x3d,0x03,0x01,0x07,0x03,0x42,0x00]
        guard data.count == 91, Array(data.prefix(prefix.count)) == prefix,
              data[prefix.count] == 0x04 else { return nil }
        return data.subdata(in: prefix.count..<data.count)
    }
}

public struct PairingInvitationService: Sendable {
    private let transport: any PairingInvitationTransport
    private let validator: PairingInvitationValidator

    public init(transport: any PairingInvitationTransport = LoopbackPairingTransport(),
                validator: PairingInvitationValidator = PairingInvitationValidator()) {
        self.transport = transport; self.validator = validator
    }

    public func createInvitation(transport selectedTransport: PairingTransport = .wifi) async throws -> PairingInvitation {
        let data = try await transport.createInvitation(transport: selectedTransport)
        return try validator.validate(data, requestedTransport: selectedTransport)
    }
}
