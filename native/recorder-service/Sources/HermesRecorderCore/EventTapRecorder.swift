import ApplicationServices
import AppKit
import Carbon
import CoreGraphics
import Foundation

private struct AXHitTarget {
    let target: JSONDict
    let ownerApplication: JSONDict?

    var isSecureTextEntry: Bool {
        let ax = target["ax"] as? JSONDict ?? [:]
        let role = ax["role"] as? String ?? target["role"] as? String ?? ""
        let subrole = ax["subrole"] as? String ?? ""
        return role == (kAXSecureTextFieldSubrole as String)
            || subrole == (kAXSecureTextFieldSubrole as String)
    }
}

struct AccessibilityTextMutation: Equatable {
    let location: Int
    let removedLength: Int
    let insertedText: String
}

enum AccessibilityMutationInference {
    static func textMutation(previous: String, current: String) -> AccessibilityTextMutation? {
        guard previous != current else { return nil }
        let before = previous as NSString
        let after = current as NSString
        let sharedLimit = min(before.length, after.length)
        var prefixLength = 0
        while prefixLength < sharedLimit,
              before.character(at: prefixLength) == after.character(at: prefixLength)
        {
            prefixLength += 1
        }

        var suffixLength = 0
        while suffixLength < before.length - prefixLength,
              suffixLength < after.length - prefixLength,
              before.character(at: before.length - suffixLength - 1)
                == after.character(at: after.length - suffixLength - 1)
        {
            suffixLength += 1
        }

        let removedLength = before.length - prefixLength - suffixLength
        let insertedLength = after.length - prefixLength - suffixLength
        return AccessibilityTextMutation(
            location: prefixLength,
            removedLength: removedLength,
            insertedText: after.substring(with: NSRange(location: prefixLength, length: insertedLength))
        )
    }

    static func actionKind(for mutation: AccessibilityTextMutation, clipboardText: String?) -> String {
        if !mutation.insertedText.isEmpty, mutation.insertedText == clipboardText {
            return "paste"
        }
        if mutation.removedLength > 0 || mutation.insertedText.isEmpty {
            return "change"
        }
        return "type"
    }
}

enum KeyboardReceiptAction: String, Equatable {
    case edit
    case paste
    case selection
}

struct RecentCGKeyboardInput: Equatable {
    let targetIdentity: String
    let action: KeyboardReceiptAction
    let monotonicNanoseconds: UInt64
}

enum AccessibilityReceiptDeduplication {
    static func actionForCGKey(modifiers: [String], keyCode: Int) -> KeyboardReceiptAction? {
        let modifiers = Set(modifiers)
        if [123, 124, 125, 126].contains(keyCode) {
            return .selection
        }
        if modifiers.contains("command") {
            switch keyCode {
            case 9: return .paste       // V
            case 0: return .selection   // A
            case 6, 7, 16, 51, 117: return .edit // Z, X, Y, delete
            default: return nil
            }
        }
        // Control/Option chords can be app commands. Prefer an extra inferred
        // receipt over suppressing an unrelated application action.
        guard !modifiers.contains("control"), !modifiers.contains("option") else {
            return nil
        }
        return .edit
    }

    static func actionForAccessibilityMutation(_ actionKind: String) -> KeyboardReceiptAction? {
        switch actionKind {
        case "paste": return .paste
        case "type", "change": return .edit
        default: return nil
        }
    }

    static func shouldSuppress(
        recent: RecentCGKeyboardInput?,
        targetIdentity: String,
        action: KeyboardReceiptAction,
        nowNanoseconds: UInt64,
        maximumAgeNanoseconds: UInt64
    ) -> Bool {
        guard let recent,
              !targetIdentity.isEmpty,
              recent.targetIdentity == targetIdentity,
              recent.action == action,
              nowNanoseconds >= recent.monotonicNanoseconds
        else {
            return false
        }
        return nowNanoseconds - recent.monotonicNanoseconds <= maximumAgeNanoseconds
    }
}

enum RecorderInputEvidenceRouting {
    static func shouldAppendRawInput(provenance: JSONDict) -> Bool {
        provenance["cg_event_observed"] as? Bool == true
    }
}

private struct FocusedAccessibilityObservation {
    let identity: String
    let deduplicationIdentity: String
    let target: JSONDict
    let ownerApplication: JSONDict?
    let secure: Bool
    let value: String?
    let selection: JSONDict?

    var selectionSignature: String {
        stableJSONSignature(selection)
    }
}

private enum PendingAccessibilityReceipt {
    case value(
        observation: FocusedAccessibilityObservation,
        mutation: AccessibilityTextMutation,
        resultingValue: String,
        actionKind: String
    )
    case selection(observation: FocusedAccessibilityObservation, selection: JSONDict)
}

public final class EventTapRecorder: @unchecked Sendable {
    private static let cgKeyboardDeduplicationNanoseconds: UInt64 = 1_500_000_000
    private static let maximumObservedTextLength = 100_000

    private let store: EventStreamStore
    private let sessionDirectory: URL
    private var thread: Thread?
    private var runLoop: CFRunLoop?
    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private let axPollQueue = DispatchQueue(label: "HermesRecorderAXPoll")
    private var axPollTimer: DispatchSourceTimer?
    private var mouseDown: JSONDict?
    private var textBuffer = ""
    private var textBufferModifiers: [String] = []
    private var textBufferTarget: JSONDict?
    private var textBufferOwnerApplication: JSONDict?
    private var workspaceObservers: [NSObjectProtocol] = []
    private var lastSelectionSignature = ""
    private var lastContextSignature = ""
    private var lastAccessibilityContextSignature = ""
    private var lastFocusedAccessibilityObservation: FocusedAccessibilityObservation?
    private var recentCGKeyboardInput: RecentCGKeyboardInput?
    private let lifecycleLock = NSRecursiveLock()
    private var acceptingEvents = true
    private let healthLock = NSLock()
    private let healthWriteQueue = DispatchQueue(label: "HermesRecorderEventTapHealth")
    private let rawEvents: RawEventBuffer
    private let activeSurfaceRecorder: ActiveSurfaceRecorder
    private var rawInputEventCount = 0
    private var axActivityEventCount = 0
    private var firstRawInputAt: String?
    private var firstAXActivityAt: String?
    private var lastRawInputHealthWriteAt: TimeInterval = 0
    private var lastAXActivityHealthWriteAt: TimeInterval = 0

    public init(store: EventStreamStore, sessionDirectory: URL) {
        self.store = store
        self.sessionDirectory = sessionDirectory
        let rawEvents = RawEventBuffer(store: store, sessionDirectory: sessionDirectory)
        self.rawEvents = rawEvents
        self.activeSurfaceRecorder = ActiveSurfaceRecorder(
            store: store,
            sessionDirectory: sessionDirectory,
            rawEvents: rawEvents
        )
    }

    public func start() {
        lifecycleLock.lock()
        acceptingEvents = true
        lifecycleLock.unlock()
        let thread = Thread { [weak self] in
            self?.run()
        }
        thread.name = "HermesRecorderEventTap"
        self.thread = thread
        thread.start()
    }

    public func stop() throws -> Error? {
        lifecycleLock.lock()
        if acceptingEvents {
            if let eventTap {
                CGEvent.tapEnable(tap: eventTap, enable: false)
            }
            flushTextBuffer(reason: "stop")
            acceptingEvents = false
            removeAXPollTimer()
            removeWorkspaceObservers()
            if let runLoop {
                CFRunLoopStop(runLoop)
            }
            eventTap = nil
            runLoopSource = nil
        }
        lifecycleLock.unlock()

        // This waits for ScreenCaptureKit and AVFoundation to finish the video
        // before the service marks the session stopped.
        var visualFinalizationWarning: Error?
        do {
            try activeSurfaceRecorder.stop()
        } catch {
            visualFinalizationWarning = error
        }
        var rawFinalizationError: Error?
        do {
            // Stop accepting raw rows even when ScreenCaptureKit reports a
            // finalization warning. The service records that warning before it
            // commits the session inactive, and no producer may append later.
            try rawEvents.stop()
        } catch {
            rawFinalizationError = error
        }
        axPollQueue.sync {}
        healthWriteQueue.sync {}
        if let worker = thread, worker !== Thread.current {
            let deadline = Date().addingTimeInterval(1)
            while worker.isExecuting, Date() < deadline {
                Thread.sleep(forTimeInterval: 0.01)
            }
        }
        thread = nil
        if let rawFinalizationError { throw rawFinalizationError }
        return visualFinalizationWarning
    }

