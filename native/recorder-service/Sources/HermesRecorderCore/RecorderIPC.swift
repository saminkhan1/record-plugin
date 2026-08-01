import Darwin
import Foundation

public struct RecorderIPC {
    public let locations: RecorderLocations

    public init(locations: RecorderLocations) {
        self.locations = locations
    }

    public func start(maxDurationSeconds: Int? = nil, showControls: Bool? = nil, autostartService: Bool = true) throws -> JSONDict {
        var params: JSONDict = [:]
        if let maxDurationSeconds {
            params["maxDurationSeconds"] = maxDurationSeconds
        }
        if let showControls {
            params["showControls"] = showControls
        }
        return try send(method: "event_stream_start", params: params, autostartService: autostartService)
    }

    public func status(autostartService: Bool = false) throws -> JSONDict {
        try send(method: "event_stream_status", params: [:], autostartService: autostartService)
    }

    public func stop(autostartService: Bool = true) throws -> JSONDict {
        try send(method: "event_stream_stop", params: [:], autostartService: autostartService)
    }

    public func requestPermissions(permissionKey: String? = nil, autostartService: Bool = true) throws -> JSONDict {
        var params: JSONDict = [:]
        if let permissionKey {
            params["permissionKey"] = permissionKey
        }
        return try send(method: "event_stream_request_permissions", params: params, autostartService: autostartService)
    }

    public func send(method: String, params: JSONDict, autostartService: Bool = true) throws -> JSONDict {
        let request: JSONDict = ["method": method, "params": params]
        do {
            return try sendOnce(request)
        } catch let error as RecorderError {
            guard autostartService, case .socket = error else { throw error }
            return try startServiceAndSend(request)
        }
    }

    private func sendOnce(_ request: JSONDict) throws -> JSONDict {
        try unwrap(UnixSocketClient(socketPath: locations.socketPath).send(request))
    }

    private func unwrap(_ response: JSONDict) throws -> JSONDict {
        if response["ok"] as? Bool == true {
            return response["result"] as? JSONDict ?? [:]
        }
        throw RecorderError.invalidState(response["error"] as? String ?? "recorder service returned an error")
    }

    private func startServiceAndSend(_ request: JSONDict) throws -> JSONDict {
        let launchLock = try RecorderServiceLaunchLock(socketPath: locations.socketPath)
        defer { launchLock.unlock() }

        if let result = try sendWhenAvailable(request, timeout: 0.35) {
            return result
        }
        try launchServiceIfPossible()
        if let result = try sendWhenAvailable(request, timeout: 2.5) {
            return result
        }
        throw RecorderError.socket("recorder service did not become available after launch")
    }

    private func sendWhenAvailable(_ request: JSONDict, timeout: TimeInterval) throws -> JSONDict? {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            do {
                return try sendOnce(request)
            } catch let error as RecorderError {
                guard case .socket = error else { throw error }
                Thread.sleep(forTimeInterval: 0.05)
            }
        } while Date() < deadline
        return nil
    }

    private func launchServiceIfPossible() throws {
        let clientURL = URL(fileURLWithPath: CommandLine.arguments[0])
        let clientDirectory = clientURL.deletingLastPathComponent()
        let serviceAppURL = clientDirectory.appendingPathComponent("Hermes Recorder Service.app")
        if FileManager.default.fileExists(atPath: serviceAppURL.path) {
            try launchServiceApp(serviceAppURL)
            return
        }

        let serviceURL = clientDirectory.appendingPathComponent("hermes-recorder-service")
        guard FileManager.default.isExecutableFile(atPath: serviceURL.path) else {
            throw RecorderError.invalidState("Hermes recorder service is not available at \(serviceAppURL.path) or \(serviceURL.path)")
        }
        let process = Process()
        process.executableURL = serviceURL
        process.arguments = ["--root", locations.root.path, "--socket", locations.socketPath]
        let null = FileHandle(forWritingAtPath: "/dev/null")
        if let null {
            process.standardOutput = null
            process.standardError = null
        }
        try process.run()
    }

    private func launchServiceApp(_ appURL: URL) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = [
            "-n",
            appURL.path,
            "--args",
            "--root",
            locations.root.path,
            "--socket",
            locations.socketPath,
        ]
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw RecorderError.invalidState("Failed to launch Hermes recorder service app at \(appURL.path)")
        }
    }
}

private final class RecorderServiceLaunchLock {
    private let fileDescriptor: Int32
    private var locked = true

    init(socketPath: String) throws {
        let path = socketPath + ".launch-lock"
        let descriptor = Darwin.open(path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else {
            throw RecorderError.socket("unable to open recorder launch lock: \(String(cString: strerror(errno)))")
        }
        guard flock(descriptor, LOCK_EX) == 0 else {
            let message = String(cString: strerror(errno))
            Darwin.close(descriptor)
            throw RecorderError.socket("unable to lock recorder launch: \(message)")
        }
        fileDescriptor = descriptor
    }

    func unlock() {
        guard locked else { return }
        locked = false
        _ = flock(fileDescriptor, LOCK_UN)
        Darwin.close(fileDescriptor)
    }

    deinit {
        unlock()
    }
}
