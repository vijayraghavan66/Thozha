import Foundation
import XCTest
@testable import ThozhaInstallerCore

private actor RecordingPendingDeviceTransport: PendingDeviceTransport {
    let pendingData: Data
    let decisionStatusOverride: String?
    private(set) var decisions: [(String, PendingDeviceDecision)] = []
    private(set) var calls: [String] = []

    init(pendingData: Data, decisionStatusOverride: String? = nil) {
        self.pendingData = pendingData
        self.decisionStatusOverride = decisionStatusOverride
    }

    func fetchPendingDevices() async throws -> Data {
        calls.append("GET /v1/jarvis/pair/pending")
        return pendingData
    }

    func sendDecision(invitationID: String, decision: PendingDeviceDecision) async throws -> Data {
        decisions.append((invitationID, decision))
        calls.append("POST /v1/jarvis/pair/\(invitationID)/decision")
        let status = decisionStatusOverride ?? (decision == .confirm ? "APPROVED" : "REJECTED")
        return try JSONSerialization.data(withJSONObject: ["invitation_id": invitationID, "status": status])
    }

    func recordedDecisions() -> [(String, PendingDeviceDecision)] { decisions }
    func recordedCalls() -> [String] { calls }
}

final class PendingDeviceConfirmationTests: XCTestCase {
    private let invitationID = "0123456789abcdef0123456789abcdef"
    private let fingerprint = "sha256:" + String(repeating: "a", count: 64)

    private func pendingData(name: String = "Samsung SM-S901E", status: String = "PENDING_CONFIRMATION",
                             fingerprint: String? = nil,
                             expires: String = "2030-01-02T03:05:00+00:00") throws -> Data {
        let request: [String: Any] = [
            "id": invitationID,
            "status": status,
            "device_name": name,
            "device_fingerprint": fingerprint ?? self.fingerprint,
            "requested_at": "2030-01-02T03:00:00+00:00",
            "expires_at": expires,
        ]
        return try JSONSerialization.data(withJSONObject: ["requests": [request], "count": 1])
    }

    private func service(_ data: Data, now: Date = ISO8601DateFormatter().date(from: "2030-01-02T03:01:00+00:00")!,
                         decisionStatusOverride: String? = nil) -> (PendingDeviceConfirmationService, RecordingPendingDeviceTransport) {
        let transport = RecordingPendingDeviceTransport(pendingData: data, decisionStatusOverride: decisionStatusOverride)
        return (PendingDeviceConfirmationService(transport: transport, now: { now }), transport)
    }

    func testPendingListDisplaysSafeFieldsExactly() async throws {
        let (service, _) = service(try pendingData())
        let items = try await service.listPending()
        XCTAssertEqual(items.count, 1)
        XCTAssertEqual(items[0].id, invitationID)
        XCTAssertEqual(items[0].deviceName, "Samsung SM-S901E")
        XCTAssertEqual(items[0].publicKeyFingerprint, fingerprint)
        XCTAssertEqual(items[0].status, "PENDING_CONFIRMATION")
        XCTAssertFalse(items[0].isExpired(at: ISO8601DateFormatter().date(from: "2030-01-02T03:01:00+00:00")!))
    }

    func testNoDecisionRequestUntilExplicitDecisionAction() async throws {
        let (service, transport) = service(try pendingData())
        let listed = try await service.listPending()
        let pending = try XCTUnwrap(listed.first)
        let beforeAction = await transport.recordedCalls()
        XCTAssertEqual(beforeAction, ["GET /v1/jarvis/pair/pending"])

        let result = try await service.decide(pending, decision: .confirm)
        XCTAssertEqual(result, .confirmed)
        let decisions = await transport.recordedDecisions()
        XCTAssertEqual(decisions.count, 1)
        XCTAssertEqual(decisions[0].0, invitationID)
        XCTAssertEqual(decisions[0].1, .confirm)
        let afterAction = await transport.recordedCalls()
        XCTAssertEqual(afterAction, ["GET /v1/jarvis/pair/pending", "POST /v1/jarvis/pair/\(invitationID)/decision"])
    }

