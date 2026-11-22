import CoreLocation
import CoreWLAN
import Combine
import Darwin
import Foundation

public enum GatewayLocationAuthorization: Equatable, Sendable {
    case notDetermined
    case authorized
    case denied
    case restricted
}

public struct GatewayWiFiSnapshot: Equatable, Sendable {
    public let interfaceName: String?
    public let ssid: String?
    public let privateIPv4: String?
    public let authorization: GatewayLocationAuthorization
    public let hasAmbiguousInterfaces: Bool

    public init(interfaceName: String?, ssid: String?, privateIPv4: String?, authorization: GatewayLocationAuthorization,
                hasAmbiguousInterfaces: Bool = false) {
        self.interfaceName = interfaceName
        self.ssid = ssid
        self.privateIPv4 = privateIPv4
        self.authorization = authorization
        self.hasAmbiguousInterfaces = hasAmbiguousInterfaces
    }
}

public struct GatewayNetworkConfiguration: Equatable, Sendable {
    public let interfaceName: String
    public let allowedSSID: String
    public let bindIPv4: String
    public let hostname: String
    public let port: Int

    public init(interfaceName: String, allowedSSID: String, bindIPv4: String, hostname: String, port: Int = 8443) {
        self.interfaceName = interfaceName
        self.allowedSSID = allowedSSID
        self.bindIPv4 = bindIPv4
        self.hostname = hostname
        self.port = port
    }
}

public enum GatewayPreflightError: Error, Equatable, LocalizedError {
    case noWifiInterface
    case multipleActiveInterfaces
    case locationPermissionRequired
    case locationPermissionDenied
    case locationPermissionRestricted
    case ssidUnavailable
    case invalidSSID
    case privateAddressUnavailable
    case addressNotPrivate
    case networkChanged

    public var errorDescription: String? {
        switch self {
        case .noWifiInterface: "No active Wi-Fi interface was detected. Connect this Mac to Wi-Fi first."
        case .multipleActiveInterfaces: "More than one Wi-Fi interface is active. Thozha cannot safely choose a bind interface automatically."
        case .locationPermissionRequired: "macOS has not authorized SSID access yet. Thozha needs Location Services only to verify the exact Wi-Fi network name before enabling the gateway."
        case .locationPermissionDenied: "macOS denied Location Services, so the current SSID cannot be verified. Enable Location Services for Thozha in System Settings; setup remains blocked until the SSID is visible."
        case .locationPermissionRestricted: "Location Services are restricted by macOS or device policy. The gateway cannot be enabled without exact SSID verification."
        case .ssidUnavailable: "CoreWLAN did not provide the current SSID. Thozha fails closed and will not infer or accept a manually entered network name."
        case .invalidSSID: "The Wi-Fi network name is unavailable or invalid. Setup remains blocked."
        case .privateAddressUnavailable: "No private IPv4 address was found on the active Wi-Fi interface."
        case .addressNotPrivate: "The detected Wi-Fi address is not an RFC1918 private IPv4 address. The gateway will not bind."
        case .networkChanged: "The Wi-Fi interface, exact SSID or private address changed after preflight. Recheck before continuing."
        }
    }
}

public enum GatewayPreflight {
    public static func evaluate(snapshot: GatewayWiFiSnapshot, installationID: UUID) -> Result<GatewayNetworkConfiguration, GatewayPreflightError> {
        guard !snapshot.hasAmbiguousInterfaces else { return .failure(.multipleActiveInterfaces) }
        guard let interface = snapshot.interfaceName, !interface.isEmpty else { return .failure(.noWifiInterface) }
        guard let ssid = snapshot.ssid else {
            switch snapshot.authorization {
            case .notDetermined: return .failure(.locationPermissionRequired)
            case .denied: return .failure(.locationPermissionDenied)
            case .restricted: return .failure(.locationPermissionRestricted)
            case .authorized: return .failure(.ssidUnavailable)
            }
        }
        guard !ssid.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              ssid.utf8.count <= 128,
              !ssid.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f })
        else { return .failure(.invalidSSID) }
        guard let address = snapshot.privateIPv4 else { return .failure(.privateAddressUnavailable) }
        guard isRFC1918IPv4(address) else { return .failure(.addressNotPrivate) }
        let suffix = installationID.uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(8)
        let hostname = "thozha-\(suffix).local"
        return .success(GatewayNetworkConfiguration(interfaceName: interface, allowedSSID: ssid,
                                                     bindIPv4: address, hostname: hostname))
    }

    public static func isRFC1918IPv4(_ address: String) -> Bool {
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
}

