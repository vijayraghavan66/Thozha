import Foundation
import Darwin

public struct HostSnapshot: Sendable {
    public let macOSMajor: Int
    public let architecture: String
    public let physicalMemoryBytes: UInt64
    public let availableDiskBytes: Int64
    public let hasUv: Bool
    public let hasQemuArm64: Bool

    public init(macOSMajor: Int, architecture: String, physicalMemoryBytes: UInt64,
                availableDiskBytes: Int64, hasUv: Bool, hasQemuArm64: Bool) {
        self.macOSMajor = macOSMajor
        self.architecture = architecture
        self.physicalMemoryBytes = physicalMemoryBytes
        self.availableDiskBytes = availableDiskBytes
        self.hasUv = hasUv
        self.hasQemuArm64 = hasQemuArm64
    }
}

public enum CheckState: Equatable, Sendable { case pass, warning, blocked }

public struct PreflightItem: Equatable, Sendable {
    public let title: String
    public let detail: String
    public let state: CheckState
    public init(_ title: String, _ detail: String, _ state: CheckState) {
        self.title = title; self.detail = detail; self.state = state
    }
}

public enum Preflight {
    public static let minimumDiskBytes: Int64 = 20 * 1_000_000_000
    public static let minimumMemoryBytes: UInt64 = 8 * 1_073_741_824

    public static func evaluate(_ host: HostSnapshot) -> [PreflightItem] {
        let arm = host.architecture == "arm64"
        return [
            PreflightItem("macOS version", host.macOSMajor >= 14 ? "macOS 14 or newer" : "macOS 14 or newer is required", host.macOSMajor >= 14 ? .pass : .blocked),
            PreflightItem("Processor", arm ? "Apple silicon (arm64)" : "Apple silicon is required for the current isolated-worker setup", arm ? .pass : .blocked),
            PreflightItem("Memory", host.physicalMemoryBytes >= minimumMemoryBytes ? "At least 8 GB physical memory; 16 GB recommended when running an isolated worker" : "8 GB physical-memory minimum; 16 GB recommended when running an isolated worker", host.physicalMemoryBytes >= minimumMemoryBytes ? .pass : .blocked),
            PreflightItem("Disk space", host.availableDiskBytes >= minimumDiskBytes ? "At least 20 GB free for application and isolated-worker setup" : "20 GB free space is required before worker provisioning", host.availableDiskBytes >= minimumDiskBytes ? .pass : .blocked),
            PreflightItem("Python environment manager", host.hasUv ? "uv detected" : "uv is not installed; setup will not install it automatically", host.hasUv ? .pass : .warning),
            PreflightItem("Isolated Linux worker", host.hasQemuArm64 ? "QEMU ARM64 detected; a verified guest image and worker still need explicit provisioning" : "QEMU ARM64 not detected; isolated-worker provisioning is unavailable", host.hasQemuArm64 ? .warning : .blocked)
        ]
    }

    public static func canContinue(_ host: HostSnapshot) -> Bool {
        evaluate(host).allSatisfy { $0.state != .blocked }
    }
}

public enum HostInspector {
    public static func snapshot(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> HostSnapshot {
        let version = ProcessInfo.processInfo.operatingSystemVersion
        var system = utsname(); uname(&system)
        let arch = withUnsafePointer(to: &system.machine) {
            $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
        let values = try? home.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        let disk = values?.volumeAvailableCapacityForImportantUsage ?? 0
        let env = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        return HostSnapshot(macOSMajor: version.majorVersion, architecture: arch,
                            physicalMemoryBytes: ProcessInfo.processInfo.physicalMemory,
                            availableDiskBytes: disk,
                            hasUv: find("uv", path: env) != nil,
                            hasQemuArm64: find("qemu-system-aarch64", path: env) != nil)
    }

    private static func find(_ name: String, path: String) -> URL? {
        for folder in path.split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(folder)).appendingPathComponent(name)
            if FileManager.default.isExecutableFile(atPath: candidate.path) { return candidate }
        }
        return nil
    }
}

public struct NewInstallIdentity: Codable, Equatable, Sendable {
    public let id: UUID
    public let createdAt: Date
    public init(id: UUID = UUID(), createdAt: Date = Date()) { self.id = id; self.createdAt = createdAt }
}

public enum StagingIdentityStore {
    /// Creates a new installation identifier only in this app's staging directory.
    /// It is not an authentication credential and is never copied from Jarvis state.
    public static func createOrLoad(in directory: URL) throws -> NewInstallIdentity {
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let file = directory.appendingPathComponent("installation-id.json")
        if let bytes = try? Data(contentsOf: file) {
            return try JSONDecoder().decode(NewInstallIdentity.self, from: bytes)
        }
        let identity = NewInstallIdentity()
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let bytes = try encoder.encode(identity)
        let fd = open(file.path, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR)
        guard fd >= 0 else {
            if errno == EEXIST, let existing = try? Data(contentsOf: file) {
                return try JSONDecoder().decode(NewInstallIdentity.self, from: existing)
            }
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        defer { _ = close(fd) }
        try bytes.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var offset = 0
            while offset < raw.count {
                let written = Darwin.write(fd, base.advanced(by: offset), raw.count - offset)
                if written < 0 {
                    if errno == EINTR { continue }
                    throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
                }
                offset += written
            }
        }
        if fsync(fd) != 0 { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        return identity
    }
}

public enum LocalHealth {
    public static func check(url: URL = URL(string: "http://127.0.0.1:8000/health")!,
                             timeout: TimeInterval = 2) async -> Bool {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = timeout
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse, http.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return json["status"] as? String == "ok"
    }
}
