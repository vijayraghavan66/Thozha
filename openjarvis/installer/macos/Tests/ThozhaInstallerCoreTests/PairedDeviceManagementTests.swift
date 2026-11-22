import Foundation
import XCTest
@testable import ThozhaInstallerCore

private actor FakePairedDeviceTransport: PairedDeviceTransport {
    private var listResponses: [Data]
    private var listIndex = 0
    private var revocationResponse: Data
    private var revocationError: PairedDeviceManagementError?
    private var calls: [String] = []

    init(listResponses: [Data], revocationResponse: Data, revocationError: PairedDeviceManagementError? = nil) {
        self.listResponses = listResponses
        self.revocationResponse = revocationResponse
        self.revocationError = revocationError
    }

    func listDevices() async throws -> Data {
        calls.append("GET /v1/jarvis/pair/devices")
        let index = min(listIndex, listResponses.count - 1)
        listIndex += 1
        return listResponses[index]
    }

    func revokeDevice(id: String) async throws -> Data {
        calls.append("POST /v1/jarvis/pair/devices/\(id)/revoke")
        if let revocationError { throw revocationError }
        return revocationResponse
    }

    func recordedCalls() -> [String] { calls }
}

final class PairedDeviceManagementTests: XCTestCase {
    private let deviceID = "0123456789abcdef0123456789abcdef"
    private let fingerprint = "SHA256:" + String(repeating: "A", count: 64)
    private let created = "2026-11-21T10:00:00+00:00"
    private let expires = "2030-11-21T10:00:00+00:00"

    private func listJSON(name: String = "Samsung SM-S901E", id: String? = nil,
                          fingerprint: String? = nil, status: String = "active",
                          revokedAt: Any = NSNull(), expiresAt: String? = nil) throws -> Data {
        let row: [String: Any] = [
            "device_id": id ?? deviceID,
            "device_name": name,
            "public_key_fingerprint": fingerprint ?? self.fingerprint,
            "created_at": created,
            "expires_at": expiresAt ?? expires,
            "revoked_at": revokedAt,
            "status": status,
        ]
        return try JSONSerialization.data(withJSONObject: ["devices": [row], "count": 1])
    }

