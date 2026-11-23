import Foundation
import XCTest
@testable import ThozhaInstallerCore

private struct FakeSSIDReader: CurrentWiFiSSIDReading {
    let current: String?
    func currentSSID(interfaceName: String) -> String? { current }
}

private struct FixedCurrentSSIDReader: CurrentWiFiSSIDReading {
    let value: String?
    func currentSSID(interfaceName: String) -> String? { value }
}

private final class RecordingGatewayFiles: GatewayServiceFileOperations, @unchecked Sendable {
    private let lock = NSLock()
    private var contents: [String: Data] = [:]
    private var modes: [String: Int] = [:]
    private var directoryPaths = Set<String>()

    func existsOrSymlink(at url: URL) throws -> Bool {
        lock.lock(); defer { lock.unlock() }
        return contents[url.path] != nil
    }

    func createPrivateDirectory(_ url: URL) throws {
        lock.lock(); defer { lock.unlock() }
        directoryPaths.insert(url.path)
    }

    func writeExclusive(_ data: Data, to url: URL, mode: Int) throws {
        lock.lock(); defer { lock.unlock() }
        guard contents[url.path] == nil else { throw GatewayServiceManagerError.launchAgentAlreadyExists }
        contents[url.path] = data
        modes[url.path] = mode
    }

    func removeRegularFile(_ url: URL) throws {
        lock.lock(); defer { lock.unlock() }
        contents.removeValue(forKey: url.path)
        modes.removeValue(forKey: url.path)
    }

    func seed(_ url: URL, data: Data = Data("existing".utf8)) {
        lock.lock(); defer { lock.unlock() }
        contents[url.path] = data
    }

    func read(_ url: URL) -> Data? { lock.lock(); defer { lock.unlock() }; return contents[url.path] }
    func mode(_ url: URL) -> Int? { lock.lock(); defer { lock.unlock() }; return modes[url.path] }
    func directories() -> Set<String> { lock.lock(); defer { lock.unlock() }; return directoryPaths }
}

private final class RecordingGatewayLaunchctl: GatewayLaunchctlOperations, @unchecked Sendable {
    private let lock = NSLock()
    private var loaded = false
    private var recorded: [String] = []
    private var failBootstrap = false
    var failBootout = false

    func isLoaded(domain: String, label: String) throws -> Bool {
        lock.lock(); defer { lock.unlock() }
        recorded.append("print \(domain)/\(label)")
        return loaded
    }

    func bootstrap(domain: String, plistURL: URL) throws {
        lock.lock(); defer { lock.unlock() }
        recorded.append("bootstrap \(domain) \(plistURL.path)")
        if failBootstrap { throw GatewayServiceManagerError.launchctlFailed(5) }
        loaded = true
    }

    func bootout(domain: String, label: String) throws {
        lock.lock(); defer { lock.unlock() }
        recorded.append("bootout \(domain)/\(label)")
        if failBootout { throw GatewayServiceManagerError.launchctlFailed(5) }
        loaded = false
    }

    func setLoaded(_ value: Bool) { lock.lock(); defer { lock.unlock() }; loaded = value }
    func setBootstrapFailure(_ value: Bool) { lock.lock(); defer { lock.unlock() }; failBootstrap = value }
    func calls() -> [String] { lock.lock(); defer { lock.unlock() }; return recorded }
}

