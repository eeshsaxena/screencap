import XCTest
@testable import ScreenCap

@MainActor
final class RecorderControllerTests: XCTestCase {
    func testMatrixDisclosureEventIsCapturedForSwiftUIPresentation() {
        let recorder = RecorderController()

        recorder._testHandleStderrLine(
            #"{"type":"matrix_disclosure_required","schema_version":1,"changes":["chat_email_calendar_video_call_mask_window"],"opt_out_command_examples":["screencap settings privacy exclude_apps add com.openai.chat"]}"#
        )

        XCTAssertEqual(
            recorder.matrixDisclosure?.changes,
            ["chat_email_calendar_video_call_mask_window"]
        )
        XCTAssertEqual(
            recorder.matrixDisclosure?.optOutCommandExamples,
            ["screencap settings privacy exclude_apps add com.openai.chat"]
        )
    }

    func testDismissMatrixDisclosureClearsPresentationState() {
        let recorder = RecorderController()

        recorder._testHandleStderrLine(
            #"{"type":"matrix_disclosure_required","schema_version":1,"changes":["ai_assistant_browser_unverified"],"opt_out_command_examples":[]}"#
        )
        recorder.dismissMatrixDisclosure()

        XCTAssertNil(recorder.matrixDisclosure)
    }

    // MARK: - capture_unhealthy advisory (SCR-76)

    func testCaptureUnhealthyWhileRecordingSetsAdvisoryAndKeepsRecording() {
        let recorder = RecorderController()
        recorder._testSetPresentation(state: .recording(elapsed: 5))

        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","elapsed":12.0,"schema_version":1}"#
        )

