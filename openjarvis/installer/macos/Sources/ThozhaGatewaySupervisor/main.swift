import Darwin
import Dispatch
import Foundation
import ThozhaInstallerCore

/// launchd sends SIGTERM when the owner disables the per-user job. The gateway
/// runner performs its TLS-proxy, DNS-SD and loopback cleanup in `finally` on
/// SIGINT/KeyboardInterrupt, so this small wrapper translates the signal and
/// waits for the runner rather than orphaning its detached children.
@main
struct ThozhaGatewaySupervisor {
    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments.first == "--ssid" {
            guard let current = GatewaySSIDHelper.output(arguments: arguments,
                                                         reader: CoreWLANCurrentWiFiSSIDReader()) else {
                // No SSID, invalid interface, or TCC/CoreWLAN denial: nonzero
                // with no stdout and no SSID in diagnostics.
                exit(78)
            }
            FileHandle.standardOutput.write(Data(current.utf8))
            exit(0)
        }

        guard arguments.count >= 3 else {
            fputs("Thozha gateway supervisor requires an interpreter and gateway module arguments.\n", stderr)
            exit(64)
        }

        let python = URL(fileURLWithPath: arguments[0])
        let process = Process()
        process.executableURL = python
        process.arguments = Array(arguments.dropFirst())
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
