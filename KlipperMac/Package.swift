// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "KlipperMac",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "KlipperMac",
            path: "Sources/KlipperMac"
        )
    ]
)