    private func run() {
        let mask =
            eventMask(.leftMouseDown)
            | eventMask(.leftMouseUp)
            | eventMask(.rightMouseDown)
            | eventMask(.rightMouseUp)
            | eventMask(.otherMouseDown)
            | eventMask(.otherMouseUp)
            | eventMask(.leftMouseDragged)
            | eventMask(.rightMouseDragged)
            | eventMask(.otherMouseDragged)
            | eventMask(.scrollWheel)
            | eventMask(.keyDown)

        let refcon = Unmanaged.passUnretained(self).toOpaque()
        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .listenOnly,
            eventsOfInterest: CGEventMask(mask),
            callback: eventTapCallback,
            userInfo: refcon
        ) else {
            let message = "Unable to create macOS event tap. Grant Accessibility and Input Monitoring permissions to Hermes."
            markEventTapCreated(false, enabled: false, extra: ["error": message])
            try? store.appendTraceEvent(
                sessionDirectory,
                type: "evidence",
                source: "native",
                kind: "capture.error",
                payload: ["meta": ["message_preview": message], "effect": ["error_text": message], "app": store.captureContext()["app"] ?? [:]],
                quality: ["confidence": "low", "warnings": ["event_tap_unavailable"]]
            )
            return
        }

        eventTap = tap
        markEventTapCreated(true, enabled: false)
        runLoop = CFRunLoopGetCurrent()
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        if let runLoopSource {
            CFRunLoopAddSource(runLoop, runLoopSource, .commonModes)
        }
        installWorkspaceObservers()
        var initialContext = store.captureContext()
        initialContext["changeReason"] = "event_tap_started"
        lastContextSignature = contextSignature(initialContext)
        try? store.updateDesiredCaptureProvider(sessionDirectory, context: initialContext, reason: "event_tap_started")
        activeSurfaceRecorder.start(initialContext: initialContext)
        appendRecorderEvent(kind: "window.changed", payload: initialContext)
        lastFocusedAccessibilityObservation = focusedAccessibilityObservation(context: initialContext)
        lastSelectionSignature = lastFocusedAccessibilityObservation?.selectionSignature ?? ""
        lastAccessibilityContextSignature = accessibilityContextSignature(initialContext)
        installAXPollTimer()
        try? store.updateAccessibilityHealth(sessionDirectory, updates: [
            "ready": AXIsProcessTrusted(),
            "observer": "poll",
        ])
        CGEvent.tapEnable(tap: tap, enable: true)
        markEventTapCreated(true, enabled: true)
        CFRunLoopRun()
        if let runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetCurrent(), runLoopSource, .commonModes)
        }
    }

    fileprivate func handle(type: CGEventType, event: CGEvent) {
        lifecycleLock.lock()
        defer { lifecycleLock.unlock() }
        guard acceptingEvents else { return }
        // Only key-down events are recorded; modifier state is attached to the
        // action that consumes it.
        let focusedKeyboardTarget = type == .keyDown ? focusedAXTarget() : nil
        let keyboardCaptureContext = type == .keyDown ? store.captureContext() : nil
        let secureInputActive = type == .keyDown && store.isSecureInputActive()
        let keyboardRedactionReason = secureInputActive
            ? "secure_input"
            : focusedKeyboardTarget?.isSecureTextEntry == true
                ? "secure_accessibility_field"
                : nil
        if type != .keyDown,
           type != .tapDisabledByTimeout,
           type != .tapDisabledByUserInput
        {
            recentCGKeyboardInput = nil
        }
        if type != .tapDisabledByTimeout, type != .tapDisabledByUserInput {
            markRawInputEvent(type: String(describing: type))
            rawEvents.enqueue(
                .input,
                kind: String(describing: type),
                payload: rawInputPayload(type: type, event: event, redactionReason: keyboardRedactionReason),
                monotonicNanoseconds: event.timestamp
            )
        }
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            if let eventTap {
                CGEvent.tapEnable(tap: eventTap, enable: true)
                try? store.appendTraceEvent(
                    sessionDirectory,
                    type: "evidence",
                    source: "native",
                    kind: "capture.error",
                    payload: ["meta": ["message_preview": "macOS disabled the recorder event tap; Hermes re-enabled it."], "effect": ["error_text": type == .tapDisabledByTimeout ? "timeout" : "user_input"]],
                    quality: ["confidence": "low", "warnings": ["event_tap_reenabled"]]
                )
            }
        case .leftMouseDown, .rightMouseDown, .otherMouseDown:
            flushTextBuffer(reason: "pointer_event")
            mouseDown = mousePointPayload(event)
            observePointerSurface(event: event, phase: "down")
        case .leftMouseUp, .rightMouseUp, .otherMouseUp:
            flushTextBuffer(reason: "pointer_event")
            let origin = mouseDown ?? mousePointPayload(event)
            let destination = mousePointPayload(event)
            let dx = abs((origin["x"] as? Double ?? 0) - (destination["x"] as? Double ?? 0))
            let dy = abs((origin["y"] as? Double ?? 0) - (destination["y"] as? Double ?? 0))
            let dragged = dx > 4 || dy > 4
            let contextMenu = !dragged && isContextMenu(type: type, event: event)
            let hitTarget = axTarget(at: event.location)
            var mouse: JSONDict = [
                "button": buttonName(type: type, event: event),
                "clickCount": Int(event.getIntegerValueField(.mouseEventClickState)),
                "coordinateSpace": "global_screen_points",
                "modifiers": modifierNames(event.flags),
                "origin": origin,
                "destination": dragged ? destination : NSNull(),
            ]
            mouse["target"] = hitTarget?.target ?? NSNull()
            var payload = correctedContext(store.captureContext(), ownerApplication: hitTarget?.ownerApplication)
            payload["mouse"] = mouse
            let kind = dragged ? "mouse.drag" : contextMenu ? "mouse.context_menu" : "mouse.click"
            appendRecorderEvent(kind: kind, payload: payload)
            _ = appendSelectionIfChanged(
                reason: "pointer_event",
                context: correctedContext(store.captureContext(), ownerApplication: hitTarget?.ownerApplication)
            )
            mouseDown = nil
        case .leftMouseDragged, .rightMouseDragged, .otherMouseDragged:
            // Raw input retains the drag path without synchronous AX queries
            // on every high-frequency pointer sample.
            break
        case .scrollWheel:
            flushTextBuffer(reason: "pointer_event")
            let hitTarget = axTarget(at: event.location)
            var payload = correctedContext(store.captureContext(), ownerApplication: hitTarget?.ownerApplication)
            payload["mouse"] = [
                "button": "wheel",
                "clickCount": 0,
                "coordinateSpace": "global_screen_points",
                "modifiers": modifierNames(event.flags),
                "origin": mousePointPayload(event),
                "destination": NSNull(),
                "deltaX": Int(event.getIntegerValueField(.scrollWheelEventDeltaAxis2)),
                "deltaY": Int(event.getIntegerValueField(.scrollWheelEventDeltaAxis1)),
                "target": hitTarget?.target ?? [:],
            ]
            appendRecorderEvent(kind: "mouse.scroll", payload: payload)
        case .keyDown:
            // A later key replaces the correlation candidate even when the
            // target or action cannot be classified. A stale earlier key must
            // never suppress a new accessibility edit.
            recentCGKeyboardInput = nil
            let flags = event.flags
            let text = keyboardText(event)
            let modifiers = modifierNames(flags)
            let keyCode = Int(event.getIntegerValueField(.keyboardEventKeycode))
            var keyboard: JSONDict = [
                "keyCode": keyCode,
                "text": text,
                "modifiers": modifiers,
            ]
            let focusedTarget = focusedKeyboardTarget
            if let target = focusedTarget?.target {
                keyboard["target"] = target
            }
            let kind = flags.contains(.maskCommand) || flags.contains(.maskControl) || flags.contains(.maskAlternate)
                ? "keyboard.shortcut"
                : text == "\r" || text == "\n"
                    ? "keyboard.submit"
                    : "keyboard.text_input"
            if let action = AccessibilityReceiptDeduplication.actionForCGKey(
                modifiers: modifiers,
                keyCode: keyCode
            ), let target = focusedTarget?.target {
                let targetIdentity = accessibilityDeduplicationTargetIdentity(
                    target: target,
                    ownerApplication: focusedTarget?.ownerApplication,
                    context: keyboardCaptureContext ?? [:]
                )
                if !targetIdentity.isEmpty {
                    recentCGKeyboardInput = RecentCGKeyboardInput(
                        targetIdentity: targetIdentity,
                        action: action,
                        monotonicNanoseconds: DispatchTime.now().uptimeNanoseconds
                    )
                }
            }
            if let keyboardRedactionReason {
                flushTextBuffer(reason: "secure_input")
                var payload = correctedContext(
                    keyboardCaptureContext ?? store.captureContext(),
                    ownerApplication: focusedTarget?.ownerApplication
                )
                payload["keyboard"] = [
                    "keyCode": Int(event.getIntegerValueField(.keyboardEventKeycode)),
                    "modifiers": modifiers,
                    "secureInput": true,
                    "redacted": true,
                    "redactionReason": keyboardRedactionReason,
                    "target": keyboard["target"] ?? NSNull(),
                ]
                appendRecorderEvent(kind: kind, payload: payload)
                return
            }
            if flags.contains(.maskCommand), keyCode == 9,
               let clipboardText = NSPasteboard.general.string(forType: .string),
               !clipboardText.isEmpty
            {
                keyboard["clipboardText"] = String(clipboardText.prefix(100_000))
            }
            if kind == "keyboard.text_input", !text.isEmpty, !text.hasControlCharacters {
                if textBuffer.isEmpty {
                    textBufferTarget = keyboard["target"] as? JSONDict
                    textBufferOwnerApplication = focusedTarget?.ownerApplication
                }
                textBuffer.append(text)
                textBufferModifiers = modifiers
                return
            }
            flushTextBuffer(reason: kind)
            var payload = correctedContext(
                keyboardCaptureContext ?? store.captureContext(),
                ownerApplication: focusedTarget?.ownerApplication
            )
            if kind == "keyboard.submit", let value = terminalValue(in: payload) {
                keyboard["value"] = String(value.suffix(8_000))
            }
            payload["keyboard"] = keyboard
            appendRecorderEvent(kind: kind, payload: payload)
            _ = appendSelectionIfChanged(reason: kind)
        default:
            break
        }
    }

    private func flushTextBuffer(reason: String) {
        lifecycleLock.lock()
        defer { lifecycleLock.unlock() }
        guard !textBuffer.isEmpty else { return }
        var payload = correctedContext(store.captureContext(), ownerApplication: textBufferOwnerApplication)
        payload["keyboard"] = [
            "text": textBuffer,
            "modifiers": textBufferModifiers,
            "coalesced": true,
            "flushReason": reason,
            "target": textBufferTarget ?? [:],
        ]
        appendRecorderEvent(kind: "keyboard.text_input", payload: payload)
        textBuffer = ""
        textBufferModifiers = []
        textBufferTarget = nil
        textBufferOwnerApplication = nil
    }

    @discardableResult
    private func appendRecorderEvent(kind originalKind: String, payload: JSONDict) -> Bool {
        lifecycleLock.lock()
        defer { lifecycleLock.unlock() }
        guard acceptingEvents else { return false }
        let sanitizedPayload = RecorderURLSanitizer.sanitizePayload(payload)
        rawEvents.enqueue(
            .context,
            kind: originalKind,
            payload: sanitizedPayload
        )
        try? store.updateDesiredCaptureProvider(sessionDirectory, context: sanitizedPayload, reason: originalKind)
        // Every resolved app/window context is authoritative. The surface
        // recorder owns transient-window collapsing and identity deduplication,
        // so routing decisions here must not silently hide legitimate AX,
        // workspace, or active-space transitions from visual capture.
        let surfaceReason = sanitizedPayload["changeReason"] as? String ?? originalKind
        activeSurfaceRecorder.observe(context: sanitizedPayload, reason: surfaceReason)
        let traceKind = traceKind(for: originalKind, payload: sanitizedPayload)
        var payloadGroups = tracePayload(kind: originalKind, traceKind: traceKind, payload: sanitizedPayload)
        let quality: JSONDict = sanitizedPayload["provenance"] is JSONDict
            ? ["confidence": "medium", "warnings": ["accessibility_observed_action"]]
            : ["confidence": "medium", "warnings": []]
        if traceKind == "window.changed" {
            do {
                try store.appendTraceEvent(sessionDirectory, type: "context", source: "native", kind: traceKind, payload: payloadGroups, quality: quality)
                return true
            } catch {
                return false
            }
        }

        if shouldRouteAsChromeFallback(kind: originalKind, payload: sanitizedPayload) {
            do {
                try store.appendTraceEvent(
                    sessionDirectory,
                    type: "evidence",
                    source: "native",
                    kind: "native.chrome_fallback",
                    payload: payloadGroups,
                    quality: quality
                )
                return true
            } catch {
                return false
            }
        }

        let correlationID = UUID().uuidString.lowercased()
        var meta = payloadGroups["meta"] as? JSONDict ?? [:]
        meta["correlation_id"] = correlationID
        payloadGroups["meta"] = meta
        do {
            try store.appendTraceEvent(
                sessionDirectory,
                type: "action",
                source: "native",
                kind: traceKind,
                payload: payloadGroups,
                quality: quality
            )
            return true
        } catch {
            return false
        }
    }

    private func observePointerSurface(event: CGEvent, phase: String) {
        let hitTarget = axTarget(at: event.location)
        var context = correctedContext(store.captureContext(), ownerApplication: hitTarget?.ownerApplication)
        context["mouse"] = [
            "origin": mousePointPayload(event),
            "target": hitTarget?.target ?? NSNull(),
            "phase": phase,
        ]
        rawEvents.enqueue(
            .context,
            kind: "active_surface.pointer_\(phase)",
            payload: context
        )
        activeSurfaceRecorder.observe(context: context, reason: "pointer_\(phase)")
    }

    private func traceKind(for kind: String, payload: JSONDict) -> String {
        if kind == "keyboard.shortcut",
           let keyboard = payload["keyboard"] as? JSONDict,
           let modifiers = keyboard["modifiers"] as? [String],
           modifiers.contains("command")
        {
            let keyCode = (keyboard["keyCode"] as? NSNumber)?.intValue ?? keyboard["keyCode"] as? Int ?? -1
            let key = String(describing: keyboard["text"] ?? "").lowercased()
            if keyCode == 8 || key == "c" { return "copy" }
            if keyCode == 7 || key == "x" { return "cut" }
            if keyCode == 9 || key == "v" { return "paste" }
        }
        switch kind {
        case "mouse.click": return "click"
        case "mouse.context_menu": return "context_menu"
        case "mouse.drag": return "drag"
        case "mouse.scroll": return "scroll"
        case "keyboard.text_input": return "type"
        case "accessibility.text_inserted": return "type"
        case "accessibility.value_changed": return "change"
        case "accessibility.paste": return "paste"
        case "keyboard.submit": return "submit"
        case "keyboard.shortcut": return "shortcut"
        case "selection.changed", "accessibility.selection_changed": return "select"
        case "window.changed": return "window.changed"
        default: return "key"
        }
    }

    private func tracePayload(kind originalKind: String, traceKind: String, payload: JSONDict) -> JSONDict {
        let app = payload["app"] as? JSONDict ?? [:]
        let window = payload["window"] as? JSONDict ?? [:]
        var meta: JSONDict = [
            "original_kind": originalKind,
            "reason": payload["changeReason"] ?? "",
        ]
        if let provenance = payload["provenance"] as? JSONDict {
            meta["provenance"] = provenance
        }
        var groups: JSONDict = [
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
            "meta": meta,
        ]
        if let url = window["url"] as? String, !url.isEmpty {
            groups["browser"] = [
                "url": url,
                "top_url": url,
                "profile_hint": "current-profile",
            ]
        }


        var target: JSONDict = [:]
        var input: JSONDict = [:]
        if ["selection.changed", "accessibility.selection_changed"].contains(originalKind),
           let selectionTarget = payload["target"] as? JSONDict
        {
            target = selectionTarget
        }

        if let mouse = payload["mouse"] as? JSONDict {
            if let mouseTarget = mouse["target"] as? JSONDict {
                target = mouseTarget
            }
            if let origin = mouse["origin"] as? JSONDict {
                input["x"] = origin["x"] ?? 0
                input["y"] = origin["y"] ?? 0
                target["coordinates"] = ["x": origin["x"] ?? 0, "y": origin["y"] ?? 0]
            }
            input["button"] = mouse["button"] ?? ""
            input["modifiers"] = mouse["modifiers"] ?? []
            input["delta_x"] = mouse["deltaX"] ?? 0
            input["delta_y"] = mouse["deltaY"] ?? 0
        }
        if let keyboard = payload["keyboard"] as? JSONDict {
            if let focusedTarget = keyboard["target"] as? JSONDict {
                target = focusedTarget
            }
            input["text"] = keyboard["text"] ?? ""
            input["value"] = keyboard["value"] ?? ""
            input["key"] = keyboard["text"] ?? ""
            input["code"] = keyboard["keyCode"] ?? ""
            input["modifiers"] = keyboard["modifiers"] ?? []
            input["redacted"] = keyboard["redacted"] ?? false
            input["secure_input"] = keyboard["secureInput"] ?? false
            if let mutation = keyboard["mutation"] as? JSONDict {
                input["mutation"] = mutation
            }
            if let clipboardText = keyboard["clipboardText"] as? String {
                input["text"] = clipboardText
                input["value"] = clipboardText
                input["clipboard_text"] = clipboardText
            }
            if let role = keyboard["role"] {
                target["role"] = role
            }
            if let title = keyboard["title"] {
                target["label"] = title
            }
            if let identifier = keyboard["identifier"] {
                target["id"] = identifier
            }
        }
        if let selection = payload["selection"] as? JSONDict {
            input["selected_text"] = selection["selectedText"] ?? ""
            input["selected_range"] = selection["selectedRange"] ?? [:]
            input["selected_items"] = selection["selectedItems"] ?? []
        }
        if traceKind == "submit" {
            groups["effect"] = ["side_effect": true, "before_url": window["url"] ?? ""]
        }
        if !target.isEmpty {
            groups["target"] = target
        }
        if !input.isEmpty {
            groups["input"] = input
        }
        return groups
    }

    private func markEventTapCreated(_ created: Bool, enabled: Bool, extra: JSONDict = [:]) {
        var updates: JSONDict = [
            "tapCreated": created,
            "tapEnabled": enabled,
            "updatedAt": isoTimestamp(),
        ]
        for (key, value) in extra {
            updates[key] = value
        }
        try? store.updateEventTapHealth(sessionDirectory, updates: updates)
    }

    private func markRawInputEvent(type: String) {
        let timestamp = isoTimestamp()
        let nowSeconds = Date().timeIntervalSince1970
        var updates: JSONDict = [:]
        healthLock.lock()
        rawInputEventCount += 1
        let firstRawInput = firstRawInputAt == nil
        if firstRawInput {
            firstRawInputAt = timestamp
        }
        let shouldWrite = firstRawInput || (nowSeconds - lastRawInputHealthWriteAt) >= 1
        if shouldWrite {
            lastRawInputHealthWriteAt = nowSeconds
            if firstRawInput {
                updates["firstRawInputAt"] = timestamp
            }
            updates["rawInputEventCount"] = rawInputEventCount
            updates["lastRawInputAt"] = timestamp
            updates["lastRawInputType"] = type
            updates["updatedAt"] = timestamp
        }
        healthLock.unlock()
        guard !updates.isEmpty else { return }
        let encodedUpdates = try? RecorderJSON.encode(updates)
        healthWriteQueue.async { [store, sessionDirectory, encodedUpdates] in
            guard let encodedUpdates,
                  let decodedUpdates = try? JSONSerialization.jsonObject(with: encodedUpdates) as? JSONDict
            else { return }
            try? store.updateEventTapHealth(sessionDirectory, updates: decodedUpdates)
        }
    }

    private func markAXActivity(reason: String) {
        let timestamp = isoTimestamp()
        let nowSeconds = Date().timeIntervalSince1970
        var updates: JSONDict = [:]
        healthLock.lock()
        axActivityEventCount += 1
        let firstAXActivity = firstAXActivityAt == nil
        if firstAXActivity {
            firstAXActivityAt = timestamp
        }
        let shouldWrite = firstAXActivity || (nowSeconds - lastAXActivityHealthWriteAt) >= 1
        if shouldWrite {
            lastAXActivityHealthWriteAt = nowSeconds
            if firstAXActivity {
                updates["firstAXActivityAt"] = timestamp
            }
            updates["axActivityEventCount"] = axActivityEventCount
            updates["lastAXActivityAt"] = timestamp
            updates["lastAXActivityReason"] = reason
            updates["axActivityWithoutRawInput"] = rawInputEventCount == 0
            updates["updatedAt"] = timestamp
        }
        healthLock.unlock()
        guard !updates.isEmpty else { return }
        let encodedUpdates = try? RecorderJSON.encode(updates)
        healthWriteQueue.async { [store, sessionDirectory, encodedUpdates] in
            guard let encodedUpdates,
                  let decodedUpdates = try? JSONSerialization.jsonObject(with: encodedUpdates) as? JSONDict
            else { return }
            try? store.updateEventTapHealth(sessionDirectory, updates: decodedUpdates)
        }
    }

    private func shouldRouteAsChromeFallback(kind: String, payload: JSONDict) -> Bool {
        guard frontmostAppIsChrome(payload) else { return false }
        return [
            "mouse.click",
            "mouse.context_menu",
            "mouse.drag",
            "mouse.scroll",
            "keyboard.text_input",
            "keyboard.submit",
            "keyboard.shortcut",
            "selection.changed",
            "accessibility.text_inserted",
            "accessibility.value_changed",
            "accessibility.paste",
            "accessibility.selection_changed",
        ].contains(kind)
    }

    private func frontmostAppIsChrome(_ payload: JSONDict) -> Bool {
        guard let app = payload["app"] as? JSONDict,
              let bundleID = app["bundleIdentifier"] as? String
        else {
            return false
        }
        return isChromeCaptureBundle(bundleID)
    }

    private func appendSelectionIfChanged(reason: String, context: JSONDict? = nil) -> Bool {
        guard let selection = selectionPayload() else { return false }
        let signature = stableJSONSignature(selection)
        guard signature != lastSelectionSignature else { return false }
        lastSelectionSignature = signature
        let focusedTarget = focusedAXTarget()
        var payload = context ?? correctedContext(store.captureContext(), ownerApplication: focusedTarget?.ownerApplication)
        if let focusedTarget {
            payload["target"] = focusedTarget.target
        }
        payload["selection"] = selection
        payload["changeReason"] = reason
        appendRecorderEvent(kind: "selection.changed", payload: payload)
        return true
    }

    private func correctedContext(_ context: JSONDict, ownerApplication: JSONDict?) -> JSONDict {
        guard let ownerApplication,
              let ownerPID = ownerApplication["processIdentifier"] as? Int
        else {
            return context
        }
        let capturedApp = context["app"] as? JSONDict ?? [:]
        guard let capturedPID = capturedApp["processIdentifier"] as? Int,
              capturedPID != ownerPID
        else {
            return context
        }
        var corrected = context
        corrected["app"] = ownerApplication
        corrected["window"] = [
            "windowID": NSNull(),
            "windowName": "",
            "title": "",
            "url": NSNull(),
        ]
        return corrected
    }

    private func terminalValue(in payload: JSONDict) -> String? {
        guard let app = payload["app"] as? JSONDict,
              let bundleID = app["bundleIdentifier"] as? String,
              isTerminalBundle(bundleID),
              let value = focusedElementStringValue(),
              !value.isEmpty
        else {
            return nil
        }
        return value
    }

    private func focusedAccessibilityObservation(context: JSONDict) -> FocusedAccessibilityObservation? {
        guard AXIsProcessTrusted(), let focused = focusedAXElement(),
              let hitTarget = axHitTarget(for: focused, includeValuePreview: false)
        else {
            return nil
        }
        let secure = store.isSecureInputActive() || isSecureTextElement(focused)
        var target = hitTarget.target
        if secure {
            target["secure_input"] = true
            target["redacted"] = true
        }
        let value: String?
        if secure || !isPositivelyEditableTextElement(focused) {
            // Never probe values for secure or non-editable controls. Besides
            // avoiding sensitive reads, this keeps AXError.noValue from being
            // interpreted as an editable empty baseline on arbitrary AX
            // attributes.
            value = nil
        } else {
            value = axEditableValueString(
                focused,
                allowNoValueAsEmptyBaseline: true
            )
        }
        let selection = secure
            ? nil
            : selectionPayload(for: focused, includeCaretRange: true)
        return FocusedAccessibilityObservation(
            identity: accessibilityElementIdentity(focused, target: target),
            deduplicationIdentity: accessibilityDeduplicationTargetIdentity(
                target: target,
                ownerApplication: hitTarget.ownerApplication,
                context: context
            ),
            target: target,
            ownerApplication: hitTarget.ownerApplication,
            secure: secure,
            value: value,
            selection: selection
        )
    }

    private func reconcileFocusedAccessibilityChanges(
        current: FocusedAccessibilityObservation?,
        clipboardText: String?,
        nowNanoseconds: UInt64
    ) -> PendingAccessibilityReceipt? {
        let previous = lastFocusedAccessibilityObservation
        lastFocusedAccessibilityObservation = current

        guard let current else {
            lastSelectionSignature = ""
            return nil
        }
        guard let previous, previous.identity == current.identity else {
            lastSelectionSignature = current.selectionSignature
            return nil
        }
        guard !previous.secure, !current.secure else {
            lastSelectionSignature = current.selectionSignature
            return nil
        }

        if let previousValue = previous.value,
           let currentValue = current.value,
           let mutation = AccessibilityMutationInference.textMutation(
               previous: previousValue,
               current: currentValue
           )
        {
            lastSelectionSignature = current.selectionSignature
            let actionKind = AccessibilityMutationInference.actionKind(
                for: mutation,
                clipboardText: clipboardText
            )
            if let action = AccessibilityReceiptDeduplication.actionForAccessibilityMutation(actionKind),
               AccessibilityReceiptDeduplication.shouldSuppress(
                   recent: recentCGKeyboardInput,
                   targetIdentity: current.deduplicationIdentity,
                   action: action,
                   nowNanoseconds: nowNanoseconds,
                   maximumAgeNanoseconds: Self.cgKeyboardDeduplicationNanoseconds
               )
            {
                return nil
            }
            return .value(
                observation: current,
                mutation: mutation,
                resultingValue: currentValue,
                actionKind: actionKind
            )
        }

        guard current.selectionSignature != previous.selectionSignature else { return nil }
        let priorSelectionSignature = lastSelectionSignature
        lastSelectionSignature = current.selectionSignature
        guard selectionIsMeaningful(current.selection),
              current.selectionSignature != priorSelectionSignature,
              let selection = current.selection
        else {
            return nil
        }
        if AccessibilityReceiptDeduplication.shouldSuppress(
            recent: recentCGKeyboardInput,
            targetIdentity: current.deduplicationIdentity,
            action: .selection,
            nowNanoseconds: nowNanoseconds,
            maximumAgeNanoseconds: Self.cgKeyboardDeduplicationNanoseconds
        ) {
            return nil
        }
        return .selection(observation: current, selection: selection)
    }

    @discardableResult
    private func appendAccessibilityValueReceipt(
        observation: FocusedAccessibilityObservation,
        mutation: AccessibilityTextMutation,
        resultingValue: String,
        actionKind: String
    ) -> Bool {
        let originalKind: String
        switch actionKind {
        case "paste": originalKind = "accessibility.paste"
        case "type": originalKind = "accessibility.text_inserted"
        default: originalKind = "accessibility.value_changed"
        }
        let insertedText = boundedObservedText(mutation.insertedText)
        let resultingText = boundedObservedText(resultingValue)
        let mutationPayload: JSONDict = [
            "action": actionKind,
            "range": ["location": mutation.location, "length": mutation.removedLength],
            "inserted_text": insertedText,
            "inserted_length": (mutation.insertedText as NSString).length,
            "resulting_value_truncated": resultingText != resultingValue,
        ]
        let provenance = accessibilityProvenance(inference: "focused_value_delta")

        if RecorderInputEvidenceRouting.shouldAppendRawInput(provenance: provenance) {
            markRawInputEvent(type: originalKind)
            rawEvents.enqueue(.input, kind: originalKind, payload: [
                "provenance": provenance,
                "target": observation.target,
                "mutation": mutationPayload,
                "redacted": false,
            ])
        }

        var keyboard: JSONDict = [
            "target": observation.target,
            "modifiers": [],
            "mutation": mutationPayload,
            "observedViaAccessibility": true,
        ]
        switch actionKind {
        case "paste":
            keyboard["text"] = insertedText
            keyboard["value"] = insertedText
            keyboard["clipboardText"] = insertedText
        case "type":
            keyboard["text"] = insertedText
            keyboard["value"] = insertedText
        default:
            keyboard["text"] = insertedText
            keyboard["value"] = resultingText
        }

        var payload = correctedContext(
            store.captureContext(),
            ownerApplication: observation.ownerApplication
        )
        payload["keyboard"] = keyboard
        payload["provenance"] = provenance
        payload["changeReason"] = "accessibility_value_changed"
        return appendRecorderEvent(kind: originalKind, payload: payload)
    }

    @discardableResult
    private func appendAccessibilitySelectionReceipt(
        observation: FocusedAccessibilityObservation,
        selection: JSONDict
    ) -> Bool {
        let originalKind = "accessibility.selection_changed"
        let provenance = accessibilityProvenance(inference: "focused_selection_delta")
        if RecorderInputEvidenceRouting.shouldAppendRawInput(provenance: provenance) {
            markRawInputEvent(type: originalKind)
            rawEvents.enqueue(.input, kind: originalKind, payload: [
                "provenance": provenance,
                "target": observation.target,
                "selection": selection,
                "redacted": false,
            ])
        }

        var payload = correctedContext(
            store.captureContext(),
            ownerApplication: observation.ownerApplication
        )
        payload["target"] = observation.target
        payload["selection"] = selection
        payload["provenance"] = provenance
        payload["changeReason"] = "accessibility_selection_changed"
        return appendRecorderEvent(kind: originalKind, payload: payload)
    }

    private func accessibilityProvenance(inference: String) -> JSONDict {
        [
            "source": "accessibility_observation",
            "api": "ApplicationServices.AXUIElement",
            "observer": "poll",
            "inference": inference,
            "cg_event_observed": false,
        ]
    }

    private func boundedObservedText(_ value: String) -> String {
        String(value.prefix(Self.maximumObservedTextLength))
    }

    private func installAXPollTimer() {
        guard AXIsProcessTrusted() else { return }
        removeAXPollTimer()
        let timer = DispatchSource.makeTimerSource(queue: axPollQueue)
        timer.schedule(deadline: .now() + .milliseconds(350), repeating: .milliseconds(500))
        timer.setEventHandler { [weak self] in
            self?.pollAXChanges()
        }
        axPollTimer = timer
        timer.resume()
    }

    private func removeAXPollTimer() {
        axPollTimer?.cancel()
        axPollTimer = nil
    }

    private func pollAXChanges() {
        lifecycleLock.lock()
        let shouldPoll = acceptingEvents
        lifecycleLock.unlock()
        guard shouldPoll, AXIsProcessTrusted() else { return }

        // All cross-process AX reads happen without the lifecycle lock. A
        // stalled application can delay this poll, but it cannot prevent stop
        // from disabling capture and changing the recorder lifecycle state.
        var payload = store.captureContext()
        // A CG key delivered after this point is not allowed to suppress the
        // observation being read. In the ambiguous overlap case we retain an
        // extra AX receipt instead of dropping an unrelated edit.
        let observationStartedAtNanoseconds = DispatchTime.now().uptimeNanoseconds
        let currentObservation = focusedAccessibilityObservation(context: payload)
        let clipboardText = NSPasteboard.general.string(forType: .string)

        var windowChange: JSONDict?
        var pendingReceipt: PendingAccessibilityReceipt?
        lifecycleLock.lock()
        guard acceptingEvents else {
            lifecycleLock.unlock()
            return
        }
        let accessibilitySignature = accessibilityContextSignature(payload)
        if !accessibilitySignature.isEmpty,
           !lastAccessibilityContextSignature.isEmpty,
           accessibilitySignature != lastAccessibilityContextSignature
        {
            lastFocusedAccessibilityObservation = nil
            lastSelectionSignature = ""
        }
        if !accessibilitySignature.isEmpty {
            lastAccessibilityContextSignature = accessibilitySignature
        }
        let signature = contextSignature(payload)
        if !signature.isEmpty, signature != lastContextSignature {
            lastContextSignature = signature
            payload["changeReason"] = "accessibility_context_changed"
            windowChange = payload
        }
        pendingReceipt = reconcileFocusedAccessibilityChanges(
            current: currentObservation,
            clipboardText: clipboardText,
            nowNanoseconds: observationStartedAtNanoseconds
        )
        lifecycleLock.unlock()

        var emittedAXActivity = false
        if let windowChange {
            emittedAXActivity = appendRecorderEvent(
                kind: "window.changed",
                payload: windowChange
            ) || emittedAXActivity
        }
        switch pendingReceipt {
        case let .value(observation, mutation, resultingValue, actionKind):
            emittedAXActivity = appendAccessibilityValueReceipt(
                observation: observation,
                mutation: mutation,
                resultingValue: resultingValue,
                actionKind: actionKind
            ) || emittedAXActivity
        case let .selection(observation, selection):
            emittedAXActivity = appendAccessibilitySelectionReceipt(
                observation: observation,
                selection: selection
            ) || emittedAXActivity
        case nil:
            break
        }
        if emittedAXActivity {
            markAXActivity(reason: "accessibility_poll")
        }
    }

    private func contextSignature(_ payload: JSONDict) -> String {
        let app = payload["app"] as? JSONDict ?? [:]
        let window = payload["window"] as? JSONDict ?? [:]
        return [
            String(describing: app["bundleIdentifier"] ?? ""),
            String(describing: app["processIdentifier"] ?? ""),
            String(describing: window["windowID"] ?? ""),
            String(describing: window["title"] ?? window["windowName"] ?? ""),
            String(describing: window["url"] ?? ""),
        ].joined(separator: "|")
    }

    private func accessibilityContextSignature(_ payload: JSONDict) -> String {
        let app = payload["app"] as? JSONDict ?? [:]
        let window = payload["window"] as? JSONDict ?? [:]
        let bundleIdentifier = String(describing: app["bundleIdentifier"] ?? "")
        let processIdentifier = String(describing: app["processIdentifier"] ?? "")
        guard !bundleIdentifier.isEmpty || !processIdentifier.isEmpty else { return "" }
        return [
            bundleIdentifier,
            processIdentifier,
            String(describing: window["windowID"] ?? ""),
        ].joined(separator: "|")
    }

    private func installWorkspaceObservers() {
        let center = NSWorkspace.shared.notificationCenter
        let names: [(Notification.Name, String)] = [
            (NSWorkspace.didActivateApplicationNotification, "frontmost_app_changed"),
            (NSWorkspace.didTerminateApplicationNotification, "frontmost_app_terminated"),
            (NSWorkspace.activeSpaceDidChangeNotification, "active_space_changed"),
        ]
        workspaceObservers = names.map { name, reason in
            center.addObserver(forName: name, object: nil, queue: nil) { [weak self] _ in
                self?.flushTextBuffer(reason: "window_change")
                guard let self else { return }
                var payload = self.store.captureContext()
                payload["changeReason"] = reason
                let signature = self.contextSignature(payload)
                self.lifecycleLock.lock()
                guard self.acceptingEvents else {
                    self.lifecycleLock.unlock()
                    return
                }
                self.lastContextSignature = signature
                self.recentCGKeyboardInput = nil
                self.lifecycleLock.unlock()
                self.appendRecorderEvent(kind: "window.changed", payload: payload)
            }
        }
    }

    private func removeWorkspaceObservers() {
        let center = NSWorkspace.shared.notificationCenter
        for observer in workspaceObservers {
            center.removeObserver(observer)
        }
        workspaceObservers = []
    }
}