    private func revokeJSON(status: String = "revoked", id: String? = nil) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["status": status, "device_id": id ?? deviceID])
    }

    private func service(transport: FakePairedDeviceTransport) -> PairedDeviceManagementService {
        PairedDeviceManagementService(transport: transport)
    }

    func testListReturnsOnlySafeMetadataAndStatusTimes() async throws {
        let data = try listJSON()
        let transport = FakePairedDeviceTransport(listResponses: [data], revocationResponse: try revokeJSON())
        let devices = try await service(transport: transport).listDevices()
        XCTAssertEqual(devices.count, 1)
        XCTAssertEqual(devices[0].id, deviceID)
        XCTAssertEqual(devices[0].name, "Samsung SM-S901E")
        XCTAssertEqual(devices[0].publicKeyFingerprint, fingerprint)
        XCTAssertEqual(devices[0].status, .active)
        XCTAssertNil(devices[0].revokedAt)
    }

    func testExactLoopbackListAndRevokeRequestShapes() throws {
        let base = URL(string: "http://127.0.0.1:8000/v1/jarvis")!
        let get = LoopbackPairedDeviceTransport.listRequest(baseURL: base)
        XCTAssertEqual(get.httpMethod, "GET")
        XCTAssertEqual(get.url?.absoluteString, "http://127.0.0.1:8000/v1/jarvis/pair/devices")
        XCTAssertEqual(get.value(forHTTPHeaderField: "Origin"), "http://tauri.localhost")
        XCTAssertNil(get.value(forHTTPHeaderField: "Authorization"))

        let post = try LoopbackPairedDeviceTransport.revokeRequest(baseURL: base, id: deviceID)
        XCTAssertEqual(post.httpMethod, "POST")
        XCTAssertEqual(post.url?.absoluteString, "http://127.0.0.1:8000/v1/jarvis/pair/devices/\(deviceID)/revoke")
        XCTAssertEqual(post.value(forHTTPHeaderField: "Origin"), "http://tauri.localhost")
        XCTAssertEqual(post.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertNil(post.value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(post.httpBody, Data("{\"confirm\":true}".utf8))
        XCTAssertThrowsError(try LoopbackPairedDeviceTransport.revokeRequest(id: "../devices")) {
            XCTAssertEqual($0 as? PairedDeviceManagementError, .malformed)
        }
    }

    func testListDoesNotRevokeAndExplicitServiceActionRefreshesBeforePost() async throws {
        let rows = try listJSON()
        let transport = FakePairedDeviceTransport(listResponses: [rows, rows, rows], revocationResponse: try revokeJSON())
        let pairedService = service(transport: transport)
        let listed = try await pairedService.listDevices()
        let beforeOwnerAction = await transport.recordedCalls()
        XCTAssertEqual(beforeOwnerAction, ["GET /v1/jarvis/pair/devices"])

        try await pairedService.revoke(try XCTUnwrap(listed.first))
        let afterOwnerAction = await transport.recordedCalls()
        XCTAssertEqual(afterOwnerAction, [
            "GET /v1/jarvis/pair/devices",
            "GET /v1/jarvis/pair/devices",
            "POST /v1/jarvis/pair/devices/\(deviceID)/revoke",
        ])
    }

    func testChangedMetadataExpiredOrRevokedDeviceNeverPostsRevoke() async throws {
        let original = try listJSON()
        let changed = try listJSON(name: "Different phone")
        let transport = FakePairedDeviceTransport(listResponses: [original, changed], revocationResponse: try revokeJSON())
        let pairedService = service(transport: transport)
        let listed = try await pairedService.listDevices()
        let selected = try XCTUnwrap(listed.first)
        do { try await pairedService.revoke(selected); XCTFail("changed row was revoked") }
        catch { XCTAssertEqual(error as? PairedDeviceManagementError, .changed) }
        let changedCalls = await transport.recordedCalls()
        XCTAssertFalse(changedCalls.contains { $0.hasPrefix("POST ") })

        let expiredTransport = FakePairedDeviceTransport(
            listResponses: [try listJSON(status: "expired", expiresAt: "2026-11-21T23:00:00+00:00")],
            revocationResponse: try revokeJSON()
        )
        let expiredService = service(transport: expiredTransport)
        let expiredList = try await expiredService.listDevices()
        let expired = try XCTUnwrap(expiredList.first)
        do { try await expiredService.revoke(expired); XCTFail("expired row was revoked") }
        catch { XCTAssertEqual(error as? PairedDeviceManagementError, .notActive) }
        let expiredCalls = await expiredTransport.recordedCalls()
        XCTAssertFalse(expiredCalls.contains { $0.hasPrefix("POST ") })
    }

    func test503AfterRevocationIsDistinctFromFailedRevocation() async throws {
        XCTAssertEqual(LoopbackPairedDeviceTransport.error(statusCode: 503, isRevocation: true), .accessRevokedCleanupPending)
        XCTAssertEqual(LoopbackPairedDeviceTransport.error(statusCode: 503, isRevocation: false), .serviceUnavailable)
        XCTAssertEqual(LoopbackPairedDeviceTransport.error(statusCode: 404, isRevocation: true), .notFound)
        XCTAssertEqual(LoopbackPairedDeviceTransport.error(statusCode: 409, isRevocation: true), .conflict)

        let transport = FakePairedDeviceTransport(listResponses: [try listJSON(), try listJSON(), try listJSON(status: "revoked", revokedAt: "2026-11-22T00:01:00+00:00")],
                                                   revocationResponse: try revokeJSON(),
                                                   revocationError: .accessRevokedCleanupPending)
        let service = service(transport: transport)
        let listed = try await service.listDevices()
        let selected = try XCTUnwrap(listed.first)
        do { try await service.revoke(selected); XCTFail("expected notification cleanup warning") }
        catch { XCTAssertEqual(error as? PairedDeviceManagementError, .accessRevokedCleanupPending) }
        let refreshed = try await service.listDevices()
        XCTAssertEqual(refreshed.first?.status, .revoked)
        XCTAssertNotNil(refreshed.first?.revokedAt)
    }

    func testMalformedListAndRevokeResponseAreRejected() async throws {
        let malformedTransport = FakePairedDeviceTransport(listResponses: [try listJSON(fingerprint: "bad")], revocationResponse: try revokeJSON())
        do { _ = try await service(transport: malformedTransport).listDevices(); XCTFail("bad fingerprint accepted") }
        catch { XCTAssertEqual(error as? PairedDeviceManagementError, .malformed) }

        let wrongResponseTransport = FakePairedDeviceTransport(listResponses: [try listJSON(), try listJSON()],
                                                               revocationResponse: try revokeJSON(status: "pending"))
        let wrongResponseService = service(transport: wrongResponseTransport)
        let listed = try await wrongResponseService.listDevices()
        let device = try XCTUnwrap(listed.first)
        do { try await wrongResponseService.revoke(device); XCTFail("wrong revoke response accepted") }
        catch { XCTAssertEqual(error as? PairedDeviceManagementError, .malformed) }
    }
}
