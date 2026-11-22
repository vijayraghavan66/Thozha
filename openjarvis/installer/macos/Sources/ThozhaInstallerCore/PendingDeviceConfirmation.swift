import Foundation

public struct PendingDeviceRequest: Equatable, Identifiable, Sendable {
    public let id: String
    public let deviceName: String
    public let publicKeyFingerprint: String
    public let requestedAt: Date
    public let expiresAt: Date
    public let status: String

    public func isExpired(at date: Date = Date()) -> Bool { expiresAt <= date }
}

public enum PendingDeviceDecision: String, Sendable {
    case confirm = "approve"
    case reject
}

public enum PendingDeviceDecisionResult: Equatable, Sendable {
    case confirmed
    case rejected
    case expired
}

public enum PendingDeviceError: Error, Equatable, LocalizedError {
    case unavailable
    case refused
    case malformed
    case notFound
    case conflict
    case httpStatus(Int)
    case expired
    case changed

    public var errorDescription: String? {
        switch self {
        case .unavailable: "The local OpenJarvis service could not be reached."
        case .refused: "OpenJarvis refused the local confirmation request."
        case .malformed: "OpenJarvis returned an invalid pending-device response. No decision was sent."
        case .notFound: "This phone request no longer exists. Refresh the list."
        case .conflict: "This phone request expired or was already decided. Refresh the list."
        case .httpStatus: "OpenJarvis could not record the phone decision. Refresh and try again if it is still pending."
        case .expired: "This phone request has expired and cannot be confirmed or rejected."
        case .changed: "The pending request changed before the decision was sent. Review the refreshed details and choose again."
        }
    }
}

public protocol PendingDeviceTransport: Sendable {
    func fetchPendingDevices() async throws -> Data
    func sendDecision(invitationID: String, decision: PendingDeviceDecision) async throws -> Data
}

public struct LoopbackPendingDeviceTransport: PendingDeviceTransport {
    private static let baseURL = URL(string: "http://127.0.0.1:8000/v1/jarvis")!
    private let session: URLSession

    public init(session: URLSession = .shared) { self.session = session }

    public func fetchPendingDevices() async throws -> Data {
        do {
            let (data, response) = try await session.data(for: Self.pendingRequest(baseURL: Self.baseURL))
            guard let http = response as? HTTPURLResponse else { throw PendingDeviceError.unavailable }
            guard (200..<300).contains(http.statusCode) else { throw Self.error(statusCode: http.statusCode) }
            return data
        } catch let error as PendingDeviceError { throw error }
        catch { throw PendingDeviceError.unavailable }
    }

    public func sendDecision(invitationID: String, decision: PendingDeviceDecision) async throws -> Data {
        guard Self.isSafeInvitationID(invitationID) else { throw PendingDeviceError.malformed }
        do {
            let request = try Self.decisionRequest(baseURL: Self.baseURL, invitationID: invitationID, decision: decision)
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw PendingDeviceError.unavailable }
            guard (200..<300).contains(http.statusCode) else { throw Self.error(statusCode: http.statusCode) }
            return data
        } catch let error as PendingDeviceError { throw error }
        catch { throw PendingDeviceError.unavailable }
    }

    static func pendingRequest(baseURL: URL = LoopbackPendingDeviceTransport.baseURL) -> URLRequest {
        let pendingURL = baseURL.appendingPathComponent("pair").appendingPathComponent("pending")
        var components = URLComponents(url: pendingURL, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "limit", value: "50")]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        request.setValue("http://tauri.localhost", forHTTPHeaderField: "Origin")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    static func decisionRequest(baseURL: URL = LoopbackPendingDeviceTransport.baseURL,
                                invitationID: String, decision: PendingDeviceDecision) throws -> URLRequest {
        guard isSafeInvitationID(invitationID) else { throw PendingDeviceError.malformed }
        let decisionURL = baseURL.appendingPathComponent("pair").appendingPathComponent(invitationID).appendingPathComponent("decision")
        var request = URLRequest(url: decisionURL)
        request.httpMethod = "POST"
        request.setValue("http://tauri.localhost", forHTTPHeaderField: "Origin")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{\"decision\":\"\(decision.rawValue)\"}".utf8)
        return request
    }

    static func isSafeInvitationID(_ invitationID: String) -> Bool {
        invitationID.range(of: #"^[0-9a-f]{32}$"#, options: .regularExpression) != nil
    }

    static func error(statusCode: Int) -> PendingDeviceError {
        switch statusCode {
        case 403: .refused
        case 404: .notFound
        case 409: .conflict
        default: .httpStatus(statusCode)
        }
    }
}

public struct PendingDeviceConfirmationService: Sendable {
    private let transport: any PendingDeviceTransport
    private let now: @Sendable () -> Date

    public init(transport: any PendingDeviceTransport = LoopbackPendingDeviceTransport(),
                now: @escaping @Sendable () -> Date = Date.init) {
        self.transport = transport
        self.now = now
    }

    public func listPending() async throws -> [PendingDeviceRequest] {
        let data = try await transport.fetchPendingDevices()
        guard data.count <= 128_000,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rawRequests = object["requests"] as? [[String: Any]],
              let count = object["count"] as? Int,
              count == rawRequests.count, count <= 50
        else { throw PendingDeviceError.malformed }
        return try rawRequests.map(parseRequest)
    }

    public func decide(_ request: PendingDeviceRequest, decision: PendingDeviceDecision) async throws -> PendingDeviceDecisionResult {
        guard LoopbackPendingDeviceTransport.isSafeInvitationID(request.id) else { throw PendingDeviceError.malformed }
        guard !request.isExpired(at: now()) else { throw PendingDeviceError.expired }
        let data = try await transport.sendDecision(invitationID: request.id, decision: decision)
        guard data.count <= 8192,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["invitation_id"] as? String == request.id,
              let status = object["status"] as? String
        else { throw PendingDeviceError.malformed }
        switch (decision, status) {
        case (.confirm, "APPROVED"): return .confirmed
        case (.reject, "REJECTED"): return .rejected
        case (_, "EXPIRED"): return .expired
        default: throw PendingDeviceError.conflict
        }
    }

    private func parseRequest(_ raw: [String: Any]) throws -> PendingDeviceRequest {
        let keys: Set<String> = ["id", "status", "device_name", "device_fingerprint", "requested_at", "expires_at"]
        guard Set(raw.keys) == keys,
              let id = raw["id"] as? String, LoopbackPendingDeviceTransport.isSafeInvitationID(id),
              let status = raw["status"] as? String, status == "PENDING_CONFIRMATION",
              let name = raw["device_name"] as? String, !name.isEmpty, name.utf8.count <= 256,
              let fingerprint = raw["device_fingerprint"] as? String,
              fingerprint.range(of: #"^sha256:[0-9a-f]{64}$"#, options: .regularExpression) != nil,
              let requestedText = raw["requested_at"] as? String,
              let expiryText = raw["expires_at"] as? String,
              let requestedAt = Self.parseDate(requestedText),
              let expiresAt = Self.parseDate(expiryText),
              expiresAt > requestedAt
        else { throw PendingDeviceError.malformed }
        return PendingDeviceRequest(id: id, deviceName: name, publicKeyFingerprint: fingerprint,
                                    requestedAt: requestedAt, expiresAt: expiresAt, status: status)
    }

    private static func parseDate(_ string: String) -> Date? {
        let fractional = ISO8601DateFormatter(); fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let parsed = fractional.date(from: string) { return parsed }
        let plain = ISO8601DateFormatter(); plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: string)
    }
}