private let eventTapCallback: CGEventTapCallBack = { _, type, event, refcon in
    if let refcon {
        let recorder = Unmanaged<EventTapRecorder>.fromOpaque(refcon).takeUnretainedValue()
        recorder.handle(type: type, event: event)
    }
    return Unmanaged.passUnretained(event)
}

private func eventMask(_ type: CGEventType) -> UInt64 {
    1 << UInt64(type.rawValue)
}

private func mousePointPayload(_ event: CGEvent) -> JSONDict {
    let point = event.location
    return [
        "x": Double(point.x),
        "y": Double(point.y),
    ]
}

private func rawInputPayload(type: CGEventType, event: CGEvent, redactionReason: String?) -> JSONDict {
    var payload: JSONDict = [
        "modifiers": modifierNames(event.flags),
    ]
    if type == .keyDown {
        payload["key_code"] = Int(event.getIntegerValueField(.keyboardEventKeycode))
        payload["autorepeat"] = event.getIntegerValueField(.keyboardEventAutorepeat) != 0
        payload["secure_input"] = redactionReason != nil
        if redactionReason == nil {
            payload["text"] = keyboardText(event)
        } else {
            payload["redacted"] = true
            payload["redaction_reason"] = redactionReason
        }
    } else {
        let point = event.location
        payload["x"] = Double(point.x)
        payload["y"] = Double(point.y)
        if type == .scrollWheel {
            payload["delta_x"] = Int(event.getIntegerValueField(.scrollWheelEventDeltaAxis2))
            payload["delta_y"] = Int(event.getIntegerValueField(.scrollWheelEventDeltaAxis1))
        } else {
            payload["click_count"] = Int(event.getIntegerValueField(.mouseEventClickState))
        }
    }
    return payload
}