        XCTAssertNotNil(recorder.captureAdvisory)
        XCTAssertTrue(recorder.captureAdvisory?.contains("Screen capture") ?? false)
        // Advisory is NON-terminal: still recording, and no terminal error set.
        XCTAssertTrue(recorder.state.isRecording)
        XCTAssertNil(recorder.lastError)
    }

    func testCaptureUnhealthyWhileIdleIsSuppressedByRecordingGuard() {
        let recorder = RecorderController()  // default .idle

        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"listener_dead","reader":"action","elapsed":3.0,"schema_version":1}"#
        )

        XCTAssertNil(recorder.captureAdvisory)
    }

    func testCaptureUnhealthyWhileStoppingIsSuppressedByRecordingGuard() {
        let recorder = RecorderController()
        recorder._testSetPresentation(state: .stopping(quitting: false))

        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","elapsed":12.0,"schema_version":1}"#
        )

        // handleCaptureUnhealthy's `if case .recording` guard suppresses a late
        // advisory arriving during teardown, so no stale hint shows while the
        // controller is .stopping (mirrors the .idle suppression above).
        XCTAssertNil(recorder.captureAdvisory)
    }

    func testCaptureAdvisoryClearedWhenRecordingReturnsToIdle() {
        let recorder = RecorderController()
        recorder._testSetPresentation(state: .recording(elapsed: 5))
        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","schema_version":1}"#
        )
        XCTAssertNotNil(recorder.captureAdvisory)

        // A clean process termination returns the controller to .idle; the
        // advisory must not linger on the idle UI.
        recorder._testHandleProcessTerminated(exitCode: 0)

        XCTAssertEqual(recorder.state, .idle)
        XCTAssertNil(recorder.captureAdvisory)
    }

    func testRepeatedCaptureUnhealthyUpdatesAdvisoryInPlace() {
        let recorder = RecorderController()
        recorder._testSetPresentation(state: .recording(elapsed: 5))

        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","schema_version":1}"#
        )
        let first = recorder.captureAdvisory
        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"listener_dead","reader":"action","schema_version":1}"#
        )
        let second = recorder.captureAdvisory

        XCTAssertNotNil(first)
        XCTAssertNotNil(second)
        // Updated in place to reflect the latest affected reader (no re-pop).
        XCTAssertTrue(second?.contains("Input capture") ?? false)
        XCTAssertTrue(recorder.state.isRecording)
    }

    // MARK: - capture_recovered paired clear (SCR-100)

    func testCaptureRecoveredClearsAdvisoryMidRecording() {
        let recorder = RecorderController()
        recorder._testSetPresentation(state: .recording(elapsed: 5))
        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","schema_version":1}"#
        )
        XCTAssertNotNil(recorder.captureAdvisory)

        // The reader recovers WITHIN the same recording — the advisory must clear
        // immediately, not linger until .idle (the SCR-100 bug).
        recorder._testHandleStderrLine(
            #"{"type":"capture_recovered","reader":"screen","elapsed":15.0,"schema_version":1}"#
        )
        XCTAssertNil(recorder.captureAdvisory)
        XCTAssertTrue(recorder.state.isRecording)
    }

    func testCaptureRecoveredForOneReaderKeepsAdvisoryWhileAnotherUnhealthy() {
        let recorder = RecorderController()
        recorder._testSetPresentation(state: .recording(elapsed: 5))
        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","schema_version":1}"#
        )
        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"listener_dead","reader":"action","schema_version":1}"#
        )
        XCTAssertTrue(recorder.captureAdvisory?.contains("Input capture") ?? false)

        // action recovers but screen is still unhealthy → the advisory must
        // persist and fall back to naming the still-unhealthy screen reader.
        recorder._testHandleStderrLine(
            #"{"type":"capture_recovered","reader":"action","schema_version":1}"#
        )
        XCTAssertNotNil(recorder.captureAdvisory)
        XCTAssertTrue(recorder.captureAdvisory?.contains("Screen capture") ?? false)

        // screen recovers too → nothing left unhealthy → advisory clears.
        recorder._testHandleStderrLine(
            #"{"type":"capture_recovered","reader":"screen","schema_version":1}"#
        )
        XCTAssertNil(recorder.captureAdvisory)
    }

    func testCaptureUnhealthyReShowsAfterRecovery() {
        let recorder = RecorderController()
        recorder._testSetPresentation(state: .recording(elapsed: 5))
        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","schema_version":1}"#
        )
        recorder._testHandleStderrLine(
            #"{"type":"capture_recovered","reader":"screen","schema_version":1}"#
        )
        XCTAssertNil(recorder.captureAdvisory)

        // The engine re-emits capture_unhealthy on a rebreak (it clears its
        // per-reader emitted flag on recovery), so the advisory re-shows.
        recorder._testHandleStderrLine(
            #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","schema_version":1}"#
        )
        XCTAssertNotNil(recorder.captureAdvisory)
    }

    func testCaptureRecoveredWhileIdleIsNoOp() {
        let recorder = RecorderController()  // default .idle
        // A late recovery arriving with no active recording must not crash or
        // set any state — mirrors the capture_unhealthy .recording guard.
        recorder._testHandleStderrLine(
            #"{"type":"capture_recovered","reader":"screen","schema_version":1}"#
        )
        XCTAssertNil(recorder.captureAdvisory)
    }

    func testCaptureRecoveredWhileStoppingIsNoOp() {
        let recorder = RecorderController()
        recorder._testSetPresentation(state: .stopping(quitting: false))

        // A late recovery arriving during teardown must be a no-op, mirroring
        // the capture_unhealthy .stopping suppression — handleCaptureRecovered's
        // `if case .recording` guard drops it (and the .idle chokepoint clears
        // everything anyway).
        recorder._testHandleStderrLine(
            #"{"type":"capture_recovered","reader":"screen","elapsed":15.0,"schema_version":1}"#
        )

        XCTAssertNil(recorder.captureAdvisory)
    }

    func testMissingPermissionsMessageNamesEveryRequiredPermission() {
        XCTAssertEqual(
            RecorderController.requiredPermissionsErrorMessage,
            "Grant Screen Recording, Accessibility, and Input Monitoring permissions before recording."
        )
    }

    func testDaemonTransportDoesNotBlockStartOnAppProcessPermissions() async {
        let previousSocket = getenv("SCREENCAP_DAEMON_SOCKET").map { String(cString: $0) }
        setenv("SCREENCAP_DAEMON_SOCKET", "/tmp/sc-missing-\(UUID().uuidString).sock", 1)
        defer {
            if let previousSocket {
                setenv("SCREENCAP_DAEMON_SOCKET", previousSocket, 1)
            } else {
                unsetenv("SCREENCAP_DAEMON_SOCKET")
            }
        }

        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)

        recorder.start(name: "daemon-owned")

        XCTAssertEqual(recorder.state, .starting)
        XCTAssertNil(recorder.lastError)
        // Tear down the in-flight daemon event Task so it doesn't leak past
        // the assertion frame. Sleeping a fixed 100ms here lets the Task
        // run unsupervised — cancel + await the cancellation instead.
        await recorder._testCancelDaemonTask()
    }

    func testCLIFallbackStillBlocksStartOnAppProcessPermissions() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.cliFallback)

        recorder.start(name: "cli-owned")

        XCTAssertEqual(recorder.state, .idle)
        XCTAssertEqual(recorder.lastError, RecorderController.requiredPermissionsErrorMessage)
    }

    func testDaemonTransportPermissionWatchdogIgnoresAppProcessPermissions() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)
        recorder._testSetPresentation(state: .recording(elapsed: 3))

        recorder._testCheckPermissionsDuringRecording()

        XCTAssertEqual(recorder.state, .recording(elapsed: 3))
        XCTAssertNil(recorder.lastError)
    }

    func testForceStoppedWarningSurvivesCleanProcessTermination() {
        let recorder = RecorderController()

        recorder._testSetPresentation(state: .stopping(quitting: false))
        recorder._testHandleStderrLine(
            #"{"type":"recording_finalized","schema_version":1,"force_stopped":true}"#
        )
        recorder._testHandleProcessTerminated(exitCode: 0)

        XCTAssertEqual(
            recorder.lastError,
            "Recording stopped, but some data may not have uploaded. Run `screencap upload` to retry."
        )
    }

    func testRecorderErrorMessageCarriesSharedRecorderWarningText() {
        let view = RecorderErrorMessage(message: "Disk is full — recording stopped.")

        XCTAssertEqual(view.message, "Disk is full — recording stopped.")
    }

    // MARK: - Cmd+Q timeout SIGKILL branch (SCR-58 review todo #001)

    /// When the Cmd+Q stop times out and the CLI process is still alive, the
    /// orchestrator must dispatch SIGKILL via `SpawnedProcessHandle.forceKill()`
    /// and surface the "force-killed" message. The fake handle returns true
    /// from `forceKill()` to signal "process was running and the kill was
    /// dispatched"; the assertion is on the surfaced `lastError`.
    func testCmdQTimeoutSendsForceKillToRunningCLIProcessAndSurfacesMessage() async {
        let handle = FakeSpawnedProcessHandle(isRunning: true, pid: 9999, forceKillReturnValue: true)
        let cliService = StubbedCLIRecorderService(currentProcess: handle)
        let stopPolicy = StubbedStopPolicyCoordinator(outcome: .timedOut)
        let alert = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateLater)

        let recorder = RecorderController(
            alertPresenter: alert,
            cliService: cliService,
            stopPolicy: stopPolicy
        )
        recorder._testSetTransport(.cliFallback)
        recorder._testSetPresentation(state: .recording(elapsed: 5))

        _ = recorder.confirmQuitWhileRecording()

        await waitUntil { recorder.state == .idle }

        XCTAssertEqual(handle.forceKillInvocations, 1)
        XCTAssertEqual(
            recorder.lastError,
            "Stop timed out after 5 minutes; recorder force-killed."
        )
    }

    /// When the Cmd+Q stop times out but the CLI process has already exited,
    /// `forceKill()` returns false and the orchestrator must skip the
    /// "force-killed" message — surfacing it would lie to the user about
    /// what just happened. The fake records that `forceKill` was attempted
    /// (the orchestrator calls it before checking the return value), but the
    /// `lastError` text doesn't change.
    func testCmdQTimeoutSkipsForceKillMessageWhenProcessAlreadyExited() async {
        let handle = FakeSpawnedProcessHandle(isRunning: false, pid: 9999, forceKillReturnValue: false)
        let cliService = StubbedCLIRecorderService(currentProcess: handle)
        let stopPolicy = StubbedStopPolicyCoordinator(outcome: .timedOut)
        let alert = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateLater)

        let recorder = RecorderController(
            alertPresenter: alert,
            cliService: cliService,
            stopPolicy: stopPolicy
        )
        recorder._testSetTransport(.cliFallback)
        recorder._testSetPresentation(state: .recording(elapsed: 5))

        _ = recorder.confirmQuitWhileRecording()

        await waitUntil { recorder.state == .idle }

        XCTAssertEqual(handle.forceKillInvocations, 1, "forceKill must still be invoked so the handle can guard internally")
        XCTAssertNotEqual(
            recorder.lastError,
            "Stop timed out after 5 minutes; recorder force-killed.",
            "no force-killed message when process already exited"
        )
    }

    // MARK: - SCR-142 CLI-fallback permission_required routing

    /// The CLI-fallback transport must route a start-time `permission_required`
    /// event into the SAME precise grant flow the daemon transport uses, naming
    /// every missing permission — not the hedged exit-1 fallback. The capturing
    /// CLI fake lets us replay the engine's stderr→termination sequence in the
    /// FIFO order `LiveCLIRecorderService` guarantees (event before terminate).
    func testCLIFallbackPermissionRequiredRoutesToGrantFlowNamingEachPermission() throws {
        let alert = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateLater)
        let cliService = CapturingCLIRecorderService()
        let recorder = RecorderController(alertPresenter: alert, cliService: cliService)
        // No permissions bound: the start guard is skipped, so we reach the
        // spawn and capture the callbacks (the stale-client scenario where the
        // daemon's fresh pre-spawn probe is the first to see the denial).
        recorder._testSetTransport(.cliFallback)

        recorder.start(name: "demo")

        let event = try XCTUnwrap(RecorderEventLine.parse(
            stderrLine: #"{"type":"permission_required","missing":["screen_recording","accessibility"],"schema_version":1}"#
        ))
        let onEvent = try XCTUnwrap(cliService.capturedEvent)
        let onTerminated = try XCTUnwrap(cliService.capturedTerminated)

        // Engine emits the structured event, THEN exits 3 (FIFO-ordered).
        onEvent(event)
        onTerminated(3)

        XCTAssertEqual(alert.lastPermissionRequiredPresented, ["Screen Recording", "Accessibility"])
        // The generic exit-3 "revoked" copy is suppressed; the grant-flow
        // message stands, and we land back idle (nothing was recording).
        XCTAssertEqual(recorder.lastError, "Grant Screen Recording, Accessibility to ScreenCap before recording.")
        XCTAssertFalse(recorder.state.isRecording)
    }

    /// SCR-142 empty-`missing` fallback: a `permission_required` event with an
    /// empty `missing` list (the CLI collapses a non-list / absent `missing` to
    /// `[]`) must still route into the grant flow, naming Screen Recording — the
    /// permission fatal to capture — via `privacyPanes(fromMissing:)`.
    func testCLIFallbackPermissionRequiredWithEmptyMissingFallsBackToScreenRecording() throws {
        let alert = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateLater)
        let cliService = CapturingCLIRecorderService()
        let recorder = RecorderController(alertPresenter: alert, cliService: cliService)
        recorder._testSetTransport(.cliFallback)

        recorder.start(name: "demo")

        let event = try XCTUnwrap(RecorderEventLine.parse(
            stderrLine: #"{"type":"permission_required","missing":[],"schema_version":1}"#
        ))
        let onEvent = try XCTUnwrap(cliService.capturedEvent)
        let onTerminated = try XCTUnwrap(cliService.capturedTerminated)

        onEvent(event)
        onTerminated(3)

        XCTAssertEqual(alert.lastPermissionRequiredPresented, ["Screen Recording"])
        XCTAssertEqual(recorder.lastError, "Grant Screen Recording to ScreenCap before recording.")
        XCTAssertFalse(recorder.state.isRecording)
    }

    // MARK: - HUD / window lifecycle (U7)

    /// The `started` event floats the HUD and hides the main window.
    func testStartedEventShowsHUDAndHidesMainWindow() {
        let fake = FakeWindowLifecycle()
        let recorder = RecorderController(windowLifecycle: fake)
        recorder._testSetPresentation(state: .starting)

        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)

        XCTAssertEqual(fake.showHUDCount, 1)
        XCTAssertEqual(fake.hideMainWindowCount, 1)
        XCTAssertEqual(fake.hideHUDCount, 0)
    }

    /// An async `recording_failed` while recording closes the HUD, restores the
    /// main window, posts the route-to-Library signal, and surfaces the error.
    /// Drives through `started` first so the window was genuinely hidden (the
    /// restore is gated on that — see `mainWindowHidden`).
    func testRecordingFailedRestoresWindowAndRoutesToLibrary() {
        let fake = FakeWindowLifecycle()
        let recorder = RecorderController(windowLifecycle: fake)
        recorder._testSetPresentation(state: .starting)
        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)
        XCTAssertEqual(fake.hideMainWindowCount, 1)  // window really hidden

        let routed = expectation(forNotification: .screenCapRecordingDidEnd, object: nil)

        recorder._testHandleStderrLine(#"{"type":"recording_failed","schema_version":1,"reason":"engine crashed"}"#)

        wait(for: [routed], timeout: 1)
        XCTAssertEqual(fake.hideHUDCount, 1)
        XCTAssertEqual(fake.restoreMainWindowCount, 1)
        XCTAssertEqual(recorder.lastError, "engine crashed")
        XCTAssertFalse(recorder.state.isRecording)
    }

    /// A start that fails in `.starting` (pre-HUD) — e.g. the daemon→CLI fallback
    /// path — must NOT restore/reroute, since the main window was never hidden.
    func testStartingFailureDoesNotRestoreOrRoute() {
        let fake = FakeWindowLifecycle()
        let recorder = RecorderController(windowLifecycle: fake)
        recorder._testSetPresentation(state: .starting)

        // Process exits 1 at start time (no `started` was ever observed).
        recorder._testHandleProcessTerminated(exitCode: 1)

        XCTAssertFalse(recorder.state.isRecording)
        XCTAssertEqual(fake.hideMainWindowCount, 0)
        XCTAssertEqual(fake.restoreMainWindowCount, 0, "no window was hidden, so none is restored")
    }

    // MARK: - Audio flag (U6)

    /// CLI fallback: an explicit audio-off threads `--no-audio` into the argv and
    /// the effective state is audio-off.
    func testCLIFallbackAudioFalseThreadsNoAudioArg() {
        let cliService = CapturingCLIRecorderService()
        let recorder = RecorderController(cliService: cliService)
        recorder._testSetTransport(.cliFallback)

        recorder.start(name: "demo", audio: false)

        XCTAssertEqual(cliService.capturedArgs, ["start", "demo", "--no-audio"])
        XCTAssertFalse(recorder.audioEnabled)
    }

    /// CLI fallback: audio-on omits `--no-audio` (the CLI defers to
    /// `audio_default`); the HUD reflects audio-on.
    func testCLIFallbackAudioTrueOmitsNoAudioArg() {
        let cliService = CapturingCLIRecorderService()
        let recorder = RecorderController(cliService: cliService)
        recorder._testSetTransport(.cliFallback)

        recorder.start(name: "demo", audio: true)

        XCTAssertEqual(cliService.capturedArgs, ["start", "demo"])
        XCTAssertTrue(recorder.audioEnabled)
    }

    /// CLI fallback: the plain (nil) start path — toolbar / menu bar — never adds
    /// `--no-audio` and is treated as audio-on.
    func testCLIFallbackAudioNilOmitsNoAudioArg() {
        let cliService = CapturingCLIRecorderService()
        let recorder = RecorderController(cliService: cliService)
        recorder._testSetTransport(.cliFallback)

        recorder.start(name: "demo")

        XCTAssertEqual(cliService.capturedArgs, ["start", "demo"])
        XCTAssertTrue(recorder.audioEnabled)
    }

    /// Daemon path: the audio choice threads into the request body and the
    /// effective state mirrors the daemon's echo.
    func testDaemonAudioThreadsToBodyAndReflectsEcho() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "demo", audioEcho: false)
        )
        let recorder = RecorderController(daemonService: daemon)
        recorder._testSetTransport(.daemon)

        recorder.start(name: "demo", audio: false)

        await waitUntil { daemon.startRecordingCalled }
        XCTAssertEqual(daemon.capturedAudio, false)
        XCTAssertFalse(recorder.audioEnabled)
        await recorder._testCancelDaemonTask()
    }

    /// Daemon path: a stale daemon that omits the `audio` echo is treated as
    /// audio-on even when audio-off was requested (the stale daemon ignored the
    /// flag and recorded with audio).
    func testDaemonMissingEchoTreatedAsAudioOn() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "demo", audioEcho: nil)
        )
        let recorder = RecorderController(daemonService: daemon)
        recorder._testSetTransport(.daemon)

        recorder.start(name: "demo", audio: false)

        await waitUntil { daemon.startRecordingCalled }
        XCTAssertEqual(daemon.capturedAudio, false)
        XCTAssertTrue(recorder.audioEnabled)
        await recorder._testCancelDaemonTask()
    }

    // MARK: - Helpers

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

