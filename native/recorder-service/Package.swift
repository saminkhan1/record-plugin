// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "HermesRecorder",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .library(name: "HermesRecorderCore", targets: ["HermesRecorderCore"]),
        .executable(name: "hermes-recorder-service", targets: ["HermesRecorderService"]),
        .executable(name: "hermes-recorder", targets: ["HermesRecorderClient"]),
        .executable(name: "record-installer", targets: ["RecordInstaller"]),
    ],
    targets: [
        .target(
            name: "HermesRecorderCore",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("ApplicationServices"),
                .linkedFramework("Carbon"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("ScreenCaptureKit"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("CoreVideo"),
            ]
        ),
        .executableTarget(
            name: "HermesRecorderService",
            dependencies: ["HermesRecorderCore"]
        ),
        .executableTarget(
            name: "HermesRecorderClient",
            dependencies: ["HermesRecorderCore"]
        ),
        .executableTarget(
            name: "RecordInstaller",
            linkerSettings: [
                .linkedFramework("AppKit")
            ]
        ),
    ]
)