    func testExactReadAndDecisionRequestShapes() throws {
        let base = URL(string: "http://127.0.0.1:8000/v1/jarvis")!
        let get = LoopbackPendingDeviceTransport.pendingRequest(baseURL: base)
        XCTAssertEqual(get.httpMethod, "GET")
        XCTAssertEqual(get.url?.absoluteString, "http://127.0.0.1:8000/v1/jarvis/pair/pending?limit=50")
        XCTAssertEqual(get.value(forHTTPHeaderField: "Origin"), "http://tauri.localhost")
        XCTAssertNil(get.value(forHTTPHeaderField: "Authorization"))

        for (decision, body) in [(PendingDeviceDecision.confirm, "{\"decision\":\"approve\"}"), (.reject, "{\"decision\":\"reject\"}")] {
            let post = try LoopbackPendingDeviceTransport.decisionRequest(baseURL: base, invitationID: invitationID, decision: decision)
            XCTAssertEqual(post.httpMethod, "POST")
            XCTAssertEqual(post.url?.absoluteString, "http://127.0.0.1:8000/v1/jarvis/pair/\(invitationID)/decision")
            XCTAssertEqual(post.value(forHTTPHeaderField: "Origin"), "http://tauri.localhost")
            XCTAssertEqual(post.value(forHTTPHeaderField: "Content-Type"), "application/json")
            XCTAssertNil(post.value(forHTTPHeaderField: "Authorization"))
            XCTAssertEqual(post.httpBody, Data(body.utf8))
        }
    }

    func testExpiredOrMalformedRequestCannotBeSentAsDecision() async throws {
        let instant = ISO8601DateFormatter().date(from: "2030-01-02T03:06:00+00:00")!
        let (expiredService, expiredTransport) = service(try pendingData(), now: instant)
        let listed = try await expiredService.listPending()
        let expired = try XCTUnwrap(listed.first)
        do { _ = try await expiredService.decide(expired, decision: .confirm); XCTFail("expired request decision was sent") }
        catch { XCTAssertEqual(error as? PendingDeviceError, .expired) }
        let expiredDecisions = await expiredTransport.recordedDecisions()
        XCTAssertTrue(expiredDecisions.isEmpty)

        XCTAssertThrowsError(try LoopbackPendingDeviceTransport.decisionRequest(
            invitationID: "../pending", decision: .confirm
        )) { error in XCTAssertEqual(error as? PendingDeviceError, .malformed) }
    }

    func testPendingResponseRejectsWrongFieldsAndFingerprints() async throws {
        let (badFingerprintService, _) = service(try pendingData(fingerprint: "not-a-pin"))
        do { _ = try await badFingerprintService.listPending(); XCTFail("malformed fingerprint accepted") }
        catch { XCTAssertEqual(error as? PendingDeviceError, .malformed) }

        let (badStatusService, _) = service(try pendingData(status: "APPROVED"))
        do { _ = try await badStatusService.listPending(); XCTFail("non-pending request accepted") }
        catch { XCTAssertEqual(error as? PendingDeviceError, .malformed) }
    }

    func testServerExpiryAndReplayResponsesAreNotReportedAsSuccess() async throws {
        let (pendingService, _) = service(try pendingData())
        let listed = try await pendingService.listPending()
        let pending = try XCTUnwrap(listed.first)
        let result = try await pendingService.decide(pending, decision: .reject)
        XCTAssertEqual(result, .rejected)
        XCTAssertEqual(LoopbackPendingDeviceTransport.error(statusCode: 404), .notFound)
        XCTAssertEqual(LoopbackPendingDeviceTransport.error(statusCode: 409), .conflict)
        XCTAssertEqual(LoopbackPendingDeviceTransport.error(statusCode: 403), .refused)

        let (expiredServerService, _) = service(try pendingData(), decisionStatusOverride: "EXPIRED")
        let expiredRows = try await expiredServerService.listPending()
        let stillPending = try XCTUnwrap(expiredRows.first)
        let serverResult = try await expiredServerService.decide(stillPending, decision: .confirm)
        XCTAssertEqual(serverResult, .expired)
    }
}
