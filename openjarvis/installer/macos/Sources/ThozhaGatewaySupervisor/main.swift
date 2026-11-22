import Darwin
import Dispatch
import Foundation

/// launchd sends SIGTERM when the owner disables the per-user job. The gateway
/// runner performs its TLS-proxy, DNS-SD and loopback cleanup in `finally` on
/// SIGINT/KeyboardInterrupt, so this small wrapper translates the signal and
/// waits for the runner rather than orphaning its detached children.
@main
struct ThozhaGatewaySupervisor {
    static func main() {
        guard CommandLine.arguments.count >= 3 else {
            fputs("Thozha gateway supervisor requires an interpreter and gateway module arguments.\n", stderr)
            exit(64)
        }

        let python = URL(fileURLWithPath: CommandLine.arguments[1])
        let process = Process()
        process.executableURL = python
        process.arguments = Array(CommandLine.arguments.dropFirst(2))
        process.environment = ProcessInfo.processInfo.environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError

        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        let signalSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global(qos: .utility))
        let signalLock = NSLock()
        var stopping = false
        signalSource.setEventHandler {
            signalLock.lock()
            let shouldSignal = !stopping
            stopping = true
            signalLock.unlock()
            if shouldSignal && process.isRunning { process.interrupt() }
        }
        signalSource.resume()

        do {
            try process.run()
            process.waitUntilExit()
            signalSource.cancel()
            exit(process.terminationStatus)
        } catch {
            signalSource.cancel()
            fputs("Thozha gateway supervisor could not start the verified Python runtime.\n", stderr)
            exit(70)
        }
    }
}