private func buttonName(type: CGEventType, event: CGEvent) -> String {
    if type == .rightMouseDown || type == .rightMouseUp || event.flags.contains(.maskControl) {
        return "right"
    }
    if type == .otherMouseDown || type == .otherMouseUp {
        return "other"
    }
    return "left"
}

private func isContextMenu(type: CGEventType, event: CGEvent) -> Bool {
    type == .rightMouseUp || event.flags.contains(.maskControl)
}

private func modifierNames(_ flags: CGEventFlags) -> [String] {
    var names: [String] = []
    if flags.contains(.maskCommand) { names.append("command") }
    if flags.contains(.maskControl) { names.append("control") }
    if flags.contains(.maskAlternate) { names.append("option") }
    if flags.contains(.maskShift) { names.append("shift") }
    if flags.contains(.maskAlphaShift) { names.append("caps_lock") }
    return names
}

private func keyboardText(_ event: CGEvent) -> String {
    let carbonText = carbonKeyboardText(event)
    if !carbonText.isEmpty {
        return carbonText
    }
    var length = 0
    var buffer = [UniChar](repeating: 0, count: 8)
    event.keyboardGetUnicodeString(maxStringLength: buffer.count, actualStringLength: &length, unicodeString: &buffer)
    guard length > 0 else { return "" }
    return String(utf16CodeUnits: buffer, count: length)
}

