import XCTest
@testable import ScreenCap

@MainActor
final class RecorderControllerDaemonTests: XCTestCase {
    private var socketPath: String!
    private var server: UnixHTTPTestServer?
    /// Holds the controller under test so tearDown can cancel its
    /// daemon event Task before stopping the test server. Without this,
    /// an in-flight NWConnection can outlive `server.stop()` and stall
    /// in CI as Network.framework retries against the removed socket.
    private var recorder: RecorderController?

    override func setUp() {
        super.setUp()
        socketPath = "/tmp/sc-rc-\(UUID().uuidString.prefix(8)).sock"
        setenv("SCREENCAP_DAEMON_SOCKET", socketPath, 1)
    }

    override func tearDown() {
        recorder?._testCancelDaemonTask()
        recorder = nil
        server?.stop()
        server = nil
        if let socketPath {
            unlink(socketPath)
        }
        unsetenv("SCREENCAP_DAEMON_SOCKET")
        super.tearDown()
    }

    func testDaemonTransportHappyPathUsesEventStreamForStateTransitions() async throws {
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-1","started_at":10.0,"engine_pid":9991,"cursor":2}"#)
            case "/v0/session.snapshot":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"demo","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":3}"#)
            // First subscribe keys off recording.start.cursor (2), not the
            // snapshot's cursor (3); replay-buffered `started` at cursor 3
            // is the boundary event we depend on.
            case "/v0/events?since=2":
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":2,"ts":11.0}"# + "\n",
                    #"{"type":"started","schema_version":1,"cursor":3,"ts":12.0}"# + "\n",
                ])
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        XCTAssertEqual(recorder.transport, .daemon)

        recorder.start(name: "demo")

        await waitUntil {
            if case .recording = recorder.state { return true }
            return false
        }
        XCTAssertNil(recorder.lastError)
    }

    func testDaemonTransportReconnectsAfterDroppedEventStream() async throws {
        let eventConnectionCount = LockedInt()
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-2","started_at":10.0,"engine_pid":9992,"cursor":4}"#)
            case "/v0/session.snapshot":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"demo","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":5}"#)
            // First subscribe uses the recording.start cursor (4); the
            // reconnect after the dropped stream falls back to the
            // snapshot cursor (5).
            case "/v0/events?since=4":
                _ = eventConnectionCount.incrementAndGet()
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":4,"ts":11.0}"# + "\n",
                ], terminate: false)
            case "/v0/events?since=5":
                _ = eventConnectionCount.incrementAndGet()
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":5,"ts":12.0}"# + "\n",
                    #"{"type":"started","schema_version":1,"cursor":6,"ts":13.0}"# + "\n",
                ])
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        recorder.start(name: "demo")

        await waitUntil {
            if case .recording = recorder.state { return true }
            return false
        }
        XCTAssertGreaterThanOrEqual(eventConnectionCount.value, 2)
    }

    func testSchemaMismatchFromProbeSetsPublishedFlagAndFallsBackToCLI() async throws {
        _ = try startServer { _ in
            .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":99,"build":null,"started_at":1.0}"#)
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()

        XCTAssertEqual(recorder.transport, .cliFallback)
        XCTAssertTrue(recorder.schemaMismatchDetected)
    }

    // MARK: - U4: production ordering — `started` already published before subscribe

    /// Mirrors the production daemon timing where `recording_started` is
    /// emitted *before* the SwiftUI controller issues its
    /// `/v0/events?since=N` subscribe. The mock server keeps a preset replay
    /// of events keyed by cursor; the events route emits every entry with
    /// `cursor > since` ahead of any live event, exactly as the daemon's
    /// U1+U2 replay buffer does on the production side.
    ///
    /// Before U1+U2 the daemon dropped `recording_started` when the
    /// SwiftUI subscribe arrived after the event was published; this test
    /// confirms that the controller transitions through `.starting` →
    /// `.recording` using only the replay-delivered event.
    func testProductionOrderingStartedBeforeSubscribe() async throws {
        // Preset replay ring: events keyed by cursor that the server emits
        // verbatim when SwiftUI subscribes at `?since=N`. The recording
        // helper exhausts the ring then keeps the stream open so the
        // RecorderController stays attached.
        let presetReplay = PresetReplay(events: [
            4: #"{"type":"started","schema_version":1,"cursor":4,"ts":12.0,"claimant":"daemon"}"# + "\n",
        ])

        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                // Start response carries cursor=3; the engine has already
                // published `started` at cursor=4 by the time this returns.
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-prod","started_at":10.0,"engine_pid":9993,"cursor":3}"#)
            case "/v0/session.snapshot":
                // Snapshot cursor (5) is strictly greater than the start
                // cursor (3); subscribing at snapshot would miss the
                // `recording_started` event published at cursor=4. The
                // controller must key the initial subscribe to the start
                // cursor so the replay buffer surfaces it.
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"prod","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":5}"#)
            case "/v0/events?since=3":
                // The "subscribed" frame mirrors the production wire shape;
                // replayed events with cursor > 3 follow before any live
                // event lands. With the preset cursor=4, the controller
                // observes `started` purely from replay.
                var chunks = [
                    #"{"type":"subscribed","schema_version":1,"cursor":3,"ts":11.0}"# + "\n",
                ]
                chunks.append(contentsOf: presetReplay.eventsAfter(cursor: 3))
                return .chunked(chunks, terminate: false)
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        XCTAssertEqual(recorder.transport, .daemon)

        recorder.start(name: "prod")

        await waitUntil {
            if case .recording = recorder.state { return true }
            return false
        }
        XCTAssertNil(recorder.lastError)
    }

    /// When the daemon side has aged the requested cursor out of its
    /// replay window, `/v0/events?since=N` returns HTTP 410 with the
    /// `cursor_unknown` envelope. The RecorderController must treat
    /// this as a recoverable stream failure (retry via fresh snapshot)
    /// rather than crashing or hanging in `.starting`.
    func testCursorUnknown410FromEventsStreamFallsBackThroughSnapshotRefetch() async throws {
        let snapshotCount = LockedInt()

        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            // First subscribe keys off this cursor (2) — it has aged
            // out of the daemon's replay window, surfaced as 410
            // cursor_unknown below.
            case "/v0/recording.start":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-unknown","started_at":10.0,"engine_pid":9994,"cursor":2}"#)
            case "/v0/session.snapshot":
                // First snapshot returns a stale cursor; second snapshot
                // (after the controller recovers from the 410) returns a
                // fresh one that the replay window covers.
                if snapshotCount.incrementAndGet() == 1 {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"unknown","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":1}"#)
                }
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"unknown","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":5}"#)
            case "/v0/events?since=2":
                return .json(
                    #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"cursor_unknown","requested_cursor":2}"#,
                    status: 410,
                    reason: "Gone"
                )
            case "/v0/events?since=5":
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":5,"ts":11.0}"# + "\n",
                    #"{"type":"started","schema_version":1,"cursor":6,"ts":12.0,"claimant":"daemon"}"# + "\n",
                ], terminate: false)
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        recorder.start(name: "unknown")

        await waitUntil {
            if case .recording = recorder.state { return true }
            return false
        }
        XCTAssertGreaterThanOrEqual(snapshotCount.value, 2)
        XCTAssertNil(recorder.lastError)
    }

    /// A `recording_failed` event delivered via replay must propagate to
    /// the controller so the UI surfaces the failure to the user instead
    /// of remaining stuck in `.starting` while the daemon has already
    /// torn the recording down.
    func testRecordingFailedDeliveredViaReplaySurfacesError() async throws {
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-fail","started_at":10.0,"engine_pid":9995,"cursor":2}"#)
            case "/v0/session.snapshot":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"fail","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":2}"#)
            case "/v0/events?since=2":
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":2,"ts":11.0}"# + "\n",
                    #"{"type":"recording_failed","schema_version":1,"cursor":3,"ts":12.0,"reason":"engine crashed"}"# + "\n",
                ])
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        recorder.start(name: "fail")

        await waitUntil {
            // The recorder should drop out of `.starting` (either to
            // `.idle` with an error, or to a failure-bearing state) once
            // the `recording_failed` replay event lands.
            if case .recording = recorder.state { return false }
            if case .starting = recorder.state { return false }
            return true
        }
        XCTAssertEqual(recorder.lastError, "engine crashed")
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

    private func waitUntil(
        timeout: TimeInterval = 3,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ predicate: @escaping @MainActor () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if predicate() { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail("Timed out waiting for condition", file: file, line: line)
    }
}

private final class LockedInt: @unchecked Sendable {
    private let lock = NSLock()
    private var storage = 0

    var value: Int {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }

    func incrementAndGet() -> Int {
        lock.lock()
        defer { lock.unlock() }
        storage += 1
        return storage
    }
}

/// Test-only stand-in for the daemon's replay buffer: events keyed by the
/// cursor at which they were "published", retrieved in ascending cursor
/// order by `eventsAfter(cursor:)`. Mirrors the daemon's
/// `bus.subscribe(since=…)` semantics enough for SwiftUI's
/// consume-after-start flow without modelling eviction.
private struct PresetReplay: Sendable {
    let events: [Int: String]

    func eventsAfter(cursor: Int) -> [String] {
        events
            .filter { $0.key > cursor }
            .sorted(by: { $0.key < $1.key })
            .map { $0.value }
    }
}
