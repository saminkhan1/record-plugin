import Darwin
import Foundation

public final class UnixSocketServer {
    private let socketPath: String

    public init(socketPath: String) {
        self.socketPath = socketPath
    }

    public func serve(handler: (JSONDict) -> JSONDict) throws -> Never {
        let directory = URL(fileURLWithPath: socketPath).deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        unlink(socketPath)

        let serverFD = socket(AF_UNIX, SOCK_STREAM, 0)
        guard serverFD >= 0 else { throw RecorderError.socket(lastErrno("socket")) }
        var closeServer = true
        defer {
            if closeServer { close(serverFD) }
            unlink(socketPath)
        }

        var addr = try sockaddrUnix(path: socketPath)
        let bindResult = withUnsafePointer(to: &addr) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { rebound in
                Darwin.bind(serverFD, rebound, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bindResult == 0 else { throw RecorderError.socket(lastErrno("bind")) }
        chmod(socketPath, 0o600)
        guard listen(serverFD, 16) == 0 else { throw RecorderError.socket(lastErrno("listen")) }

        while true {
            let clientFD = accept(serverFD, nil, nil)
            if clientFD < 0 { continue }
            autoreleasepool {
                handle(clientFD: clientFD, handler: handler)
            }
        }
        closeServer = false
        fatalError("unreachable")
    }

    private func handle(clientFD: Int32, handler: (JSONDict) -> JSONDict) {
        defer { close(clientFD) }
        do {
            let requestData = try readLine(fd: clientFD)
            let request = try RecorderJSON.decodeDict(requestData)
            let response = handler(request)
            try writeLine(fd: clientFD, data: RecorderJSON.encodeLine(response))
        } catch {
            let response: JSONDict = ["ok": false, "error": String(describing: error)]
            if let data = try? RecorderJSON.encodeLine(response) {
                try? writeLine(fd: clientFD, data: data)
            }
        }
    }
}

public final class UnixSocketClient {
    private let socketPath: String

    public init(socketPath: String) {
        self.socketPath = socketPath
    }

    public func send(_ request: JSONDict) throws -> JSONDict {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw RecorderError.socket(lastErrno("socket")) }
        defer { close(fd) }

        var addr = try sockaddrUnix(path: socketPath)
        let connectResult = withUnsafePointer(to: &addr) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { rebound in
                Darwin.connect(fd, rebound, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connectResult == 0 else { throw RecorderError.socket(lastErrno("connect")) }
        try writeLine(fd: fd, data: RecorderJSON.encodeLine(request))
        return try RecorderJSON.decodeDict(readLine(fd: fd))
    }
}

private func sockaddrUnix(path: String) throws -> sockaddr_un {
    let bytes = Array(path.utf8) + [0]
    let capacity = MemoryLayout.size(ofValue: sockaddr_un().sun_path)
    guard bytes.count <= capacity else {
        throw RecorderError.socket("socket path is too long: \(path)")
    }
    var addr = sockaddr_un()
    addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    addr.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutableBytes(of: &addr.sun_path) { buffer in
        _ = buffer.initializeMemory(as: UInt8.self, from: bytes)
    }
    return addr
}

private func readLine(fd: Int32) throws -> Data {
    var data = Data()
    var byte: UInt8 = 0
    while true {
        let count = Darwin.read(fd, &byte, 1)
        if count == 0 { break }
        if count < 0 { throw RecorderError.socket(lastErrno("read")) }
        if byte == 0x0a { break }
        data.append(byte)
    }
    if data.isEmpty {
        throw RecorderError.socket("connection closed before a JSON line was received")
    }
    return data
}

private func writeLine(fd: Int32, data: Data) throws {
    try data.withUnsafeBytes { rawBuffer in
        guard let base = rawBuffer.baseAddress else { return }
        var written = 0
        while written < data.count {
            let result = Darwin.write(fd, base.advanced(by: written), data.count - written)
            if result < 0 { throw RecorderError.socket(lastErrno("write")) }
            written += result
        }
    }
}

private func lastErrno(_ operation: String) -> String {
    "\(operation) failed with errno \(errno): \(String(cString: strerror(errno)))"
}
