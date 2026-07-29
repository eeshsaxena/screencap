import Darwin
import Foundation
import XCTest
@testable import Screencap

/// Share-by-link (SCR-299 U2) — typed Swift access to the `recording.share`
/// verb. Reuses `UnixHTTPTestServer` (the stub daemon harness defined in
/// `DaemonClientTests.swift`), mirroring `DaemonClientRenameTests`.
///
/// The verb backs three ops with ONE response model whose unused fields are
/// null, so the decode tests below deliberately assert both what each op
/// populates and what it leaves absent — a decoder that required create's
/// fields would break revoke and list.
final class DaemonClientShareTests: XCTestCase {
    private var socketPath: String!
    private var server: UnixHTTPTestServer?

    override func setUp() {
        super.setUp()
        socketPath = "/tmp/sc-sh-\(UUID().uuidString.prefix(8)).sock"
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

    // MARK: - Create

    func testShareCreateFramesPOSTWithBodyAndDecodesLink() async throws {
        let server = try startServer { request in
            XCTAssertEqual(request.method, "POST")
            XCTAssertEqual(request.path, "/v0/recording.share")
            XCTAssertEqual(request.headers["content-type"], "application/json")
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: Any]
            XCTAssertEqual(body?["op"] as? String, "create")
            XCTAssertEqual(body?["recording_id"] as? String, "2026-07-03_10-04-32")
            XCTAssertEqual(body?["expires_days"] as? Int, 7)
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"url":"https://screencap.sh/share/tok9#Zm9va2V5","token":"tok9","expires_at":"2026-08-28"}"#
            )
        }

        let response = try await DaemonClient.shareCreate(
            recording: "2026-07-03_10-04-32",
            expiresDays: 7
        )

        XCTAssertEqual(server.requestCount, 1)
        XCTAssertEqual(response.url, "https://screencap.sh/share/tok9#Zm9va2V5")
        XCTAssertEqual(response.token, "tok9")
        XCTAssertEqual(response.expiresAt, "2026-08-28")
        // Revoke/list fields stay absent rather than decoding to a default.
        XCTAssertNil(response.revoked)
        XCTAssertNil(response.shares)
    }

    /// With no override the key is omitted entirely so the daemon applies its
    /// configured default expiry — sending an explicit null would be a
    /// different statement.
    func testShareCreateOmitsExpiresDaysWhenNotSpecified() async throws {
        _ = try startServer { request in
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: Any]
            XCTAssertNil(body?["expires_days"])
            XCTAssertNil(body?["token"])
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"url":"https://screencap.sh/share/t#k","token":"t","expires_at":"2026-08-28"}"#
            )
        }

        let response = try await DaemonClient.shareCreate(recording: "rec-1")
        XCTAssertEqual(response.token, "t")
    }

    // MARK: - Revoke

    func testShareRevokeDecodesWithoutCreateFields() async throws {
        _ = try startServer { request in
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: Any]
            XCTAssertEqual(body?["op"] as? String, "revoke")
            XCTAssertEqual(body?["token"] as? String, "tok9")
            XCTAssertNil(body?["recording_id"])
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"revoked":true,"token":"tok9"}"#
            )
        }

        let response = try await DaemonClient.shareRevoke(token: "tok9")
        XCTAssertEqual(response.revoked, true)
        XCTAssertEqual(response.token, "tok9")
        XCTAssertNil(response.url)
    }

    // MARK: - List

    func testShareListDecodesEmptyArray() async throws {
        _ = try startServer { request in
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: Any]
            XCTAssertEqual(body?["op"] as? String, "list")
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"shares":[]}"#
            )
        }

        let response = try await DaemonClient.shareList()
        XCTAssertEqual(response.shares, [])
    }

    /// `revoked` is written only once a share has been revoked, so a live
    /// record simply lacks the key — it must decode as absent, not fail.
    func testShareListDecodesRecordsWithAndWithoutRevokedFlag() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"shares":[{"token":"a","recording":"rec-1","expires_at":"2026-08-28"},{"token":"b","recording":"rec-2","expires_at":"2026-08-01","revoked":true}]}"#
            )
        }

        let shares = try await DaemonClient.shareList().shares
        XCTAssertEqual(shares?.count, 2)
        XCTAssertEqual(shares?.first?.token, "a")
        XCTAssertEqual(shares?.first?.recording, "rec-1")
        XCTAssertNil(shares?.first?.revoked)
        XCTAssertEqual(shares?.last?.revoked, true)
    }

    // MARK: - Failure paths

    /// U1's typed codes reach the caller verbatim so the copy layer can key on
    /// them. If this collapsed to a message string the app would be back to one
    /// undifferentiated failure.
    func testSignedOutSurfacesTypedEnvelopeCode() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"not_signed_in"}"#,
                status: 403,
                reason: "Forbidden"
            )
        }

        do {
            _ = try await DaemonClient.shareCreate(recording: "rec-1")
            XCTFail("Expected envelope error")
        } catch DaemonClientError.envelopeError(let code, _) {
            XCTAssertEqual(code, "not_signed_in")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testBackendUnavailableSurfacesTypedEnvelopeCode() async throws {
        _ = try startServer { _ in
            .json(
                #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"share_backend_unavailable"}"#,
                status: 503,
                reason: "Service Unavailable"
            )
        }

        do {
            _ = try await DaemonClient.shareRevoke(token: "tok9")
            XCTFail("Expected envelope error")
        } catch DaemonClientError.envelopeError(let code, _) {
            XCTAssertEqual(code, "share_backend_unavailable")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    /// A missing daemon is a transport failure, not a decode failure — the
    /// copy layer renders a different message for it.
    func testMissingSocketSurfacesTransportError() async throws {
        setenv("SCREENCAP_DAEMON_SOCKET", "/tmp/sc-sh-definitely-absent.sock", 1)
        do {
            _ = try await DaemonClient.shareList()
            XCTFail("Expected transport error")
        } catch DaemonClientError.socketUnavailable {
            // expected
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - Timeout

    /// Create fetches the masked cloud copy, re-encrypts, and uploads inside the
    /// verb, so it must NOT inherit the client's short default ceiling. Proven
    /// by outcome — a response deliberately slower than that default still
    /// succeeds — rather than by asserting the constant, which would pass even
    /// if the call site forgot to pass it.
    func testShareCreateOutlastsTheClientDefaultTimeout() async throws {
        _ = try startServer { _ in
            Thread.sleep(forTimeInterval: 11.0)
            return .json(
                #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"url":"https://screencap.sh/share/slow#k","token":"slow","expires_at":"2026-08-28"}"#
            )
        }

        let response = try await DaemonClient.shareCreate(recording: "big-recording")
        XCTAssertEqual(response.token, "slow")
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
