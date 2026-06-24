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

    override func tearDown() async throws {
        await recorder?._testCancelDaemonTask()
        recorder = nil
        server?.stop()
        server = nil
        if let socketPath {
            unlink(socketPath)
        }
        unsetenv("SCREENCAP_DAEMON_SOCKET")
        try await super.tearDown()
    }

    func testDaemonTransportHappyPathUsesEventStreamForStateTransitions() async throws {
        let startCount = LockedInt()
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-1","started_at":10.0,"engine_pid":9991,"cursor":2}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"demo","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":3}"#)
            // First subscribe keys off recording.start.cursor (2), not the
            // snapshot's cursor (3); replay-buffered `started` at cursor 3
            // is the boundary event we depend on.
            case "/v0/events?since=2":
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":2,"ts":11.0}"# + "\n",
                    #"{"type":"started","schema_version":1,"cursor":3,"ts":12.0}"# + "\n",
                ], terminate: false)
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

    func testProbeDaemonRestoresActiveDaemonRecording() async throws {
        let startedAt = Date().addingTimeInterval(-12).timeIntervalSince1970
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/session.snapshot":
                return .json(
                    #"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"existing","started_at":"# +
                    "\(startedAt)" +
                    #","claimant":"daemon","recovering":false,"cursor":9}"#
                )
            case "/v0/events?since=9":
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":9,"ts":13.0}"# + "\n",
                ], terminate: false)
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()

        XCTAssertEqual(recorder.transport, .daemon)
        guard case .recording(let elapsed) = recorder.state else {
            return XCTFail("Expected active daemon recording, got \(recorder.state)")
        }
        XCTAssertGreaterThan(elapsed, 5)
        XCTAssertNil(recorder.lastError)
    }

    // MARK: - U4: daemon start-block (Screen Recording only)

    @MainActor
    func testDaemonStartBlockedWhenDaemonReportsScreenRecordingDenied() {
        // No server needed — the block is a synchronous client-side gate on the
        // daemon grant snapshot, so drive transport + grants directly.
        let permissions = PermissionController()
        permissions.updateDaemonGrants(
            DaemonPermissionGrants(screenRecording: .denied, accessibility: .granted, inputMonitoring: .granted)
        )
        let alerts = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateCancel)
        let recorder = RecorderController(alertPresenter: alerts)
        self.recorder = recorder
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)

        recorder.start(name: "demo")

        XCTAssertFalse(recorder.state.isRecording, "start must be blocked, not enter .starting")
        XCTAssertNotNil(recorder.lastError)
        XCTAssertEqual(alerts.lastPermissionRequiredPresented, ["Screen Recording"])
    }

    func testDaemonStartProceedsWhenOnlyAccessibilityDenied() async throws {
        // Decision: hard-block on Screen Recording only. An Accessibility denial
        // is advisory — start must proceed (warn-and-proceed), not block. We
        // assert the start dispatched (recording.start hit) without depending on
        // the chunked event stream delivering `started`.
        let startCount = LockedInt()
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"granted","accessibility":"denied","input_monitoring":"granted"}}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"s","started_at":10.0,"engine_pid":1,"cursor":2}"#)
            case "/v0/session.snapshot":
                // Not recording until start fires, then daemon-owned recording —
                // keeps the event consumer from treating the session as ended.
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"demo","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":3}"#)
            case "/v0/events?since=2":
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":2,"ts":11.0}"# + "\n",
                ], terminate: false)
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let permissions = PermissionController()
        let recorder = RecorderController()
        self.recorder = recorder
        recorder.bindPermissions(permissions)
        await recorder.probeDaemon()
        XCTAssertEqual(permissions.daemonGrants.accessibility, .denied)

        recorder.start(name: "demo")

        // Proceeded past the start-block: the daemon recording.start was sent.
        await waitUntil { startCount.value > 0 }
        XCTAssertNil(recorder.lastError)
        XCTAssertTrue(recorder.state.isRecording, "start should be in-flight, not blocked")
    }

    func testDaemonStartProceedsWhenOnlyInputMonitoringDenied() async throws {
        // Decision: hard-block on Screen Recording only. An Input Monitoring
        // denial is advisory — start must proceed (warn-and-proceed), not block.
        // We assert the start dispatched (recording.start hit) without depending
        // on the chunked event stream delivering `started`.
        let startCount = LockedInt()
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"granted","accessibility":"granted","input_monitoring":"denied"}}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"s","started_at":10.0,"engine_pid":1,"cursor":2}"#)
            case "/v0/session.snapshot":
                // Not recording until start fires, then daemon-owned recording —
                // keeps the event consumer from treating the session as ended.
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"demo","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":3}"#)
            case "/v0/events?since=2":
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":2,"ts":11.0}"# + "\n",
                ], terminate: false)
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let permissions = PermissionController()
        let recorder = RecorderController()
        self.recorder = recorder
        recorder.bindPermissions(permissions)
        await recorder.probeDaemon()
        XCTAssertEqual(permissions.daemonGrants.inputMonitoring, .denied)

        recorder.start(name: "demo")

        // Proceeded past the start-block: the daemon recording.start was sent.
        await waitUntil { startCount.value > 0 }
        XCTAssertNil(recorder.lastError)
        XCTAssertTrue(recorder.state.isRecording, "start should be in-flight, not blocked")
    }

    func testDaemonStartPermissionRequiredRoutesToGrantFlowWithoutCLIFallback() async throws {
        // Stale-client scenario (U6): daemon.info reports granted so the U4
        // client-side start-block passes, but the daemon's fresh pre-spawn
        // preflight rejects the start with a typed permission_required. The app
        // must route into the grant flow, NOT fall back to the CLI path.
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0,"permissions":{"screen_recording":"granted","accessibility":"granted","input_monitoring":"granted"}}"#)
            case "/v0/session.snapshot":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
            case "/v0/recording.start":
                return .json(
                    #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"permission_required","missing":["screen_recording"]}"#,
                    status: 403
                )
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let permissions = PermissionController()
        let alerts = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateCancel)
        let recorder = RecorderController(alertPresenter: alerts)
        self.recorder = recorder
        recorder.bindPermissions(permissions)
        await recorder.probeDaemon()
        XCTAssertEqual(recorder.transport, .daemon)

        recorder.start(name: "demo")

        await waitUntil { !recorder.state.isRecording && recorder.lastError != nil }
        // Routed into the grant flow naming the missing permission; idle, not
        // bounced into a CLI-fallback recording.
        XCTAssertEqual(alerts.lastPermissionRequiredPresented, ["Screen Recording"])
        XCTAssertFalse(recorder.state.isRecording)
    }

    func testDaemonTransportReconnectsAfterDroppedEventStream() async throws {
        let eventConnectionCount = LockedInt()
        let startCount = LockedInt()
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-2","started_at":10.0,"engine_pid":9992,"cursor":4}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"demo","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":5}"#)
            // Keep using the recording.start cursor until a start outcome
            // arrives. A stream that drops after `subscribed` but before
            // `started` must not make the next attempt jump to snapshot cursor.
            case "/v0/events?since=4":
                let count = eventConnectionCount.incrementAndGet()
                if count == 1 {
                    return .chunked([
                        #"{"type":"subscribed","schema_version":1,"cursor":4,"ts":11.0}"# + "\n",
                    ], terminate: false)
                }
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":4,"ts":12.0}"# + "\n",
                    #"{"type":"started","schema_version":1,"cursor":6,"ts":13.0}"# + "\n",
                ], terminate: false)
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

    /// When `/v0/session.snapshot` reports another claimant owns the active
    /// recording (`is_recording=true`, `daemon_owned=false`), the SwiftUI
    /// controller must surface the conflict via `lastError` and leave its
    /// own state at `.idle` rather than blindly attaching to a session
    /// it doesn't own.
    func testProbeDaemonSurfacesForeignRecordingAsLastError() async throws {
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/session.snapshot":
                // Active recording, but daemon does not own it — another
                // process (e.g. a bare `screencap start` invocation) is the
                // claimant. The controller should not attach.
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":false,"recording_name":"foreign","started_at":10.0,"claimant":"cli","recovering":false,"cursor":1}"#)
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()

        XCTAssertEqual(recorder.transport, .daemon)
        XCTAssertEqual(recorder.lastError, "Another process is recording.")
        XCTAssertEqual(recorder.state, .idle)
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
        let startCount = LockedInt()
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
                _ = startCount.incrementAndGet()
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-prod","started_at":10.0,"engine_pid":9993,"cursor":3}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
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
        let startCount = LockedInt()

        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            // First subscribe keys off this cursor (2) — it has aged
            // out of the daemon's replay window, surfaced as 410
            // cursor_unknown below.
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                // `session_id` must equal the snapshot `recording_name` below:
                // the daemon sets `session_id == recording_name == name`, and
                // the SCR-68 identity gate compares the two. A mismatch here
                // would reject the promotion as a foreign session.
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"unknown","started_at":10.0,"engine_pid":9994,"cursor":2}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
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
        // Pre-SCR-59 this asserted `>= 2` because the second snapshot was a
        // *prerequisite* for reaching `.recording` (the `started` event arrived
        // only via the iter-2 resubscribe). After SCR-59, iter 1 promotes
        // state to `.recording` directly from the in-scope snapshot, so the
        // iter-2 snapshot fetch is racy with the waitUntil. The `>= 1`
        // assertion still proves the cursor_unknown catch ran; the recovery
        // path itself is asserted by `state == .recording` above.
        XCTAssertGreaterThanOrEqual(snapshotCount.value, 1)
        XCTAssertNil(recorder.lastError)
    }

    /// SCR-59 red test. When `cursor_unknown` (HTTP 410) evicts the start
    /// cursor *and* the `started` event has already aged past the fresh
    /// snapshot cursor, the controller's only path out of `.starting` —
    /// the `started` event delivered via replay — never arrives. The
    /// existing `testCursorUnknown410FromEventsStreamFallsBackThroughSnapshotRefetch`
    /// papers over this branch by keeping `started` in the second-subscribe
    /// replay; here the second subscribe yields only `subscribed`, mirroring
    /// the production race where the daemon's replay buffer has fully aged
    /// past the start boundary.
    ///
    /// Expected behavior after fix: controller treats the daemon snapshot
    /// as authoritative (mirroring `syncDaemonSnapshot`) and promotes
    /// `.starting → .recording` from `snapshot.startedAt` instead of
    /// hanging on the never-arriving event.
    func testCursorUnknownEvictsStartedThenStuckInStartingWhenReplayHasAgedPast() async throws {
        let startCount = LockedInt()
        let snapshotCount = LockedInt()
        let secondSubscribeCount = LockedInt()

        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                // session_id == recording_name ("stuck") per daemon contract (SCR-68 gate).
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"stuck","started_at":10.0,"engine_pid":9996,"cursor":2}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
                // First snapshot returns cursor=2 to set up the 410. Second
                // and later snapshots return cursor=5 — the daemon has
                // advanced past `started` (at cursor 3) and the event has
                // been evicted from the replay window.
                if snapshotCount.incrementAndGet() == 1 {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"stuck","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":2}"#)
                }
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"stuck","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":5}"#)
            case "/v0/events?since=2":
                // 410 cursor_unknown — the bug trigger.
                return .json(
                    #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"cursor_unknown","requested_cursor":2}"#,
                    status: 410,
                    reason: "Gone"
                )
            case "/v0/events?since=5":
                // Critical contrast with the sibling test: only `subscribed`,
                // never `started`. The daemon's replay buffer has aged past
                // the start boundary, so the event will never be delivered.
                _ = secondSubscribeCount.incrementAndGet()
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":5,"ts":11.0}"# + "\n",
                ], terminate: false)
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        recorder.start(name: "stuck")

        // Give the controller time to: (1) handle the 410 with backoff
        // (~100ms), (2) re-fetch the snapshot, (3) issue the second
        // subscribe, (4) receive `subscribed`, and (5) park inside the
        // for-await waiting for events that won't arrive. 800ms is well
        // beyond the 100ms baseBackoff and leaves slack for CI scheduling.
        try await Task.sleep(nanoseconds: 800_000_000)

        // Confirm the second subscribe was reached so the assertion below
        // isn't passing for the wrong reason (e.g., test timed out before
        // the recovery path ran).
        XCTAssertGreaterThanOrEqual(secondSubscribeCount.value, 1, "Second subscribe at since=5 should have been issued after cursor_unknown recovery")

        // Expected post-fix behavior: snapshot promotion lifts the state to
        // `.recording` even though `started` never arrives. On `main` this
        // assertion fails because the controller has no snapshot-promotion
        // path inside `consumeEventStream`.
        guard case .recording = recorder.state else {
            return XCTFail("Expected .recording (via snapshot promotion); got \(recorder.state) — controller is stuck after cursor_unknown evicted `started` past the new snapshot cursor")
        }
        XCTAssertNil(recorder.lastError)
    }

    /// SCR-68 regression test (negative counterpart of the promotion test
    /// above). If the daemon restarts or a foreign claimant takes over between
    /// `recording.start` returning and the 410 firing, the refetched snapshot
    /// can report a *different* healthy daemon-owned session. The
    /// `cursor_unknown` catch must NOT promote `.starting → .recording` onto
    /// that session — doing so would attribute the UI to a recording we never
    /// started. Here we start session "mine" but every post-start snapshot
    /// reports `recording_name:"intruder"`; the controller must stay
    /// `.starting` (identity mismatch) instead of latching onto the intruder.
    func testCursorUnknownDoesNotPromoteOntoForeignSession() async throws {
        let startCount = LockedInt()
        let cursorUnknownCount = LockedInt()

        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                // We start session "mine" (session_id == recording_name).
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"mine","started_at":10.0,"engine_pid":9993,"cursor":2}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
                // A foreign session took over: healthy and daemon-owned, but a
                // DIFFERENT recording_name than the "mine" we started. Distinct
                // started_at so a (buggy) promotion would also be detectable via
                // elapsed, not just state.
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"intruder","started_at":9000000.0,"claimant":"daemon","recovering":false,"cursor":5}"#)
            case let path where path.hasPrefix("/v0/events?since="):
                // Both the start cursor (since=2) and the refetched snapshot
                // cursor (since=5) 410, keeping the recovery loop spinning on
                // the intruder snapshot.
                _ = cursorUnknownCount.incrementAndGet()
                return .json(
                    #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"cursor_unknown","requested_cursor":2}"#,
                    status: 410,
                    reason: "Gone"
                )
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        recorder.start(name: "mine")

        // Deterministically wait for the recovery loop to fire at least two
        // cursor_unknown cycles (baseBackoff 0.1, 0.2, 0.4 …) rather than
        // sleeping a fixed interval. Reaching `>= 2` is positive proof the
        // catch engaged at least twice against the foreign snapshot; the
        // generous 5s cap is well below the 10-failure `.lostContact` ceiling
        // (tens of seconds of cumulative backoff away), so when this returns the
        // state can only be `.starting` (rejected) or — pre-fix — `.recording`
        // (wrongly promoted onto the intruder). `waitUntil` XCTFails on timeout,
        // which doubles as the "recovery loop never engaged" assertion.
        await waitUntil(timeout: 5) { cursorUnknownCount.value >= 2 }

        // Core SCR-68 invariant: the foreign session must not be promoted.
        // Pre-fix this is `.recording` (the catch trusted isRecording &&
        // daemonOwned only); post-fix the identity gate keeps us `.starting`.
        guard case .starting = recorder.state else {
            return XCTFail("cursor_unknown promoted the UI onto a foreign session; state=\(recorder.state). The identity gate must reject a snapshot whose recording_name differs from the started session_id.")
        }
    }

    /// SCR-59 follow-on regression test. Post-fix, the `cursor_unknown` catch
    /// path is a *recovery-success* when the in-scope snapshot confirms an
    /// active daemon-owned recording — the orchestrator has either just
    /// promoted out of `.starting` (first iteration) or is already
    /// `.recording` (subsequent iterations where the orchestrator handler
    /// no-ops). Either way it must not consume the failure budget; otherwise
    /// repeated evictions against a healthy recording would hit
    /// `.lostContact` after `maxConsecutiveFailures` iterations and tear down
    /// a recording the snapshot has confirmed is alive.
    ///
    /// Discrimination is timing-based: with the budget skip, `consecutiveFailures`
    /// stays at 0 and backoff stays at `baseBackoff` (~100ms) → many subscribe
    /// attempts fit in a short window. Without the skip, backoff doubles
    /// (0.1, 0.2, 0.4, 0.8, 1.6 s…) and only ~4-5 attempts fit in 1.5s. The
    /// `≥8` threshold leaves CI variance headroom while preserving the signal.
    func testRepeatedCursorUnknownWithActiveSnapshotDoesNotTearDownRecording() async throws {
        let startCount = LockedInt()
        let subscribeCount = LockedInt()

        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                // session_id == recording_name ("repeated") per daemon contract (SCR-68 gate).
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"repeated","started_at":10.0,"engine_pid":9997,"cursor":2}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
                // Steady cursor=5 every snapshot post-start — every subscribe
                // beyond the first (since=2 from pendingStartCursor) is at
                // since=5, and the events handler 410s both.
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"repeated","started_at":10.0,"claimant":"daemon","recovering":false,"cursor":5}"#)
            case let path where path.hasPrefix("/v0/events?since="):
                _ = subscribeCount.incrementAndGet()
                return .json(
                    #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"cursor_unknown","requested_cursor":5}"#,
                    status: 410,
                    reason: "Gone"
                )
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        recorder.start(name: "repeated")

        // The recovery streak uses exponential backoff capped at 5s
        // (`recoveryCappedBackoff`) starting from 100ms (`baseBackoff`).
        // Cumulative backoff after N subscribes: 0.1+0.2+0.4+0.8+1.6+3.2+5.0+5.0...
        // In 3s of wall time we expect 4-6 subscribes; pre-fix flat-100ms
        // looping produced 25+ in the same window.
        try await Task.sleep(nanoseconds: 3_000_000_000)

        // Recording must survive the repeated evictions (SCR-59 invariant:
        // healthy snapshot keeps the session alive even through repeated
        // `cursor_unknown`).
        guard case .recording = recorder.state else {
            return XCTFail("Repeated cursor_unknown tore down a healthy recording; state=\(recorder.state). The orchestrator should stay `.recording` indefinitely while the daemon snapshot keeps reporting an active session.")
        }
        XCTAssertNil(recorder.lastError, "Expected no error after repeated cursor_unknown with healthy snapshot; got: \(recorder.lastError ?? "nil")")

        // Bound the recovery loop: a hot loop at 100ms would produce ≥20
        // subscribes in 3s. Exponential backoff with the cap allows at
        // most ~12 even on a fast scheduler. The ≥2 floor confirms recovery
        // actually engaged rather than the test exiting before any retry.
        XCTAssertGreaterThanOrEqual(
            subscribeCount.value, 2,
            "Recovery loop never engaged; expected at least the initial subscribe and one cursor_unknown retry."
        )
        XCTAssertLessThanOrEqual(
            subscribeCount.value, 12,
            "Recovery loop appears to be hot-looping (\(subscribeCount.value) subscribes in 3s). Exponential backoff capped at 5s should bound retries; a high count suggests the backoff escalation regressed."
        )
    }

    /// SCR-59 follow-on regression test. The orchestrator's
    /// `onSnapshotConfirmedActiveRecording` handler is `.starting`-only — it
    /// must never invoke `observeActiveDaemonSession` from `.recording`
    /// (which would clobber `recordingStartedAt` and reset elapsed) or
    /// `.stopping` (which would regress the lifecycle). This test drives
    /// the controller to `.recording` via a normal `started` event, then
    /// forces repeated `cursor_unknown` recoveries with a snapshot whose
    /// `started_at` differs from the original — if the guard fails,
    /// elapsed would jump to match the snapshot's `started_at` and we'd
    /// detect the regression.
    func testCursorUnknownRecoveryFromRecordingDoesNotRegressState() async throws {
        let startCount = LockedInt()
        let firstSubscribeCount = LockedInt()
        let recoverySubscribeCount = LockedInt()

        // Use two distinct, post-epoch `started_at` values so elapsed
        // computations produce meaningfully different numbers:
        //   - original (50.0): elapsed at state-set ≈ now - 50
        //   - snapshot (5_000_000.0): if guard fails, elapsed jumps
        //     to ≈ now - 5_000_000 (much smaller — regression detectable).
        let originalStartedAt: Double = 50.0
        let snapshotStartedAt: Double = 5_000_000.0

        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                // session_id == recording_name ("guard") per daemon contract (SCR-68 gate).
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"guard","started_at":\#(originalStartedAt),"engine_pid":9995,"cursor":2}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":true,"daemon_owned":true,"recording_name":"guard","started_at":\#(snapshotStartedAt),"claimant":"daemon","recovering":false,"cursor":5}"#)
            case "/v0/events?since=2":
                // First subscribe: deliver `subscribed` + `started`, then
                // close cleanly so the loop retries and lands on the 410.
                _ = firstSubscribeCount.incrementAndGet()
                return .chunked([
                    #"{"type":"subscribed","schema_version":1,"cursor":2,"ts":11.0}"# + "\n",
                    #"{"type":"started","schema_version":1,"cursor":3,"ts":11.0,"recording_name":"guard","engine_pid":9995}"# + "\n",
                ], terminate: true)
            case let path where path.hasPrefix("/v0/events?since="):
                // Subsequent subscribes always 410. Drives the recovery
                // path while state is already `.recording`.
                _ = recoverySubscribeCount.incrementAndGet()
                return .json(
                    #"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"cursor_unknown","requested_cursor":5}"#,
                    status: 410,
                    reason: "Gone"
                )
            default:
                XCTFail("Unexpected request path \(request.path)")
                return .json(#"{"ok":false,"schema_version":1,"daemon_version":"test","api_schema_version":1,"error":"unexpected"}"#, status: 500)
            }
        }

        let recorder = RecorderController()
        self.recorder = recorder
        await recorder.probeDaemon()
        recorder.start(name: "guard")

        // Wait until the `started` event lifts state to `.recording`.
        await waitUntil {
            if case .recording = recorder.state { return true }
            return false
        }

        guard case .recording(let elapsedBeforeRecovery) = recorder.state else {
            return XCTFail("Expected `.recording` before triggering recovery; got \(recorder.state)")
        }

        // Give the recovery loop time to fire at least one 410 cycle.
        try await Task.sleep(nanoseconds: 600_000_000)

        XCTAssertGreaterThanOrEqual(
            recoverySubscribeCount.value, 1,
            "Recovery path never engaged; expected at least one cursor_unknown retry after `.recording`"
        )

        guard case .recording(let elapsedAfterRecovery) = recorder.state else {
            return XCTFail("State regressed from `.recording` after cursor_unknown recovery; got \(recorder.state). The `.starting`-only guard in `onSnapshotConfirmedActiveRecording` failed.")
        }

        // If the guard had failed, `observeActiveDaemonSession(startedAt:)`
        // would have set `recordingStartedAt` to a 1970-era Date and
        // `elapsed` would jump to ~1.7 billion seconds. The guard suppresses
        // the call, so elapsed stays near `elapsedBeforeRecovery` plus at
        // most one timer tick (1s). A 60s ceiling gives generous slack for
        // CI scheduling without missing the clobber signal.
        XCTAssertLessThan(
            elapsedAfterRecovery, elapsedBeforeRecovery + 60.0,
            "`recordingStartedAt` was clobbered by cursor_unknown recovery — elapsed went from \(elapsedBeforeRecovery)s to \(elapsedAfterRecovery)s. The `.starting`-only guard must prevent `observeActiveDaemonSession` from running while state is `.recording`."
        )
        XCTAssertNil(recorder.lastError)
    }

    /// A `recording_failed` event delivered via replay must propagate to
    /// the controller so the UI surfaces the failure to the user instead
    /// of remaining stuck in `.starting` while the daemon has already
    /// torn the recording down.
    func testRecordingFailedDeliveredViaReplaySurfacesError() async throws {
        let startCount = LockedInt()
        _ = try startServer { request in
            switch request.path {
            case "/v0/daemon.info":
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"build":null,"started_at":1.0}"#)
            case "/v0/recording.start":
                _ = startCount.incrementAndGet()
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-fail","started_at":10.0,"engine_pid":9995,"cursor":2}"#)
            case "/v0/session.snapshot":
                guard startCount.value > 0 else {
                    return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"is_recording":false,"daemon_owned":false,"recording_name":null,"started_at":null,"claimant":null,"recovering":false,"cursor":0}"#)
                }
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
