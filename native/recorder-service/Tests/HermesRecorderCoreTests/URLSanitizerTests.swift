import Foundation
import XCTest

@testable import HermesRecorderCore

final class URLSanitizerTests: XCTestCase {
    func testSanitizeMatchesRecordURLCredentialContract() throws {
        for vector in try vectors() {
            XCTAssertEqual(
                RecorderURLSanitizer.sanitize(vector.input),
                vector.expected,
                vector.name
            )
        }
    }

    func testEventStreamStoreSanitizesTracePayloadBeforePersistence() throws {
        let fixture = try makeSessionFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let store = EventStreamStore(rootDirectory: fixture.root)
        let credentialVector = try vector(named: "credential_query_and_oauth_route_fragment")
        let signedReferrer = try vector(named: "signed_url_signatures_are_redacted")

        try store.appendTraceEvent(
            fixture.session,
            type: "context",
            source: "native",
            kind: "window.changed",
            payload: [
                "window": ["url": credentialVector.input],
                "browser": [
                    "url": credentialVector.input,
                    "top_url": credentialVector.input,
                ],
                "effect": ["download": ["referrer": [signedReferrer.input]]],
            ],
            quality: ["confidence": "medium", "warnings": []]
        )

        let row = try firstJSONLine(at: fixture.session.appendingPathComponent("trace.jsonl"))
        let payload = try XCTUnwrap(row["payload"] as? JSONDict)
        let window = try XCTUnwrap(payload["window"] as? JSONDict)
        let browser = try XCTUnwrap(payload["browser"] as? JSONDict)
        let effect = try XCTUnwrap(payload["effect"] as? JSONDict)
        let download = try XCTUnwrap(effect["download"] as? JSONDict)
        XCTAssertEqual(window["url"] as? String, credentialVector.expected)
        XCTAssertEqual(browser["url"] as? String, credentialVector.expected)
        XCTAssertEqual(browser["top_url"] as? String, credentialVector.expected)
        XCTAssertEqual(download["referrer"] as? [String], [signedReferrer.expected])
        let encoded = try XCTUnwrap(String(data: RecorderJSON.encode(row), encoding: .utf8))
        XCTAssertFalse(encoded.contains("opaque"))
        XCTAssertFalse(encoded.contains("aws-secret"))
    }

    func testEventTapRawContextQueueSanitizesBeforePersistence() throws {
        let fixture = try makeSessionFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let store = EventStreamStore(rootDirectory: fixture.root)
        let buffer = RawEventBuffer(store: store, sessionDirectory: fixture.session)
        let vector = try vector(named: "credential_query_and_oauth_route_fragment")

        buffer.enqueue(
            .context,
            kind: "window.changed",
            payload: [
                "window": ["url": vector.input],
                "browser": ["url": vector.input, "top_url": vector.input],
            ]
        )
        try buffer.stop()

        let row = try firstJSONLine(at: fixture.session.appendingPathComponent("raw/context.jsonl"))
        let payload = try XCTUnwrap(row["payload"] as? JSONDict)
        let window = try XCTUnwrap(payload["window"] as? JSONDict)
        let browser = try XCTUnwrap(payload["browser"] as? JSONDict)
        XCTAssertEqual(window["url"] as? String, vector.expected)
        XCTAssertEqual(browser["url"] as? String, vector.expected)
        XCTAssertEqual(browser["top_url"] as? String, vector.expected)
        let encoded = try XCTUnwrap(String(data: RecorderJSON.encode(row), encoding: .utf8))
        XCTAssertFalse(encoded.contains("fragment-secret"))
    }

    func testEventStreamStoreSanitizesVisualHealthStateBeforePersistence() throws {
        let fixture = try makeSessionFixture()
        defer { try? FileManager.default.removeItem(at: fixture.root) }
        let store = EventStreamStore(rootDirectory: fixture.root)
        let vector = try vector(named: "credential_query_and_oauth_route_fragment")

        try store.updateVisualCaptureHealth(
            fixture.session,
            updates: [
                "activeSurface": [
                    "window": ["url": vector.input],
                    "redirectUrl": [vector.input],
                ],
            ]
        )

        for name in ["metadata.json", "capture-state.json"] {
            let object = try jsonObject(at: fixture.session.appendingPathComponent(name))
            let health = try XCTUnwrap(object["visualCaptureHealth"] as? JSONDict)
            let surface = try XCTUnwrap(health["activeSurface"] as? JSONDict)
            let window = try XCTUnwrap(surface["window"] as? JSONDict)
            XCTAssertEqual(window["url"] as? String, vector.expected)
            XCTAssertEqual(surface["redirectUrl"] as? [String], [vector.expected])
            let encoded = try XCTUnwrap(
                String(data: RecorderJSON.encode(object), encoding: .utf8)
            )
            XCTAssertFalse(encoded.contains("fragment-secret"), name)
        }
    }

    private func makeSessionFixture() throws -> (root: URL, session: URL) {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("record-url-sanitizer-\(UUID().uuidString)", isDirectory: true)
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
            "rawEventCounts": ["input": 0, "context": 0, "activeSurface": 0, "frames": 0],
            "rawInputEventCount": 0,
            "rawContextEventCount": 0,
            "rawActiveSurfaceEventCount": 0,
            "visualFrameEventCount": 0,
        ]
        try RecorderJSON.encode(metadata, pretty: true)
            .write(to: session.appendingPathComponent("metadata.json"), options: [.atomic])
        return (root, session)
    }

    private func firstJSONLine(at url: URL) throws -> JSONDict {
        let data = try Data(contentsOf: url)
        let line = try XCTUnwrap(data.split(separator: 0x0a).first)
        return try RecorderJSON.decodeDict(Data(line))
    }

    private func jsonObject(at url: URL) throws -> JSONDict {
        try RecorderJSON.decodeDict(Data(contentsOf: url))
    }

    private func vector(named name: String) throws -> URLVector {
        try XCTUnwrap(vectors().first { $0.name == name })
    }

    private func vectors() throws -> [URLVector] {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let repositoryRoot =
            packageRoot
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let fixtureURL =
            repositoryRoot
            .appendingPathComponent("tests/fixtures/url_sanitization_vectors.json")
        let fixture = try JSONDecoder().decode(
            URLVectorFixture.self,
            from: Data(contentsOf: fixtureURL)
        )
        XCTAssertEqual(fixture.version, 1)
        return fixture.vectors
    }
}

private struct URLVectorFixture: Decodable {
    let version: Int
    let vectors: [URLVector]
}

private struct URLVector: Decodable {
    let name: String
    let input: String
    let expected: String
}
