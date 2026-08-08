import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import Darwin
@preconcurrency import ScreenCaptureKit

private final class ScreenCaptureCapabilityResult: @unchecked Sendable {
    private let lock = NSLock()
    private var granted = false
    private var failure = "ScreenCaptureKit capability probe timed out"

    func set(content: SCShareableContent?, error: (any Error)?) {
        lock.lock()
        defer { lock.unlock() }
        granted = content != nil && error == nil
        if let error {
            failure = error.localizedDescription
        } else if content == nil {
            failure = "ScreenCaptureKit returned no shareable content"
        } else {
            failure = ""
        }
    }

    func json() -> JSONDict {
        lock.lock()
        defer { lock.unlock() }
        return ["granted": granted, "error": failure.isEmpty ? NSNull() : failure]
    }
}

private enum RecorderPrivacyAccess: Sendable {
    case accessibility
    case screenCapture
    case inputMonitoring
}

private final class RecorderPrivacyAccessResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    func set(_ value: Bool) {
        lock.lock()
        self.value = value
        lock.unlock()
    }

    func get() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

public final class EventStreamStore: @unchecked Sendable {
    public let rootDirectory: URL
    private let fileManager = FileManager.default
    private let lock = NSLock()
    private let startupLock = NSLock()
    private var startupReconciled = false
    private let captureStateSchema = "hermes-adaptive-capture-v1"
    private let chromeHeartbeatFreshSeconds: TimeInterval = 5

    public init(rootDirectory: URL) {
        self.rootDirectory = rootDirectory
    }

    /// Repair durable counters from JSONL before this service instance can
    /// append. A malformed or ambiguous stream is preserved and fails closed.
    public func reconcileStartupState() throws {
        try ensureRoot()
    }

    public func start(maxDurationSeconds: Int = 1800) throws -> JSONDict {
        let permissions = permissionState()
        let missingPermissions = missingRequiredPermissions(permissions)
        if !missingPermissions.isEmpty {
            throw RecorderError.invalidState("Missing required macOS recorder permissions: \(missingPermissions.joined(separator: ", "))")
        }
        try ensureRoot()
        if let active = try activeSessionDirectory(), try isActive(active) {
            _ = try stop(endReason: "superseded")
        }

        let sessionID = UUID().uuidString.lowercased()
        let sessionDirectory = rootDirectory.appendingPathComponent(sessionID, isDirectory: true)
        try fileManager.createDirectory(at: sessionDirectory, withIntermediateDirectories: true)
        let now = isoTimestamp()
        let paths = sessionPaths(sessionDirectory)
        if let path = paths["tracePath"] as? String {
            fileManager.createFile(atPath: path, contents: nil)
        }
        for relative in [
            "raw/input.jsonl",
            "raw/context.jsonl",
            "raw/active-surface.jsonl",
            "visual/frames.jsonl",
        ] {
            let url = sessionDirectory.appendingPathComponent(relative)
            try fileManager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            fileManager.createFile(atPath: url.path, contents: nil)
        }

        let metadata = baseMetadata(
            sessionID: sessionID,
            sessionDirectory: sessionDirectory,
            startedAt: now,
            maxDurationSeconds: maxDurationSeconds
        )
        try write(initialCaptureState(sessionID: sessionID, at: now), to: captureStateURL(sessionDirectory))
        try write(metadata, to: metadataURL(sessionDirectory))
        try write(sessionJSON(from: metadata, status: "recording"), to: sessionURL(sessionDirectory))
        try writeState(["activeSessionPath": sessionDirectory.path, "sessionID": sessionID])

        try appendTraceEvent(
            sessionDirectory,
            type: "context",
            source: "system",
            kind: "session.started",
            payload: [
                "session": [
                    "id": sessionID,
                    "maximum_duration_seconds": maxDurationSeconds,
                ],
            ],
            quality: ["confidence": "high", "warnings": []]
        )
        if let window = frontmostWindowEvent() {
            try appendTraceEvent(sessionDirectory, type: "context", source: "native", kind: "window.changed", payload: tracePayload(from: window), quality: ["confidence": "medium", "warnings": []])
        }
        return try statusForSession(sessionDirectory)
    }

    public func status() throws -> JSONDict {
        try ensureRoot()
        guard let sessionDirectory = try activeSessionDirectory() else {
            return [
                "active": false,
                "state": "inactive",
                "eventStreamRootPath": rootDirectory.path,
                "permissionState": permissionState(),
            ]
        }
        return try statusForSession(sessionDirectory)
    }

    public func stop(endReason: String = "client_stopped") throws -> JSONDict {
        try ensureRoot()
        guard let sessionDirectory = try activeSessionDirectory() else {
            return [
                "active": false,
                "state": "inactive",
                "eventStreamRootPath": rootDirectory.path,
                "permissionState": permissionState(),
            ]
        }
        let requestedEndedAt = isoTimestamp()
        let wasActive = try isActive(sessionDirectory)
        var metadata: JSONDict
        if wasActive {
            lock.lock()
            do {
                metadata = try appendTraceEventLocked(
                    sessionDirectory,
                    type: "context",
                    source: "system",
                    kind: "session.ended",
                    payload: [
                        "session": [
                            "id": sessionDirectory.lastPathComponent,
                            "end_reason": endReason,
                        ],
                    ],
                    quality: ["confidence": "high", "warnings": []],
                    metadataUpdates: [
                        "active": false,
                        "state": "stopped",
                        "endedAt": requestedEndedAt,
                        "endReason": endReason,
                    ]
                )
            } catch {
                lock.unlock()
                throw error
            }
            lock.unlock()
        } else {
            metadata = try readJSON(metadataURL(sessionDirectory))
        }
        let endedAt = metadata["endedAt"] as? String ?? requestedEndedAt
        metadata["active"] = false
        metadata["state"] = "stopped"
        metadata["endedAt"] = endedAt
        if wasActive || metadata["endReason"] == nil || metadata["endReason"] is NSNull {
            metadata["endReason"] = endReason
        }
        metadata["permissionState"] = permissionState()
        try updateCaptureState(
            sessionDirectory,
            updates: [
                "sessionActive": false,
                "desiredProvider": "native",
                "effectiveProvider": "native",
                "chromeSegmentActive": false,
                "chromeSegmentID": NSNull(),
                "activeTargetId": NSNull(),
                "activeTabId": NSNull(),
                "activeWindowId": NSNull(),
                "activeUrl": NSNull(),
                "activeTitle": NSNull(),
                "activeTargetAttached": false,
                "activeTargetUpdatedAt": NSNull(),
                "contentScriptReady": false,
                "contentScriptUpdatedAt": NSNull(),
                "endedAt": endedAt,
            ],
            reason: "session_stopped"
        )
        metadata["captureState"] = captureState(sessionDirectory)
        try write(metadata, to: metadataURL(sessionDirectory))
        try write(sessionJSON(from: metadata, status: "stopped"), to: sessionURL(sessionDirectory))
        try writeState([:])
        return try statusForSession(sessionDirectory)
    }

