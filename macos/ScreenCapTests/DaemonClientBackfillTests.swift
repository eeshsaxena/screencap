import Darwin
import Foundation
import XCTest
@testable import ScreenCap

/// SCR-178 (U7) — typed Swift access to the three content-index backfill verbs
/// and the `backfill.*` progress event stream. Reuses `UnixHTTPTestServer` (the
/// stub daemon harness defined in `DaemonClientTests.swift`).
final class DaemonClientBackfillTests: XCTestCase {
    private var socketPath: String!
    private var server: UnixHTTPTestServer?

    override func setUp() {
        super.setUp()
        socketPath = "/tmp/sc-bf-\(UUID().uuidString.prefix(8)).sock"
        setenv("SCREENCAP_DAEMON_SOCKET", socketPath, 1)
    }

    override func tearDown() {
        server?.stop()
        server = nil
        if let socketPath {
            unlink(socketPath)
        }
        unsetenv("SCREENCAP_DAEMON_SOCKET")
        super.tearDown()
    }

    // MARK: - Status / start / cancel envelope decode

    func testBackfillStartFramesPOSTWithEmptyBodyAndDecodesStatus() async throws {
        let server = try startServer { request in
            XCTAssertEqual(request.method, "POST")
            XCTAssertEqual(request.path, "/v0/backfill.start")
            XCTAssertEqual(request.headers["content-type"], "application/json")
            // The verb carries no parameters — an empty JSON object body.
            let body = String(data: request.body, encoding: .utf8) ?? ""
            XCTAssertEqual(body, "{}")
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"state":"running","done":0,"skipped":0,"failed":0,"total":12,"current_unit_index":0}"#
            )
        }

        let response = try await DaemonClient.backfillStart()

        XCTAssertEqual(server.requestCount, 1)
        XCTAssertEqual(response.ok, true)
        XCTAssertEqual(response.apiSchemaVersion, 1)
        XCTAssertEqual(response.status.state, .running)
        XCTAssertEqual(response.status.total, 12)
        XCTAssertEqual(response.status.currentUnitIndex, 0)
    }

    func testBackfillStatusFramesGETAndDecodesAllSixStates() async throws {
        // Pin every documented state decodes, with `paused` called out by the
        // plan as the high-value resumable case.
        let states: [(String, BackfillState)] = [
            ("idle", .idle),
            ("running", .running),
            ("completed", .completed),
            ("paused", .paused),
            ("cancelled", .cancelled),
            ("failed", .failed),
        ]

        for (wire, expected) in states {
            let server = try startServer { request in
                XCTAssertEqual(request.method, "GET")
                XCTAssertEqual(request.path, "/v0/backfill.status")
                return .json(
                    """
                    {"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"state":"\(wire)","done":7,"skipped":2,"failed":1,"total":12,"current_unit_index":9}
                    """
                )
            }

            let response = try await DaemonClient.backfillStatus()
            XCTAssertEqual(response.status.state, expected, "wire=\(wire)")
            XCTAssertEqual(response.status.stateRaw, wire)
            XCTAssertEqual(response.status.done, 7)
            XCTAssertEqual(response.status.skipped, 2)
            XCTAssertEqual(response.status.failed, 1)
            XCTAssertEqual(response.status.total, 12)
            XCTAssertEqual(response.status.currentUnitIndex, 9)

            server.stop()
            self.server = nil
        }
    }

    func testBackfillStatusUnknownStateDecodesTolerantlyToIdle() async throws {
        // A future run-state token a newer daemon emits must not brick the
        // parser; it decodes to the neutral `.idle` (never a misleading
        // running/failed), and the raw token stays observable.
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"state":"draining","done":1,"skipped":0,"failed":0,"total":3,"current_unit_index":1}"#
            )
        }

        let response = try await DaemonClient.backfillStatus()
        XCTAssertEqual(response.status.state, .idle)
        XCTAssertEqual(response.status.stateRaw, "draining")
    }

    func testBackfillCancelFramesPOSTAndDecodesCancelledStatus() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.method, "POST")
            XCTAssertEqual(request.path, "/v0/backfill.cancel")
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"state":"cancelled","done":4,"skipped":1,"failed":0,"total":12,"current_unit_index":5}"#
            )
        }

        let response = try await DaemonClient.backfillCancel()
        XCTAssertEqual(response.status.state, .cancelled)
        XCTAssertEqual(response.status.done, 4)
    }

    // MARK: - Progress event decode (from the subscribe stream)

    func testBackfillProgressEventDecodesAllFields() throws {
        let line = #"{"type":"backfill.progress","state":"running","done":5,"skipped":2,"failed":1,"total":20,"current_unit_index":8}"#
        let event = try XCTUnwrap(DaemonClient.backfillProgressEvent(fromLine: line))

        XCTAssertEqual(event.type, "backfill.progress")
        XCTAssertEqual(event.state, .running)
        XCTAssertEqual(event.done, 5)
        XCTAssertEqual(event.skipped, 2)
        XCTAssertEqual(event.failed, 1)
        XCTAssertEqual(event.total, 20)
        XCTAssertEqual(event.currentUnitIndex, 8)
        XCTAssertFalse(event.isTerminal)
    }

    func testBackfillTerminalEventsAreFlaggedTerminal() {
        for type in [
            "backfill.completed", "backfill.paused", "backfill.cancelled", "backfill.failed",
        ] {
            let line = """
                {"type":"\(type)","state":"completed","done":20,"skipped":0,"failed":0,"total":20,"current_unit_index":20}
                """
            let event = DaemonClient.backfillProgressEvent(fromLine: line)
            XCTAssertEqual(event?.type, type)
            XCTAssertEqual(event?.isTerminal, true, "type=\(type)")
        }
    }

    func testNonBackfillEventLineIsIgnored() {
        // A recorder/lifecycle event on the same stream (e.g. `chunk_finalized`,
        // `subscribed`, `_close`) is not a backfill event → nil, not a crash.
        XCTAssertNil(DaemonClient.backfillProgressEvent(
            fromLine: #"{"type":"chunk_finalized","schema_version":1,"cursor":9,"ts":3.0}"#
        ))
        XCTAssertNil(DaemonClient.backfillProgressEvent(
            fromLine: #"{"type":"_close","reason":"shutdown","ts":4.0}"#
        ))
    }

    func testMalformedEventLineIsIgnoredNotFatal() {
        // A `backfill.`-prefixed line missing required count fields fails decode
        // and is dropped — never throws.
        XCTAssertNil(DaemonClient.backfillProgressEvent(
            fromLine: #"{"type":"backfill.progress","state":"running"}"#
        ))
        // Garbage / non-JSON.
        XCTAssertNil(DaemonClient.backfillProgressEvent(fromLine: "}{ not json"))
        XCTAssertNil(DaemonClient.backfillProgressEvent(fromLine: ""))
    }

    // MARK: - Error path

    func testBackfillErrorEnvelopeSurfacesDaemonClientError() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"backfill_unavailable","reason":"indexing disabled"}"#,
                status: 409,
                reason: "Conflict"
            )
        }

        do {
            _ = try await DaemonClient.backfillStart()
            XCTFail("Expected envelope error")
        } catch DaemonClientError.envelopeError(let code, _) {
            XCTAssertEqual(code, "backfill_unavailable")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - Privacy (R9): no recording-name field

    func testBackfillStatusExposesOnlySafeFieldsEvenWithExtraKey() async throws {
        // A hypothetical (contract-forbidden) recording-name key on the wire must
        // not surface through the model — the struct simply has no such property,
        // and the extra key is ignored, leaving only the count-only/opaque-ordinal
        // surface (R9). This is the structural privacy guard.
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"state":"running","done":3,"skipped":0,"failed":0,"total":10,"current_unit_index":3,"recording":"2026-06-27-secret-meeting"}"#
            )
        }

        let response = try await DaemonClient.backfillStatus()
        // Decodes cleanly, exposing only the safe fields.
        XCTAssertEqual(response.status.state, .running)
        XCTAssertEqual(response.status.done, 3)
        XCTAssertEqual(response.status.total, 10)

        // Mirror.children proves no property holds the leaked dir name.
        let leaked = Mirror(reflecting: response.status).children.contains { _, value in
            (value as? String)?.contains("secret-meeting") == true
        }
        XCTAssertFalse(leaked, "BackfillStatus must not expose a recording-name field")
    }

    func testBackfillProgressEventExposesNoRecordingName() {
        let line = #"{"type":"backfill.progress","state":"running","done":1,"skipped":0,"failed":0,"total":4,"current_unit_index":1,"recording":"2026-06-27-secret-meeting"}"#
        guard let event = DaemonClient.backfillProgressEvent(fromLine: line) else {
            return XCTFail("expected a decoded backfill progress event")
        }
        let leaked = Mirror(reflecting: event).children.contains { _, value in
            (value as? String)?.contains("secret-meeting") == true
        }
        XCTAssertFalse(leaked, "BackfillProgressEvent must not expose a recording-name field")
    }

    @discardableResult
    private func startServer(
        _ handler: @escaping @Sendable (UnixHTTPTestServer.Request) -> UnixHTTPTestServer.Response
    ) throws -> UnixHTTPTestServer {
        let server = try UnixHTTPTestServer(socketPath: socketPath, handler: handler)
        self.server = server
        server.start()
        return server
    }
}
