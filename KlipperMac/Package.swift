// swift-tools-version: 5.9
import Foundation
import PackageDescription

// KlipperCam is a bare CLI binary, so it needs its own Info.plist inside the
// executable: macOS only shows the camera permission prompt when it can read
// NSCameraUsageDescription and a bundle identifier out of __TEXT.__info_plist.
let packageDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent().path
let camInfoPlist = packageDir + "/Resources/KlipperCam-Info.plist"

let package = Package(
    name: "KlipperMac",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "KlipperMac",
            path: "Sources/KlipperMac"
        ),
        .executableTarget(
            name: "KlipperCam",
            path: "Sources/KlipperCam",
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", camInfoPlist,
                ])
            ]
        )
    ]
)
