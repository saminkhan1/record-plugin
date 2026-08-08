import AVFoundation
import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
@preconcurrency import ScreenCaptureKit

private final class SurfaceJSONBox: @unchecked Sendable {
    let value: JSONDict

    init(_ value: JSONDict) {
        self.value = value
    }
}

private final class AssetWriterBox: @unchecked Sendable {
    let value: AVAssetWriter

    init(_ value: AVAssetWriter) {
        self.value = value
    }
}

private struct SurfaceRequest: @unchecked Sendable {
    let version: UInt64
    let key: String
    let captureToken: String
    let surface: JSONDict
    let reason: String
}

private struct SurfaceCaptureResolution {
    let filter: SCContentFilter
    let evidence: JSONDict
}

enum ActiveSurfaceIdentity {
    static func key(for surface: JSONDict) -> String {
        let app = surface["app"] as? JSONDict ?? [:]
        let window = surface["window"] as? JSONDict ?? [:]
        let appKey = applicationKey(app)
        guard !appKey.isEmpty else { return "" }
        let title = String(describing: window["title"] ?? window["windowName"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let frame = window["frame"] as? JSONDict ?? [:]

        // Small AX children, menus, and tooltips are not independent drafting
        // surfaces. Collapse them to their owning application.
        guard !title.isEmpty,
              number(frame["width"]) >= 160,
              number(frame["height"]) >= 120
        else {
            return "\(appKey)|app"
        }
        return "\(appKey)|\(window["windowID"] ?? "")|\(title)"
    }

    static func applicationKey(_ app: JSONDict) -> String {
        if let pid = app["processIdentifier"] as? Int { return String(pid) }
        if let pid = app["processIdentifier"] as? NSNumber { return pid.stringValue }
        return app["bundleIdentifier"] as? String ?? ""
    }

    private static func number(_ value: Any?) -> Double {
        (value as? NSNumber)?.doubleValue ?? 0
    }
}

/// Maintains one ScreenCaptureKit stream and one MP4 for the recording. The
/// stream's inclusive application/display filter is updated in place whenever
/// the active surface changes; frames are ignored until the update completes.
final class ActiveSurfaceRecorder: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private static let videoRelativePath = "visual/active.mp4"
    private static let frameIndexIntervalSeconds = 0.2
    // The IPC client has an eight-second request timeout. Leave enough room
    // for raw-evidence flush and the service response after video finalization.
    private static let stopDeadlineSeconds = 6
    private static let streamStopGraceSeconds = 2
    private static let filterOperationTimeoutSeconds = 3

    private let store: EventStreamStore
    private let sessionDirectory: URL
    private let rawEvents: RawEventBuffer
    private let queue = DispatchQueue(label: "HermesRecorderActiveSurface")
    private let stopGroup = DispatchGroup()

    private var stream: SCStream?
    private var writer: AVAssetWriter?
    private var writerInput: AVAssetWriterInput?
    private var writerStarted = false
    private var writerSessionStartTimestamp: CMTime?
    private var outputWidth = 0
    private var outputHeight = 0

    private var currentSurfaceKey = ""
    private var currentCaptureToken = ""
    private var latestRequest: SurfaceRequest?
    private var desiredRequest: SurfaceRequest?
    private var activeRequest: SurfaceRequest?
    private var requestVersion: UInt64 = 0
    private var operationInFlight = false
    private var filterReady = false
    private var currentFilterHasFrame = false

    private var accepting = true
    private var stopping = false
    private var stopFinalizationStarted = false
    private var writerFinalizationStarted = false
    private var stopped = false
    private var terminalError: RecorderError?
    private var firstFrameTimestamp: String?
    private var lastIndexedFrameSeconds: Double?
    private var droppedFrames = 0
    private var receivedSamples = 0
    private var currentFailure: String?

    init(store: EventStreamStore, sessionDirectory: URL, rawEvents: RawEventBuffer) {
        self.store = store
        self.sessionDirectory = sessionDirectory
        self.rawEvents = rawEvents
    }

    func start(initialContext: JSONDict) {
        observe(context: initialContext, reason: "recording_started")
    }

    func observe(context: JSONDict, reason: String) {
        let context = SurfaceJSONBox(RecorderURLSanitizer.sanitizePayload(context))
        queue.async { [weak self] in
            self?.requestSurface(context: context.value, reason: reason)
        }
    }

    func stop() throws {
        queue.sync {
            guard !stopping else { return }
            stopping = true
            accepting = false
            requestVersion &+= 1
            desiredRequest = nil
            // A ScreenCaptureKit filter update can remain pending while the
            // foreground app changes. Stopping must still tear down the
            // existing stream instead of waiting for that stale callback.
            operationInFlight = false
            activeRequest = nil
            stopGroup.enter()
            finishStopIfPossible()
        }
        guard stopGroup.wait(timeout: .now() + .seconds(Self.stopDeadlineSeconds)) == .success else {
            throw RecorderError.invalidState("Timed out while finalizing active-surface video")
        }
        if let error = queue.sync(execute: { terminalError }) { throw error }
    }

    var isStopped: Bool {
        queue.sync { stopped }
    }

    private func requestSurface(context: JSONDict, reason: String) {
        guard accepting else { return }
        let surface = normalizedSurface(context)
        let surfaceKey = ActiveSurfaceIdentity.key(for: surface)
        guard !surfaceKey.isEmpty else { return }
        let displayID = displayID(for: surface)
        let token = "\(ActiveSurfaceIdentity.applicationKey(surface["app"] as? JSONDict ?? [:]))|display:\(displayID)"
        let version: UInt64
        if token == activeRequest?.captureToken {
            version = activeRequest?.version ?? requestVersion
        } else if token == desiredRequest?.captureToken {
            version = desiredRequest?.version ?? requestVersion
        } else if token == currentCaptureToken {
            version = requestVersion
        } else {
            requestVersion &+= 1
            version = requestVersion
        }
        let request = SurfaceRequest(
            version: version,
            key: "\(surfaceKey)|display:\(displayID)",
            captureToken: token,
            surface: surface,
            reason: reason
        )
        latestRequest = request
        publishSurface(request)

        guard token != currentCaptureToken || operationInFlight else { return }
        desiredRequest = request
        reconcileFilter()
    }

    private func reconcileFilter() {
        guard accepting, !operationInFlight, let request = desiredRequest else { return }
        if request.captureToken == currentCaptureToken {
            desiredRequest = nil
            return
        }
        operationInFlight = true
        activeRequest = request
        desiredRequest = nil
        filterReady = false
        currentFilterHasFrame = false
        updateVisualHealth(["ready": false])
        scheduleFilterOperationTimeout(request)

        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) { [weak self] content, error in
            guard let self else { return }
            self.queue.async {
                guard self.activeRequest?.version == request.version else { return }
                guard self.accepting, request.version == self.requestVersion else {
                    self.finishFilterOperation()
                    return
                }
                guard let content, error == nil,
                      let resolution = self.contentFilter(content: content, request: request)
                else {
                    self.markVisualFailure("shareable_content_unavailable")
                    self.finishFilterOperation()
                    return
                }
                self.updateVisualHealth(["filterResolution": resolution.evidence])
                self.apply(filter: resolution.filter, request: request, content: content)
            }
        }
    }

