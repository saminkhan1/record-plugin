import AppKit
import Darwin
import Foundation

private enum InstallerFailure: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case .message(let text):
            return text
        }
    }
}

@main
@MainActor
struct RecordInstaller {
    private static let profilePattern =
        try! NSRegularExpression(pattern: "^[a-z0-9][a-z0-9_-]{0,63}$")

    static func main() {
        do {
            try launchInstaller()
            if shouldShowAlert {
                showAlert(
                    style: .informational,
                    message: "Record installed",
                    detail: "Restart the target Hermes profile backend in Hermes Desktop to load the plugin."
                )
            }
        } catch {
            let detail = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
            FileHandle.standardError.write(
                Data("Record installation failed: \(detail)\n".utf8)
            )
            if shouldShowAlert {
                showAlert(
                    style: .critical,
                    message: "Record installation failed",
                    detail: detail
                )
            }
            exit(1)
        }
    }

    private static var shouldShowAlert: Bool {
        ProcessInfo.processInfo.environment["TERM"] == nil
            && isatty(STDOUT_FILENO) == 0
    }

    private static func showAlert(
        style: NSAlert.Style,
        message: String,
        detail: String
    ) {
        NSApplication.shared.setActivationPolicy(.accessory)
        NSApplication.shared.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = style
        alert.messageText = message
        alert.informativeText = detail
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    private static func launchInstaller() throws {
        let arguments = Array(CommandLine.arguments.dropFirst())
        let appURL = Bundle.main.bundleURL.resolvingSymlinksInPath()
        guard appURL.pathExtension == "app" else {
            throw InstallerFailure.message(
                "Run the packaged Record Installer.app, not the raw launcher binary."
            )
        }
        try verifyInstallerApp(appURL)
        guard let resourceURL = Bundle.main.resourceURL else {
            throw InstallerFailure.message(
                "Installer.app is missing its Resources directory."
            )
        }
        let packageURL = resourceURL.appendingPathComponent(
            "payload",
            isDirectory: true
        )
        let scriptURL = packageURL.appendingPathComponent(
            "scripts/install-mac.py"
        )
        guard FileManager.default.fileExists(atPath: scriptURL.path) else {
            throw InstallerFailure.message(
                "Installer.app is missing scripts/install-mac.py."
            )
        }

        let requestedHome = argumentValue("--hermes-home", in: arguments)
            ?? ProcessInfo.processInfo.environment["HERMES_HOME"]
        let hermesHome = requestedHome.map(expandHome)
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".hermes", isDirectory: true)
        let hermesRoot = rootHome(for: hermesHome)
        let explicitPython = argumentValue("--python", in: arguments)
            .map(expandHome)
        let managedPython = explicitPython
            ?? hermesRoot
                .appendingPathComponent("hermes-agent", isDirectory: true)
                .appendingPathComponent("venv", isDirectory: true)
                .appendingPathComponent("bin", isDirectory: true)
                .appendingPathComponent("python")

        guard FileManager.default.isExecutableFile(atPath: managedPython.path) else {
            throw InstallerFailure.message(
                "Hermes Desktop's managed runtime is not ready at \(managedPython.path). "
                    + "Open Hermes Desktop and finish setup, then run this installer again."
            )
        }

        let environment = managedEnvironment(hermesRoot: hermesRoot)
        try probeHermesPython(managedPython, environment: environment)

        let process = Process()
        process.executableURL = managedPython
        process.arguments = [
            scriptURL.path,
            "--package",
            packageURL.path,
            "--installer-app",
            appURL.path,
            "--python",
            managedPython.path,
        ] + sanitizedForwardedArguments(arguments)
        var installerEnvironment = environment
        if requestedHome == nil {
            // Let the Python transaction follow Desktop's stock primary-profile
            // precedence: active-profile.json, then ~/.hermes/active_profile,
            // then default. HERMES_HOME here was needed only to probe the
            // managed runtime; leaving our synthesized root in the child would
            // pin installation to default.
            installerEnvironment.removeValue(forKey: "HERMES_HOME")
        }
        process.environment = installerEnvironment
        process.standardInput = FileHandle.standardInput
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
        try process.run()
        process.waitUntilExit()
        guard process.terminationReason == .exit, process.terminationStatus == 0 else {
            throw InstallerFailure.message(
                "The managed Python installer exited with status \(process.terminationStatus)."
            )
        }
    }

