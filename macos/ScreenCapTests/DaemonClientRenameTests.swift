import Darwin
import Foundation
import XCTest
@testable import ScreenCap

/// Editable titles (U6) — typed Swift access to the `recording.rename` verb.
/// Reuses `UnixHTTPTestServer` (the stub daemon harness defined in
/// `DaemonClientTests.swift`), mirroring `DaemonClientBackfillTests`.
final class DaemonClientRenameTests: XCTestCase {
    private var socketPath: String!
    private var server: UnixHTTPTestServer?

    override func setUp() {
        super.setUp()
        socketPath = "/tmp/sc-rn-\(UUID().uuidString.prefix(8)).sock"
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

    // MARK: - Request framing + response decode

    func testRenameFramesPOSTWithBodyAndDecodesResponse() async throws {
        let server = try startServer { request in
            XCTAssertEqual(request.method, "POST")
            XCTAssertEqual(request.path, "/v0/recording.rename")
            XCTAssertEqual(request.headers["content-type"], "application/json")
            // The body carries the stable id + the free display title.
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: Any]
            XCTAssertEqual(body?["recording_id"] as? String, "2026-07-03_10-04-32")
            XCTAssertEqual(body?["title"] as? String, "Stripe Webhook Debugging")
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"title":"Stripe Webhook Debugging","title_is_user_set":true,"cursor":42}"#
            )
        }

        let response = try await DaemonClient.recordingRename(
            recording: "2026-07-03_10-04-32",
            title: "Stripe Webhook Debugging"
        )

        XCTAssertEqual(server.requestCount, 1)
        XCTAssertEqual(response.title, "Stripe Webhook Debugging")
        XCTAssertTrue(response.titleIsUserSet)
        XCTAssertEqual(response.cursor, 42)
    }

    /// An empty title is a valid submission — it clears the rename, and the
    /// daemon echoes the freshly-recomputed date/time default with
    /// `title_is_user_set: false`.
    func testEmptyTitleClearsToDefault() async throws {
        _ = try startServer { request in
            XCTAssertEqual(request.path, "/v0/recording.rename")
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: Any]
            XCTAssertEqual(body?["title"] as? String, "")
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"title":"3 Jul 2026 · 10:04","title_is_user_set":false,"cursor":7}"#
            )
        }

        let response = try await DaemonClient.recordingRename(recording: "rec-1", title: "")
        XCTAssertFalse(response.titleIsUserSet)
        XCTAssertEqual(response.title, "3 Jul 2026 · 10:04")
    }

    // MARK: - Error path

    /// The daemon refuses a rename of the live recording with a 409
    /// `recording_active` envelope error, surfaced as `.envelopeError`.
    func testActiveRecordingSurfacesEnvelopeError() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"recording_active","reason":"recording_active"}"#,
                status: 409,
                reason: "Conflict"
            )
        }

        do {
            _ = try await DaemonClient.recordingRename(recording: "live", title: "nope")
            XCTFail("Expected envelope error")
        } catch DaemonClientError.envelopeError(let code, _) {
            XCTAssertEqual(code, "recording_active")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
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
