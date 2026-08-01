import Foundation

public typealias JSONDict = [String: Any]

public enum RecorderJSON {
    public static func encode(_ value: Any, pretty: Bool = false) throws -> Data {
        let options: JSONSerialization.WritingOptions = pretty ? [.prettyPrinted, .sortedKeys] : [.sortedKeys]
        return try JSONSerialization.data(withJSONObject: value, options: options)
    }

    public static func encodeLine(_ value: Any) throws -> Data {
        var data = try encode(value)
        data.append(0x0a)
        return data
    }

    public static func decodeDict(_ data: Data) throws -> JSONDict {
        let value = try JSONSerialization.jsonObject(with: data, options: [])
        guard let dict = value as? JSONDict else {
            throw RecorderError.invalidJSON("Expected a JSON object")
        }
        return dict
    }

    public static func string(_ value: Any) throws -> String {
        String(decoding: try encode(value, pretty: true), as: UTF8.self)
    }
}

public enum RecorderError: Error, CustomStringConvertible {
    case invalidJSON(String)
    case invalidState(String)
    case socket(String)
    case filesystem(String)

    public var description: String {
        switch self {
        case .invalidJSON(let message): return "invalid JSON: \(message)"
        case .invalidState(let message): return "invalid recorder state: \(message)"
        case .socket(let message): return "socket error: \(message)"
        case .filesystem(let message): return "filesystem error: \(message)"
        }
    }
}

public func isoTimestamp(_ date: Date = Date()) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: date)
}