@MainActor public struct CoreWLANWiFiSnapshotProvider {
    public init() { }

    public func snapshot() -> GatewayWiFiSnapshot {
        let authorization = Self.locationAuthorization()
        let client = CWWiFiClient.shared()
        let interfaces = client.interfaces() ?? []
        let stationInterfaces = interfaces.filter { $0.powerOn() && $0.interfaceMode() == .station }
        guard !stationInterfaces.isEmpty else {
            return GatewayWiFiSnapshot(interfaceName: nil, ssid: nil, privateIPv4: nil, authorization: authorization)
        }
        let active = stationInterfaces.compactMap { interface -> GatewayWiFiSnapshot? in
            guard let name = interface.interfaceName, !name.isEmpty else { return nil }
            return GatewayWiFiSnapshot(interfaceName: name, ssid: interface.ssid(),
                                       privateIPv4: Self.ipv4Address(on: name), authorization: authorization)
        }
        guard active.count == 1, let selected = active.first else {
            return GatewayWiFiSnapshot(interfaceName: nil, ssid: nil, privateIPv4: nil,
                                       authorization: authorization, hasAmbiguousInterfaces: active.count > 1)
        }
        return selected
    }

    private static func locationAuthorization() -> GatewayLocationAuthorization {
        let manager = CLLocationManager()
        return switch manager.authorizationStatus {
        case .notDetermined: .notDetermined
        case .authorized, .authorizedAlways, .authorizedWhenInUse: .authorized
        case .denied: .denied
        case .restricted: .restricted
        @unknown default: .restricted
        }
    }

    private static func ipv4Address(on interfaceName: String) -> String? {
        var first: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&first) == 0, let head = first else { return nil }
        defer { freeifaddrs(first) }
        var node: UnsafeMutablePointer<ifaddrs>? = head
        while let current = node {
            let record = current.pointee
            if let name = record.ifa_name, String(cString: name) == interfaceName,
               let address = record.ifa_addr, address.pointee.sa_family == UInt8(AF_INET) {
                var ipv4 = address.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee.sin_addr }
                var buffer = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                let rendered = buffer.withUnsafeMutableBufferPointer { output in
                    inet_ntop(AF_INET, &ipv4, output.baseAddress, socklen_t(INET_ADDRSTRLEN))
                }
                if rendered != nil {
                    let bytes = buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }
                    return String(decoding: bytes, as: UTF8.self)
                }
            }
            node = record.ifa_next
        }
        return nil
    }
}

public protocol CurrentWiFiSSIDReading {
    func currentSSID(interfaceName: String) -> String?
}

public struct CoreWLANCurrentWiFiSSIDReader: CurrentWiFiSSIDReading {
    public init() { }

    public func currentSSID(interfaceName: String) -> String? {
        guard interfaceName.range(of: #"^[A-Za-z0-9._-]{1,32}$"#, options: .regularExpression) != nil,
              let interface = CWWiFiClient.shared().interface(withName: interfaceName),
              interface.powerOn(), interface.interfaceMode() == .station,
              let ssid = interface.ssid(), GatewaySSIDHelper.isSafeSSID(ssid) else { return nil }
        return ssid
    }
}

/// Pure command and output validation shared by the bundled helper and tests.
/// Missing CoreWLAN/TCC data is represented only as failure; there is no fallback.
public enum GatewaySSIDHelper {
    public static func output(arguments: [String], reader: any CurrentWiFiSSIDReading) -> String? {
        guard arguments.count == 2, arguments[0] == "--ssid",
              arguments[1].range(of: #"^[A-Za-z0-9._-]{1,32}$"#, options: .regularExpression) != nil,
              let ssid = reader.currentSSID(interfaceName: arguments[1]), isSafeSSID(ssid) else { return nil }
        return ssid + "\n"
    }

    public static func isSafeSSID(_ ssid: String) -> Bool {
        !ssid.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && ssid.utf8.count <= 128
            && !ssid.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f })
    }