    private func contentFilter(content: SCShareableContent, request: SurfaceRequest) -> SurfaceCaptureResolution? {
        let app = request.surface["app"] as? JSONDict ?? [:]
        let window = matchingWindow(content: content, surface: request.surface)
        let display = matchingDisplay(content: content, surface: request.surface, window: window)
        if let applicationMatch = matchingApplication(content: content, app: app),
           let display
        {
            let application = applicationMatch.application
            return SurfaceCaptureResolution(
                filter: SCContentFilter(display: display, including: [application], exceptingWindows: []),
                evidence: [
                    "api": "ScreenCaptureKit.SCShareableContent",
                    "kind": "application",
                    "matchedBy": applicationMatch.matchedBy,
                    "application": shareableApplicationPayload(application),
                    "displayID": Int(display.displayID),
                ]
            )
        }
        guard let window else { return nil }
        return SurfaceCaptureResolution(
            filter: SCContentFilter(desktopIndependentWindow: window),
            evidence: [
                "api": "ScreenCaptureKit.SCShareableContent",
                "kind": "window",
                "matchedBy": "owning_application",
                "window": shareableWindowPayload(window),
            ]
        )
    }

    private func matchingApplication(
        content: SCShareableContent,
        app: JSONDict
    ) -> (application: SCRunningApplication, matchedBy: String)? {
        if let pid = integer(app["processIdentifier"]),
           let application = content.applications.first(where: { Int($0.processID) == pid })
        {
            return (application, "processIdentifier")
        }
        let bundleIdentifier = String(describing: app["bundleIdentifier"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !bundleIdentifier.isEmpty,
              let application = content.applications.first(where: {
                  $0.bundleIdentifier == bundleIdentifier
              })
        else {
            return nil
        }
        return (application, "bundleIdentifier")
    }

    private func matchingWindow(content: SCShareableContent, surface: JSONDict) -> SCWindow? {
        let app = surface["app"] as? JSONDict ?? [:]
        let requestedPID = integer(app["processIdentifier"])
        let requestedBundleIdentifier = String(describing: app["bundleIdentifier"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let candidates = content.windows.filter { window in
            guard let owner = window.owningApplication else { return false }
            return requestedPID.map { Int(owner.processID) == $0 } == true
                || (!requestedBundleIdentifier.isEmpty
                    && owner.bundleIdentifier == requestedBundleIdentifier)
        }
        guard !candidates.isEmpty else { return nil }
        let requestedWindowID = integer((surface["window"] as? JSONDict)?["windowID"])
        if let requestedWindowID,
           let exact = candidates.first(where: { Int($0.windowID) == requestedWindowID })
        {
            return exact
        }
        return candidates.max { windowScore($0) < windowScore($1) }
    }

    private func matchingDisplay(
        content: SCShareableContent,
        surface: JSONDict,
        window: SCWindow?
    ) -> SCDisplay? {
        if let window {
            let center = CGPoint(x: window.frame.midX, y: window.frame.midY)
            if let display = content.displays.first(where: {
                CGDisplayBounds($0.displayID).contains(center)
            }) {
                return display
            }
        }
        let requestedDisplayID = displayID(for: surface)
        return content.displays.first(where: { $0.displayID == requestedDisplayID })
            ?? content.displays.first
    }

    private func windowScore(_ window: SCWindow) -> Double {
        let area = max(0, window.frame.width) * max(0, window.frame.height)
        let activeScore: Double
        if #available(macOS 13.1, *) {
            activeScore = window.isActive ? 100_000_000_000 : 0
        } else {
            activeScore = 0
        }
        return area
            + (window.windowLayer == 0 ? 1_000_000_000 : 0)
            + (window.isOnScreen ? 10_000_000_000 : 0)
            + activeScore
    }

    private func shareableApplicationPayload(_ application: SCRunningApplication) -> JSONDict {
        [
            "name": application.applicationName,
            "bundleIdentifier": application.bundleIdentifier,
            "processIdentifier": Int(application.processID),
        ]
    }

    private func shareableWindowPayload(_ window: SCWindow) -> JSONDict {
        var payload: JSONDict = [
            "windowID": Int(window.windowID),
            "title": window.title ?? "",
            "windowLayer": window.windowLayer,
            "onScreen": window.isOnScreen,
            "frame": [
                "x": Double(window.frame.origin.x),
                "y": Double(window.frame.origin.y),
                "width": Double(window.frame.width),
                "height": Double(window.frame.height),
            ],
        ]
        if #available(macOS 13.1, *) {
            payload["active"] = window.isActive
        }
        if let owner = window.owningApplication {
            payload["application"] = shareableApplicationPayload(owner)
        }
        return payload
    }

    private func apply(filter: SCContentFilter, request: SurfaceRequest, content: SCShareableContent) {
        if let stream {
            stream.updateContentFilter(filter) { [weak self] error in
                guard let self else { return }
                let message = error?.localizedDescription
                self.queue.async { [self] in
                    completeFilterUpdate(request: request, errorMessage: message)
                }
            }
            return
        }

        guard let display = content.displays.first(where: { $0.displayID == displayID(for: request.surface) }) else {
            markVisualFailure("active_display_not_shareable")
            finishFilterOperation()
            return
        }
        if outputWidth == 0 || outputHeight == 0 {
            outputWidth = evenDimension(display.width)
            outputHeight = evenDimension(display.height)
        }
        let configuration = SCStreamConfiguration()
        configuration.width = outputWidth
        configuration.height = outputHeight
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 30)
        configuration.queueDepth = 5
        configuration.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        do {
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
            self.stream = stream
            stream.startCapture { [weak self] error in
                guard let self else { return }
                let message = error?.localizedDescription
                self.queue.async { [self] in
                    if message != nil, self.stream === stream { self.stream = nil }
                    self.completeFilterUpdate(request: request, errorMessage: message)
                }
            }
        } catch {
            markVisualFailure("stream_output_failed: \(error.localizedDescription)")
            finishFilterOperation()
        }
    }

    private func completeFilterUpdate(request: SurfaceRequest, errorMessage: String?) {
        guard activeRequest?.version == request.version else { return }
        if let errorMessage {
            markVisualFailure("stream_filter_failed: \(errorMessage)")
        } else if accepting, request.version == requestVersion {
            currentCaptureToken = request.captureToken
            filterReady = true
            currentFilterHasFrame = false
            lastIndexedFrameSeconds = nil
            currentFailure = nil
            updateVisualHealth([
                "activeSurface": request.surface,
                "error": NSNull(),
            ])
        }
        finishFilterOperation()
    }

    private func scheduleFilterOperationTimeout(_ request: SurfaceRequest) {
        queue.asyncAfter(deadline: .now() + .seconds(Self.filterOperationTimeoutSeconds)) { [weak self] in
            guard let self,
                  self.accepting,
                  self.operationInFlight,
                  self.activeRequest?.version == request.version
            else { return }
            self.markVisualFailure("filter_setup_timeout")
            self.operationInFlight = false
            self.activeRequest = nil
            guard let latest = self.latestRequest else { return }
            self.requestVersion &+= 1
            self.desiredRequest = SurfaceRequest(
                version: self.requestVersion,
                key: latest.key,
                captureToken: latest.captureToken,
                surface: latest.surface,
                reason: "filter_timeout_recovery"
            )
            self.reconcileFilter()
        }
    }

    private func finishFilterOperation() {
        operationInFlight = false
        activeRequest = nil
        if stopping {
            finishStopIfPossible()
        } else {
            reconcileFilter()
        }
    }

    private func publishSurface(_ request: SurfaceRequest) {
        guard request.key != currentSurfaceKey else { return }
        currentSurfaceKey = request.key
        var transition = request.surface
        transition["reason"] = request.reason
        rawEvents.enqueue(.activeSurface, kind: "active_surface.changed", payload: transition)
        updateVisualHealth([
            "activeSurface": transition,
            "droppedFrames": droppedFrames,
        ])
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of outputType: SCStreamOutputType) {
        guard outputType == .screen else { return }
        receivedSamples += 1
        let dataReady = CMSampleBufferDataIsReady(sampleBuffer)
        let frameStatus = screenFrameStatus(sampleBuffer)
        if receivedSamples == 1 || receivedSamples.isMultiple(of: 150) {
            updateVisualHealth([
                "receivedSampleCount": receivedSamples,
                "sampleDataReady": dataReady,
                "lastFrameStatus": frameStatus?.rawValue ?? -1,
                "streamCurrent": self.stream === stream,
                "filterReady": filterReady,
            ])
        }
        guard self.stream === stream,
              accepting,
              filterReady,
              dataReady,
              frameStatus == .complete
        else {
            return
        }
        if writer == nil, !configureWriter(sampleBuffer) {
            markVisualFailure("video_writer_unavailable")
            return
        }
        guard let writer, let writerInput else { return }
        let timestamp = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        if !writerStarted {
            guard writer.startWriting() else {
                markVisualFailure("video_writer_start_failed: \(writer.error?.localizedDescription ?? "unknown error")")
                return
            }
            writer.startSession(atSourceTime: timestamp)
            writerStarted = true
            writerSessionStartTimestamp = timestamp
        }
        guard writerInput.isReadyForMoreMediaData else {
            droppedFrames += 1
            return
        }
        guard writerInput.append(sampleBuffer) else {
            markVisualFailure("video_frame_append_failed: \(writer.error?.localizedDescription ?? "unknown error")")
            return
        }

        if !currentFilterHasFrame {
            currentFilterHasFrame = true
            let firstFrameAt = isoTimestamp()
            if firstFrameTimestamp == nil { firstFrameTimestamp = firstFrameAt }
            updateVisualHealth([
                "ready": true,
                "firstFrameAt": firstFrameTimestamp ?? firstFrameAt,
                "droppedFrames": droppedFrames,
            ])
        }
        indexFrameIfNeeded(timestamp)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        let message = error.localizedDescription
        queue.async { [weak self] in
            guard let self, self.stream === stream else { return }
            self.stream = nil
            self.filterReady = false
            self.currentFilterHasFrame = false
            self.currentCaptureToken = ""
            // A stream can terminate while updateContentFilter is in flight.
            // ScreenCaptureKit then never invokes that update callback, so the
            // old operation lease must be retired before starting recovery.
            self.operationInFlight = false
            self.activeRequest = nil
            self.markVisualFailure("stream_stopped: \(message)")
            if self.accepting, let latest = self.latestRequest {
                self.requestVersion &+= 1
                self.desiredRequest = SurfaceRequest(
                    version: self.requestVersion,
                    key: latest.key,
                    captureToken: latest.captureToken,
                    surface: latest.surface,
                    reason: "stream_recovery"
                )
                self.reconcileFilter()
            }
        }
    }

    private func configureWriter(_ sampleBuffer: CMSampleBuffer) -> Bool {
        guard let format = CMSampleBufferGetFormatDescription(sampleBuffer) else { return false }
        let dimensions = CMVideoFormatDescriptionGetDimensions(format)
        let output = sessionDirectory.appendingPathComponent(Self.videoRelativePath)
        try? FileManager.default.removeItem(at: output)
        do {
            let writer = try AVAssetWriter(outputURL: output, fileType: .mp4)
            let settings = [AVVideoCodecType.hevc, .h264]
                .map { codec in
                    [
                        AVVideoCodecKey: codec,
                        AVVideoWidthKey: Int(dimensions.width),
                        AVVideoHeightKey: Int(dimensions.height),
                    ] as [String: Any]
                }
                .first { writer.canApply(outputSettings: $0, forMediaType: .video) }
            guard let settings else { return false }
            let input = AVAssetWriterInput(
                mediaType: .video,
                outputSettings: settings,
                sourceFormatHint: format
            )
            input.expectsMediaDataInRealTime = true
            guard writer.canAdd(input) else { return false }
            writer.add(input)
            self.writer = writer
            writerInput = input
            writerSessionStartTimestamp = nil
            return true
        } catch {
            return false
        }
    }

    private func indexFrameIfNeeded(_ timestamp: CMTime) {
        guard let writerSessionStartTimestamp else { return }
        let seconds = CMTimeGetSeconds(CMTimeSubtract(timestamp, writerSessionStartTimestamp))
        guard seconds.isFinite,
              lastIndexedFrameSeconds == nil
                || seconds - (lastIndexedFrameSeconds ?? 0) >= Self.frameIndexIntervalSeconds
        else {
            return
        }
        lastIndexedFrameSeconds = seconds
        rawEvents.enqueue(.frames, kind: "visual.frame", payload: [
            "path": Self.videoRelativePath,
            "presentation_time_seconds": seconds,
        ])
    }

    private func finishStopIfPossible() {
        guard stopping, !stopFinalizationStarted, !stopped else { return }
        stopFinalizationStarted = true
        filterReady = false
        let stream = self.stream
        self.stream = nil
        guard let stream else {
            finishWriter()
            return
        }
        stream.stopCapture { [weak self] error in
            guard let self else { return }
            let message = error?.localizedDescription
            self.queue.async { [self] in
                guard !self.writerFinalizationStarted, !self.stopped else { return }
                if let message {
                    self.terminalError = RecorderError.invalidState("ScreenCaptureKit stream stop failed: \(message)")
                    self.markVisualFailure("stream_stop_failed: \(message)")
                }
                self.finishWriter()
            }
        }
        // ScreenCaptureKit occasionally withholds its stop callback after a
        // foreground-app filter transition. No more frames can be appended:
        // accepting is false and self.stream was cleared above. Finalize the
        // writer inside the IPC deadline, and make a late callback a no-op.
        queue.asyncAfter(deadline: .now() + .seconds(Self.streamStopGraceSeconds)) { [weak self] in
            guard let self, !self.writerFinalizationStarted, !self.stopped else { return }
            self.finishWriter()
        }
    }

    private func finishWriter() {
        guard !writerFinalizationStarted, !stopped else { return }
        writerFinalizationStarted = true
        updateVisualHealth(["droppedFrames": droppedFrames])
        let writer = self.writer
        let input = writerInput
        let didStart = writerStarted
        self.writer = nil
        writerInput = nil
        writerStarted = false
        writerSessionStartTimestamp = nil

        guard let writer, didStart else {
            writer?.cancelWriting()
            try? FileManager.default.removeItem(at: sessionDirectory.appendingPathComponent(Self.videoRelativePath))
            finishStoppedRecording()
            return
        }
        input?.markAsFinished()
        let writerBox = AssetWriterBox(writer)
        writer.finishWriting { [weak self] in
            guard let self else { return }
            self.queue.async { [self] in
                let writer = writerBox.value
                if writer.status != .completed {
                    let message = writer.error?.localizedDescription ?? "status \(writer.status.rawValue)"
                    self.terminalError = RecorderError.invalidState("Active-surface video finalization failed: \(message)")
                    self.markVisualFailure("video_writer_finalize_failed: \(message)")
                }
                self.finishStoppedRecording()
            }
        }
    }

    private func finishStoppedRecording() {
        rawEvents.enqueue(.activeSurface, kind: "active_surface.recording_ended", payload: [
            "reason": "recording_stopped",
        ])
        stopped = true
        stopGroup.leave()
    }

    private func markVisualFailure(_ message: String) {
        filterReady = false
        guard message != currentFailure else { return }
        currentFailure = message
        updateVisualHealth([
            "ready": false,
            "error": message,
            "droppedFrames": droppedFrames,
        ])
        try? store.appendTraceEvent(
            sessionDirectory,
            type: "evidence",
            source: "native",
            kind: "capture.gap",
            payload: ["meta": ["message_preview": message]],
            quality: ["confidence": "low", "warnings": ["active_surface_visual_unavailable"]]
        )
    }

    private func updateVisualHealth(_ updates: JSONDict) {
        try? store.updateVisualCaptureHealth(sessionDirectory, updates: updates)
    }

    private func normalizedSurface(_ context: JSONDict) -> JSONDict {
        var surface: JSONDict = ["app": context["app"] ?? [:], "window": context["window"] ?? [:]]
        if let mouse = context["mouse"] as? JSONDict,
           let origin = mouse["origin"] as? JSONDict
        {
            surface["pointer"] = origin
        }
        return surface
    }

    private func displayID(for surface: JSONDict) -> CGDirectDisplayID {
        let pointer = surface["pointer"] as? JSONDict ?? [:]
        let frame = surface["window"] as? JSONDict ?? [:]
        let bounds = frame["frame"] as? JSONDict ?? [:]
        let point: CGPoint
        if let x = number(pointer["x"]), let y = number(pointer["y"]) {
            point = CGPoint(x: x, y: y)
        } else {
            let x = number(bounds["x"]) ?? 0
            let y = number(bounds["y"]) ?? 0
            point = CGPoint(
                x: x + (number(bounds["width"]) ?? 1) / 2,
                y: y + (number(bounds["height"]) ?? 1) / 2
            )
        }
        var count: UInt32 = 0
        CGGetActiveDisplayList(0, nil, &count)
        var displays = [CGDirectDisplayID](repeating: 0, count: Int(count))
        CGGetActiveDisplayList(count, &displays, &count)
        return displays.first(where: { CGDisplayBounds($0).contains(point) }) ?? CGMainDisplayID()
    }

    private func number(_ value: Any?) -> CGFloat? {
        (value as? NSNumber).map { CGFloat($0.doubleValue) }
    }

    private func integer(_ value: Any?) -> Int? {
        if let value = value as? Int { return value }
        return (value as? NSNumber)?.intValue
    }

    private func evenDimension(_ value: Int) -> Int {
        max(2, value - value % 2)
    }

    private func screenFrameStatus(_ sampleBuffer: CMSampleBuffer) -> SCFrameStatus? {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(
            sampleBuffer,
            createIfNecessary: false
        ) as? [[SCStreamFrameInfo: Any]],
        let rawStatus = integer(attachments.first?[.status]),
        let status = SCFrameStatus(rawValue: rawStatus)
        else {
            return nil
        }
        return status
    }
}