    public func discard(endReason: String = "recording_controls_cancelled") throws -> JSONDict {
        try ensureRoot()
        guard let sessionDirectory = try activeSessionDirectory() else {
            return [
                "active": false,
                "state": "inactive",
                "discarded": false,
                "eventStreamRootPath": rootDirectory.path,
                "permissionState": permissionState(),
            ]
        }
        var result = try stop(endReason: endReason)
        try fileManager.removeItem(at: sessionDirectory)
        result["active"] = false
        result["state"] = "discarded"
        result["discarded"] = true
        result["discardedSessionPath"] = sessionDirectory.path
        result["eventStreamRootPath"] = rootDirectory.path
        return result
    }

    func appendTraceEvent(
        _ sessionDirectory: URL,
        type: String,
        source: String,
        kind: String,
        payload: JSONDict = [:],
        quality: JSONDict = [:]
    ) throws {
        lock.lock()
        defer { lock.unlock() }
        _ = try appendTraceEventLocked(
            sessionDirectory,
            type: type,
            source: source,
            kind: kind,
            payload: payload,
            quality: quality
        )
    }

    public func recordCaptureFinalizationWarning(_ message: String) throws {
        try ensureRoot()
        guard let sessionDirectory = try activeSessionDirectory(),
              try isActive(sessionDirectory)
        else {
            return
        }
        let warning: JSONDict = [
            "ok": false,
            "error": message,
            "recordedAt": isoTimestamp(),
        ]
        lock.lock()
        defer { lock.unlock() }
        _ = try appendTraceEventLocked(
            sessionDirectory,
            type: "evidence",
            source: "native",
            kind: "capture.error",
            payload: [
                "meta": ["message_preview": "Native capture finalization completed with a warning."],
                "effect": ["error_text": message],
            ],
            quality: ["confidence": "low", "warnings": ["native_capture_finalization_failed"]],
            metadataUpdates: ["captureFinalization": warning]
        )
    }

    /// Append a batch with one metadata transaction. High-frequency input and
    /// video callbacks use this path so system callback queues never perform a
    /// metadata read and atomic rewrite for every sample.
    @discardableResult
    func appendRawEvents(
        _ sessionDirectory: URL,
        events: [RawEventRecord]
    ) throws -> [Int] {
        guard !events.isEmpty else { return [] }
        lock.lock()
        defer { lock.unlock() }
        return try withTraceFileLock(sessionDirectory) {
            var metadata = try readJSON(metadataURL(sessionDirectory))
            guard metadata["active"] as? Bool == true else {
                throw RecorderError.invalidState("Recording session already stopped")
            }
            let firstSequence = (metadata["rawEventCount"] as? Int ?? 0) + 1
            var dataByURL: [URL: Data] = [:]
            var streamCounts = metadata["rawEventCounts"] as? JSONDict ?? [
                "input": metadata["rawInputEventCount"] as? Int ?? 0,
                "context": metadata["rawContextEventCount"] as? Int ?? 0,
                "activeSurface": metadata["rawActiveSurfaceEventCount"] as? Int ?? 0,
                "frames": metadata["visualFrameEventCount"] as? Int ?? 0,
            ]
            var sequences: [Int] = []
            sequences.reserveCapacity(events.count)
            for (offset, event) in events.enumerated() {
                let sequence = firstSequence + offset
                sequences.append(sequence)
                let row: JSONDict = [
                    "seq": sequence,
                    "ts": event.timestamp,
                    "monotonic_ns": event.monotonicNanoseconds,
                    "kind": event.kind,
                    "payload": RecorderURLSanitizer.sanitizePayload(event.payload),
                ]
                let url = rawURL(sessionDirectory, stream: event.stream)
                dataByURL[url, default: Data()].append(try RecorderJSON.encodeLine(row))
                let countKey = rawCountKey(event.stream)
                streamCounts[countKey] = (streamCounts[countKey] as? Int ?? 0) + 1
            }
            let originalSizes = try Dictionary(uniqueKeysWithValues: dataByURL.keys.map { url in
                (url, try fileSize(of: url))
            })
            do {
                for (url, data) in dataByURL {
                    try append(data, to: url)
                }
                metadata["rawEventCount"] = firstSequence + events.count - 1
                metadata["rawEventCounts"] = streamCounts
                metadata["rawInputEventCount"] = streamCounts["input"] ?? 0
                metadata["rawContextEventCount"] = streamCounts["context"] ?? 0
                metadata["rawActiveSurfaceEventCount"] = streamCounts["activeSurface"] ?? 0
                metadata["visualFrameEventCount"] = streamCounts["frames"] ?? 0
                try write(metadata, to: metadataURL(sessionDirectory))
            } catch {
                // Keep a failed batch retryable without duplicating any stream
                // fragments written before a later append or metadata failure.
                for (url, originalSize) in originalSizes {
                    try? truncate(url, to: originalSize)
                }
                throw error
            }
            return sequences
        }
    }

