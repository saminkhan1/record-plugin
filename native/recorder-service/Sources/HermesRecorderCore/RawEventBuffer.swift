import Dispatch
import Foundation

enum RawEventStream: Sendable {
    case input
    case context
    case activeSurface
    case frames
}

struct RawEventRecord: @unchecked Sendable {
    let stream: RawEventStream
    let kind: String
    let payload: JSONDict
    let timestamp: String
    let monotonicNanoseconds: UInt64

    init(
        stream: RawEventStream,
        kind: String,
        payload: JSONDict,
        timestamp: String = isoTimestamp(),
        monotonicNanoseconds: UInt64 = DispatchTime.now().uptimeNanoseconds
    ) {
        self.stream = stream
        self.kind = kind
        self.payload = payload
        self.timestamp = timestamp
        self.monotonicNanoseconds = monotonicNanoseconds
    }
}

/// A single ordered write-behind buffer for immutable recorder evidence.
final class RawEventBuffer: @unchecked Sendable {
    private let store: EventStreamStore
    private let sessionDirectory: URL
    private let queue = DispatchQueue(label: "HermesRecorderRawEvidenceWriter", qos: .utility)
    private var pending: [RawEventRecord] = []
    private var flushScheduled = false
    private var accepting = true
    private var lastError: Error?
    private let maximumBatchSize = 128
    private let maximumBatchDelay: DispatchTimeInterval = .milliseconds(100)
    private let retryDelay: DispatchTimeInterval = .milliseconds(250)
    private let maximumStopFlushAttempts = 3

    init(store: EventStreamStore, sessionDirectory: URL) {
        self.store = store
        self.sessionDirectory = sessionDirectory
    }

    func enqueue(
        _ stream: RawEventStream,
        kind: String,
        payload: JSONDict = [:],
        monotonicNanoseconds: UInt64 = DispatchTime.now().uptimeNanoseconds
    ) {
        // RawEventRecord is an immutable ownership transfer. Callers build a
        // fresh payload and do not mutate it after enqueueing.
        let event = RawEventRecord(
            stream: stream,
            kind: kind,
            payload: payload,
            timestamp: "",
            monotonicNanoseconds: monotonicNanoseconds
        )
        let capturedAt = Date()
        queue.async { [weak self] in
            guard let self, self.accepting else { return }
            self.pending.append(RawEventRecord(
                stream: event.stream,
                kind: event.kind,
                payload: event.payload,
                timestamp: isoTimestamp(capturedAt),
                monotonicNanoseconds: event.monotonicNanoseconds
            ))
            if self.pending.count >= self.maximumBatchSize {
                self.flush()
            } else {
                self.scheduleFlush(after: self.maximumBatchDelay)
            }
        }
    }

    func stop() throws {
        var error: Error?
        queue.sync {
            accepting = false
            var attempts = 0
            while !pending.isEmpty, attempts < maximumStopFlushAttempts {
                attempts += 1
                flush(scheduleRetryOnFailure: false)
            }
            if !pending.isEmpty {
                // Preserve pending rows for an explicit stop retry. Once
                // accepting is false, no delayed writer may outlive the
                // terminal stop transaction.
                error = lastError
            }
        }
        if let error { throw error }
    }

    private func flush(scheduleRetryOnFailure: Bool = true) {
        flushScheduled = false
        guard !pending.isEmpty else { return }
        guard accepting || !scheduleRetryOnFailure else { return }
        let batch = pending
        do {
            try store.appendRawEvents(sessionDirectory, events: batch)
            pending.removeFirst(batch.count)
            lastError = nil
        } catch {
            lastError = error
            if scheduleRetryOnFailure {
                scheduleFlush(after: retryDelay)
            }
        }
    }

    private func scheduleFlush(after delay: DispatchTimeInterval) {
        guard accepting, !flushScheduled, !pending.isEmpty else { return }
        flushScheduled = true
        queue.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.flush()
        }
    }
}