// MARK: - Test fakes (Cmd+Q SIGKILL branch)

/// Fake `SpawnedProcessHandle` that records `forceKill()` invocations and
/// returns a configurable result so the orchestrator's branch logic is
/// exercised without signalling a real PID.
@MainActor
final class FakeSpawnedProcessHandle: SpawnedProcessHandle {
    var isRunning: Bool
    var processIdentifier: Int32
    private let forceKillReturnValue: Bool
    private(set) var forceKillInvocations = 0
    private(set) var terminateInvocations = 0

    init(isRunning: Bool, pid: Int32, forceKillReturnValue: Bool) {
        self.isRunning = isRunning
        self.processIdentifier = pid
        self.forceKillReturnValue = forceKillReturnValue
    }

    func terminate() {
        terminateInvocations += 1
    }

    func forceKill() -> Bool {
        forceKillInvocations += 1
        return forceKillReturnValue
    }
}

/// Fake `CLIRecorderService` exposing a fixed `currentProcess`. The `start`
/// method is a no-op since these tests don't spawn a real recorder.
@MainActor
final class StubbedCLIRecorderService: CLIRecorderService {
    var currentProcess: SpawnedProcessHandle?

    init(currentProcess: SpawnedProcessHandle?) {
        self.currentProcess = currentProcess
    }