    public static func matchesAllowedSSID(_ currentSSID: String?, allowedSSID: String) -> Bool {
        guard isSafeSSID(allowedSSID), let currentSSID, isSafeSSID(currentSSID) else { return false }
        return currentSSID == allowedSSID
    }
}

@MainActor public final class GatewayLocationPermissionRequester: NSObject, ObservableObject, @preconcurrency CLLocationManagerDelegate {
    @Published public private(set) var authorization: GatewayLocationAuthorization = .notDetermined
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<GatewayLocationAuthorization, Never>?

    public override init() {
        super.init()
        authorization = Self.map(manager.authorizationStatus)
        manager.delegate = self
    }

    /// Call only after an explicit user tap explaining the SSID verification need.
    public func requestWhenInUseIfNeeded() async -> GatewayLocationAuthorization {
        guard authorization == .notDetermined else { return authorization }
        return await withCheckedContinuation { continuation in
            self.continuation = continuation
            manager.requestWhenInUseAuthorization()
        }
    }

    public func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let value = Self.map(manager.authorizationStatus)
        authorization = value
        if value != .notDetermined, let continuation {
            self.continuation = nil
            continuation.resume(returning: value)
        }
    }

    private static func map(_ status: CLAuthorizationStatus) -> GatewayLocationAuthorization {
        switch status {
        case .notDetermined: .notDetermined
        case .authorized, .authorizedAlways, .authorizedWhenInUse: .authorized
        case .denied: .denied
        case .restricted: .restricted
        @unknown default: .restricted
        }
    }
}

public struct GatewayRuntime: Equatable, Sendable {
    public let openJarvisPython: URL
    public let tlsPython: URL
    public let shutdownSupervisor: URL
    public let launchdPath: String

    public init(openJarvisPython: URL, tlsPython: URL, shutdownSupervisor: URL, launchdPath: String) {
        self.openJarvisPython = openJarvisPython
        self.tlsPython = tlsPython
        self.shutdownSupervisor = shutdownSupervisor
        self.launchdPath = launchdPath
    }
}

public enum GatewayRuntimeError: Error, Equatable, LocalizedError {
    case openJarvisRuntimeUnavailable
    case tlsRuntimeUnavailable
    case shutdownSupervisorUnavailable
    case setupAppMustBeInstalled

    public var errorDescription: String? {
        switch self {
        case .openJarvisRuntimeUnavailable: "A verified Python environment containing the OpenJarvis Wi-Fi gateway was not found. The installer will not guess or use system Python."
        case .tlsRuntimeUnavailable: "The required Python 3.14 / OpenSSL 3.6 TLS frontend was not found. No download or package-manager install was started."
        case .shutdownSupervisorUnavailable: "The signed Thozha gateway shutdown supervisor is missing. It is required to stop the gateway and clean up its TLS and DNS-SD children safely."
        case .setupAppMustBeInstalled: "Install Thozha Setup in /Applications before enabling a login-time gateway. A LaunchAgent must not reference a movable app bundle."
        }
    }
}

public struct GatewayRuntimeDetector: Sendable {
    private let home: URL
    private let path: String

    public init(home: URL = FileManager.default.homeDirectoryForCurrentUser,
                path: String = ProcessInfo.processInfo.environment["PATH"] ?? "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin") {
        self.home = home
        self.path = path
    }

