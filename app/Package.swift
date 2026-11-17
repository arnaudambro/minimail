// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "Minimail",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "Minimail", path: "Sources/Minimail"),
        // Background helper: runs `minimail watch --emit` and shows native notifications.
        .executableTarget(name: "MinimailAgent", path: "Sources/MinimailAgent"),
    ]
)
