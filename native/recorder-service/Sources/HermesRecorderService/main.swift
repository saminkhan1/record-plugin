import AppKit
import Foundation
import HermesRecorderCore

@MainActor
private final class RecorderControlsController: NSObject {
    private let onStop: () -> Void
    private let onDiscard: () -> Void
    private var panel: NSPanel?
    private let statusLabel = NSTextField(labelWithString: "Recording")
    private let detailLabel = NSTextField(labelWithString: "")
    private let stopButton = NSButton(title: "Stop", target: nil, action: nil)
    private let discardButton = NSButton(title: "Discard", target: nil, action: nil)

    init(onStop: @escaping () -> Void, onDiscard: @escaping () -> Void) {
        self.onStop = onStop
        self.onDiscard = onDiscard
        super.init()
    }

    func show(sessionID: String, path: String) {
        ensurePanel()
        statusLabel.stringValue = "Hermes is recording"
        detailLabel.stringValue = "Session \(shortSessionID(sessionID))"
        setBusy(false)
        positionPanel()
        NSApp.activate(ignoringOtherApps: true)
        panel?.makeKeyAndOrderFront(nil)
        panel?.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
    }

    func showError(_ message: String) {
        statusLabel.stringValue = "Recorder control failed"
        detailLabel.stringValue = message
        setBusy(false)
        NSApp.activate(ignoringOtherApps: true)
        panel?.makeKeyAndOrderFront(nil)
        panel?.orderFrontRegardless()
    }

    private func ensurePanel() {
        guard panel == nil else { return }

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 126),
            styleMask: [.titled, .utilityWindow, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        panel.title = "Hermes Recorder"
        panel.identifier = NSUserInterfaceItemIdentifier("hermes-recorder-controls")
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = true
        panel.isReleasedWhenClosed = false
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true

        statusLabel.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        detailLabel.font = NSFont.systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.lineBreakMode = .byTruncatingMiddle

        stopButton.target = self
        stopButton.action = #selector(stopClicked)
        stopButton.bezelStyle = .rounded
        stopButton.keyEquivalent = "\r"
        stopButton.setAccessibilityIdentifier("hermes-recorder-stop-button")

        discardButton.target = self
        discardButton.action = #selector(discardClicked)
        discardButton.bezelStyle = .rounded
        discardButton.setAccessibilityIdentifier("hermes-recorder-discard-button")

        let buttonRow = NSStackView(views: [discardButton, stopButton])
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY
        buttonRow.distribution = .fillEqually
        buttonRow.spacing = 8

        let root = NSStackView(views: [statusLabel, detailLabel, buttonRow])
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 10
        root.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        root.translatesAutoresizingMaskIntoConstraints = false

        panel.contentView = NSView()
        panel.contentView?.addSubview(root)
        if let contentView = panel.contentView {
            NSLayoutConstraint.activate([
                root.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
                root.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
                root.topAnchor.constraint(equalTo: contentView.topAnchor),
                root.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
                buttonRow.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -32),
            ])
        }

        self.panel = panel
    }

    private func positionPanel() {
        guard let panel else { return }
        let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let size = panel.frame.size
        let origin = NSPoint(
            x: screenFrame.maxX - size.width - 24,
            y: screenFrame.maxY - size.height - 24
        )
        panel.setFrameOrigin(origin)
    }

    private func setBusy(_ busy: Bool) {
        stopButton.isEnabled = !busy
        discardButton.isEnabled = !busy
    }

    private func shortSessionID(_ value: String) -> String {
        guard value.count > 8 else { return value }
        return String(value.prefix(8))
    }

    @objc private func stopClicked() {
        statusLabel.stringValue = "Stopping recording"
        setBusy(true)
        onStop()
    }

    @objc private func discardClicked() {
        let alert = NSAlert()
        alert.messageText = "Discard this recording?"
        alert.informativeText = "Hermes will stop recording and delete the current session files."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Discard")
        alert.addButton(withTitle: "Keep Recording")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        statusLabel.stringValue = "Discarding recording"
        setBusy(true)
        onDiscard()
    }
}

private final class RecorderServiceCoordinator: @unchecked Sendable {
    private let store: EventStreamStore
    private let lifecycleLock = NSLock()
    private let tapLock = NSLock()
    private var activeTap: EventTapRecorder?
    private var autoStopWorkItem: DispatchWorkItem?
    private var idleExitWorkItem: DispatchWorkItem?
    @MainActor
    private lazy var controls = RecorderControlsController(
        onStop: { [weak self] in self?.stopFromControls() },
        onDiscard: { [weak self] in self?.discardFromControls() }
    )

    init(store: EventStreamStore) {
        self.store = store
    }

    func handle(_ request: JSONDict) -> JSONDict {
        let method = request["method"] as? String ?? ""
        let params = request["params"] as? JSONDict ?? [:]
        switch method {
        case "event_stream_start":
            cancelIdleExit()
            let maxDuration = params["maxDurationSeconds"] as? Int ?? 1800
            let showControls = params["showControls"] as? Bool ?? true
            return response { try start(maxDurationSeconds: maxDuration, showControls: showControls) }
        case "event_stream_status":
            let result = response { try store.status() }
            if ((result["result"] as? JSONDict)?["active"] as? Bool) != true {
                scheduleIdleExit()
            }
            return result
        case "event_stream_stop":
            return response { try stop(endReason: "client_stopped") }
        case "event_stream_request_permissions":
            let permissionKey = params["permissionKey"] as? String
            let result = response { ["permissionState": store.requestPermissionAccess(permissionKey: permissionKey)] }
            scheduleIdleExit()
            return result
        case "ping":
            let result = response { ["state": "ok"] }
            scheduleIdleExit()
            return result
        default:
            return ["ok": false, "error": "unknown method: \(method)"]
        }
    }

