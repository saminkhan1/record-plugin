import Foundation
import Darwin

public struct RecorderLocations {
    public let root: URL
    public let socketPath: String

    public init(root: URL, socketPath: String? = nil) {
        self.root = root
        self.socketPath = socketPath ?? Self.defaultSocketPath(for: root)
    }

    public static func resolve(rootArg: String? = nil, socketArg: String? = nil) -> RecorderLocations {
        let env = ProcessInfo.processInfo.environment
        let root: URL
        if let raw = rootArg, !raw.isEmpty {
            root = URL(fileURLWithPath: NSString(string: raw).expandingTildeInPath)
        } else if let home = env["HERMES_HOME"], !home.isEmpty {
            root = URL(fileURLWithPath: NSString(string: home).expandingTildeInPath)
                .appendingPathComponent("recorder")
                .appendingPathComponent("event-streams")
        } else {
            let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
            root = support.appendingPathComponent("Hermes").appendingPathComponent("Recorder")
        }
        return RecorderLocations(root: root, socketPath: socketArg)
    }

    private static func defaultSocketPath(for root: URL) -> String {
        let hash = stableHash(root.path)
        return "/tmp/hermes-recorder-\(getuid())-\(hash).sock"
    }

    private static func stableHash(_ value: String) -> String {
        var hash: UInt64 = 1469598103934665603
        for byte in value.utf8 {
            hash ^= UInt64(byte)
            hash &*= 1099511628211
        }
        return String(hash, radix: 16)
    }
}

public struct CLIOptions {
    public var root: String?
    public var socket: String?
    public var json = false
    public var maxDurationSeconds: Int?
    public var showControls: Bool?
    public var settingsPane: String?
    public var permissionKey: String?
    public init() {}

    public static func parse(_ args: [String]) -> (commands: [String], options: CLIOptions) {
        var commands: [String] = []
        var options = CLIOptions()
        var index = 0
        while index < args.count {
            let arg = args[index]
            switch arg {
            case "--root":
                index += 1
                if index < args.count { options.root = args[index] }
            case "--socket":
                index += 1
                if index < args.count { options.socket = args[index] }
            case "--json":
                options.json = true
            case "--max-duration-seconds":
                index += 1
                if index < args.count { options.maxDurationSeconds = Int(args[index]) }
            case "--show-controls":
                options.showControls = true
            case "--hide-controls":
                options.showControls = false
            case "--settings-pane":
                index += 1
                if index < args.count { options.settingsPane = args[index] }
            case "--permission":
                index += 1
                if index < args.count { options.permissionKey = args[index] }
            default:
                commands.append(arg)
            }
            index += 1
        }
        return (commands, options)
    }
}
