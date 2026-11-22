import Foundation

public enum PairedDeviceStatus: String, Equatable, Sendable {
    case active
    case expired
    case revoked
}

public struct PairedDevice: Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let publicKeyFingerprint: String
    public let createdAt: Date
    public let expiresAt: Date
    public let revokedAt: Date?
    public let status: PairedDeviceStatus
}

public enum PairedDeviceManagementError: Error, Equatable, LocalizedError {
    case unavailable
    case refused
    case malformed
    case notFound
    case conflict
    case serviceUnavailable
    case accessRevokedCleanupPending
    case httpStatus(Int)
    case changed
    case notActive

    public var errorDescription: String? {
        switch self {
        case .unavailable: "The local OpenJarvis device-management endpoint could not be reached."
        case .refused: "OpenJarvis refused this local device-management request."
        case .malformed: "OpenJarvis returned invalid paired-device metadata. No revocation was sent."
        case .notFound: "The paired device no longer exists. Refresh the list."
        case .conflict: "The device changed state before revocation. Refresh the list."
        case .serviceUnavailable: "OpenJarvis could not verify paired-device identities. No revocation was sent."
        case .accessRevokedCleanupPending: "Device access is already revoked, but notification-token cleanup needs retry. Refresh the list; do not treat this as an undo or successful cleanup."
        case .httpStatus: "OpenJarvis could not complete the paired-device request."
        case .changed: "The selected device changed since it was displayed. Review the refreshed details before another action."
        case .notActive: "Only an active paired device can be revoked from this screen."
        }
    }
}

public protocol PairedDeviceTransport: Sendable {
    func listDevices() async throws -> Data
    func revokeDevice(id: String) async throws -> Data
}

public struct LoopbackPairedDeviceTransport: PairedDeviceTransport {
    private static let baseURL = URL(string: "http://127.0.0.1:8000/v1/jarvis")!
    private let session: URLSession

    public init(session: URLSession = .shared) { self.session = session }

    public func listDevices() async throws -> Data {
        do {
            let (data, response) = try await session.data(for: Self.listRequest())
            guard let http = response as? HTTPURLResponse else { throw PairedDeviceManagementError.unavailable }
            guard (200..<300).contains(http.statusCode) else { throw Self.error(statusCode: http.statusCode, isRevocation: false) }
            return data
        } catch let error as PairedDeviceManagementError { throw error }
        catch { throw PairedDeviceManagementError.unavailable }
    }

    public func revokeDevice(id: String) async throws -> Data {
        let request = try Self.revokeRequest(id: id)
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw PairedDeviceManagementError.unavailable }
            guard (200..<300).contains(http.statusCode) else { throw Self.error(statusCode: http.statusCode, isRevocation: true) }
            return data
        } catch let error as PairedDeviceManagementError { throw error }
        catch { throw PairedDeviceManagementError.unavailable }
    }

    static func listRequest(baseURL: URL = Self.baseURL) -> URLRequest {
        var request = URLRequest(url: baseURL.appendingPathComponent("pair").appendingPathComponent("devices"))
        request.httpMethod = "GET"
        request.setValue("http://tauri.localhost", forHTTPHeaderField: "Origin")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    static func revokeRequest(baseURL: URL = Self.baseURL, id: String) throws -> URLRequest {
        guard isSafeDeviceID(id) else { throw PairedDeviceManagementError.malformed }
        let endpoint = baseURL.appendingPathComponent("pair").appendingPathComponent("devices")
            .appendingPathComponent(id).appendingPathComponent("revoke")
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("http://tauri.localhost", forHTTPHeaderField: "Origin")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{\"confirm\":true}".utf8)
        return request
    }

    static func isSafeDeviceID(_ value: String) -> Bool {
        value.range(of: #"^[0-9a-f]{32}$"#, options: .regularExpression) != nil
    }

    static func error(statusCode: Int, isRevocation: Bool) -> PairedDeviceManagementError {
        switch statusCode {
        case 403: .refused
        case 404: .notFound
        case 409: .conflict
        case 503: isRevocation ? .accessRevokedCleanupPending : .serviceUnavailable
        default: .httpStatus(statusCode)
        }
    }
}

public struct PairedDeviceManagementService: Sendable {
    private let transport: any PairedDeviceTransport

    public init(transport: any PairedDeviceTransport = LoopbackPairedDeviceTransport()) {
        self.transport = transport
    }

    public func listDevices() async throws -> [PairedDevice] {
        let data = try await transport.listDevices()
        guard data.count <= 128_000,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(object.keys) == ["devices", "count"],
              let rows = object["devices"] as? [[String: Any]],
              let count = object["count"] as? Int, count == rows.count, count <= 100
        else { throw PairedDeviceManagementError.malformed }
        return try rows.map(parseDevice)
    }

    public func revoke(_ device: PairedDevice) async throws {
        guard LoopbackPairedDeviceTransport.isSafeDeviceID(device.id) else { throw PairedDeviceManagementError.malformed }
        guard device.status == .active else { throw PairedDeviceManagementError.notActive }
        let latest = try await listDevices()
        guard let current = latest.first(where: { $0.id == device.id }),
              current.id == device.id,
              current.name == device.name,
              current.publicKeyFingerprint == device.publicKeyFingerprint,
              current.createdAt == device.createdAt,
              current.expiresAt == device.expiresAt else { throw PairedDeviceManagementError.changed }
        guard current.status == .active else { throw PairedDeviceManagementError.notActive }
        let data = try await transport.revokeDevice(id: device.id)
        guard data.count <= 8192,
              let response = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(response.keys) == ["status", "device_id"],
              response["status"] as? String == "revoked",
              response["device_id"] as? String == device.id
        else { throw PairedDeviceManagementError.malformed }
    }

    private func parseDevice(_ row: [String: Any]) throws -> PairedDevice {
        let keys: Set<String> = ["device_id", "device_name", "public_key_fingerprint", "created_at", "expires_at", "revoked_at", "status"]
        guard Set(row.keys) == keys,
              let id = row["device_id"] as? String, LoopbackPairedDeviceTransport.isSafeDeviceID(id),
              let name = row["device_name"] as? String, !name.isEmpty, name.utf8.count <= 80,
              !name.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f }),
              let fingerprint = row["public_key_fingerprint"] as? String,
              fingerprint.range(of: #"^SHA256:[0-9A-F]{64}$"#, options: .regularExpression) != nil,
              let createdText = row["created_at"] as? String,
              let expiryText = row["expires_at"] as? String,
              let created = Self.parseDate(createdText), let expiry = Self.parseDate(expiryText), expiry > created,
              let statusText = row["status"] as? String, let status = PairedDeviceStatus(rawValue: statusText)
        else { throw PairedDeviceManagementError.malformed }

        let revokedText = row["revoked_at"] as? String
        let revokedAt = revokedText.flatMap(Self.parseDate)
        guard (status == .revoked) == (revokedAt != nil) else { throw PairedDeviceManagementError.malformed }
        return PairedDevice(id: id, name: name, publicKeyFingerprint: fingerprint,
                            createdAt: created, expiresAt: expiry, revokedAt: revokedAt, status: status)
    }

    private static func parseDate(_ string: String) -> Date? {
        let fractional = ISO8601DateFormatter(); fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let parsed = fractional.date(from: string) { return parsed }
        let plain = ISO8601DateFormatter(); plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: string)
    }
}