private func carbonKeyboardText(_ event: CGEvent) -> String {
    guard let source = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
          let layoutDataRef = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData)
    else {
        return ""
    }
    let data = unsafeBitCast(layoutDataRef, to: CFData.self)
    guard let bytes = CFDataGetBytePtr(data) else { return "" }
    let layout = bytes.withMemoryRebound(to: UCKeyboardLayout.self, capacity: 1) { $0 }
    let keyCode = UInt16(event.getIntegerValueField(.keyboardEventKeycode))
    let modifierState = UInt32((event.flags.rawValue >> 16) & 0xff)
    var deadKeyState: UInt32 = 0
    var length = 0
    var buffer = [UniChar](repeating: 0, count: 8)
    let status = UCKeyTranslate(
        layout,
        keyCode,
        UInt16(kUCKeyActionDown),
        modifierState,
        UInt32(LMGetKbdType()),
        OptionBits(kUCKeyTranslateNoDeadKeysBit),
        &deadKeyState,
        buffer.count,
        &length,
        &buffer
    )
    guard status == noErr, length > 0 else { return "" }
    return String(utf16CodeUnits: buffer, count: length)
}

private extension String {
    var hasControlCharacters: Bool {
        unicodeScalars.contains { scalar in
            CharacterSet.controlCharacters.contains(scalar)
        }
    }
}