    private func appendTraceEventLocked(
        _ sessionDirectory: URL,
        type: String,
        source: String,
        kind: String,
        payload: JSONDict,
        quality: JSONDict,
        metadataUpdates: JSONDict = [:]
    ) throws -> JSONDict {
        try withTraceFileLock(sessionDirectory) {
            var metadata = try readJSON(metadataURL(sessionDirectory))
            guard metadata["active"] as? Bool == true else {
                throw RecorderError.invalidState("Recording session already stopped")
            }
            let nextID = (metadata["eventCount"] as? Int ?? 0) + 1
            let row: JSONDict = [
                "id": nextID,
                "ts": isoTimestamp(),
                "type": type,
                "source": source,
                "kind": kind,
                "payload": RecorderURLSanitizer.sanitizePayload(payload),
                "quality": quality.isEmpty ? ["confidence": "medium", "warnings": []] : quality,
            ]
            try append(row, to: traceURL(sessionDirectory))
            metadata["eventCount"] = nextID
            for (key, value) in metadataUpdates {
                metadata[key] = value
            }
            try write(metadata, to: metadataURL(sessionDirectory))
            return metadata
        }
    }

    private func withTraceFileLock<T>(_ sessionDirectory: URL, operation: () throws -> T) throws -> T {
        let fd = Darwin.open(traceLockURL(sessionDirectory).path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard fd >= 0 else {
            throw RecorderError.filesystem("Unable to open trace lock for \(sessionDirectory.path)")
        }
        defer { Darwin.close(fd) }
        var traceLock = Darwin.flock()
        traceLock.l_type = Int16(F_WRLCK)
        traceLock.l_whence = Int16(SEEK_SET)
        traceLock.l_start = 0
        traceLock.l_len = 0
        guard Darwin.fcntl(fd, F_SETLKW, &traceLock) != -1 else {
            throw RecorderError.filesystem("Unable to lock trace for \(sessionDirectory.path)")
        }
        defer {
            var traceUnlock = Darwin.flock()
            traceUnlock.l_type = Int16(F_UNLCK)
            traceUnlock.l_whence = Int16(SEEK_SET)
            traceUnlock.l_start = 0
            traceUnlock.l_len = 0
            _ = Darwin.fcntl(fd, F_SETLK, &traceUnlock)
        }
        return try operation()
    }

    func updateEventTapHealth(_ sessionDirectory: URL, updates: JSONDict) throws {
        lock.lock()
        defer { lock.unlock() }
        try withTraceFileLock(sessionDirectory) {
            let timestamp = isoTimestamp()
            var metadata = try readJSON(metadataURL(sessionDirectory))
            guard metadata["active"] as? Bool == true else { return }
            var health = metadata["eventTapHealth"] as? JSONDict ?? [:]
            for (key, value) in updates {
                health[key] = value
            }
            health["updatedAt"] = timestamp
            metadata["eventTapHealth"] = health
            try write(metadata, to: metadataURL(sessionDirectory))

            var state = captureState(sessionDirectory)
            var stateHealth = state["eventTapHealth"] as? JSONDict ?? [:]
            for (key, value) in updates {
                stateHealth[key] = value
            }
            stateHealth["updatedAt"] = timestamp
            state["eventTapHealth"] = stateHealth
            state["schemaVersion"] = captureStateSchema
            state["updatedAt"] = timestamp
            state["reason"] = "event_tap_health"
            try write(state, to: captureStateURL(sessionDirectory))
        }
    }

    func updateVisualCaptureHealth(_ sessionDirectory: URL, updates: JSONDict) throws {
        lock.lock()
        defer { lock.unlock() }
        try withTraceFileLock(sessionDirectory) {
            var metadata = try readJSON(metadataURL(sessionDirectory))
            guard metadata["active"] as? Bool == true else { return }
            var health = metadata["visualCaptureHealth"] as? JSONDict ?? [:]
            for (key, value) in updates { health[key] = value }
            health["updatedAt"] = isoTimestamp()
            metadata["visualCaptureHealth"] = health
            try write(metadata, to: metadataURL(sessionDirectory))

            var state = captureState(sessionDirectory)
            state["visualCaptureHealth"] = health
            state["schemaVersion"] = captureStateSchema
            state["updatedAt"] = isoTimestamp()
            state["reason"] = "visual_capture_health"
            try write(state, to: captureStateURL(sessionDirectory))
        }
    }

    func updateAccessibilityHealth(_ sessionDirectory: URL, updates: JSONDict) throws {
        lock.lock()
        defer { lock.unlock() }
        try withTraceFileLock(sessionDirectory) {
            var metadata = try readJSON(metadataURL(sessionDirectory))
            guard metadata["active"] as? Bool == true else { return }
            var health = metadata["accessibilityHealth"] as? JSONDict ?? [:]
            for (key, value) in updates { health[key] = value }
            health["updatedAt"] = isoTimestamp()
            metadata["accessibilityHealth"] = health
            try write(metadata, to: metadataURL(sessionDirectory))

            var state = captureState(sessionDirectory)
            state["accessibilityHealth"] = health
            state["schemaVersion"] = captureStateSchema
            state["updatedAt"] = isoTimestamp()
            state["reason"] = "accessibility_health"
            try write(state, to: captureStateURL(sessionDirectory))
        }
    }

    public func permissionState() -> JSONDict {
        let legacyScreenCaptureGranted = CGPreflightScreenCaptureAccess()
        let screenCaptureCapability = screenCaptureCapabilityState()
        return [
            "accessibilityTrusted": AXIsProcessTrusted(),
            // On current macOS releases, System Settings can show Screen &
            // System Audio Recording enabled while the legacy CoreGraphics
            // preflight remains false for an app bundle. ScreenCaptureKit is
            // the API the recorder actually uses, so its ability to enumerate
            // shareable content is the authoritative capability check.
            "screenCaptureGranted": screenCaptureCapability["granted"] as? Bool ?? legacyScreenCaptureGranted,
            "screenCaptureLegacyPreflightGranted": legacyScreenCaptureGranted,
            "screenCaptureCapabilityGranted": screenCaptureCapability["granted"] as? Bool ?? false,
            "screenCaptureCapabilityError": screenCaptureCapability["error"] ?? NSNull(),
            "listenEventGranted": CGPreflightListenEventAccess(),
            "accessibilityAPI": "ApplicationServices.AXUIElement",
            "screenCaptureAPI": "ScreenCaptureKit.SCShareableContent + CoreGraphics.CGPreflightScreenCaptureAccess",
            "listenEventAPI": "CoreGraphics.CGPreflightListenEventAccess",
            "platform": "macos",
        ]
    }

    private func screenCaptureCapabilityState() -> JSONDict {
        let completion = DispatchSemaphore(value: 0)
        let result = ScreenCaptureCapabilityResult()
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) { content, error in
            result.set(content: content, error: error)
            completion.signal()
        }
        guard completion.wait(timeout: .now() + .seconds(2)) == .success else {
            return result.json()
        }
        return result.json()
    }