    public func detect(shutdownSupervisor: URL) throws -> GatewayRuntime {
        guard shutdownSupervisor.isFileURL, FileManager.default.isExecutableFile(atPath: shutdownSupervisor.path) else {
            throw GatewayRuntimeError.shutdownSupervisorUnavailable
        }
        guard let python = locateOpenJarvisPython(), Self.run(python, arguments: ["-B", "-I", "-c", "import openjarvis.server.companion_wifi_gateway"]) else {
            throw GatewayRuntimeError.openJarvisRuntimeUnavailable
        }
        let tlsCandidates = Self.executableCandidates("python3.14", path: path, home: home)
            + [URL(fileURLWithPath: "/opt/homebrew/bin/python3.14"), URL(fileURLWithPath: "/usr/local/bin/python3.14")]
        guard let tlsPython = tlsCandidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0.path)
            && Self.run($0, arguments: ["-B", "-I", "-c", "import ssl,sys; sys.exit(0 if sys.version_info >= (3,14) and ssl.OPENSSL_VERSION_INFO >= (3,6) else 1)"])
        }) else { throw GatewayRuntimeError.tlsRuntimeUnavailable }
        return GatewayRuntime(openJarvisPython: python, tlsPython: tlsPython, shutdownSupervisor: shutdownSupervisor,
                              launchdPath: "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    }

    private func locateOpenJarvisPython() -> URL? {
        let launchers = Self.executableCandidates("jarvis", path: path, home: home)
            + [home.appendingPathComponent(".local/bin/jarvis"), URL(fileURLWithPath: "/opt/homebrew/bin/jarvis"), URL(fileURLWithPath: "/usr/local/bin/jarvis")]
        for launcher in launchers where FileManager.default.isExecutableFile(atPath: launcher.path) {
            guard let firstLine = try? String(contentsOf: launcher, encoding: .utf8).components(separatedBy: .newlines).first,
                  firstLine.hasPrefix("#!") else { continue }
            let words = firstLine.dropFirst(2).split(whereSeparator: \.isWhitespace).map(String.init)
            guard let executableText = words.first else { continue }
            if executableText == "/usr/bin/env", words.count == 2, words[1].hasPrefix("python"),
               let candidate = Self.executableCandidates(words[1], path: path, home: home).first,
               FileManager.default.isExecutableFile(atPath: candidate.path) { return candidate }
            let candidate = URL(fileURLWithPath: executableText)
            if candidate.lastPathComponent.hasPrefix("python"), FileManager.default.isExecutableFile(atPath: candidate.path) { return candidate }
        }
        return nil
    }

    private static func executableCandidates(_ name: String, path: String, home: URL) -> [URL] {
        var folders = path.split(separator: ":").map { URL(fileURLWithPath: String($0), isDirectory: true) }
        folders += [home.appendingPathComponent(".local/bin", isDirectory: true), URL(fileURLWithPath: "/opt/homebrew/bin", isDirectory: true), URL(fileURLWithPath: "/usr/local/bin", isDirectory: true)]
        var seen = Set<String>()
        return folders.map { $0.appendingPathComponent(name) }.filter { seen.insert($0.path).inserted }
    }

    private static func run(_ executable: URL, arguments: [String]) -> Bool {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = ["HOME": FileManager.default.homeDirectoryForCurrentUser.path,
                               "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                               "PYTHONNOUSERSITE": "1"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let completion = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in completion.signal() }
        do {
            try process.run()
            guard completion.wait(timeout: .now() + 10) == .success else {
                process.terminate()
                _ = completion.wait(timeout: .now() + 1)
                return false
            }
            return process.terminationStatus == 0
        } catch { return false }
    }
}

public enum GatewayPlanError: Error, Equatable, LocalizedError {
    case ownerOptInRequired
    case invalidHome
    case invalidRuntimePath

    public var errorDescription: String? {
        switch self {
        case .ownerOptInRequired: "Select the explicit Wi-Fi gateway opt-in before generating the enable plan."
        case .invalidHome: "The per-user LaunchAgents destination is invalid."
        case .invalidRuntimePath: "The verified gateway Python paths must be absolute."
        }
    }
}

public struct GatewayEnablePlan: Equatable, Sendable {
    public let plistURL: URL
    public let logDirectoryURL: URL
    public let plistXML: String
    public let fileMode: Int
    public let launchdDomain: String
    public let bootstrapCommand: [String]
    public let disableCommand: [String]
    public let environmentKeys: Set<String>
}

public struct GatewayDisablePlan: Equatable, Sendable {
    public let plistURL: URL
    public let launchdDomain: String
    public let bootoutCommand: [String]
    public let removalDescription: String
    public let preservedStateDescription: String
}

public enum GatewayLaunchAgentPlanner {
    public static let label = "com.thozha.companion-wifi-gateway"
    public static let activationEnabled = true

    public static func enablePlan(configuration: GatewayNetworkConfiguration, runtime: GatewayRuntime,
                                  homeDirectory: URL, userID: UInt32, ownerOptIn: Bool) throws -> GatewayEnablePlan {
        guard ownerOptIn else { throw GatewayPlanError.ownerOptInRequired }
        guard homeDirectory.isFileURL, homeDirectory.path.hasPrefix("/") else { throw GatewayPlanError.invalidHome }
        guard runtime.openJarvisPython.isFileURL, runtime.openJarvisPython.path.hasPrefix("/"),
              runtime.tlsPython.isFileURL, runtime.tlsPython.path.hasPrefix("/"),
              runtime.shutdownSupervisor.isFileURL, runtime.shutdownSupervisor.path.hasPrefix("/"),
              configuration.port == 8443,
              configuration.hostname.range(of: #"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local$"#, options: .regularExpression) != nil,
              !configuration.allowedSSID.isEmpty, configuration.allowedSSID.utf8.count <= 128,
              !configuration.allowedSSID.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f }),
              GatewayPreflight.isRFC1918IPv4(configuration.bindIPv4) else { throw GatewayPlanError.invalidRuntimePath }

        let plistURL = homeDirectory.appendingPathComponent("Library/LaunchAgents/\(label).plist")
        let logDirectory = homeDirectory.appendingPathComponent("Library/Logs/Thozha", isDirectory: true)
        let environment: [String: String] = [
            "JARVIS_COMPANION_WIFI_ENABLED": "1",
            "JARVIS_COMPANION_WIFI_INTERFACE": configuration.interfaceName,
            "JARVIS_COMPANION_WIFI_ALLOWED_SSID": configuration.allowedSSID,
            "JARVIS_COMPANION_WIFI_BIND_HOST": configuration.bindIPv4,
            "JARVIS_COMPANION_WIFI_PORT": "8443",
            "JARVIS_COMPANION_WIFI_HOSTNAME": configuration.hostname,
            "JARVIS_COMPANION_TLS_PROXY_PYTHON": runtime.tlsPython.path,
            "JARVIS_COMPANION_WIFI_SSID_HELPER": runtime.shutdownSupervisor.path,
            "PATH": runtime.launchdPath,
        ]
        let plist: [String: Any] = [
            "Label": label,
            "ProgramArguments": [runtime.shutdownSupervisor.path, runtime.openJarvisPython.path, "-m", "openjarvis.server.companion_wifi_gateway"],
            "RunAtLoad": true,
            "KeepAlive": true,
            "ProcessType": "Background",
            "ThrottleInterval": 30,
            "EnvironmentVariables": environment,
            "StandardOutPath": logDirectory.appendingPathComponent("companion-wifi-gateway.stdout.log").path,
            "StandardErrorPath": logDirectory.appendingPathComponent("companion-wifi-gateway.stderr.log").path,
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        guard let xml = String(data: data, encoding: .utf8) else { throw GatewayPlanError.invalidRuntimePath }
        let domain = "gui/\(userID)"
        return GatewayEnablePlan(plistURL: plistURL, logDirectoryURL: logDirectory, plistXML: xml, fileMode: 0o600,
                                 launchdDomain: domain,
                                 bootstrapCommand: ["launchctl", "bootstrap", domain, plistURL.path],
                                 disableCommand: ["launchctl", "bootout", "\(domain)/\(label)"],
                                 environmentKeys: Set(environment.keys))
    }

    public static func disablePlan(homeDirectory: URL, userID: UInt32) throws -> GatewayDisablePlan {
        guard homeDirectory.isFileURL, homeDirectory.path.hasPrefix("/") else { throw GatewayPlanError.invalidHome }
        let plistURL = homeDirectory.appendingPathComponent("Library/LaunchAgents/\(label).plist")
        return GatewayDisablePlan(plistURL: plistURL, launchdDomain: "gui/\(userID)",
                                  bootoutCommand: ["launchctl", "bootout", "gui/\(userID)/\(label)"],
                                  removalDescription: "After the per-user job is stopped, remove only \(plistURL.path).",
                                  preservedStateDescription: "Keep OpenJarvis, pairing, the server identity, device tokens, and task databases. Gateway TLS identity and logs are preserved for diagnostics.")
    }
}

public enum GatewayServiceManagerError: Error, Equatable, LocalizedError {
    case confirmationRequired
    case launchAgentAlreadyExists
    case launchAgentAlreadyLoaded
    case unsafeLaunchAgentPath
    case launchctlFailed(Int)
    case activationStateUncertain(String)

    public var errorDescription: String? {
        switch self {
        case .confirmationRequired: "Review the plan and explicitly confirm before changing this user's LaunchAgent."
        case .launchAgentAlreadyExists: "The Thozha gateway LaunchAgent file already exists. It was not overwritten. Review or disable that installation first."
        case .launchAgentAlreadyLoaded: "The Thozha gateway job is already loaded for this user. No duplicate was started."
        case .unsafeLaunchAgentPath: "The expected per-user LaunchAgent path is a symlink or non-regular file. No file was changed."
        case .launchctlFailed(let code): "launchctl returned status \(code). Review the LaunchAgent file and service state before retrying."
        case .activationStateUncertain(let path): "launchctl returned an error and the job state could not be verified. The reviewed plist was preserved at \(path). Do not retry until you inspect the job."
        }
    }
}

public protocol GatewayServiceFileOperations: Sendable {
    func existsOrSymlink(at url: URL) throws -> Bool
    func createPrivateDirectory(_ url: URL) throws
    func writeExclusive(_ data: Data, to url: URL, mode: Int) throws
    func removeRegularFile(_ url: URL) throws
}

public struct LocalGatewayServiceFileOperations: GatewayServiceFileOperations {
    public init() { }

    public func existsOrSymlink(at url: URL) throws -> Bool {
        try ensureNoSymlinkAncestors(url.deletingLastPathComponent())
        var metadata = stat()
        if lstat(url.path, &metadata) == 0 { return true }
        if errno == ENOENT { return false }
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }

    public func createPrivateDirectory(_ url: URL) throws {
        try ensureNoSymlinkAncestors(url.deletingLastPathComponent())
        if try existsOrSymlink(at: url) {
            var metadata = stat()
            guard lstat(url.path, &metadata) == 0, (metadata.st_mode & S_IFMT) == S_IFDIR else {
                throw GatewayServiceManagerError.unsafeLaunchAgentPath
            }
            return
        }
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        guard chmod(url.path, mode_t(0o700)) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    }

    public func writeExclusive(_ data: Data, to url: URL, mode: Int) throws {
        try ensureNoSymlinkAncestors(url.deletingLastPathComponent())
        let descriptor = open(url.path, O_WRONLY | O_CREAT | O_EXCL, mode_t(mode))
        guard descriptor >= 0 else {
            if errno == EEXIST { throw GatewayServiceManagerError.launchAgentAlreadyExists }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        var keepFile = false
        defer {
            _ = close(descriptor)
            if !keepFile { _ = unlink(url.path) }
        }
        guard fchmod(descriptor, mode_t(mode)) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        try data.withUnsafeBytes { raw in
            guard let start = raw.baseAddress else { return }
            var offset = 0
            while offset < raw.count {
                let result = Darwin.write(descriptor, start.advanced(by: offset), raw.count - offset)
                if result < 0 {
                    if errno == EINTR { continue }
                    throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
                }
                offset += result
            }
        }
        guard fsync(descriptor) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        keepFile = true
    }

    public func removeRegularFile(_ url: URL) throws {
        try ensureNoSymlinkAncestors(url.deletingLastPathComponent())
        var metadata = stat()
        if lstat(url.path, &metadata) != 0 {
            if errno == ENOENT { return }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        guard (metadata.st_mode & S_IFMT) == S_IFREG else { throw GatewayServiceManagerError.unsafeLaunchAgentPath }
        guard unlink(url.path) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    }

    private func ensureNoSymlinkAncestors(_ url: URL) throws {
        var current = url.standardizedFileURL
        while current.path != "/" {
            var metadata = stat()
            if lstat(current.path, &metadata) == 0 {
                guard (metadata.st_mode & S_IFMT) != S_IFLNK else { throw GatewayServiceManagerError.unsafeLaunchAgentPath }
            } else if errno != ENOENT {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            current.deleteLastPathComponent()
        }
    }
}

public protocol GatewayLaunchctlOperations: Sendable {
    func isLoaded(domain: String, label: String) throws -> Bool
    func bootstrap(domain: String, plistURL: URL) throws
    func bootout(domain: String, label: String) throws
}

public struct SystemGatewayLaunchctlOperations: GatewayLaunchctlOperations {
    private let executable = URL(fileURLWithPath: "/bin/launchctl")
    public init() { }

    public func isLoaded(domain: String, label: String) throws -> Bool {
        let result = try run(["print", "\(domain)/\(label)"])
        if result.status == 0 { return true }
        if result.status == 113 || result.output.localizedCaseInsensitiveContains("could not find service")
            || result.output.localizedCaseInsensitiveContains("no such process") { return false }
        throw GatewayServiceManagerError.launchctlFailed(Int(result.status))
    }

    public func bootstrap(domain: String, plistURL: URL) throws {
        let result = try run(["bootstrap", domain, plistURL.path])
        guard result.status == 0 else { throw GatewayServiceManagerError.launchctlFailed(Int(result.status)) }
    }

    public func bootout(domain: String, label: String) throws {
        let result = try run(["bootout", "\(domain)/\(label)"])
        guard result.status == 0 else { throw GatewayServiceManagerError.launchctlFailed(Int(result.status)) }
    }

    private func run(_ arguments: [String]) throws -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        let done = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in done.signal() }
        do {
            try process.run()
            guard done.wait(timeout: .now() + 10) == .success else {
                process.terminate()
                throw GatewayServiceManagerError.launchctlFailed(-1)
            }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            // Keep output in memory only for known not-loaded classification; never log it.
            return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
        } catch let error as GatewayServiceManagerError { throw error }
        catch { throw GatewayServiceManagerError.launchctlFailed(-1) }
    }
}

public struct GatewayLaunchAgentManager: Sendable {
    private let files: any GatewayServiceFileOperations
    private let launchctl: any GatewayLaunchctlOperations

    public init(files: any GatewayServiceFileOperations = LocalGatewayServiceFileOperations(),
                launchctl: any GatewayLaunchctlOperations = SystemGatewayLaunchctlOperations()) {
        self.files = files
        self.launchctl = launchctl
    }

    public func enable(_ plan: GatewayEnablePlan, userConfirmed: Bool) throws {
        guard userConfirmed else { throw GatewayServiceManagerError.confirmationRequired }
        guard !(try files.existsOrSymlink(at: plan.plistURL)) else { throw GatewayServiceManagerError.launchAgentAlreadyExists }
        let domain = plan.launchdDomain
        let label = GatewayLaunchAgentPlanner.label
        guard !(try launchctl.isLoaded(domain: domain, label: label)) else { throw GatewayServiceManagerError.launchAgentAlreadyLoaded }
        try files.createPrivateDirectory(plan.plistURL.deletingLastPathComponent())
        try files.createPrivateDirectory(plan.logDirectoryURL)
        try files.writeExclusive(Data(plan.plistXML.utf8), to: plan.plistURL, mode: plan.fileMode)
        do {
            try launchctl.bootstrap(domain: domain, plistURL: plan.plistURL)
        } catch {
            if (try? launchctl.isLoaded(domain: domain, label: label)) == true {
                throw GatewayServiceManagerError.activationStateUncertain(plan.plistURL.path)
            }
            try? files.removeRegularFile(plan.plistURL)
            throw error
        }
    }

    public func disable(_ plan: GatewayDisablePlan, userConfirmed: Bool) throws {
        guard userConfirmed else { throw GatewayServiceManagerError.confirmationRequired }
        let domain = plan.launchdDomain
        let label = GatewayLaunchAgentPlanner.label
        if try launchctl.isLoaded(domain: domain, label: label) {
            try launchctl.bootout(domain: domain, label: label)
        }
        try files.removeRegularFile(plan.plistURL)
    }
}