private func axTarget(at point: CGPoint) -> AXHitTarget? {
    guard AXIsProcessTrusted() else { return nil }
    let system = AXUIElementCreateSystemWide()
    var element: AXUIElement?
    guard AXUIElementCopyElementAtPosition(system, Float(point.x), Float(point.y), &element) == .success,
          let element
    else {
        return nil
    }
    return axHitTarget(for: element, includeValuePreview: true)
}

private func axHitTarget(for element: AXUIElement, includeValuePreview: Bool) -> AXHitTarget? {
    var target: JSONDict = [:]
    let axFields: [(String, String)] = [
        ("role", kAXRoleAttribute),
        ("subrole", kAXSubroleAttribute),
        ("title", kAXTitleAttribute),
        ("description", kAXDescriptionAttribute),
        ("identifier", kAXIdentifierAttribute),
        ("placeholder", kAXPlaceholderValueAttribute),
    ]
    var ax: JSONDict = [:]
    for (key, attribute) in axFields {
        let value = axStringAttribute(element, attribute as CFString)
        if !value.isEmpty {
            ax[key] = value
            if key == "role" { target["role"] = value }
            if key == "title" || (key == "description" && target["label"] == nil) { target["label"] = value }
            if key == "identifier" { target["id"] = value }
            if key == "placeholder" { target["placeholder"] = value }
        }
    }
    let secureTextEntry = (ax["role"] as? String) == (kAXSecureTextFieldSubrole as String)
        || (ax["subrole"] as? String) == (kAXSecureTextFieldSubrole as String)
    if secureTextEntry {
        ax["value_redacted"] = true
    } else if includeValuePreview {
        let valuePreview = axStringAttribute(element, kAXValueAttribute as CFString)
        if !valuePreview.isEmpty {
            ax["value_preview"] = String(valuePreview.prefix(160))
        }
    }
    let ancestors = axAncestorPath(from: element, limit: 8)
    if !ancestors.isEmpty {
        ax["ancestor_path"] = ancestors
        target["shadow_path"] = ancestors
    }
    if !ax.isEmpty {
        target["ax"] = ax
    }
    guard !target.isEmpty else { return nil }
    return AXHitTarget(target: target, ownerApplication: axOwnerApplication(for: element))
}

