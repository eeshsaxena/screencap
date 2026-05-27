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
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-unknown","started_at":10.0,"engine_pid":9994,"cursor":2}"#)
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
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-stuck","started_at":10.0,"engine_pid":9996,"cursor":2}"#)
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
                return .json(#"{"ok":true,"schema_version":1,"daemon_version":"test","api_schema_version":1,"session_id":"session-repeated","started_at":10.0,"engine_pid":9997,"cursor":2}"#)
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

        // 1.5s is well past the 100ms baseBackoff repeated ~10-15 times;
        // also past the cumulative ~1.5s where exponential backoff would
        // only have allowed ~4 attempts. The threshold below distinguishes
        // the two regimes without making the test slow.
        try await Task.sleep(nanoseconds: 1_500_000_000)

        XCTAssertGreaterThanOrEqual(
            subscribeCount.value,
            8,
            "Budget-skip on cursor_unknown-paired-with-healthy-snapshot should keep backoff at baseline (~100ms). Saw \(subscribeCount.value) subscribes in 1.5s; expected ≥8. A low count indicates the failure budget is still being incremented and backoff is escalating exponentially."
        )

        // Recording must survive the repeated evictions.
        guard case .recording = recorder.state else {
            return XCTFail("Repeated cursor_unknown tore down a healthy recording; state=\(recorder.state). The orchestrator should stay `.recording` indefinitely while the daemon snapshot keeps reporting an active session.")
        }
        XCTAssertNil(recorder.lastError, "Expected no error after repeated cursor_unknown with healthy snapshot; got: \(recorder.lastError ?? "nil")")
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