    private static func probeHermesPython(
        _ python: URL,
        environment: [String: String]
    ) throws {
        let probe = Process()
        probe.executableURL = python
        probe.arguments = [
            "-c",
            "import hermes_constants; import hermes_cli.main; import hermes_cli.plugins",
        ]
        probe.environment = environment
        probe.standardOutput = FileHandle.nullDevice
        probe.standardError = FileHandle.nullDevice
        try probe.run()
        probe.waitUntilExit()
        guard probe.terminationReason == .exit, probe.terminationStatus == 0 else {
            throw InstallerFailure.message(
                "The managed Python at \(python.path) cannot import Hermes. "
                    + "Open Hermes Desktop and finish or repair setup, then retry."
            )
        }
    }

    private static func verifyInstallerApp(_ appURL: URL) throws {
        guard let executableURL = Bundle.main.executableURL else {
            throw InstallerFailure.message(
                "Installer.app is missing its launcher executable."
            )
        }

        try runCodesign([
            "--verify",
            "--deep",
            "--strict",
            "--verbose=4",
            appURL.path,
        ])

        let launcherTeam = try codeSigningTeam(for: executableURL)
        let appTeam = try codeSigningTeam(for: appURL)
        guard launcherTeam == appTeam else {
            throw InstallerFailure.message(
                "Installer.app and its launcher are signed by different Apple teams."
            )
        }
    }

    private static func codeSigningTeam(for target: URL) throws -> String {
        let details = try runCodesign([
            "-d",
            "--verbose=4",
            target.path,
        ])
        let prefix = "TeamIdentifier="
        guard let line = details.split(whereSeparator: \.isNewline)
            .map(String.init)
            .first(where: { $0.hasPrefix(prefix) })
        else {
            throw InstallerFailure.message(
                "Installer signature is missing its Apple Team ID: \(target.path)"
            )
        }
        let teamID = String(line.dropFirst(prefix.count))
        let validTeamID = teamID.utf8.count == 10
            && teamID.utf8.allSatisfy {
                (48...57).contains($0) || (65...90).contains($0)
            }
        guard validTeamID else {
            throw InstallerFailure.message(
                "Installer signature has an invalid Apple Team ID: \(target.path)"
            )
        }
        return teamID
    }

    @discardableResult
    private static func runCodesign(_ arguments: [String]) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
        process.arguments = arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        process.waitUntilExit()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let detail = String(data: data, encoding: .utf8) ?? ""
        guard process.terminationReason == .exit,
              process.terminationStatus == 0
        else {
            throw InstallerFailure.message(
                "Installer signature verification failed"
                    + (detail.isEmpty ? "." : ": \(detail.trimmingCharacters(in: .whitespacesAndNewlines))")
            )
        }
        return detail
    }

    private static func managedEnvironment(hermesRoot: URL) -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment["HERMES_HOME"] = hermesRoot.path
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "VIRTUAL_ENV")
        return environment
    }

    private static func rootHome(for home: URL) -> URL {
        let standardized = home.standardizedFileURL
        let profile = standardized.lastPathComponent
        let profiles = standardized.deletingLastPathComponent()
        if profiles.lastPathComponent == "profiles", isProfileName(profile) {
            return profiles.deletingLastPathComponent()
        }
        return standardized
    }

    private static func isProfileName(_ value: String) -> Bool {
        let range = NSRange(value.startIndex..<value.endIndex, in: value)
        return profilePattern.firstMatch(in: value, range: range) != nil
    }

    private static func expandHome(_ raw: String) -> URL {
        let expanded = NSString(string: raw).expandingTildeInPath
        return URL(fileURLWithPath: expanded).standardizedFileURL
    }

    private static func argumentValue(
        _ name: String,
        in arguments: [String]
    ) -> String? {
        let inlinePrefix = "\(name)="
        if let inline = arguments.first(where: {
            $0.hasPrefix(inlinePrefix)
        }) {
            return String(inline.dropFirst(inlinePrefix.count))
        }
        guard let index = arguments.firstIndex(of: name) else {
            return nil
        }
        let valueIndex = arguments.index(after: index)
        guard valueIndex < arguments.endIndex else {
            return nil
        }
        return arguments[valueIndex]
    }

    private static func sanitizedForwardedArguments(
        _ arguments: [String]
    ) -> [String] {
        let launcherOwned = Set([
            "--package",
            "--installer-app",
            "--python",
        ])
        var result: [String] = []
        var index = arguments.startIndex
        while index < arguments.endIndex {
            let value = arguments[index]
            if launcherOwned.contains(where: {
                value.hasPrefix("\($0)=")
            }) {
                index = arguments.index(after: index)
                continue
            }
            if launcherOwned.contains(value) {
                index = arguments.index(after: index)
                if index < arguments.endIndex {
                    index = arguments.index(after: index)
                }
                continue
            }
            result.append(value)
            index = arguments.index(after: index)
        }
        return result
    }
}