    private func missingRequiredPermissions(_ state: JSONDict) -> [String] {
        var missing: [String] = []
        if state["accessibilityTrusted"] as? Bool != true {
            missing.append("Accessibility")
        }
        if state["screenCaptureGranted"] as? Bool != true {
            missing.append("Screen Recording")
        }
        if state["listenEventGranted"] as? Bool != true {
            missing.append("Input Monitoring")
        }
        return missing
    }

    public func requestScreenCaptureAccess() -> JSONDict {
        let granted = requestPrivacyAccessOnMainThread(.screenCapture)
        var state = permissionState()
        state["screenCaptureGranted"] = granted
        state["screenCaptureRequested"] = true
        return state
    }

    public func requestListenEventAccess() -> JSONDict {
        let granted = requestPrivacyAccessOnMainThread(.inputMonitoring)
        var state = permissionState()
        state["listenEventGranted"] = granted
        state["listenEventRequested"] = true
        return state
    }

    public func requestAccessibilityAccess() -> JSONDict {
        let granted = requestPrivacyAccessOnMainThread(.accessibility)
        var state = permissionState()
        state["accessibilityTrusted"] = granted
        state["accessibilityRequested"] = true
        return state
    }

    private func requestPrivacyAccessOnMainThread(_ access: RecorderPrivacyAccess) -> Bool {
        if Thread.isMainThread {
            return MainActor.assumeIsolated { self.performPrivacyAccessRequest(access) }
        }
        let result = RecorderPrivacyAccessResult()
        DispatchQueue.main.sync {
            result.set(MainActor.assumeIsolated { self.performPrivacyAccessRequest(access) })
        }
        return result.get()
    }

