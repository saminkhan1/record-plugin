import Foundation
import XCTest

@testable import HermesRecorderCore

final class EventStreamRecoveryTests: XCTestCase {
    func testStartupRepairsKillWindowCountersBeforeNewWrites() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        try writeLines(
            [[
                "id": 1,
                "ts": isoTimestamp(),
                "type": "context",
                "source": "system",
                "kind": "session.started",
                "payload": [:],
                "quality": ["confidence": "high", "warnings": []],
            ]],
            to: fixture.session.appendingPathComponent("trace.jsonl")
        )
        try writeLines(
            [[
                "seq": 1,
                "ts": isoTimestamp(),
                "monotonic_ns": 1,
                "kind": "key.down",
                "payload": [:],
            ]],
            to: fixture.session.appendingPathComponent("raw/input.jsonl")
        )
        // A process death can persist one stream from a multi-stream batch
        // while a later stream and the metadata update never reach disk.
        try writeLines(
            [[
                "seq": 3,
                "ts": isoTimestamp(),
                "monotonic_ns": 3,
                "kind": "window.changed",
                "payload": [:],
            ]],
            to: fixture.session.appendingPathComponent("raw/context.jsonl")
        )

        let store = EventStreamStore(rootDirectory: fixture.root)
        try store.reconcileStartupState()

        let repaired = try readJSON(fixture.session.appendingPathComponent("metadata.json"))
        XCTAssertEqual(repaired["eventCount"] as? Int, 1)
        XCTAssertEqual(repaired["rawEventCount"] as? Int, 3)
        XCTAssertEqual(repaired["rawInputEventCount"] as? Int, 1)
        XCTAssertEqual(repaired["rawContextEventCount"] as? Int, 1)
        XCTAssertEqual(repaired["rawActiveSurfaceEventCount"] as? Int, 0)
        XCTAssertEqual(repaired["visualFrameEventCount"] as? Int, 0)

        let sequences = try store.appendRawEvents(
            fixture.session,
            events: [
                RawEventRecord(
                    stream: .context,
                    kind: "window.changed",
                    payload: [:]
                ),
            ]
        )
        XCTAssertEqual(sequences, [4])
        try store.appendTraceEvent(
            fixture.session,
            type: "context",
            source: "system",
            kind: "capture.recovered",
            payload: [:],
            quality: [:]
        )
        let traceMetadata = try readJSON(
            fixture.session.appendingPathComponent("metadata.json")
        )
        XCTAssertEqual(traceMetadata["eventCount"] as? Int, 2)
    }

    func testStartupPreservesMalformedTailAndFailsClosed() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        var trace = try RecorderJSON.encodeLine([
            "id": 1,
            "ts": isoTimestamp(),
            "type": "context",
            "source": "system",
            "kind": "session.started",
            "payload": [:],
            "quality": ["confidence": "high", "warnings": []],
        ])
        let malformedTail = Data(#"{"id":2"#.utf8)
        trace.append(malformedTail)
        let traceURL = fixture.session.appendingPathComponent("trace.jsonl")
        try trace.write(to: traceURL)

        let store = EventStreamStore(rootDirectory: fixture.root)
        XCTAssertThrowsError(try store.reconcileStartupState()) { error in
            XCTAssertTrue(String(describing: error).contains(
                "Malformed trace JSONL at line 2; refusing to discard durable evidence"
            ))
        }
        XCTAssertEqual(try Data(contentsOf: traceURL).suffix(malformedTail.count), malformedTail)
        let metadata = try readJSON(fixture.session.appendingPathComponent("metadata.json"))
        XCTAssertEqual(metadata["eventCount"] as? Int, 0)
    }

    func testStartupRejectsAmbiguousDuplicateRawSequence() throws {
        let fixture = try makeFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let row: JSONDict = [
            "seq": 1,
            "ts": isoTimestamp(),
            "monotonic_ns": 1,
            "kind": "captured",
            "payload": [:],
        ]
        try writeLines(
            [row],
            to: fixture.session.appendingPathComponent("raw/input.jsonl")
        )
        try writeLines(
            [row],
            to: fixture.session.appendingPathComponent("raw/context.jsonl")
        )

        let store = EventStreamStore(rootDirectory: fixture.root)
        XCTAssertThrowsError(try store.reconcileStartupState()) { error in
            XCTAssertTrue(String(describing: error).contains(
                "Ambiguous raw JSONL sequence 1"
            ))
        }
    }

    private func makeFixture() throws -> (root: URL, session: URL) {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("record-event-recovery-\(UUID().uuidString)", isDirectory: true)
        let session = root.appendingPathComponent("session", isDirectory: true)
        try FileManager.default.createDirectory(
            at: session.appendingPathComponent("raw", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: session.appendingPathComponent("visual", isDirectory: true),
            withIntermediateDirectories: true
        )
        for relativePath in [
            "trace.jsonl",
            "raw/input.jsonl",
            "raw/context.jsonl",
            "raw/active-surface.jsonl",
            "visual/frames.jsonl",
        ] {
            FileManager.default.createFile(
                atPath: session.appendingPathComponent(relativePath).path,
                contents: nil
            )
        }
        let metadata: JSONDict = [
            "active": true,
            "eventCount": 0,
            "rawEventCount": 0,
            "rawEventCounts": [
                "input": 0,
                "context": 0,
                "activeSurface": 0,
                "frames": 0,
            ],
            "rawInputEventCount": 0,
            "rawContextEventCount": 0,
            "rawActiveSurfaceEventCount": 0,
            "visualFrameEventCount": 0,
        ]
        try writeJSON(metadata, to: session.appendingPathComponent("metadata.json"))
        try writeJSON(
            [
                "activeSessionPath": session.path,
                "sessionID": "session",
            ],
            to: root.appendingPathComponent("service-state.json")
        )
        return (root, session)
    }

    private func writeLines(_ rows: [JSONDict], to url: URL) throws {
        var data = Data()
        for row in rows {
            data.append(try RecorderJSON.encodeLine(row))
        }
        try data.write(to: url)
    }

    private func writeJSON(_ object: JSONDict, to url: URL) throws {
        try RecorderJSON.encode(object, pretty: true).write(to: url, options: [.atomic])
    }

    private func readJSON(_ url: URL) throws -> JSONDict {
        try RecorderJSON.decodeDict(Data(contentsOf: url))
    }
}