    private func start(maxDurationSeconds: Int, showControls: Bool = true) throws -> JSONDict {
        lifecycleLock.lock()
        defer { lifecycleLock.unlock() }
        cancelAutoStop()
        if let warning = try stopActiveTap() {
            let message = String(describing: warning)
            try? store.recordCaptureFinalizationWarning(message)
            _ = try? store.stop(endReason: "superseded_after_capture_finalization_warning")
        }
        var result = try store.start(maxDurationSeconds: maxDurationSeconds)
        if let rawPath = result["eventStreamRootPath"] as? String {
            let tap = EventTapRecorder(store: store, sessionDirectory: URL(fileURLWithPath: rawPath, isDirectory: true))
            tapLock.lock()
            activeTap = tap
            tapLock.unlock()
            tap.start()
            result["captureState"] = "event_tap_recording"
            result["controlsVisible"] = showControls
            scheduleAutoStop(after: maxDurationSeconds)
            if showControls {
                showFloatingControls(result: result, path: rawPath)
            } else {
                hideControls()
            }
        }
        return result
    }

    private func stop(endReason: String) throws -> JSONDict {
        lifecycleLock.lock()
        defer { lifecycleLock.unlock() }
        cancelAutoStop()
        let finalizationWarning = try stopActiveTap()
        if let finalizationWarning {
            try? store.recordCaptureFinalizationWarning(String(describing: finalizationWarning))
        }
        var result = try store.stop(endReason: endReason)
        if let finalizationWarning {
            result["captureFinalization"] = captureFinalizationWarning(finalizationWarning)
        }
        hideControls()
        scheduleIdleExit(after: 60)
        return result
    }

    private func discard() throws -> JSONDict {
        lifecycleLock.lock()
        defer { lifecycleLock.unlock() }
        cancelAutoStop()
        let finalizationWarning = try stopActiveTap()
        if let finalizationWarning {
            try? store.recordCaptureFinalizationWarning(String(describing: finalizationWarning))
        }
        var result = try store.discard(endReason: "recording_controls_cancelled")
        if let finalizationWarning {
            result["captureFinalization"] = captureFinalizationWarning(finalizationWarning)
        }
        hideControls()
        scheduleIdleExit(after: 60)
        return result
    }

    private func stopActiveTap() throws -> Error? {
        tapLock.lock()
        let tap = activeTap
        tapLock.unlock()
        guard let tap else { return nil }
        let finalizationWarning = try tap.stop()
        tapLock.lock()
        if activeTap === tap {
            activeTap = nil
        }
        tapLock.unlock()
        return finalizationWarning
    }

    private func captureFinalizationWarning(_ error: Error) -> JSONDict {
        [
            "ok": false,
            "error": String(describing: error),
        ]
    }

    private func scheduleAutoStop(after seconds: Int) {
        cancelAutoStop()
        guard seconds > 0 else { return }
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            do {
                _ = try self.stop(endReason: "maximum_duration_reached")
            } catch {
                self.showControlError(error)
            }
        }
        tapLock.lock()
        autoStopWorkItem = item
        tapLock.unlock()
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + .seconds(seconds), execute: item)
    }

    private func cancelAutoStop() {
        tapLock.lock()
        let item = autoStopWorkItem
        autoStopWorkItem = nil
        tapLock.unlock()
        item?.cancel()
    }

    private func scheduleIdleExit(after seconds: Int = 300) {
        cancelIdleExit()
        let item = DispatchWorkItem {
            Task { @MainActor in
                NSApp.terminate(nil)
            }
        }
        tapLock.lock()
        idleExitWorkItem = item
        tapLock.unlock()
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + .seconds(seconds), execute: item)
    }

    private func cancelIdleExit() {
        tapLock.lock()
        let item = idleExitWorkItem
        idleExitWorkItem = nil
        tapLock.unlock()
        item?.cancel()
    }

    private func showFloatingControls(result: JSONDict, path: String) {
        let sessionID = result["sessionID"] as? String ?? URL(fileURLWithPath: path).lastPathComponent
        Task { @MainActor in
            self.controls.show(sessionID: sessionID, path: path)
        }
    }

    private func hideControls() {
        Task { @MainActor in
            self.controls.hide()
        }
    }

    private func stopFromControls() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try self.stop(endReason: "recording_controls_stopped")
            } catch {
                self.showControlError(error)
            }
        }
    }

    private func discardFromControls() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try self.discard()
            } catch {
                self.showControlError(error)
            }
        }
    }

    private func showControlError(_ error: Error) {
        Task { @MainActor in
            self.controls.showError(String(describing: error))
        }
    }

    private func response(_ body: () throws -> JSONDict) -> JSONDict {
        do {
            return ["ok": true, "result": try body()]
        } catch {
            return ["ok": false, "error": String(describing: error)]
        }
    }
}

@main
private struct HermesRecorderServiceApp {
    @MainActor
    static func main() {
        let parsed = CLIOptions.parse(Array(CommandLine.arguments.dropFirst()))
        let locations = RecorderLocations.resolve(rootArg: parsed.options.root, socketArg: parsed.options.socket)
        let store = EventStreamStore(rootDirectory: locations.root)
        let coordinator = RecorderServiceCoordinator(store: store)
        let socketPath = locations.socketPath

        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)

        DispatchQueue.global(qos: .userInitiated).async {
            let server = UnixSocketServer(socketPath: socketPath)
            do {
                try server.serve { request in
                    coordinator.handle(request)
                }
            } catch {
                fputs("Hermes recorder service failed: \(String(describing: error))\n", stderr)
                Task { @MainActor in
                    NSApp.terminate(nil)
                }
            }
        }

        application.run()
    }
}