private func axAncestorPath(from element: AXUIElement, limit: Int) -> [JSONDict] {
    var path: [JSONDict] = []
    var current: AXUIElement? = element
    while path.count < limit, let node = current {
        let role = axStringAttribute(node, kAXRoleAttribute as CFString)
        let title = axStringAttribute(node, kAXTitleAttribute as CFString)
        let description = axStringAttribute(node, kAXDescriptionAttribute as CFString)
        let identifier = axStringAttribute(node, kAXIdentifierAttribute as CFString)
        var item: JSONDict = [:]
        if !role.isEmpty { item["role"] = role }
        if !title.isEmpty { item["title"] = title }
        if !description.isEmpty { item["description"] = description }
        if !identifier.isEmpty { item["identifier"] = identifier }
        if !item.isEmpty {
            path.append(item)
        }
        guard let parent = axAttribute(node, kAXParentAttribute as CFString) else {
            break
        }
        let ref = parent as CFTypeRef
        guard CFGetTypeID(ref) == AXUIElementGetTypeID() else {
            break
        }
        current = (ref as! AXUIElement)
    }
    return path
}


private func axStringAttribute(_ element: AXUIElement, _ attribute: CFString, fallback: String = "") -> String {
    guard let value = axAttribute(element, attribute) else { return fallback }
    if let string = value as? String {
        return string
    }
    if let number = value as? NSNumber {
        return number.stringValue
    }
    return fallback
}

private func axStringAttributeAllowingEmpty(_ element: AXUIElement, _ attribute: CFString) -> String? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success,
          let value
    else {
        return nil
    }
    return normalizedAccessibilityStringValue(value)
}

private func axEditableValueString(
    _ element: AXUIElement,
    allowNoValueAsEmptyBaseline: Bool
) -> String? {
    var value: CFTypeRef?
    let copyResult = AXUIElementCopyAttributeValue(
        element,
        kAXValueAttribute as CFString,
        &value
    )
    return normalizedEditableAccessibilityValue(
        copyResult: copyResult,
        value: value,
        allowNoValueAsEmptyBaseline: allowNoValueAsEmptyBaseline
    )
}

func normalizedEditableAccessibilityValue(
    copyResult: AXError,
    value: Any?,
    allowNoValueAsEmptyBaseline: Bool
) -> String? {
    switch copyResult {
    case .success:
        guard let value else { return nil }
        return normalizedAccessibilityStringValue(value)
    case .noValue:
        // Newly-created rich text documents can expose a settable AXValue
        // while returning AXError.noValue until the first edit. The caller may
        // enable the empty baseline only after positively
        // identifying an editable text control, so the sentinel is not a
        // blanket fallback for unsupported or protected attributes.
        return allowNoValueAsEmptyBaseline ? "" : nil
    default:
        return nil
    }
}

func normalizedAccessibilityStringValue(_ value: Any) -> String? {
    if let string = value as? String {
        return string
    }
    if let attributed = value as? NSAttributedString {
        return attributed.string
    }
    return nil
}

private func selectionPayload() -> JSONDict? {
    guard AXIsProcessTrusted(), let focused = focusedAXElement() else { return nil }
    guard !isSecureTextElement(focused) else { return nil }
    return selectionPayload(for: focused, includeCaretRange: false)
}

private func selectionPayload(for focused: AXUIElement, includeCaretRange: Bool) -> JSONDict? {
    var payload: JSONDict = [:]
    if let selectedText = axStringAttributeAllowingEmpty(focused, kAXSelectedTextAttribute as CFString),
       !selectedText.isEmpty
    {
        payload["selectedText"] = selectedText
    }
    if let range = axRangeAttribute(focused, kAXSelectedTextRangeAttribute as CFString),
       let length = range["length"] as? Int,
       includeCaretRange || length > 0
    {
        payload["selectedRange"] = range
    }
    if let selectedChildren = axAttribute(focused, kAXSelectedChildrenAttribute as CFString) as? [AXUIElement], !selectedChildren.isEmpty {
        payload["selectedItems"] = selectedChildren.compactMap { element in
            selectedElementSummary(element)
        }
    }
    return payload.isEmpty ? nil : payload
}

private func selectionIsMeaningful(_ selection: JSONDict?) -> Bool {
    guard let selection else { return false }
    if let text = selection["selectedText"] as? String, !text.isEmpty { return true }
    if let range = selection["selectedRange"] as? JSONDict,
       let length = range["length"] as? Int,
       length > 0
    {
        return true
    }
    if let items = selection["selectedItems"] as? [Any], !items.isEmpty { return true }
    return false
}

private func stableJSONSignature(_ value: JSONDict?) -> String {
    guard let value, let data = try? RecorderJSON.encode(value) else { return "" }
    return String(decoding: data, as: UTF8.self)
}

private func focusedElementStringValue() -> String? {
    guard AXIsProcessTrusted(), let focused = focusedAXElement() else { return nil }
    guard !isSecureTextElement(focused) else { return nil }
    if let value = axAttribute(focused, kAXValueAttribute as CFString) as? String {
        return value
    }
    return nil
}

private func focusedAXTarget() -> AXHitTarget? {
    guard AXIsProcessTrusted(), let focused = focusedAXElement() else { return nil }
    return axHitTarget(for: focused, includeValuePreview: false)
}