    func start(
        args: [String],
        onEvent: @escaping @MainActor @Sendable (RecorderEventLine) -> Void,
        onTerminated: @escaping @MainActor @Sendable (Int32) -> Void
    ) throws {
        // No-op: these tests drive state via `_testSetPresentation`, not via
        // a real subprocess lifecycle.
    }
}

/// Fake `CLIRecorderService` that captures the orchestrator's `onEvent` /
/// `onTerminated` callbacks so a test can drive the CLI-fallback stderr →
/// termination sequence deterministically (no real subprocess).
@MainActor
final class CapturingCLIRecorderService: CLIRecorderService {
    var currentProcess: SpawnedProcessHandle?
    private(set) var capturedEvent: (@MainActor @Sendable (RecorderEventLine) -> Void)?
    private(set) var capturedTerminated: (@MainActor @Sendable (Int32) -> Void)?
    /// U6: the argv the orchestrator spawned with, so a test can assert the
    /// `--no-audio` threading.
    private(set) var capturedArgs: [String]?

    func start(
        args: [String],
        onEvent: @escaping @MainActor @Sendable (RecorderEventLine) -> Void,
        onTerminated: @escaping @MainActor @Sendable (Int32) -> Void
    ) throws {
        capturedArgs = args
        capturedEvent = onEvent
        capturedTerminated = onTerminated
    }
}