    @MainActor
    private func performPrivacyAccessRequest(_ access: RecorderPrivacyAccess) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        switch access {
        case .accessibility:
            let options = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
            return AXIsProcessTrustedWithOptions(options)
        case .screenCapture:
            return CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess()
        case .inputMonitoring:
            return CGPreflightListenEventAccess() || CGRequestListenEventAccess()
        }
    }

    public func requestPermissionAccess(permissionKey: String? = nil) -> JSONDict {
        switch permissionKey {
        case nil:
            break
        case "accessibilityTrusted":
            return requestAccessibilityAccess()
        case "screenCaptureGranted":
            return requestScreenCaptureAccess()
        case "listenEventGranted":
            return requestListenEventAccess()
        default:
            var state = permissionState()
            state["permissionRequestError"] = "unsupported permission key"
            return state
        }
        var state = requestAccessibilityAccess()
        let screen = requestScreenCaptureAccess()
        let listenEvent = requestListenEventAccess()
        state["screenCaptureGranted"] = screen["screenCaptureGranted"] ?? false
        state["screenCaptureRequested"] = screen["screenCaptureRequested"] ?? true
        state["listenEventGranted"] = listenEvent["listenEventGranted"] ?? false
        state["listenEventRequested"] = listenEvent["listenEventRequested"] ?? true
        return state
    }

    func captureContext() -> JSONDict {
        frontmostWindowEvent() ?? [:]
    }

    func captureState(_ sessionDirectory: URL) -> JSONDict {
        do {
            var state = try readJSON(captureStateURL(sessionDirectory))
            state["schemaVersion"] = state["schemaVersion"] ?? captureStateSchema
            state["desiredProvider"] = state["desiredProvider"] ?? "native"
            state["effectiveProvider"] = state["effectiveProvider"] ?? "native"
            state["chromeSegmentActive"] = state["chromeSegmentActive"] ?? false
            state["sessionActive"] = state["sessionActive"] ?? true
            state["activeTargetId"] = state["activeTargetId"] ?? NSNull()
            state["activeTabId"] = state["activeTabId"] ?? NSNull()
            state["activeWindowId"] = state["activeWindowId"] ?? NSNull()
            state["activeUrl"] = state["activeUrl"] ?? NSNull()
            state["activeTitle"] = state["activeTitle"] ?? NSNull()
            state["activeTargetAttached"] = state["activeTargetAttached"] ?? false
            state["activeTargetUpdatedAt"] = state["activeTargetUpdatedAt"] ?? NSNull()
            state["contentScriptReady"] = state["contentScriptReady"] ?? false
            state["contentScriptUpdatedAt"] = state["contentScriptUpdatedAt"] ?? NSNull()
            return state
        } catch {
            let metadata = (try? readJSON(metadataURL(sessionDirectory))) ?? [:]
            let sessionID = metadata["sessionID"] as? String ?? sessionDirectory.lastPathComponent
            return initialCaptureState(sessionID: sessionID, at: isoTimestamp())
        }
    }

    func updateDesiredCaptureProvider(_ sessionDirectory: URL, context: JSONDict, reason: String) throws {
        guard try isActive(sessionDirectory) else { return }
        let desired = desiredProvider(for: context)
        let current = captureState(sessionDirectory)
        var updates: JSONDict = [
            "sessionActive": true,
            "desiredProvider": desired,
            "frontmostApp": frontmostAppState(from: context),
        ]
        if desired == "native" {
            updates["effectiveProvider"] = "native"
            updates["chromeSegmentActive"] = false
            updates["activeTargetId"] = NSNull()
            updates["activeTabId"] = NSNull()
            updates["activeWindowId"] = NSNull()
            updates["activeUrl"] = NSNull()
            updates["activeTitle"] = NSNull()
            updates["activeTargetAttached"] = false
            updates["activeTargetUpdatedAt"] = NSNull()
            updates["contentScriptReady"] = false
            updates["contentScriptUpdatedAt"] = NSNull()
        } else if chromeSegmentIsFresh(current) {
            updates["effectiveProvider"] = current["effectiveProvider"] as? String == "chrome" ? "chrome" : "native"
        } else {
            updates["effectiveProvider"] = "native"
            updates["chromeSegmentActive"] = false
            updates["activeTargetId"] = NSNull()
            updates["activeTabId"] = NSNull()
            updates["activeWindowId"] = NSNull()
            updates["activeUrl"] = NSNull()
            updates["activeTitle"] = NSNull()
            updates["activeTargetAttached"] = false
            updates["activeTargetUpdatedAt"] = NSNull()
            updates["contentScriptReady"] = false
            updates["contentScriptUpdatedAt"] = NSNull()
        }
        try updateCaptureState(sessionDirectory, updates: updates, reason: reason)
    }

    func isSecureInputActive() -> Bool {
        guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else {
            return false
        }
        let keys = [
            "kCGSSessionSecureInputPID",
            "CGSSessionSecureInputPID",
            "SecureInputPID",
        ]
        return keys.contains { key in
            if let pid = session[key] as? Int {
                return pid > 0
            }
            if let pid = session[key] as? NSNumber {
                return pid.intValue > 0
            }
            return false
        }
    }

    private func baseMetadata(sessionID: String, sessionDirectory: URL, startedAt: String, maxDurationSeconds: Int) -> JSONDict {
        var metadata = sessionPaths(sessionDirectory)
        metadata["schemaVersion"] = "hermes-workflow-trace-v2"
        metadata["recorder"] = "hermes-native-recorder"
        metadata["sessionID"] = sessionID
        metadata["workflow_id"] = sessionID
        metadata["title"] = "Record Recording"
        metadata["goal"] = ""
        metadata["startedAt"] = startedAt
        metadata["endedAt"] = NSNull()
        metadata["endReason"] = NSNull()
        metadata["eventCount"] = 0
        metadata["rawEventCount"] = 0
        metadata["rawEventCounts"] = [
            "input": 0,
            "context": 0,
            "activeSurface": 0,
            "frames": 0,
        ]
        metadata["rawInputEventCount"] = 0
        metadata["rawContextEventCount"] = 0
        metadata["rawActiveSurfaceEventCount"] = 0
        metadata["visualFrameEventCount"] = 0
        metadata["active"] = true
        metadata["state"] = "recording"
        metadata["maximumDurationSeconds"] = maxDurationSeconds
        metadata["permissionState"] = permissionState()
        metadata["captureQuality"] = ["confidence": "medium", "warnings": []]
        metadata["preconditions"] = [:]
        metadata["paths"] = ["trace": "trace.jsonl"]
        metadata["captureStatePath"] = captureStateURL(sessionDirectory).path
        metadata["captureCapabilities"] = [
            "eventTap": true,
            "eventTapHealth": true,
            "browserURLDetection": true,
            "inputMonitoringPermission": true,
            "secureInputDetection": true,
            "selectionCapture": true,
            "terminalSubmitValueCapture": true,
            "adaptiveCaptureProviders": true,
            "rawInput": true,
            "activeSurface": true,
            "continuousVisualEvidence": true,
        ]
        return metadata
    }

    private func sessionJSON(from metadata: JSONDict, status: String) -> JSONDict {
        [
            "schemaVersion": metadata["schemaVersion"] ?? "hermes-workflow-trace-v2",
            "sessionID": metadata["sessionID"] ?? "",
            "status": status,
            "startedAt": metadata["startedAt"] ?? "",
            "endedAt": metadata["endedAt"] ?? NSNull(),
            "endReason": metadata["endReason"] ?? NSNull(),
            "eventStreamRootPath": metadata["eventStreamRootPath"] ?? "",
            "tracePath": metadata["tracePath"] ?? "",
        ]
    }

    private func initialCaptureState(sessionID: String, at timestamp: String) -> JSONDict {
        [
            "schemaVersion": captureStateSchema,
            "sessionID": sessionID,
            "sessionActive": true,
            "desiredProvider": "native",
            "effectiveProvider": "native",
            "chromeSegmentActive": false,
            "chromeSegmentID": NSNull(),
            "lastHeartbeatAt": NSNull(),
            "activeTargetId": NSNull(),
            "activeTabId": NSNull(),
            "activeWindowId": NSNull(),
            "activeUrl": NSNull(),
            "activeTitle": NSNull(),
            "activeTargetAttached": false,
            "activeTargetUpdatedAt": NSNull(),
            "contentScriptReady": false,
            "contentScriptUpdatedAt": NSNull(),
            "eventTapHealth": [
                "tapCreated": false,
                "tapEnabled": false,
                "rawInputEventCount": 0,
                "axActivityEventCount": 0,
                "axActivityWithoutRawInput": false,
            ],
            "accessibilityHealth": ["ready": false, "observer": "poll"],
            "visualCaptureHealth": ["ready": false, "droppedFrames": 0],
            "startedAt": timestamp,
            "updatedAt": timestamp,
            "reason": "session_started",
        ]
    }

    private func frontmostWindowEvent() -> JSONDict? {
        guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
        let pid = Int(app.processIdentifier)
        let appPayload: JSONDict = [
            "name": app.localizedName ?? "",
            "bundleIdentifier": app.bundleIdentifier ?? "",
            "processIdentifier": pid,
            "secureInput": isSecureInputActive(),
        ]
        var windowPayload: JSONDict = [
            "windowID": NSNull(),
            "windowName": "",
            "title": "",
            "url": NSNull(),
        ]
        if let window = frontmostWindow(forPID: pid) {
            windowPayload = window
        }
        if let url = browserURL(bundleIdentifier: app.bundleIdentifier, pid: pid) {
            windowPayload["url"] = RecorderURLSanitizer.sanitize(url)
        }
        return [
            "app": appPayload,
            "window": windowPayload,
        ]
    }

    private func tracePayload(from context: JSONDict) -> JSONDict {
        let app = context["app"] as? JSONDict ?? [:]
        let window = context["window"] as? JSONDict ?? [:]
        var payload: JSONDict = [
            "app": [
                "name": app["name"] ?? "",
                "bundle_id": app["bundleIdentifier"] ?? "",
                "process_id": app["processIdentifier"] ?? 0,
                "secure_input": app["secureInput"] ?? false,
            ],
            "window": [
                "id": window["windowID"] ?? 0,
                "title": window["title"] ?? "",
                "name": window["windowName"] ?? "",
                "url": window["url"] ?? "",
                "frame": window["frame"] ?? [:],
            ],
        ]
        if let url = window["url"] as? String, !url.isEmpty {
            payload["browser"] = [
                "url": url,
                "top_url": url,
                "profile_hint": "current-profile",
            ]
        }
        return payload
    }

    private func frontmostWindow(forPID pid: Int) -> JSONDict? {
        guard let rawWindows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }
        for window in rawWindows {
            guard (window[kCGWindowOwnerPID as String] as? Int) == pid else { continue }
            if let layer = window[kCGWindowLayer as String] as? Int, layer != 0 { continue }
            let title = window[kCGWindowName as String] as? String ?? ""
            let number = window[kCGWindowNumber as String] as? Int ?? 0
            var payload: JSONDict = [
                "windowID": number,
                "windowName": title,
                "title": title,
                "url": NSNull(),
            ]
            if let bounds = window[kCGWindowBounds as String] as? [String: Any],
               let frame = rectPayload(from: bounds)
            {
                payload["frame"] = frame
            }
            if let ownerName = window[kCGWindowOwnerName as String] as? String, !ownerName.isEmpty {
                payload["ownerName"] = ownerName
            }
            return payload
        }
        return nil
    }

    private func browserURL(bundleIdentifier: String?, pid: Int) -> String? {
        guard AXIsProcessTrusted(),
              let bundleIdentifier,
              isWebBrowserBundle(bundleIdentifier)
        else {
            return nil
        }
        let app = AXUIElementCreateApplication(pid_t(pid))
        let roots = [
            axElement(axElementAttribute(app, kAXFocusedWindowAttribute as CFString)),
            axElement(axElementAttribute(app, kAXMainWindowAttribute as CFString)),
        ].compactMap { $0 }
        var remaining = 90
        for root in roots {
            if let url = findURL(in: root, depth: 0, remaining: &remaining) {
                return url
            }
        }
        return nil
    }

    private func findURL(in element: AXUIElement, depth: Int, remaining: inout Int) -> String? {
        guard depth < 7, remaining > 0 else { return nil }
        remaining -= 1
        let role = axString(element, kAXRoleAttribute as CFString).lowercased()
        let title = axString(element, kAXTitleAttribute as CFString).lowercased()
        let description = axString(element, kAXDescriptionAttribute as CFString).lowercased()
        let identifier = axString(element, kAXIdentifierAttribute as CFString).lowercased()
        let value = axString(element, kAXValueAttribute as CFString)
        let looksLikeAddressField =
            role.contains("textfield")
            && [title, description, identifier].contains { marker in
                marker.contains("address") || marker.contains("location") || marker.contains("search")
            }
        if looksLikeAddressField, !value.isEmpty {
            return normalizedAddressValue(value)
        }
        guard let children = axElementAttribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] else {
            return nil
        }
        for child in children {
            if let url = findURL(in: child, depth: depth + 1, remaining: &remaining) {
                return url
            }
        }
        return nil
    }

    private func normalizedURLCandidate(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if trimmed.hasPrefix("http://") || trimmed.hasPrefix("https://") {
            return trimmed
        }
        return nil
    }

    private func normalizedAddressValue(_ value: String) -> String? {
        if let url = normalizedURLCandidate(value) {
            return url
        }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !trimmed.contains(" "), trimmed.contains(".") else { return nil }
        return "https://\(trimmed)"
    }

    private func rectPayload(from dictionary: [String: Any]) -> JSONDict? {
        var rect = CGRect.zero
        guard CGRectMakeWithDictionaryRepresentation(dictionary as CFDictionary, &rect) else {
            return nil
        }
        return [
            "x": Double(rect.minX),
            "y": Double(rect.minY),
            "width": Double(rect.width),
            "height": Double(rect.height),
        ]
    }

    private func axString(_ element: AXUIElement, _ attribute: CFString) -> String {
        guard let value = axElementAttribute(element, attribute) else { return "" }
        if let string = value as? String {
            return string
        }
        if let number = value as? NSNumber {
            return number.stringValue
        }
        if let url = value as? URL {
            return url.absoluteString
        }
        return ""
    }

    private func axElementAttribute(_ element: AXUIElement, _ attribute: CFString) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success else {
            return nil
        }
        return value
    }

    private func axElement(_ value: CFTypeRef?) -> AXUIElement? {
        guard let value, CFGetTypeID(value) == AXUIElementGetTypeID() else {
            return nil
        }
        return (value as! AXUIElement)
    }

    private func sessionPaths(_ sessionDirectory: URL) -> JSONDict {
        [
            "eventStreamRootPath": sessionDirectory.path,
            "metadataPath": metadataURL(sessionDirectory).path,
            "tracePath": traceURL(sessionDirectory).path,
            "captureStatePath": captureStateURL(sessionDirectory).path,
            "sessionPath": sessionURL(sessionDirectory).path,
            "rawInputPath": rawURL(sessionDirectory, stream: .input).path,
            "rawContextPath": rawURL(sessionDirectory, stream: .context).path,
            "activeSurfacePath": rawURL(sessionDirectory, stream: .activeSurface).path,
            "visualFramesPath": rawURL(sessionDirectory, stream: .frames).path,
        ]
    }

    private func statusForSession(_ sessionDirectory: URL) throws -> JSONDict {
        lock.lock()
        defer { lock.unlock() }
        return try withTraceFileLock(sessionDirectory) {
            var metadata = try readJSON(metadataURL(sessionDirectory))
            metadata["eventCount"] = try lineCount(traceURL(sessionDirectory))
            metadata["permissionState"] = permissionState()
            metadata["captureState"] = captureState(sessionDirectory)
            if metadata["active"] == nil {
                metadata["active"] = false
            }
            if metadata["state"] == nil {
                metadata["state"] = (metadata["active"] as? Bool == true) ? "recording" : "stopped"
            }
            try write(metadata, to: metadataURL(sessionDirectory))
            return metadata
        }
    }

    private func activeSessionDirectory() throws -> URL? {
        let state = try readState()
        guard let raw = state["activeSessionPath"] as? String, !raw.isEmpty else { return nil }
        return URL(fileURLWithPath: raw, isDirectory: true)
    }

    private func isActive(_ sessionDirectory: URL) throws -> Bool {
        let metadata = try readJSON(metadataURL(sessionDirectory))
        return metadata["active"] as? Bool == true
    }

    private func ensureRoot() throws {
        do {
            try fileManager.createDirectory(at: rootDirectory, withIntermediateDirectories: true)
        } catch {
            throw RecorderError.filesystem("Unable to create \(rootDirectory.path): \(error)")
        }
        startupLock.lock()
        defer { startupLock.unlock() }
        guard !startupReconciled else { return }
        if let sessionDirectory = try activeSessionDirectory() {
            try reconcileDurableEventState(sessionDirectory)
        }
        startupReconciled = true
    }

    private func append(_ object: JSONDict, to url: URL) throws {
        try append(RecorderJSON.encodeLine(object), to: url)
    }

    private func append(_ data: Data, to url: URL) throws {
        if !fileManager.fileExists(atPath: url.path) {
            fileManager.createFile(atPath: url.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        try handle.write(contentsOf: data)
        try handle.close()
    }

    private func fileSize(of url: URL) throws -> UInt64 {
        guard fileManager.fileExists(atPath: url.path) else { return 0 }
        let attributes = try fileManager.attributesOfItem(atPath: url.path)
        return (attributes[.size] as? NSNumber)?.uint64Value ?? 0
    }

    private func truncate(_ url: URL, to size: UInt64) throws {
        guard fileManager.fileExists(atPath: url.path) else { return }
        let handle = try FileHandle(forWritingTo: url)
        defer { try? handle.close() }
        try handle.truncate(atOffset: size)
    }

    private func write(_ object: JSONDict, to url: URL) throws {
        let data = try RecorderJSON.encode(
            RecorderURLSanitizer.sanitizePayload(object),
            pretty: true
        )
        try data.write(to: url, options: [.atomic])
    }

    private func writeState(_ object: JSONDict) throws {
        try write(object, to: rootDirectory.appendingPathComponent("service-state.json"))
    }

    private func readState() throws -> JSONDict {
        let url = rootDirectory.appendingPathComponent("service-state.json")
        guard fileManager.fileExists(atPath: url.path) else { return [:] }
        return try readJSON(url)
    }

    private func readJSON(_ url: URL) throws -> JSONDict {
        guard fileManager.fileExists(atPath: url.path) else {
            throw RecorderError.invalidState("Missing \(url.path)")
        }
        return try RecorderJSON.decodeDict(try Data(contentsOf: url))
    }

    private func lineCount(_ url: URL) throws -> Int {
        guard fileManager.fileExists(atPath: url.path) else { return 0 }
        let data = try Data(contentsOf: url)
        if data.isEmpty { return 0 }
        return data.split(separator: 0x0a).filter { !$0.isEmpty }.count
    }

    private struct JSONLScan {
        let count: Int
        let maximumIdentifier: Int
    }

    private func scanJSONL(
        _ url: URL,
        label: String,
        identifierKey: String,
        visit: (Int, Int) throws -> Void
    ) throws -> JSONLScan {
        let data: Data
        do {
            data = try Data(contentsOf: url, options: [.mappedIfSafe])
        } catch {
            throw RecorderError.filesystem(
                "Unable to read \(label) JSONL at \(url.path): \(error)"
            )
        }
        var count = 0
        var maximumIdentifier = 0
        let lines = data.split(separator: 0x0a, omittingEmptySubsequences: false)
        for (offset, line) in lines.enumerated() {
            if line.isEmpty {
                if offset == lines.count - 1 { continue }
                throw RecorderError.filesystem(
                    "Malformed \(label) JSONL at line \(offset + 1): blank rows are not recoverable"
                )
            }
            let row: JSONDict
            do {
                row = try RecorderJSON.decodeDict(Data(line))
            } catch {
                throw RecorderError.filesystem(
                    "Malformed \(label) JSONL at line \(offset + 1); refusing to discard durable evidence"
                )
            }
            let identifier: Int
            if let value = row[identifierKey] as? Int {
                identifier = value
            } else if let value = row[identifierKey] as? NSNumber,
                      value.doubleValue == Double(value.intValue)
            {
                identifier = value.intValue
            } else {
                identifier = 0
            }
            guard identifier > 0 else {
                throw RecorderError.filesystem(
                    "Malformed \(label) JSONL at line \(offset + 1): missing positive \(identifierKey)"
                )
            }
            try visit(identifier, offset + 1)
            count += 1
            maximumIdentifier = max(maximumIdentifier, identifier)
        }
        return JSONLScan(count: count, maximumIdentifier: maximumIdentifier)
    }

    private func reconcileDurableEventState(_ sessionDirectory: URL) throws {
        try withTraceFileLock(sessionDirectory) {
            var metadata = try readJSON(metadataURL(sessionDirectory))
            var previousTraceID = 0
            let trace = try scanJSONL(
                traceURL(sessionDirectory),
                label: "trace",
                identifierKey: "id"
            ) { identifier, line in
                guard identifier == previousTraceID + 1 else {
                    throw RecorderError.filesystem(
                        "Ambiguous trace JSONL at line \(line): expected id \(previousTraceID + 1), found \(identifier)"
                    )
                }
                previousTraceID = identifier
            }

            var seenRawSequences = Set<Int>()
            var rawMaximumSequence = 0
            var rawCounts: JSONDict = [:]
            for stream in [
                RawEventStream.input,
                .context,
                .activeSurface,
                .frames,
            ] {
                let countKey = rawCountKey(stream)
                var previousStreamSequence = 0
                let scan = try scanJSONL(
                    rawURL(sessionDirectory, stream: stream),
                    label: "raw \(countKey)",
                    identifierKey: "seq"
                ) { identifier, line in
                    guard identifier > previousStreamSequence else {
                        throw RecorderError.filesystem(
                            "Ambiguous raw \(countKey) JSONL at line \(line): sequence \(identifier) is not increasing"
                        )
                    }
                    guard seenRawSequences.insert(identifier).inserted else {
                        throw RecorderError.filesystem(
                            "Ambiguous raw JSONL sequence \(identifier) at \(countKey) line \(line)"
                        )
                    }
                    previousStreamSequence = identifier
                }
                rawCounts[countKey] = scan.count
                rawMaximumSequence = max(rawMaximumSequence, scan.maximumIdentifier)
            }

            metadata["eventCount"] = trace.maximumIdentifier
            // rawEventCount is the durable global sequence high-water mark;
            // per-stream fields retain actual persisted row counts.
            metadata["rawEventCount"] = rawMaximumSequence
            metadata["rawEventCounts"] = rawCounts
            metadata["rawInputEventCount"] = rawCounts["input"] ?? 0
            metadata["rawContextEventCount"] = rawCounts["context"] ?? 0
            metadata["rawActiveSurfaceEventCount"] = rawCounts["activeSurface"] ?? 0
            metadata["visualFrameEventCount"] = rawCounts["frames"] ?? 0
            try write(metadata, to: metadataURL(sessionDirectory))
        }
    }

    private func metadataURL(_ sessionDirectory: URL) -> URL {
        sessionDirectory.appendingPathComponent("metadata.json")
    }

    private func sessionURL(_ sessionDirectory: URL) -> URL {
        sessionDirectory.appendingPathComponent("session.json")
    }

    private func traceURL(_ sessionDirectory: URL) -> URL {
        sessionDirectory.appendingPathComponent("trace.jsonl")
    }

    private func rawURL(_ sessionDirectory: URL, stream: RawEventStream) -> URL {
        switch stream {
        case .input: return sessionDirectory.appendingPathComponent("raw/input.jsonl")
        case .context: return sessionDirectory.appendingPathComponent("raw/context.jsonl")
        case .activeSurface: return sessionDirectory.appendingPathComponent("raw/active-surface.jsonl")
        case .frames: return sessionDirectory.appendingPathComponent("visual/frames.jsonl")
        }
    }

    private func rawCountKey(_ stream: RawEventStream) -> String {
        switch stream {
        case .input: return "input"
        case .context: return "context"
        case .activeSurface: return "activeSurface"
        case .frames: return "frames"
        }
    }

    private func traceLockURL(_ sessionDirectory: URL) -> URL {
        sessionDirectory.appendingPathComponent("trace.jsonl.lock")
    }

    private func captureStateURL(_ sessionDirectory: URL) -> URL {
        sessionDirectory.appendingPathComponent("capture-state.json")
    }

    private func updateCaptureState(_ sessionDirectory: URL, updates: JSONDict, reason: String) throws {
        lock.lock()
        defer { lock.unlock() }
        try withTraceFileLock(sessionDirectory) {
            var state = captureState(sessionDirectory)
            for (key, value) in updates {
                state[key] = value
            }
            state["schemaVersion"] = captureStateSchema
            state["updatedAt"] = isoTimestamp()
            state["reason"] = reason
            try write(state, to: captureStateURL(sessionDirectory))
        }
    }

    private func desiredProvider(for context: JSONDict) -> String {
        guard let app = context["app"] as? JSONDict,
              let bundleID = app["bundleIdentifier"] as? String,
              isChromeCaptureBundle(bundleID)
        else {
            return "native"
        }
        return "chrome"
    }

    private func frontmostAppState(from context: JSONDict) -> JSONDict {
        let app = context["app"] as? JSONDict ?? [:]
        let window = context["window"] as? JSONDict ?? [:]
        return [
            "name": app["name"] ?? "",
            "bundleIdentifier": app["bundleIdentifier"] ?? "",
            "processIdentifier": app["processIdentifier"] ?? NSNull(),
            "windowTitle": window["title"] ?? window["windowName"] ?? "",
            "url": window["url"] ?? NSNull(),
        ]
    }

    private func chromeSegmentIsFresh(_ state: JSONDict) -> Bool {
        guard let raw = state["lastHeartbeatAt"] as? String,
              let heartbeat = parseTimestamp(raw)
        else {
            return false
        }
        return Date().timeIntervalSince(heartbeat) <= chromeHeartbeatFreshSeconds
    }

    private func parseTimestamp(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) {
            return date
        }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
}

func isChromeCaptureBundle(_ bundleIdentifier: String) -> Bool {
    RecorderBrowserIdentity.isChromeCaptureBundle(bundleIdentifier)
}

func isWebBrowserBundle(_ bundleIdentifier: String) -> Bool {
    RecorderBrowserIdentity.isWebBrowserBundle(bundleIdentifier)
}

private enum RecorderBrowserIdentity {
    private static let browserBundleIdentifiers: Set<String> = [
        "com.apple.safari",
        "com.microsoft.edgemac",
        "com.brave.browser",
        "company.thebrowser.browser",
        "org.mozilla.firefox",
    ]

    static func isChromeCaptureBundle(_ bundleIdentifier: String) -> Bool {
        let normalized = bundleIdentifier.lowercased()
        guard normalized == "com.google.chrome" || normalized.hasPrefix("com.google.chrome.") else {
            return false
        }
        // Chrome helper processes share the application bundle prefix but are
        // not browser surfaces and must never own an adaptive capture lease.
        return !normalized.contains(".helper")
    }

    static func isWebBrowserBundle(_ bundleIdentifier: String) -> Bool {
        isChromeCaptureBundle(bundleIdentifier)
            || browserBundleIdentifiers.contains(bundleIdentifier.lowercased())
    }
}