final class GatewaySetupTests: XCTestCase {
    private let installID = UUID(uuidString: "12345678-1234-5678-9abc-def012345678")!
    private let home = URL(fileURLWithPath: "/tmp/thozha-gateway-plan-home", isDirectory: true)
    private let runtime = GatewayRuntime(
        openJarvisPython: URL(fileURLWithPath: "/opt/homebrew/opt/openjarvis/bin/python3.13"),
        tlsPython: URL(fileURLWithPath: "/opt/homebrew/bin/python3.14"),
        shutdownSupervisor: URL(fileURLWithPath: "/Applications/Thozha Setup.app/Contents/Helpers/ThozhaGatewaySupervisor"),
        launchdPath: "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )

    private func snapshot(ssid: String? = "HomeNet", ip: String? = "192.168.1.42",
                          auth: GatewayLocationAuthorization = .authorized,
                          interface: String? = "en0", ambiguous: Bool = false) -> GatewayWiFiSnapshot {
        GatewayWiFiSnapshot(interfaceName: interface, ssid: ssid, privateIPv4: ip,
                            authorization: auth, hasAmbiguousInterfaces: ambiguous)
    }

    func testPreflightAutoConfiguresExactCurrentNetworkWithoutManualValues() throws {
        let result = GatewayPreflight.evaluate(snapshot: snapshot(), installationID: installID)
        guard case .success(let config) = result else { return XCTFail("expected ready config") }
        XCTAssertEqual(config.interfaceName, "en0")
        XCTAssertEqual(config.allowedSSID, "HomeNet")
        XCTAssertEqual(config.bindIPv4, "192.168.1.42")
        XCTAssertEqual(config.hostname, "thozha-12345678.local")
        XCTAssertEqual(config.port, 8443)
    }

    func testPreflightFailsClosedForNoInterfaceAmbiguityAndNonPrivateAddress() {
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(interface: nil), installationID: installID), .failure(.noWifiInterface))
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ambiguous: true), installationID: installID), .failure(.multipleActiveInterfaces))
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ip: "8.8.8.8"), installationID: installID), .failure(.addressNotPrivate))
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ip: nil), installationID: installID), .failure(.privateAddressUnavailable))
    }

    func testSSIDPrivacyStatusesNeverFallBackToManualNetworkEntry() {
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ssid: nil, auth: .notDetermined), installationID: installID), .failure(.locationPermissionRequired))
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ssid: nil, auth: .denied), installationID: installID), .failure(.locationPermissionDenied))
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ssid: nil, auth: .restricted), installationID: installID), .failure(.locationPermissionRestricted))
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ssid: nil, auth: .authorized), installationID: installID), .failure(.ssidUnavailable))
    }

    func testBundledSSIDHelperModeOutputsOnlyCurrentSSIDAndFailsClosed() {
        XCTAssertEqual(GatewaySSIDHelper.output(arguments: ["--ssid", "en0"], reader: FakeSSIDReader(current: "My Home WiFi")), "My Home WiFi\n")
        XCTAssertNil(GatewaySSIDHelper.output(arguments: ["--ssid", "en0"], reader: FakeSSIDReader(current: nil)))
        XCTAssertNil(GatewaySSIDHelper.output(arguments: ["--ssid", "en0"], reader: FakeSSIDReader(current: "Hidden\nNetwork")))
        XCTAssertNil(GatewaySSIDHelper.output(arguments: ["--ssid", "../en0"], reader: FakeSSIDReader(current: "My Home WiFi")))
        XCTAssertNil(GatewaySSIDHelper.output(arguments: ["--ssid", "en0", "extra"], reader: FakeSSIDReader(current: "My Home WiFi")))
        XCTAssertTrue(GatewaySSIDHelper.matchesAllowedSSID("My Home WiFi", allowedSSID: "My Home WiFi"))
        XCTAssertFalse(GatewaySSIDHelper.matchesAllowedSSID("Guest", allowedSSID: "My Home WiFi"))
        XCTAssertFalse(GatewaySSIDHelper.matchesAllowedSSID(nil, allowedSSID: "My Home WiFi"))
    }

    func testSSIDHelperStrictModePrintsOnlySSIDAndFailsClosed() {
        XCTAssertEqual(GatewaySSIDHelper.output(arguments: ["--ssid", "en0"], reader: FixedCurrentSSIDReader(value: "HomeNet")), "HomeNet\n")
        XCTAssertNil(GatewaySSIDHelper.output(arguments: ["--ssid", "en0", "extra"], reader: FixedCurrentSSIDReader(value: "HomeNet")))
        XCTAssertNil(GatewaySSIDHelper.output(arguments: ["--ssid", "../../etc"], reader: FixedCurrentSSIDReader(value: "HomeNet")))
        XCTAssertNil(GatewaySSIDHelper.output(arguments: ["--ssid", "en0"], reader: FixedCurrentSSIDReader(value: nil)))
        XCTAssertNil(GatewaySSIDHelper.output(arguments: ["--ssid", "en0"], reader: FixedCurrentSSIDReader(value: "Hidden\nSSID")))
        XCTAssertTrue(GatewaySSIDHelper.matchesAllowedSSID("HomeNet", allowedSSID: "HomeNet"))
        XCTAssertFalse(GatewaySSIDHelper.matchesAllowedSSID("Guest", allowedSSID: "HomeNet"))
        XCTAssertFalse(GatewaySSIDHelper.matchesAllowedSSID(nil, allowedSSID: "HomeNet"))
    }

    func testPreflightRejectsInvalidSSIDAndPublicOrLoopbackAddresses() {
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ssid: ""), installationID: installID), .failure(.invalidSSID))
        XCTAssertEqual(GatewayPreflight.evaluate(snapshot: snapshot(ssid: "Home\nInjected"), installationID: installID), .failure(.invalidSSID))
        for address in ["127.0.0.1", "169.254.1.2", "172.15.1.2", "172.32.1.2", "192.0.2.1", "::1", "192.168.001.2"] {
            XCTAssertFalse(GatewayPreflight.isRFC1918IPv4(address), "accepted \(address)")
        }
        XCTAssertTrue(GatewayPreflight.isRFC1918IPv4("10.2.3.4"))
        XCTAssertTrue(GatewayPreflight.isRFC1918IPv4("172.31.2.3"))
        XCTAssertTrue(GatewayPreflight.isRFC1918IPv4("192.168.9.1"))
    }

    func testEnablePlanRequiresExplicitOptInAndIsPrivatePerUserXMLWithoutSecrets() throws {
        let config = try XCTUnwrap(tryResult(GatewayPreflight.evaluate(snapshot: snapshot(), installationID: installID)))
        XCTAssertThrowsError(try GatewayLaunchAgentPlanner.enablePlan(configuration: config, runtime: runtime,
                                                                       homeDirectory: home, userID: 501, ownerOptIn: false)) {
            XCTAssertEqual($0 as? GatewayPlanError, .ownerOptInRequired)
        }

        let plan = try GatewayLaunchAgentPlanner.enablePlan(configuration: config, runtime: runtime,
                                                             homeDirectory: home, userID: 501, ownerOptIn: true)
        XCTAssertEqual(plan.plistURL.path, "/tmp/thozha-gateway-plan-home/Library/LaunchAgents/com.thozha.companion-wifi-gateway.plist")
        XCTAssertEqual(plan.fileMode, 0o600)
        XCTAssertEqual(plan.bootstrapCommand, ["launchctl", "bootstrap", "gui/501", plan.plistURL.path])
        XCTAssertEqual(plan.disableCommand, ["launchctl", "bootout", "gui/501/com.thozha.companion-wifi-gateway"])
        XCTAssertEqual(plan.environmentKeys, [
            "JARVIS_COMPANION_WIFI_ENABLED", "JARVIS_COMPANION_WIFI_INTERFACE", "JARVIS_COMPANION_WIFI_ALLOWED_SSID",
            "JARVIS_COMPANION_WIFI_BIND_HOST", "JARVIS_COMPANION_WIFI_PORT", "JARVIS_COMPANION_WIFI_HOSTNAME",
            "JARVIS_COMPANION_TLS_PROXY_PYTHON", "JARVIS_COMPANION_WIFI_SSID_HELPER", "PATH",
        ])
        XCTAssertTrue(GatewayLaunchAgentPlanner.activationEnabled)

        let data = Data(plan.plistXML.utf8)
        let plist = try XCTUnwrap(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
        XCTAssertEqual(plist["Label"] as? String, "com.thozha.companion-wifi-gateway")
        XCTAssertEqual(plist["RunAtLoad"] as? Bool, true)
        XCTAssertEqual(plist["KeepAlive"] as? Bool, true)
        XCTAssertEqual(plist["ThrottleInterval"] as? Int, 30)
        XCTAssertEqual(plist["ProgramArguments"] as? [String], [runtime.shutdownSupervisor.path, runtime.openJarvisPython.path, "-m", "openjarvis.server.companion_wifi_gateway"])
        let environment = try XCTUnwrap(plist["EnvironmentVariables"] as? [String: String])
        XCTAssertEqual(environment["JARVIS_COMPANION_WIFI_ENABLED"], "1")
        XCTAssertEqual(environment["JARVIS_COMPANION_WIFI_ALLOWED_SSID"], "HomeNet")
        XCTAssertEqual(environment["JARVIS_COMPANION_WIFI_BIND_HOST"], "192.168.1.42")
        XCTAssertEqual(environment["JARVIS_COMPANION_WIFI_PORT"], "8443")
        XCTAssertEqual(environment["JARVIS_COMPANION_WIFI_HOSTNAME"], "thozha-12345678.local")
        XCTAssertEqual(environment["JARVIS_COMPANION_TLS_PROXY_PYTHON"], runtime.tlsPython.path)
        XCTAssertEqual(environment["JARVIS_COMPANION_WIFI_SSID_HELPER"], runtime.shutdownSupervisor.path)
        XCTAssertNil(environment["OPENJARVIS_API_KEY"])
        XCTAssertNil(environment["SUPERVISOR_API_KEY"])
        XCTAssertNil(environment["JARVIS_DEV_WORKER_CONFIG"])
        XCTAssertNil(environment["DEVICE_TOKEN"])
        XCTAssertFalse(plan.plistXML.localizedCaseInsensitiveContains("private key"))
        XCTAssertFalse(plan.plistXML.localizedCaseInsensitiveContains("api_key"))
        XCTAssertFalse(FileManager.default.fileExists(atPath: plan.plistURL.path), "plan generation must not write a LaunchAgent")
        XCTAssertFalse(FileManager.default.fileExists(atPath: home.appendingPathComponent("Library/LaunchAgents").path), "tests must not create user LaunchAgents")
    }

    func testDisablePlanOnlyTargetsThozhaGatewayAndPreservesPairingState() throws {
        let plan = try GatewayLaunchAgentPlanner.disablePlan(homeDirectory: home, userID: 501)
        XCTAssertEqual(plan.plistURL.path, "/tmp/thozha-gateway-plan-home/Library/LaunchAgents/com.thozha.companion-wifi-gateway.plist")
        XCTAssertEqual(plan.bootoutCommand, ["launchctl", "bootout", "gui/501/com.thozha.companion-wifi-gateway"])
        XCTAssertTrue(plan.removalDescription.contains(plan.plistURL.path))
        XCTAssertTrue(plan.preservedStateDescription.contains("pairing"))
        XCTAssertTrue(plan.preservedStateDescription.contains("task databases"))
        XCTAssertFalse(FileManager.default.fileExists(atPath: plan.plistURL.path))
    }

    func testGatewayManagerDoesNothingWithoutSeparateConfirmation() throws {
        let config = try XCTUnwrap(tryResult(GatewayPreflight.evaluate(snapshot: snapshot(), installationID: installID)))
        let plan = try GatewayLaunchAgentPlanner.enablePlan(configuration: config, runtime: runtime,
                                                             homeDirectory: home, userID: 501, ownerOptIn: true)
        let files = RecordingGatewayFiles()
        let launchctl = RecordingGatewayLaunchctl()
        let manager = GatewayLaunchAgentManager(files: files, launchctl: launchctl)
        XCTAssertThrowsError(try manager.enable(plan, userConfirmed: false)) {
            XCTAssertEqual($0 as? GatewayServiceManagerError, .confirmationRequired)
        }
        XCTAssertNil(files.read(plan.plistURL))
        XCTAssertTrue(launchctl.calls().isEmpty)
    }

    func testEnableWritesOwnerOnlyPlistThenBootstrapsOnlyPerUserJob() throws {
        let config = try XCTUnwrap(tryResult(GatewayPreflight.evaluate(snapshot: snapshot(), installationID: installID)))
        let plan = try GatewayLaunchAgentPlanner.enablePlan(configuration: config, runtime: runtime,
                                                             homeDirectory: home, userID: 501, ownerOptIn: true)
        let files = RecordingGatewayFiles()
        let launchctl = RecordingGatewayLaunchctl()
        try GatewayLaunchAgentManager(files: files, launchctl: launchctl).enable(plan, userConfirmed: true)
        XCTAssertEqual(files.mode(plan.plistURL), 0o600)
        XCTAssertEqual(files.read(plan.plistURL), Data(plan.plistXML.utf8))
        XCTAssertTrue(files.directories().contains(plan.logDirectoryURL.path))
        XCTAssertEqual(launchctl.calls(), [
            "print gui/501/com.thozha.companion-wifi-gateway",
            "bootstrap gui/501 \(plan.plistURL.path)",
        ])
    }

    func testEnableRefusesExistingFileAndRollsBackAfterFailedBootstrap() throws {
        let config = try XCTUnwrap(tryResult(GatewayPreflight.evaluate(snapshot: snapshot(), installationID: installID)))
        let plan = try GatewayLaunchAgentPlanner.enablePlan(configuration: config, runtime: runtime,
                                                             homeDirectory: home, userID: 501, ownerOptIn: true)
        let existingFiles = RecordingGatewayFiles()
        existingFiles.seed(plan.plistURL)
        let existingLaunchctl = RecordingGatewayLaunchctl()
        XCTAssertThrowsError(try GatewayLaunchAgentManager(files: existingFiles, launchctl: existingLaunchctl).enable(plan, userConfirmed: true)) {
            XCTAssertEqual($0 as? GatewayServiceManagerError, .launchAgentAlreadyExists)
        }
        XCTAssertEqual(existingFiles.read(plan.plistURL), Data("existing".utf8))
        XCTAssertTrue(existingLaunchctl.calls().isEmpty)

        let rollbackFiles = RecordingGatewayFiles()
        let failingLaunchctl = RecordingGatewayLaunchctl()
        failingLaunchctl.setBootstrapFailure(true)
        XCTAssertThrowsError(try GatewayLaunchAgentManager(files: rollbackFiles, launchctl: failingLaunchctl).enable(plan, userConfirmed: true))
        XCTAssertNil(rollbackFiles.read(plan.plistURL))
    }

    func testDisableRequiresConfirmationBootsOutAndRemovesOnlyItsPlist() throws {
        let disablePlan = try GatewayLaunchAgentPlanner.disablePlan(homeDirectory: home, userID: 501)
        let enableConfig = try XCTUnwrap(tryResult(GatewayPreflight.evaluate(snapshot: snapshot(), installationID: installID)))
        let enablePlan = try GatewayLaunchAgentPlanner.enablePlan(configuration: enableConfig, runtime: runtime,
                                                                   homeDirectory: home, userID: 501, ownerOptIn: true)
        let files = RecordingGatewayFiles()
        files.seed(enablePlan.plistURL, data: Data(enablePlan.plistXML.utf8))
        let launchctl = RecordingGatewayLaunchctl()
        launchctl.setLoaded(true)
        let manager = GatewayLaunchAgentManager(files: files, launchctl: launchctl)
        XCTAssertThrowsError(try manager.disable(disablePlan, userConfirmed: false)) {
            XCTAssertEqual($0 as? GatewayServiceManagerError, .confirmationRequired)
        }
        XCTAssertEqual(files.read(disablePlan.plistURL), Data(enablePlan.plistXML.utf8))
        XCTAssertTrue(launchctl.calls().isEmpty)
        try manager.disable(disablePlan, userConfirmed: true)
        XCTAssertNil(files.read(disablePlan.plistURL))
        XCTAssertEqual(launchctl.calls(), [
            "print gui/501/com.thozha.companion-wifi-gateway",
            "bootout gui/501/com.thozha.companion-wifi-gateway",
        ])
    }

    private func tryResult(_ result: Result<GatewayNetworkConfiguration, GatewayPreflightError>) -> GatewayNetworkConfiguration? {
        try? result.get()
    }
}
