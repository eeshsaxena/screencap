import Foundation
import XCTest
@testable import ScreenCap

/// SCR-174 U2 — exercises the three search verbs end-to-end through the real
/// `DaemonClient` request/decode chain over a fake UNIX-socket server (reusing
/// `UnixHTTPTestServer` from DaemonClientTests). Covers framing, pointer-model
/// decode, tolerant/pessimistic enum defaulting, and the `invalid_range` typed
/// error. The SearchService protocol is driven against a fake in U4.
final class SearchServiceTests: XCTestCase {
    private var socketPath: String!
    private var server: UnixHTTPTestServer?

    override func setUp() {
        super.setUp()
        socketPath = "/tmp/sc-search-\(UUID().uuidString.prefix(8)).sock"
        setenv("SCREENCAP_DAEMON_SOCKET", socketPath, 1)
    }

    override func tearDown() {
        server?.stop()
        server = nil
        if let socketPath { unlink(socketPath) }
        unsetenv("SCREENCAP_DAEMON_SOCKET")
        super.tearDown()
    }

    private func startServer(
        _ handler: @escaping @Sendable (UnixHTTPTestServer.Request) -> UnixHTTPTestServer.Response
    ) throws -> UnixHTTPTestServer {
        let server = try UnixHTTPTestServer(socketPath: socketPath, handler: handler)
        self.server = server
        server.start()
        return server
    }

    // MARK: - content.search

    func testContentSearchFramesPOSTAndDecodesHits() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.method, "POST")
            XCTAssertEqual(request.path, "/v0/content.search")
            let body = String(data: request.body, encoding: .utf8) ?? ""
            XCTAssertTrue(body.contains(#""query":"refund""#), body)
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"hits":[{"recording":"rec-a","timestamp_ms":1719000000000,"snippet":"refund issued","score":-1.5}],"index_state":"ok","future_field":"ignored"}"#
            )
        }

        let resp = try await DaemonClient.contentSearch(ContentSearchRequest(query: "refund"))
        XCTAssertEqual(resp.hits.count, 1)
        XCTAssertEqual(resp.hits.first?.recording, "rec-a")
        XCTAssertEqual(resp.hits.first?.timestampMs, 1719000000000)
        XCTAssertEqual(resp.hits.first?.score, -1.5)
        XCTAssertEqual(resp.indexState, .ok)
    }

    func testContentSearchNotIndexedDecodes() async throws {
        _ = try startServer { _ in
            .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"hits":[],"index_state":"not_indexed"}"#)
        }
        let resp = try await DaemonClient.contentSearch(ContentSearchRequest(query: "x"))
        XCTAssertEqual(resp.indexState, .notIndexed)
    }

    func testContentSearchUnknownIndexStateDefaultsPessimistic() async throws {
        // An unknown/future state must default to storeUnavailable — never
        // silently "ok"/"no_match" — so the UI never overstates coverage.
        _ = try startServer { _ in
            .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"hits":[],"index_state":"some_future_state"}"#)
        }
        let resp = try await DaemonClient.contentSearch(ContentSearchRequest(query: "x"))
        XCTAssertEqual(resp.indexState, .storeUnavailable)
    }

    func testContentSearchMissingIndexStateDefaultsPessimistic() async throws {
        _ = try startServer { _ in
            .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"hits":[]}"#)
        }
        let resp = try await DaemonClient.contentSearch(ContentSearchRequest(query: "x"))
        XCTAssertEqual(resp.indexState, .storeUnavailable)
    }

    // MARK: - transcript.search

    func testTranscriptSearchDecodesChunkGranularHits() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.path, "/v0/transcript.search")
            return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"hits":[{"recording":"rec-b","chunk_index":4,"snippet":"the invoice"}],"coverage":"best_effort"}"#)
        }
        let resp = try await DaemonClient.transcriptSearch(TranscriptSearchRequest(query: "invoice"))
        XCTAssertEqual(resp.hits.first?.recording, "rec-b")
        XCTAssertEqual(resp.hits.first?.chunkIndex, 4)
        XCTAssertEqual(resp.coverage, .bestEffort)
    }

    // MARK: - timeline.query

    func testTimelineQueryFramesRangeAndDecodesRows() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.path, "/v0/timeline.query")
            let body = String(data: request.body, encoding: .utf8) ?? ""
            XCTAssertTrue(body.contains(#""start_ms":1000"#), body)
            XCTAssertTrue(body.contains(#""limit":200"#), body)
            return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"rows":[{"recording":"rec-c","timestamp_ms":1719000005000,"app":"Salesforce","title":null}],"coverage":"authoritative"}"#)
        }
        let resp = try await DaemonClient.timelineQuery(
            TimelineQueryRequest(startMs: 1000, endMs: 2000, app: "salesforce", limit: 200)
        )
        XCTAssertEqual(resp.rows.first?.recording, "rec-c")
        XCTAssertEqual(resp.rows.first?.app, "Salesforce")
        XCTAssertNil(resp.rows.first?.title)
        XCTAssertEqual(resp.coverage, .authoritative)
    }

    func testTimelineQueryInvalidRangeSurfacesEnvelopeError() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"invalid_range"}"#,
                status: 400,
                reason: "Bad Request"
            )
        }
        do {
            _ = try await DaemonClient.timelineQuery(
                TimelineQueryRequest(startMs: 2000, endMs: 1000, limit: 200)
            )
            XCTFail("Expected envelope error")
        } catch DaemonClientError.envelopeError(let code, _) {
            XCTAssertEqual(code, "invalid_range")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - apps.list (SCR-179)

    func testAppsListFramesGETAndDecodesVocabulary() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.path, "/v0/apps.list")
            XCTAssertEqual(request.method, "GET")
            return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"app_names":["Slack","Safari"],"app_bundles":["com.tinyspeck.slackmacgap"],"hostnames":["github.com"],"truncated":false}"#)
        }
        let resp = try await DaemonClient.appsList()
        XCTAssertEqual(resp.appNames, ["Slack", "Safari"])
        XCTAssertEqual(resp.appBundles, ["com.tinyspeck.slackmacgap"])
        XCTAssertEqual(resp.hostnames, ["github.com"])
        XCTAssertFalse(resp.truncated)
    }

    // MARK: - transport

    func testSearchSurfacesSocketUnavailableWhenDaemonDown() async {
        // No server started → socket file absent. The view model maps this to a
        // "daemon not running" state (no CLI fallback for these verbs).
        do {
            _ = try await DaemonClient.contentSearch(ContentSearchRequest(query: "x"))
            XCTFail("Expected socketUnavailable")
        } catch DaemonClientError.socketUnavailable {
            // expected
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }
}