/// U6: minimal `DaemonSessionService` fake that captures the `audio` body flag
/// and returns a configurable start result (incl. a `nil` `audioEcho` to model a
/// stale daemon). The event stream returns immediately so `startViaDaemon` settles
/// without a live long-poll.
@MainActor
final class CapturingDaemonSessionService: DaemonSessionService {
    var startResult: DaemonSession.StartedRecording
    private(set) var capturedAudio: Bool?
    private(set) var startRecordingCalled = false

    init(startResult: DaemonSession.StartedRecording) {
        self.startResult = startResult
    }

    func probe() async -> DaemonSession.ProbeOutcome { .daemon(grants: .allIndeterminate) }
    func snapshot() async -> DaemonSession.SnapshotOutcome { .noActiveSession }

    func startRecording(name: String?, audio: Bool?) async throws -> DaemonSession.StartedRecording {
        startRecordingCalled = true
        capturedAudio = audio
        return startResult
    }

    func stopRecording(force: Bool) async throws {}
    func translateFailure(_ error: Error) -> DaemonSession.FailureOutcome { .other(localizedDescription: "") }
    func reload() async -> Result<Void, DaemonSession.ReloadError> { .success(()) }
    func consumeEventStream(callbacks: DaemonSession.EventStreamCallbacks) async -> DaemonSession.AttachOutcome {
        .shutdown
    }
}