private func isSecureTextElement(_ element: AXUIElement) -> Bool {
    let role = axStringAttribute(element, kAXRoleAttribute as CFString)
    let subrole = axStringAttribute(element, kAXSubroleAttribute as CFString)
    return role == (kAXSecureTextFieldSubrole as String)
        || subrole == (kAXSecureTextFieldSubrole as String)
}

func isPositivelyEditableTextRole(
    role: String,
    subrole: String,
    valueAttributeSettable: Bool
) -> Bool {
    guard valueAttributeSettable,
          role != (kAXSecureTextFieldSubrole as String),
          subrole != (kAXSecureTextFieldSubrole as String)
    else {
        return false
    }
    return [
        kAXTextAreaRole as String,
        kAXTextFieldRole as String,
        kAXComboBoxRole as String,
    ].contains(role)
}

private func isPositivelyEditableTextElement(_ element: AXUIElement) -> Bool {
    let role = axStringAttribute(element, kAXRoleAttribute as CFString)
    let subrole = axStringAttribute(element, kAXSubroleAttribute as CFString)
    var settable = DarwinBoolean(false)
    let result = AXUIElementIsAttributeSettable(
        element,
        kAXValueAttribute as CFString,
        &settable
    )
    return result == .success && isPositivelyEditableTextRole(
        role: role,
        subrole: subrole,
        valueAttributeSettable: settable.boolValue
    )
}

func accessibilityDeduplicationTargetIdentity(
    target: JSONDict,
    ownerApplication: JSONDict?,
    context: JSONDict
) -> String {
    let contextApplication = context["app"] as? JSONDict ?? [:]
    let application = ownerApplication ?? contextApplication
    let bundleIdentifier = String(describing: application["bundleIdentifier"] ?? "")
    let processIdentifier = (application["processIdentifier"] as? NSNumber)?.intValue
        ?? application["processIdentifier"] as? Int
    guard !bundleIdentifier.isEmpty || processIdentifier != nil else { return "" }

    var identity: JSONDict = [
        "app_bundle_id": bundleIdentifier,
        "app_process_id": processIdentifier ?? 0,
        "target": stableAccessibilityTargetIdentity(target),
    ]
    let contextProcessIdentifier = (contextApplication["processIdentifier"] as? NSNumber)?.intValue
        ?? contextApplication["processIdentifier"] as? Int
    if let processIdentifier,
       contextProcessIdentifier == processIdentifier,
       let window = context["window"] as? JSONDict,
       let windowIdentifier = (window["windowID"] as? NSNumber)?.intValue
            ?? window["windowID"] as? Int
    {
        identity["window_id"] = windowIdentifier
    }
    return stableJSONSignature(identity)
}

private func accessibilityElementIdentity(_ element: AXUIElement, target: JSONDict) -> String {
    var pid: pid_t = 0
    AXUIElementGetPid(element, &pid)
    // AXUIElement is a remote proxy. Its CFHash can change when the same
    // focused control is fetched on a later poll, which makes a value mutation
    // look like two unrelated elements and silently drops the receipt. Many
    // apps derive labels and ancestor titles from editable content, so use only
    // locator fields that do not change with the observed value.
    return [
        String(pid),
        accessibilityWindowIdentity(element),
        stableAccessibilityTargetIdentity(target),
    ].joined(separator: "|")
}

private func accessibilityWindowIdentity(_ element: AXUIElement) -> String {
    guard let value = axAttribute(element, kAXWindowAttribute as CFString) else {
        return "window:unknown"
    }
    let reference = value as CFTypeRef
    guard CFGetTypeID(reference) == AXUIElementGetTypeID() else {
        return "window:unknown"
    }
    let window = reference as! AXUIElement
    let windowTarget = axHitTarget(for: window, includeValuePreview: false)?.target ?? [:]
    var identity: JSONDict = [
        "target": stableAccessibilityTargetIdentity(windowTarget),
    ]
    if let position = axPointAttribute(window, kAXPositionAttribute as CFString) {
        identity["position"] = [
            "x": Int(position.x.rounded()),
            "y": Int(position.y.rounded()),
        ]
    }
    return stableJSONSignature(identity)
}

func stableAccessibilityTargetIdentity(_ target: JSONDict) -> String {
    let ax = target["ax"] as? JSONDict ?? [:]
    let identifier = String(describing: ax["identifier"] ?? target["id"] ?? "")
        .trimmingCharacters(in: .whitespacesAndNewlines)
    var stable: JSONDict = [
        "role": ax["role"] ?? target["role"] ?? "",
        "subrole": ax["subrole"] ?? "",
        "identifier": identifier,
    ]
    if !identifier.isEmpty {
        // An explicit AX identifier is the application's locator contract.
        // Do not weaken it with mutable rich-editor ancestry.
        return stableJSONSignature(stable)
    }
    stable["placeholder"] = ax["placeholder"] ?? target["placeholder"] ?? ""
    if let ancestors = ax["ancestor_path"] as? [JSONDict] {
        stable["ancestor_path"] = ancestors.map { ancestor in
            [
                "role": ancestor["role"] ?? "",
                "identifier": ancestor["identifier"] ?? "",
            ]
        }
    }
    return stableJSONSignature(stable)
}

private func axOwnerApplication(for element: AXUIElement) -> JSONDict? {
    var pid: pid_t = 0
    guard AXUIElementGetPid(element, &pid) == .success,
          let app = NSRunningApplication(processIdentifier: pid)
    else {
        return nil
    }
    return [
        "name": app.localizedName ?? "",
        "bundleIdentifier": app.bundleIdentifier ?? "",
        "processIdentifier": Int(pid),
    ]
}

private func focusedAXElement() -> AXUIElement? {
    let system = AXUIElementCreateSystemWide()
    guard let value = axAttribute(system, kAXFocusedUIElementAttribute as CFString) else { return nil }
    guard CFGetTypeID(value as CFTypeRef) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}

private func axRangeAttribute(_ element: AXUIElement, _ attribute: CFString) -> JSONDict? {
    guard let value = axAttribute(element, attribute) else { return nil }
    guard CFGetTypeID(value as CFTypeRef) == AXValueGetTypeID() else { return nil }
    let axValue = value as! AXValue
    var range = CFRange(location: 0, length: 0)
    guard AXValueGetType(axValue) == .cfRange,
          AXValueGetValue(axValue, .cfRange, &range)
    else {
        return nil
    }
    return ["location": range.location, "length": range.length]
}

private func axPointAttribute(_ element: AXUIElement, _ attribute: CFString) -> CGPoint? {
    guard let value = axAttribute(element, attribute) else { return nil }
    guard CFGetTypeID(value as CFTypeRef) == AXValueGetTypeID() else { return nil }
    let axValue = value as! AXValue
    var point = CGPoint.zero
    guard AXValueGetType(axValue) == .cgPoint,
          AXValueGetValue(axValue, .cgPoint, &point)
    else {
        return nil
    }
    return point
}

private func selectedElementSummary(_ element: AXUIElement) -> JSONDict? {
    var summary: JSONDict = [:]
    for (key, attribute) in [
        ("role", kAXRoleAttribute),
        ("title", kAXTitleAttribute),
        ("description", kAXDescriptionAttribute),
        ("value", kAXValueAttribute),
    ] {
        if let value = axAttribute(element, attribute as CFString) {
            summary[key] = value
        }
    }
    return summary.isEmpty ? nil : summary
}

private func isTerminalBundle(_ bundleID: String) -> Bool {
    [
        "com.apple.Terminal",
        "com.googlecode.iterm2",
        "net.kovidgoyal.kitty",
        "dev.warp.Warp-Stable",
        "com.mitchellh.ghostty",
    ].contains(bundleID)
}

private func axAttribute(_ element: AXUIElement, _ attribute: CFString) -> Any? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute, &value) == .success,
          let value
    else {
        return nil
    }
    switch value {
    case let string as String:
        return string.isEmpty ? nil : string
    case let number as NSNumber:
        return number
    case let element as AXUIElement:
        return element
    case let axValue as AXValue:
        return axValue
    case let array as [Any]:
        return array.isEmpty ? nil : array
    default:
        let description = String(describing: value)
        return description.isEmpty ? nil : description
    }
}
