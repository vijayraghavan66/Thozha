import Foundation
import XCTest
@testable import ThozhaInstallerCore

final class InstallerTests: XCTestCase {
    func testPreflightAllowsSupportedAppleSiliconAndFlagsMissingWorkerAsBlocked() {
        let host = HostSnapshot(macOSMajor: 14, architecture: "arm64", physicalMemoryBytes: 16 * 1_073_741_824,
                                availableDiskBytes: 30_000_000_000, hasUv: true, hasQemuArm64: false)
        let results = Preflight.evaluate(host)
        XCTAssertTrue(results.contains { $0.title == "Processor" && $0.state == .pass })
        XCTAssertTrue(results.contains { $0.title == "Isolated Linux worker" && $0.state == .blocked })
        XCTAssertFalse(Preflight.canContinue(host))
    }

    func testPreflightRejectsIntelLowMemoryAndLowDisk() {
        let host = HostSnapshot(macOSMajor: 14, architecture: "x86_64", physicalMemoryBytes: 4,
                                availableDiskBytes: 10, hasUv: false, hasQemuArm64: false)
        XCTAssertFalse(Preflight.canContinue(host))
        XCTAssertEqual(Preflight.evaluate(host).filter { $0.state == .blocked }.count, 4)
    }

    func testPreflightAllowsArmHostWhenQemuDetectedButWarnsGuestNotConfigured() {
        let host = HostSnapshot(macOSMajor: 15, architecture: "arm64", physicalMemoryBytes: 16 * 1_073_741_824,
                                availableDiskBytes: 30_000_000_000, hasUv: true, hasQemuArm64: true)
        XCTAssertTrue(Preflight.canContinue(host))
        XCTAssertEqual(Preflight.evaluate(host).last?.state, .warning)
    }

    func testStagingIdentityIsStableFreshAndPrivateToStagingDirectory() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let first = try StagingIdentityStore.createOrLoad(in: root)
        let second = try StagingIdentityStore.createOrLoad(in: root)
        XCTAssertEqual(first, second)
        XCTAssertTrue(FileManager.default.fileExists(atPath: root.appendingPathComponent("installation-id.json").path))
        let attrs = try FileManager.default.attributesOfItem(atPath: root.appendingPathComponent("installation-id.json").path)
        XCTAssertEqual((attrs[.posixPermissions] as? NSNumber)?.intValue, 0o600)
    }
}