/// U7: records the window-lifecycle calls so controller tests can assert the HUD
/// / main-window effects without touching AppKit.
@MainActor
final class FakeWindowLifecycle: WindowLifecycle {
    private(set) var showHUDCount = 0
    private(set) var hideHUDCount = 0
    private(set) var hideMainWindowCount = 0
    private(set) var restoreMainWindowCount = 0

    func showHUD(for recorder: RecorderController) { showHUDCount += 1 }
    func hideHUD() { hideHUDCount += 1 }
    func hideMainWindow() { hideMainWindowCount += 1 }
    func restoreMainWindow() { restoreMainWindowCount += 1 }
}

/// Fake `StopPolicyCoordinator` that returns a fixed `StopPolicyOutcome`
/// immediately so the orchestrator's runStop branch reaches its
/// `case .timedOut` / `case .completed` arm without waiting on real
/// 30s/300s timeouts.
@MainActor
final class StubbedStopPolicyCoordinator: StopPolicyCoordinator {
    private let outcome: StopPolicyOutcome

    init(outcome: StopPolicyOutcome) {
        self.outcome = outcome
    }

    func resolveFinalized(_ success: Bool) {}
    func resolveStopped(_ success: Bool) {}
    func cancelAll() {}

    func runStop(
        quitting: Bool,
        timeout: TimeInterval?,
        sendStopSignal: () async throws -> Void,
        onTickQuitProgress: @escaping @MainActor (Int) async -> Void
    ) async -> StopPolicyOutcome {
        // Dispatch the stop signal so any test expectations on it fire, but
        // ignore failures — the outcome the orchestrator processes is the
        // one passed into this fake's initializer.
        try? await sendStopSignal()
        return outcome
    }
}
