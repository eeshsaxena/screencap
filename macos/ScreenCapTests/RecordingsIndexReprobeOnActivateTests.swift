import AppKit
import Combine
import Darwin
import Foundation
import XCTest
@testable import ScreenCap

/// SCR-259 — the "running without the background helper" advisory
/// (`usingCLIFallback`) is sticky: once a load during the daemon-swap window
/// (e.g. right after an app update, while the old daemon is being booted off
/// `api.sock`) falls back to the CLI, nothing re-lists after the fresh daemon
/// rebinds the socket, so the banner persists for the whole session.
///
/// These pin the fix: an app re-activation (`didBecomeActive`) while in CLI
/// fallback re-probes the daemon and clears the advisory once it is reachable,
/// and does NOT re-list in the healthy path (the gate).
///
/// Drives `RecordingsIndex.refresh()` end-to-end via the same seams the
/// DaemonClient tests use: `SCREENCAP_DAEMON_SOCKET` + `UnixHTTPTestServer` for
/// the daemon path, and `FakeCLIBinary` (`SCREENCAP_CLI_PATH`) so the CLI
/// fallback succeeds with an empty archive rather than erroring.
///
/// The class stays non-`@MainActor` (like `DaemonClientTests`) so `setUp`/
/// `tearDown` can touch the socket/server fields without cross-isolation
/// warnings; the test bodies are `@MainActor` because `RecordingsIndex` is.
final class RecordingsIndexReprobeOnActivateTests: XCTestCase {
    private var socketPath: String!
    private var server: UnixHTTPTestServer?
    private var fakeCLI: FakeCLIBinary?

    private static let listEnvelope =
        #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"recordings":[]}"#

    override func setUpWithError() throws {
        try super.setUpWithError()
        socketPath = "/tmp/sc-idx-\(UUID().uuidString.prefix(8)).sock"
        setenv("SCREENCAP_DAEMON_SOCKET", socketPath, 1)
        // The CLI fallback must SUCCEED (empty archive) so the first load lands
        // in `usingCLIFallback`, not the hard-error state.
        fakeCLI = try FakeCLIBinary(stdout: "[]", exitCode: 0)
    }

    override func tearDown() {
        server?.stop()
        server = nil
        fakeCLI?.remove()
        fakeCLI = nil
        if let socketPath { unlink(socketPath) }
        unsetenv("SCREENCAP_DAEMON_SOCKET")
        super.tearDown()
    }

    /// The bug: first load falls back to the CLI (daemon unreachable), so the
    /// advisory shows; after the daemon rebinds the socket, a `didBecomeActive`
    /// must re-probe and clear it. Fails pre-fix (no observer → banner sticks).
    @MainActor
    func testDidBecomeActiveWhileInFallbackClearsAdvisoryOnceDaemonReachable() async throws {
        let center = NotificationCenter()
        let index = RecordingsIndex(autoload: false, notificationCenter: center)

        // First load: nothing is listening on the socket → daemon unreachable →
        // CLI fallback → the advisory is showing.
        await index.refresh()
        XCTAssertTrue(
            index.usingCLIFallback,
            "expected the CLI-fallback advisory after an unreachable daemon"
        )

        // The fresh daemon rebinds api.sock and starts answering recording.list.
        let server = try UnixHTTPTestServer(socketPath: socketPath) { request in
            XCTAssertEqual(request.path, "/v0/recording.list")
            return .json(Self.listEnvelope)
        }
        self.server = server
        server.start()

        let cleared = expectation(description: "advisory cleared after re-activation")
        let cancellable = index.$usingCLIFallback
            .dropFirst()
            .first(where: { $0 == false })
            .sink { _ in cleared.fulfill() }

        center.post(name: NSApplication.didBecomeActiveNotification, object: nil)

        await fulfillment(of: [cleared], timeout: 2.0)
        cancellable.cancel()
        XCTAssertFalse(index.usingCLIFallback)
    }

    /// The gate: a daemon-served first load shows no advisory, so a
    /// `didBecomeActive` must NOT trigger another list. Guards against a
    /// refresh-on-every-focus regression if the observer is left ungated.
    @MainActor
    func testDidBecomeActiveInHealthyPathDoesNotReList() async throws {
        let server = try UnixHTTPTestServer(socketPath: socketPath) { _ in
            .json(Self.listEnvelope)
        }
        self.server = server
        server.start()

        let center = NotificationCenter()
        let index = RecordingsIndex(autoload: false, notificationCenter: center)
        await index.refresh()
        XCTAssertFalse(index.usingCLIFallback, "daemon-served load must not set the advisory")
        let baseline = server.requestCount

        center.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        // Give any (erroneously scheduled) refresh time to dial the server.
        try await Task.sleep(nanoseconds: 300_000_000)

        XCTAssertEqual(
            server.requestCount, baseline,
            "healthy-path activation must not re-list recordings"
        )
    }
}
