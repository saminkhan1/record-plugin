import AppKit
import HermesRecorderCore

@main
struct HermesRecorderCLI {
    static func main() {
        let parsed = CLIOptions.parse(Array(CommandLine.arguments.dropFirst()))
        let commands = parsed.commands
        let locations = RecorderLocations.resolve(rootArg: parsed.options.root, socketArg: parsed.options.socket)
        let ipc = RecorderIPC(locations: locations)

        if commands.count >= 2, commands[0] == "event-stream" {
            if commands[1] == "mcp" {
                runMCP(ipc: ipc, maxDurationSeconds: parsed.options.maxDurationSeconds, showControls: parsed.options.showControls)
            } else {
                runDirect(
                    commands[1],
                    ipc: ipc,
                    maxDurationSeconds: parsed.options.maxDurationSeconds,
                    showControls: parsed.options.showControls,
                    settingsPane: parsed.options.settingsPane,
                    permissionKey: parsed.options.permissionKey
                )
            }
        } else {
            fail("usage: hermes-recorder event-stream <start|status|permission-status|stop|request-permissions|open-settings|mcp> [--root PATH] [--socket PATH] [--json] [--permission KEY] [--settings-pane PANE] [--show-controls|--hide-controls]")
        }
    }

    private static func printJSON(_ object: Any) {
        if let data = try? RecorderJSON.encode(object, pretty: true) {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data([0x0a]))
        }
    }

    private static func fail(_ message: String) -> Never {
        fputs(message + "\n", stderr)
        exit(1)
    }

    private static func runDirect(
        _ action: String,
        ipc: RecorderIPC,
        maxDurationSeconds: Int?,
        showControls: Bool?,
        settingsPane: String?,
        permissionKey: String?
    ) {
        do {
            let result: JSONDict
            switch action {
            case "start":
                result = try ipc.start(maxDurationSeconds: maxDurationSeconds, showControls: showControls)
            case "status":
                result = try ipc.status()
            case "permission-status":
                result = try ipc.status(autostartService: true)
            case "stop":
                result = try ipc.stop()
            case "request-permissions":
                result = try ipc.requestPermissions(permissionKey: permissionKey)
            case "open-settings":
                result = try openSettings(pane: settingsPane)
            default:
                fail("unknown event-stream command: \(action)")
            }
            printJSON(result)
        } catch {
            fail(String(describing: error))
        }
    }

    private static func openSettings(pane: String?) throws -> JSONDict {
        let supportedPanes: Set<String> = [
            "Privacy_Accessibility",
            "Privacy_ScreenCapture",
            "Privacy_ListenEvent",
        ]
        guard let pane, supportedPanes.contains(pane) else {
            throw RecorderError.invalidState("unsupported macOS privacy settings pane")
        }
        let url = URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?\(pane)")!
        guard NSWorkspace.shared.open(url) else {
            throw RecorderError.invalidState("failed to open macOS privacy settings")
        }
        return ["settingsPane": pane]
    }

    private static func rpcResponse(id: Any?, result: Any) -> JSONDict {
        ["jsonrpc": "2.0", "id": id ?? NSNull(), "result": result]
    }

    private static func rpcError(id: Any?, message: String) -> JSONDict {
        ["jsonrpc": "2.0", "id": id ?? NSNull(), "error": ["code": -32000, "message": message]]
    }

    private static func mcpToolResult(_ result: JSONDict) throws -> JSONDict {
        [
            "content": [
                [
                    "type": "text",
                    "text": try RecorderJSON.string(result),
                ]
            ],
            "isError": false,
        ]
    }

    private static func runMCP(ipc: RecorderIPC, maxDurationSeconds: Int?, showControls: Bool?) {
        while let line = readLine(strippingNewline: true) {
            guard !line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            let request: JSONDict
            do {
                request = try RecorderJSON.decodeDict(Data(line.utf8))
            } catch {
                printJSON(rpcError(id: nil, message: String(describing: error)))
                continue
            }
            let id = request["id"]
            let method = request["method"] as? String ?? ""
            if method.hasPrefix("notifications/") {
                continue
            }
            do {
                switch method {
                case "initialize":
                    printJSON(rpcResponse(id: id, result: [
                        "protocolVersion": "2024-11-05",
                        "serverInfo": ["name": "hermes-recorder", "version": "0.1.0"],
                        "capabilities": ["tools": [:]],
                    ]))
                case "tools/list":
                    let emptySchema: JSONDict = ["type": "object", "properties": [:], "additionalProperties": false]
                    printJSON(rpcResponse(id: id, result: [
                        "tools": [
                            ["name": "event_stream_start", "description": "Start a local Hermes Record & Replay event stream.", "inputSchema": emptySchema],
                            ["name": "event_stream_status", "description": "Read the current local event stream status.", "inputSchema": emptySchema],
                            ["name": "event_stream_stop", "description": "Stop the current local event stream.", "inputSchema": emptySchema],
                            ["name": "event_stream_request_permissions", "description": "Request macOS recorder permissions for the native Hermes recorder service.", "inputSchema": emptySchema],
                        ]
                    ]))
                case "tools/call":
                    let params = request["params"] as? JSONDict ?? [:]
                    let name = params["name"] as? String ?? ""
                    let result: JSONDict
                    switch name {
                    case "event_stream_start":
                        result = try ipc.start(maxDurationSeconds: maxDurationSeconds, showControls: showControls)
                    case "event_stream_status":
                        result = try ipc.status()
                    case "event_stream_stop":
                        result = try ipc.stop()
                    case "event_stream_request_permissions":
                        result = try ipc.requestPermissions()
                    default:
                        printJSON(rpcError(id: id, message: "unknown tool: \(name)"))
                        continue
                    }
                    printJSON(rpcResponse(id: id, result: try mcpToolResult(result)))
                default:
                    printJSON(rpcError(id: id, message: "unknown method: \(method)"))
                }
            } catch {
                printJSON(rpcError(id: id, message: String(describing: error)))
            }
        }
    }
}
