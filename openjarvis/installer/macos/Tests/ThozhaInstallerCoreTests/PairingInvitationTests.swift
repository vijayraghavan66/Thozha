import CoreImage
import CryptoKit
import Foundation
import XCTest
@testable import ThozhaInstallerCore

private struct FixedInvitationTransport: PairingInvitationTransport {
    let data: Data
    func createInvitation(transport: PairingTransport) async throws -> Data { data }
}

final class PairingInvitationTests: XCTestCase {
    private func fixture() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "pairing_invitation_vector.test", withExtension: "json", subdirectory: "Fixtures"))
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func wifiFixture() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "pairing_invitation_v2.test", withExtension: "json", subdirectory: "Fixtures"))
        let data = try Data(contentsOf: url)
        let wrapper = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try XCTUnwrap(wrapper["qr_payload"] as? [String: Any])
    }

    private func wrapped(_ payload: [String: Any]) throws -> Data {
        var response = payload
        response["qr_payload"] = payload
        return try JSONSerialization.data(withJSONObject: response, options: [.sortedKeys])
    }

    private func date(_ string: String) throws -> Date {
        let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime]
        return try XCTUnwrap(formatter.date(from: string))
    }

    private func validator(now: String = "2030-01-02T03:00:00+00:00") throws -> PairingInvitationValidator {
        let instant = try date(now)
        return PairingInvitationValidator(now: { instant })
    }

    func testCanonicalBytesMatchSharedPythonAndroidVector() throws {
        let payload = try fixture()
        let keys = ["challenge", "expires_at", "invitation_id", "protocol", "server_fingerprint", "server_url", "server_public_key"]
        let fields = try Dictionary(uniqueKeysWithValues: keys.map { key -> (String, String) in
            (key, try XCTUnwrap(payload[key] as? String))
        })
        let digest = SHA256.hash(data: PairingInvitationValidator.canonicalJSON(fields)).map { String(format: "%02x", $0) }.joined()
        XCTAssertEqual(digest, "fa649db2ad3ec1e3bc17a94534738176a8726ba70e0cfd0d7bd8227ccf219fbf")
    }

    func testRealFixedSignatureVerifiesAndProducesQRImage() throws {
        let payload = try fixture()
        let invitation = try validator().validate(wrapped(payload), requestedTransport: .usb)
        XCTAssertEqual(invitation.invitationID, "invite/雪")
        XCTAssertEqual(invitation.serverFingerprint, "sha256:5cd252fb0ce8932436faf8ccd1040981b89ee4ad6b9fe9e2a2b7e71aacb27cd3")
        XCTAssertEqual(invitation.serverURL.host, "localhost")
        XCTAssertEqual(invitation.transport, .usb)
        XCTAssertGreaterThan(invitation.remainingSeconds, 0)
        let qr = try XCTUnwrap(PairingQRCodeGenerator.image(payload: invitation.qrText))
        XCTAssertGreaterThan(qr.width, 200)
        XCTAssertEqual(qr.width, qr.height)
    }

    func testTamperedChallengeRejected() throws {
        var payload = try fixture()
        payload["challenge"] = "attacker-modified"
        XCTAssertThrowsError(try validator().validate(wrapped(payload), requestedTransport: .usb)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .invalidSignature)
        }
    }

    func testFingerprintMismatchRejectedBeforeQrCreation() throws {
        var payload = try fixture()
        payload["server_fingerprint"] = "sha256:" + String(repeating: "0", count: 64)
        XCTAssertThrowsError(try validator().validate(wrapped(payload), requestedTransport: .usb)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .invalidFingerprint)
        }
    }

    func testNonLoopbackServerUrlRejected() throws {
        var payload = try fixture()
        payload["server_url"] = "http://192.168.1.20:8000"
        XCTAssertThrowsError(try validator().validate(wrapped(payload), requestedTransport: .usb)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .invalidServerURL)
        }
    }

    func testExpiredAndOverlongInvitationsRejected() throws {
        let payload = try fixture()
        XCTAssertThrowsError(try validator(now: "2030-01-02T03:05:00+00:00").validate(wrapped(payload), requestedTransport: .usb)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .expired)
        }
        XCTAssertThrowsError(try validator(now: "2029-01-01T00:00:00+00:00").validate(wrapped(payload), requestedTransport: .usb)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .malformed)
        }
    }

    func testWrapperMustMatchSignedPayload() throws {
        var response = try fixture()
        response["qr_payload"] = try fixture()
        response["server_url"] = "http://127.0.0.1:8000/"
        let data = try JSONSerialization.data(withJSONObject: response, options: [.sortedKeys])
        XCTAssertThrowsError(try validator().validate(data, requestedTransport: .usb)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .malformed)
        }
    }

    func testTransportTargetsLoopbackWithAllowedOriginAndNoBearerCredential() throws {
        let request = LoopbackPairingTransport.invitationRequest(transport: .wifi)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8000/v1/jarvis/pair/invitations")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Origin"), "http://tauri.localhost")
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(request.httpBody, Data("{\"transport\":\"wifi\"}".utf8))
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let usbRequest = LoopbackPairingTransport.invitationRequest(transport: .usb)
        XCTAssertEqual(usbRequest.url?.absoluteString, "http://127.0.0.1:8000/v1/jarvis/pair/invitations")
        XCTAssertNil(usbRequest.httpBody)
        XCTAssertNil(usbRequest.value(forHTTPHeaderField: "Content-Type"))
    }

    func testWifiGatewayDisabled409IsSpecificAndUsb409IsNotMisrepresented() {
        XCTAssertEqual(LoopbackPairingTransport.invitationError(statusCode: 409, transport: .wifi), .gatewayDisabled)
        XCTAssertEqual(LoopbackPairingTransport.invitationError(statusCode: 409, transport: .usb), .httpStatus(409))
        XCTAssertEqual(LoopbackPairingTransport.invitationError(statusCode: 403, transport: .wifi), .refused)
    }

    func testServiceUsesFakeTransportAndReturnsVerifiedInvitation() async throws {
        let response = try wrapped(fixture())
        let service = PairingInvitationService(transport: FixedInvitationTransport(data: response), validator: try validator())
        let invitation = try await service.createInvitation(transport: .usb)
        XCTAssertEqual(invitation.invitationID, "invite/雪")
    }

    func testV2SignedWiFiFixtureVerifiesAndExposesPinnedGatewayDetails() throws {
        let payload = try wifiFixture()
        let invitation = try validator().validate(wrapped(payload), requestedTransport: .wifi)
        XCTAssertEqual(invitation.transport, .wifi)
        XCTAssertEqual(invitation.serverURL.absoluteString, "https://thozha-mac.local:8443")
        XCTAssertEqual(invitation.gatewayAddress, "192.168.1.55")
        XCTAssertEqual(invitation.gatewayPort, 8443)
        XCTAssertEqual(invitation.gatewayCertificateSHA256, "sha256:" + String(repeating: "a", count: 64))
        XCTAssertTrue(invitation.supportsWiFi)
        XCTAssertNotNil(PairingQRCodeGenerator.image(payload: invitation.qrText))
    }

    func testV2RejectsPublicGatewayAddress() throws {
        var payload = try wifiFixture()
        payload["gateway_address"] = "8.8.8.8"
        XCTAssertThrowsError(try validator().validate(wrapped(payload), requestedTransport: .wifi)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .invalidGatewayAddress)
        }
    }

    func testV2RejectsWrongPortAndMalformedCertificatePin() throws {
        var badPort = try wifiFixture()
        badPort["gateway_port"] = "9443"
        XCTAssertThrowsError(try validator().validate(wrapped(badPort), requestedTransport: .wifi)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .invalidGatewayPort)
        }
        var badPin = try wifiFixture()
        badPin["gateway_certificate_sha256"] = "sha256:1234"
        XCTAssertThrowsError(try validator().validate(wrapped(badPin), requestedTransport: .wifi)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .invalidCertificatePin)
        }
    }

    func testV2RejectsInvalidLocalHostnameAndExpiredInvite() throws {
        var badHost = try wifiFixture()
        badHost["server_url"] = "https://mac.example:8443"
        XCTAssertThrowsError(try validator().validate(wrapped(badHost), requestedTransport: .wifi)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .invalidGatewayHostname)
        }
        let valid = try wifiFixture()
        XCTAssertThrowsError(try validator(now: "2030-01-02T03:05:00+00:00").validate(wrapped(valid), requestedTransport: .wifi)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .expired)
        }
    }

    func testV2SignatureCoversGatewayAddressAndV1CannotSatisfyWiFiRequest() throws {
        var tampered = try wifiFixture()
        tampered["gateway_address"] = "192.168.1.88"
        XCTAssertThrowsError(try validator().validate(wrapped(tampered), requestedTransport: .wifi)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .invalidSignature)
        }
        XCTAssertThrowsError(try validator().validate(wrapped(fixture()), requestedTransport: .wifi)) { error in
            XCTAssertEqual(error as? PairingInvitationError, .malformed)
        }
    }
}
