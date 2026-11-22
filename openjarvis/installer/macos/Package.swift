// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ThozhaSetup",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "ThozhaSetup", targets: ["ThozhaSetup"]), .library(name: "ThozhaInstallerCore", targets: ["ThozhaInstallerCore"])],
    targets: [
        .target(name: "ThozhaInstallerCore"),
        .executableTarget(name: "ThozhaSetup", dependencies: ["ThozhaInstallerCore"]),
        .testTarget(name: "ThozhaInstallerCoreTests", dependencies: ["ThozhaInstallerCore"])
    ]
)
