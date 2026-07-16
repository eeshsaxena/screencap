import AVFoundation
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

    /// SCR-263: a menu-bar Start during a post-update helper swap lands in the
    /// same `.cliFallback` + grants-unverifiable state as a genuine missing
    /// grant. Keyed on `updateConverging`, the block copy reads like the window's
    /// "Finishing update…" interstitial instead of the permission-required error
    /// — the two must not contradict each other.
    func testCLIFallbackStartDuringConvergenceShowsFinishingUpdateCopy() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.cliFallback)
        let suiteName = "sc-scr263-start-\(UUID().uuidString.prefix(8))"
        let suite = UserDefaults(suiteName: suiteName)!
        defer { suite.removePersistentDomain(forName: suiteName) }
        recorder._testBeginUpdateConvergence(
            anchor: Date(timeIntervalSince1970: 1_000), defaults: suite
        )

        recorder.start(name: "cli-owned")

        XCTAssertEqual(recorder.state, .idle)
        XCTAssertEqual(
            recorder.lastError,
            UpdateConvergenceCopy.startBlockedDuringConvergence
        )
    }

    /// SCR-263: the New-recording sheet's inline block reason mirrors the same
    /// convergence-aware copy — permission error when settled, finishing-update
    /// stand-in mid-swap — so the sheet can't contradict the interstitial either.
    func testNewRecordingBlockReasonIsConvergenceAware() {
        let permissions = PermissionController()
        permissions._testSetRequiredPermissionsGranted(false)

        let recorder = RecorderController()
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.cliFallback)

        // Settled (not converging): today's permission-required copy is preserved.
        XCTAssertEqual(
            recorder.newRecordingBlockReason(),
            RecorderController.requiredPermissionsErrorMessage
        )

        // Mid-swap: the same unverifiable state now reads as the update stand-in.
        let suiteName = "sc-scr263-reason-\(UUID().uuidString.prefix(8))"
        let suite = UserDefaults(suiteName: suiteName)!
        defer { suite.removePersistentDomain(forName: suiteName) }
        recorder._testBeginUpdateConvergence(
            anchor: Date(timeIntervalSince1970: 1_000), defaults: suite
        )
        XCTAssertEqual(
            recorder.newRecordingBlockReason(),
            UpdateConvergenceCopy.startBlockedDuringConvergence
        )
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
            "Recording stopped, but some data may not have uploaded. Open the recording to retry the upload."
        )
    }

    func testForceStoppedLocalRecordingSurfacesProcessingWarningNotUpload() {
        let recorder = RecorderController()

        recorder._testSetPresentation(state: .stopping(quitting: false))
        // A local recording's finalized event carries destination "local" — the
        // banner must not mention uploads (nothing was uploaded). Exercises the
        // full stderr-JSON decode path (the `destination` CodingKey).
        recorder._testHandleStderrLine(
            #"{"type":"recording_finalized","schema_version":1,"force_stopped":true,"destination":"local"}"#
        )
        recorder._testHandleProcessTerminated(exitCode: 0)

        XCTAssertEqual(
            recorder.lastError,
            "Recording stopped before it finished processing. Open the recording to finish it."
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

    /// The freeze fix: clicking in-app Stop must dismiss the HUD pill
    /// IMMEDIATELY — not leave it frozen on screen until the (up to 60s)
    /// background finalization wait completes — and must conclude staying
    /// backgrounded: the main window is never restored/activated, so focus stays
    /// in whatever app the user moved on to. The recording still routes to
    /// Library so it's there when the user reopens ScreenCap themselves.
    func testInAppStopDismissesHUDImmediatelyAndStaysBackgrounded() async {
        let fake = FakeWindowLifecycle()
        let recorder = RecorderController(
            stopPolicy: StubbedStopPolicyCoordinator(outcome: .completed),
            windowLifecycle: fake
        )
        recorder._testSetTransport(.cliFallback)
        recorder._testSetPresentation(state: .starting)
        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)
        XCTAssertEqual(fake.hideMainWindowCount, 1)  // window really hidden
        XCTAssertEqual(fake.hideHUDCount, 0)

        // The still-hidden window routes to Library for when the user returns.
        let routed = expectation(forNotification: .screenCapRecordingDidEnd, object: nil)

        recorder.stop()

        // Synchronous: `enterStopping` drops the pill the instant Stop is
        // clicked, BEFORE `runStop` awaits finalization. This is the freeze fix —
        // pre-fix, `hideHUDCount` stayed 0 here and only flipped after the wait.
        XCTAssertEqual(fake.hideHUDCount, 1, "the pill must be dismissed immediately, not after finalization")
        XCTAssertEqual(fake.restoreMainWindowCount, 0)

        // Background finalization then settles the controller to `.idle`.
        await waitUntil { recorder.state == .idle }
        await fulfillment(of: [routed], timeout: 1)

        // Stay put: even after the terminal transition, the window is never
        // restored/activated — a clean in-app Stop must not steal focus.
        XCTAssertEqual(fake.restoreMainWindowCount, 0, "a clean in-app Stop must not pull ScreenCap to the foreground")
    }

    /// An in-app Stop whose background finalization outlasts the 60s wait
    /// (`.timedOut`) — routine for a long recording whose chunk scrub + upload
    /// drain takes a while — must NOT leave a stale terminal error banner.
    /// Finalization is a non-terminal, self-resolving condition; the daemon's
    /// later `recording_finalized` refreshes the Library so the per-recording
    /// status chip reflects it, but nothing clears `lastError`. So the pre-fix
    /// `.timedOut` branch's "Stop is still finalizing in the background." write
    /// sat stale on the red error surface until the next recording start.
    func testInAppStopTimeoutDoesNotSurfaceStaleFinalizingBanner() async {
        let recorder = RecorderController(
            stopPolicy: StubbedStopPolicyCoordinator(outcome: .timedOut)
        )
        recorder._testSetTransport(.cliFallback)
        recorder._testSetPresentation(state: .recording(elapsed: 5))

        recorder.stop()

        await waitUntil { recorder.state == .idle }

        XCTAssertNil(
            recorder.lastError,
            "an in-app Stop that finalizes in the background must not leave a stale terminal error banner"
        )
    }

    /// If the in-app Stop signal never dispatches, the recording is still live,
    /// so the rollback must undo the synchronous pill hide: roll back to
    /// `.recording` AND re-float the pill (`.showHUD`). Guards the
    /// `apply(machine.restoreRecordingAfterStopFailure())` callsite added
    /// alongside the immediate-hide fix.
    func testInAppStopSignalFailureRollsBackAndReshowsHUD() async {
        let fake = FakeWindowLifecycle()
        let recorder = RecorderController(
            stopPolicy: StubbedStopPolicyCoordinator(outcome: .sendSignalFailed(NSError(domain: "test", code: 1))),
            windowLifecycle: fake
        )
        recorder._testSetTransport(.cliFallback)
        recorder._testSetPresentation(state: .starting)
        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)
        let showsAfterStart = fake.showHUDCount  // 1, from the `started` event

        recorder.stop()
        XCTAssertEqual(fake.hideHUDCount, 1, "the pill is dropped synchronously on click")

        // The failed send rolls the state machine back to `.recording` and the
        // controller re-applies `.showHUD` so the pill returns for the live recording.
        await waitUntil { if case .recording = recorder.state { return true }; return false }
        XCTAssertEqual(fake.showHUDCount, showsAfterStart + 1, "a failed stop signal must re-float the pill")
        XCTAssertFalse(recorder.state.isStopping)
    }

    // MARK: - HUD hide control

    /// Drive the controller into `.recording` via the real `.starting` →
    /// `started` path (matching the existing HUD tests).
    private func recordingController(_ fake: FakeWindowLifecycle) -> RecorderController {
        let recorder = RecorderController(windowLifecycle: fake)
        recorder._testSetPresentation(state: .starting)
        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)
        return recorder
    }

    /// Hiding the pill orders the panel out, sets `hudHidden`, and leaves the
    /// recording running (R1, R2).
    func testHideRecordingHUDDismissesPillWithoutStopping() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)
        XCTAssertFalse(recorder.hudHidden)

        recorder.hideRecordingHUD()

        XCTAssertTrue(recorder.hudHidden)
        XCTAssertEqual(fake.hideHUDCount, 1)
        XCTAssertTrue(recorder.state.isRecording, "hiding must not stop the recording")
    }

    /// Restoring re-shows the panel and clears `hudHidden` (R4, R5).
    func testShowRecordingHUDRestoresPill() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)
        recorder.hideRecordingHUD()
        let showsBefore = fake.showHUDCount

        recorder.showRecordingHUD()

        XCTAssertFalse(recorder.hudHidden)
        XCTAssertEqual(fake.showHUDCount, showsBefore + 1)
    }

    /// Both methods are no-ops outside `.recording` (guard behavior).
    func testHideShowAreNoOpsWhenNotRecording() {
        let fake = FakeWindowLifecycle()
        let recorder = RecorderController(windowLifecycle: fake)  // stays .idle

        recorder.hideRecordingHUD()
        recorder.showRecordingHUD()

        XCTAssertFalse(recorder.hudHidden)
        XCTAssertEqual(fake.hideHUDCount, 0)
        XCTAssertEqual(fake.showHUDCount, 0)
    }

    /// A teardown to `.idle` after hiding resets `hudHidden` via the `.idle`
    /// chokepoint, so the next recording starts shown (R6).
    func testTeardownAfterHideResetsHudHidden() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)
        recorder.hideRecordingHUD()
        XCTAssertTrue(recorder.hudHidden)

        // A clean process exit tears the recording down to `.idle`.
        recorder._testHandleProcessTerminated(exitCode: 0)

        XCTAssertFalse(recorder.state.isRecording)
        XCTAssertFalse(recorder.hudHidden, "hidden state must not survive a recording (R6)")
    }

    /// A `recording_failed` while hidden resets `hudHidden` and still surfaces
    /// the failure — the AE4 async-failure path, not a transitionToIdle shortcut.
    func testRecordingFailedWhileHiddenResetsHudHidden() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)
        recorder.hideRecordingHUD()

        recorder._testHandleStderrLine(#"{"type":"recording_failed","schema_version":1,"reason":"engine crashed"}"#)

        XCTAssertFalse(recorder.hudHidden, "AE4: next recording starts shown after a hidden-pill failure")
        XCTAssertEqual(recorder.lastError, "engine crashed")
        XCTAssertFalse(recorder.state.isRecording)
    }

    /// Double-hide is safe: user-hide then a teardown that also closes the HUD
    /// does not crash, and the flag ends reset.
    func testDoubleHideIsSafe() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)
        recorder.hideRecordingHUD()  // hideHUDCount == 1

        recorder._testHandleProcessTerminated(exitCode: 0)  // teardown drains .hideHUD too

        XCTAssertFalse(recorder.hudHidden)
        XCTAssertEqual(fake.hideHUDCount, 2, "user-hide + teardown each order the panel out exactly once")
    }

    /// Hiding twice in a row is idempotent — the second call no-ops on the
    /// `!hudHidden` guard rather than ordering the panel out again.
    func testHideRecordingHUDIsIdempotent() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)

        recorder.hideRecordingHUD()
        recorder.hideRecordingHUD()

        XCTAssertTrue(recorder.hudHidden)
        XCTAssertEqual(fake.hideHUDCount, 1, "second hide is a no-op")
        XCTAssertTrue(recorder.state.isRecording)
    }

    /// `showRecordingHUD()` is a no-op while recording but not hidden (the pill
    /// is already showing) — exercises the `hudHidden` arm of its guard.
    func testShowRecordingHUDNoOpWhenNotHidden() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)
        let showsBefore = fake.showHUDCount

        recorder.showRecordingHUD()

        XCTAssertFalse(recorder.hudHidden)
        XCTAssertEqual(fake.showHUDCount, showsBefore, "no-op when already shown")
    }

    // MARK: - HUD hide control v2 (toggle, ⌘⇧H monitor, one-time hint)

    /// Drive into `.recording` with a fake input monitor injected (U2/U3).
    private func recordingControllerWithMonitor(
        _ fake: FakeWindowLifecycle, _ monitor: FakeHUDInputMonitor
    ) -> RecorderController {
        let recorder = RecorderController(windowLifecycle: fake, inputMonitor: monitor)
        recorder._testSetPresentation(state: .starting)
        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)
        return recorder
    }

    /// `toggleRecordingHUD()` hides the pill when it is shown (R1). The ⌘⇧H entry.
    func testToggleHidesWhenShown() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)

        recorder.toggleRecordingHUD()

        XCTAssertTrue(recorder.hudHidden)
        XCTAssertEqual(fake.hideHUDCount, 1)
        XCTAssertTrue(recorder.state.isRecording, "toggling visibility must not stop the recording")
    }

    /// `toggleRecordingHUD()` restores the pill when it is hidden (R1).
    func testToggleShowsWhenHidden() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)
        recorder.hideRecordingHUD()
        let showsBefore = fake.showHUDCount

        recorder.toggleRecordingHUD()

        XCTAssertFalse(recorder.hudHidden)
        XCTAssertEqual(fake.showHUDCount, showsBefore + 1)
    }

    /// A hide→show round-trip via two toggles ends shown, ordering the panel out
    /// exactly once for the hide.
    func testToggleRoundTrip() {
        let fake = FakeWindowLifecycle()
        let recorder = recordingController(fake)

        recorder.toggleRecordingHUD()  // hide
        recorder.toggleRecordingHUD()  // show

        XCTAssertFalse(recorder.hudHidden)
        XCTAssertEqual(fake.hideHUDCount, 1)
    }

    /// `toggleRecordingHUD()` is a no-op outside `.recording` (guard behavior).
    func testToggleNoOpWhenNotRecording() {
        let fake = FakeWindowLifecycle()
        let recorder = RecorderController(windowLifecycle: fake)  // stays .idle

        recorder.toggleRecordingHUD()

        XCTAssertFalse(recorder.hudHidden)
        XCTAssertEqual(fake.hideHUDCount, 0)
        XCTAssertEqual(fake.showHUDCount, 0)
    }

    /// The input monitor starts on the recording-start edge, bound to this
    /// recorder (U2/U3, R2).
    func testInputMonitorStartsOnRecordingStart() {
        let fake = FakeWindowLifecycle()
        let monitor = FakeHUDInputMonitor()
        let recorder = recordingControllerWithMonitor(fake, monitor)

        XCTAssertEqual(monitor.startCount, 1)
        XCTAssertTrue(monitor.isMonitoring)
        XCTAssertTrue(monitor.boundRecorder === recorder)
    }

    /// A normal stop tears the monitor down — ⌘⇧H must not stay registered once the
    /// recording ends (R3).
    func testInputMonitorStopsOnNormalStop() {
        let fake = FakeWindowLifecycle()
        let monitor = FakeHUDInputMonitor()
        let recorder = recordingControllerWithMonitor(fake, monitor)

        recorder._testHandleProcessTerminated(exitCode: 0)

        XCTAssertFalse(recorder.state.isRecording)
        XCTAssertFalse(monitor.isMonitoring, "⌘⇧H must not stay registered after a recording (R3)")
        XCTAssertGreaterThanOrEqual(monitor.stopCount, 1)
    }

    /// An async `recording_failed` also tears the monitor down (R3, abnormal end).
    func testInputMonitorStopsOnRecordingFailed() {
        let fake = FakeWindowLifecycle()
        let monitor = FakeHUDInputMonitor()
        let recorder = recordingControllerWithMonitor(fake, monitor)

        recorder._testHandleStderrLine(#"{"type":"recording_failed","schema_version":1,"reason":"engine crashed"}"#)

        XCTAssertFalse(recorder.state.isRecording)
        XCTAssertFalse(monitor.isMonitoring, "abnormal end must unregister ⌘⇧H (R3)")
    }

    /// The recorder handed to the monitor is the live one, so the ⌘⇧H handler's
    /// `toggleRecordingHUD()` call round-trips visibility.
    func testInputMonitorHotkeyCallbackDrivesToggle() {
        let fake = FakeWindowLifecycle()
        let monitor = FakeHUDInputMonitor()
        let recorder = recordingControllerWithMonitor(fake, monitor)

        monitor.boundRecorder?.toggleRecordingHUD()
        XCTAssertTrue(recorder.hudHidden)
        monitor.boundRecorder?.toggleRecordingHUD()
        XCTAssertFalse(recorder.hudHidden)
    }

    /// The first-ever hide presents the one-time hint; the store flag is set only
    /// after the hint is actually shown (completion fired), and never re-presents
    /// (U5, R7).
    func testFirstHideShowsHintOncePersistedAfterDisplay() {
        let fake = FakeWindowLifecycle()
        let suite = "hud-hint-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = HUDHintStore(defaults: defaults)
        let recorder = RecorderController(windowLifecycle: fake, hintStore: store)
        recorder._testSetPresentation(state: .starting)
        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)

        recorder.hideRecordingHUD()
        XCTAssertEqual(fake.presentHideHintCount, 1, "first-ever hide presents the hint")
        XCTAssertFalse(store.hasShownHideHint, "flag is not set until the hint is actually shown")

        fake.lastHintCompletion?()  // simulate the hint being shown + dismissed
        XCTAssertTrue(store.hasShownHideHint)

        recorder.showRecordingHUD()
        recorder.hideRecordingHUD()
        XCTAssertEqual(fake.presentHideHintCount, 1, "the hint is one-time — no second present")
    }

    /// The one-time hint is torn down when the recording ends (via the `.hideHUD`
    /// teardown), so it never outlives the recording with now-false "Still
    /// recording" copy (review fix).
    func testHintDismissedWhenRecordingEnds() {
        let fake = FakeWindowLifecycle()
        let suite = "hud-hint-teardown-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let recorder = RecorderController(windowLifecycle: fake, hintStore: HUDHintStore(defaults: defaults))
        recorder._testSetPresentation(state: .starting)
        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)
        recorder.hideRecordingHUD()
        XCTAssertEqual(fake.presentHideHintCount, 1)

        recorder._testHandleProcessTerminated(exitCode: 0)

        XCTAssertFalse(recorder.state.isRecording)
        XCTAssertGreaterThanOrEqual(fake.dismissHideHintCount, 1, "hint must be dismissed on recording end")
    }

    /// An abnormal end (recording_failed → transitionToIdle) also dismisses the hint.
    func testHintDismissedOnAbnormalEnd() {
        let fake = FakeWindowLifecycle()
        let suite = "hud-hint-teardown-abn-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let recorder = RecorderController(windowLifecycle: fake, hintStore: HUDHintStore(defaults: defaults))
        recorder._testSetPresentation(state: .starting)
        recorder._testHandleStderrLine(#"{"type":"started","schema_version":1,"cursor":1,"ts":1.0}"#)
        recorder.hideRecordingHUD()

        recorder._testHandleStderrLine(#"{"type":"recording_failed","schema_version":1,"reason":"engine crashed"}"#)

        XCTAssertFalse(recorder.state.isRecording)
        XCTAssertGreaterThanOrEqual(fake.dismissHideHintCount, 1, "hint must be dismissed on abnormal end too")
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

    // MARK: - Mid-recording mic mute (SCR-254 U7)

    /// `toggleMute()` from an unmuted recording arms `muteInFlight` and dispatches
    /// the mute verb, but `muted` stays false until the confirming event — never
    /// flipped from the request/echo (KTD4).
    func testToggleMuteSetsInFlightAndSendsRequestWithoutFlippingMuted() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        let recorder = RecorderController(daemonService: daemon)
        recorder._testSetTransport(.daemon)
        recorder._testSetPresentation(state: .recording(elapsed: 5))

        recorder.toggleMute()

        XCTAssertTrue(recorder.muteInFlight, "the mute path arms the pending flag synchronously")
        XCTAssertFalse(recorder.muted, "muted must not flip from the request")
        await waitUntil { daemon.setMutedCalled }
        XCTAssertEqual(daemon.capturedMuted, true, "an unmuted recording mutes")
        XCTAssertFalse(recorder.muted, "still not flipped after the verb returns (echo ignored)")
        XCTAssertTrue(recorder.muteInFlight, "stays in-flight until the confirming event")
    }

    /// The confirmed `audio_muted` event flips `muted` and clears `muteInFlight`.
    func testAudioMutedEventFlipsMutedAndClearsInFlight() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        let recorder = RecorderController(daemonService: daemon)
        recorder._testSetTransport(.daemon)
        recorder._testSetPresentation(state: .recording(elapsed: 5))
        recorder.toggleMute()
        await waitUntil { daemon.setMutedCalled }
        XCTAssertTrue(recorder.muteInFlight)

        recorder._testHandleStderrLine(#"{"type":"audio_muted","schema_version":1,"muted":true}"#)

        XCTAssertTrue(recorder.muted, "the confirmed event flips muted")
        XCTAssertFalse(recorder.muteInFlight, "and clears the pending flag")
    }

    /// A mute-verb failure must NOT tear down the recording (contrast
    /// `handleDaemonOperationFailure`, which idles). It reverts to the prior
    /// confirmed `muted`, clears the pending flag, and surfaces a non-terminal
    /// advisory — no `lastError`, still recording.
    func testMuteVerbFailureKeepsRecordingRevertsAndSetsAdvisory() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        daemon.setMutedError = DaemonClientError.socketUnavailable(path: "/tmp/x.sock")
        let recorder = RecorderController(daemonService: daemon)
        recorder._testSetTransport(.daemon)
        recorder._testSetPresentation(state: .recording(elapsed: 5))

        recorder.toggleMute()

        await waitUntil { recorder.captureAdvisory != nil }
        XCTAssertTrue(recorder.state.isRecording, "a mute-verb failure must not end the recording")
        XCTAssertFalse(recorder.muted, "muted reverts to its prior confirmed value")
        XCTAssertFalse(recorder.muteInFlight, "the pending flag is cleared on failure")
        XCTAssertEqual(recorder.captureAdvisory, RecorderController.muteRequestFailedAdvisory)
        XCTAssertNil(recorder.lastError, "the failure is advisory, not terminal")
    }

    /// Mute is daemon-only (KTD1): on the CLI-fallback transport `toggleMute()` is
    /// a no-op — no verb dispatched, no in-flight state.
    func testToggleMuteBlockedOnCLIFallbackTransport() {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        let recorder = RecorderController(daemonService: daemon)
        recorder._testSetTransport(.cliFallback)
        recorder._testSetPresentation(state: .recording(elapsed: 5))

        recorder.toggleMute()

        XCTAssertFalse(daemon.setMutedCalled, "CLI-fallback has no live control channel")
        XCTAssertFalse(recorder.muteInFlight)
        XCTAssertFalse(recorder.muted)
    }

    /// Attaching to a muted daemon recording hydrates `muted` from the snapshot
    /// overlay so the surfaces show the correct state without a re-toggle (AE4).
    func testSnapshotMutedTrueHydratesMutedOnAttach() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        daemon.snapshotResult = .daemonOwnedSession(startedAt: Date(), muted: true)
        let recorder = RecorderController(daemonService: daemon)

        await recorder.probeDaemon()

        XCTAssertTrue(recorder.muted, "snapshot muted:true hydrates muted on attach")
        await recorder._testCancelDaemonTask()
    }

    /// A snapshot without the additive `muted` field (mapped to false by the
    /// service) leaves the recording unmuted — the stale-daemon back-compat rule.
    func testSnapshotWithoutMutedFieldLeavesUnmuted() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        daemon.snapshotResult = .daemonOwnedSession(startedAt: Date(), muted: false)
        let recorder = RecorderController(daemonService: daemon)

        await recorder.probeDaemon()

        XCTAssertFalse(recorder.muted, "a missing muted field defaults to unmuted")
        await recorder._testCancelDaemonTask()
    }

    // MARK: - Unmute permission UX (SCR-254 U9)

    /// A muted recording + a live mic-mute state; used to drive the unmute branch.
    private func makeMutedDaemonRecording(
        mic: MicAuthorizing,
        alerts: RecorderAlertPresenter = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateCancel),
        daemon: CapturingDaemonSessionService
    ) -> RecorderController {
        let recorder = RecorderController(
            alertPresenter: alerts, daemonService: daemon, micAuthorizer: mic
        )
        recorder._testSetTransport(.daemon)
        recorder._testSetPresentation(state: .recording(elapsed: 5))
        // Confirmed muted, so the next toggle is an UNMUTE that hits the U9 gate.
        recorder._testHandleStderrLine(#"{"type":"audio_muted","schema_version":1,"muted":true}"#)
        return recorder
    }

    /// Unmute with the mic already authorized sends `setMuted(false)` (AE2 grant).
    func testUnmuteWithAuthorizedMicSendsVerb() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        let recorder = makeMutedDaemonRecording(
            mic: FakeMicAuthorizer(status: .authorized), daemon: daemon
        )

        recorder.toggleMute()

        await waitUntil { daemon.setMutedCalled }
        XCTAssertEqual(daemon.capturedMuted, false, "authorized unmute sends setMuted(false)")
    }

    /// Undetermined mic prompts, and on grant sends the unmute (AE2).
    func testUnmuteUndeterminedPromptsThenSendsOnGrant() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        let mic = FakeMicAuthorizer(status: .notDetermined, requestAccessResult: true)
        let recorder = makeMutedDaemonRecording(mic: mic, daemon: daemon)

        recorder.toggleMute()

        await waitUntil { mic.requestAccessCalled && daemon.setMutedCalled }
        XCTAssertEqual(daemon.capturedMuted, false, "unmute is sent once the prompt is granted")
    }

    /// Undetermined mic prompt DENIED: stay muted, do not send the verb, surface a
    /// visible denial (AE3, R3 — never silent).
    func testUnmuteUndeterminedDeniedStaysMutedAndSurfacesDenial() async {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        let mic = FakeMicAuthorizer(status: .notDetermined, requestAccessResult: false)
        let alerts = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateCancel)
        let recorder = makeMutedDaemonRecording(mic: mic, alerts: alerts, daemon: daemon)

        recorder.toggleMute()

        await waitUntil { alerts.microphoneAccessDeniedPresentedCount == 1 }
        XCTAssertFalse(daemon.setMutedCalled, "a denied unmute must not send the verb")
        XCTAssertTrue(recorder.muted, "stays muted after a denied unmute")
        XCTAssertNotNil(recorder.captureAdvisory, "the denial is surfaced, never silent")
    }

    /// Denied status from the MENU BAR routes to the modal (the user isn't looking
    /// at the pill), stays muted, and never dispatches the verb (AE3, R3).
    func testUnmuteDeniedFromMenuBarUsesModalAndStaysMuted() {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: true)
        )
        let mic = FakeMicAuthorizer(status: .denied)
        let alerts = FakeRecorderAlertPresenter(stopAndQuitReply: .terminateCancel)
        let recorder = makeMutedDaemonRecording(mic: mic, alerts: alerts, daemon: daemon)

        // `.denied` resolves synchronously (no requestAccess), so the modal path
        // fires on the call — no await needed.
        recorder.toggleMute(source: .menuBar)

        XCTAssertEqual(alerts.microphoneAccessDeniedPresentedCount, 1, "menu-bar denial uses the modal")
        XCTAssertFalse(daemon.setMutedCalled, "denied unmute never dispatches the verb")
        XCTAssertTrue(recorder.muted, "stays muted")
        XCTAssertEqual(recorder.captureAdvisory, RecorderController.microphoneAccessDeniedAdvisory)
    }

    // MARK: - Stale daemon grant probe (defeat launch-time TCC pin)

    /// The core regression: a required grant reads denied while a permission
    /// surface is up, we are not recording, and the daemon transport is live, so
    /// a fresh daemon must be kicked to escape the launch-time-pinned grant probe.
    /// The first restart is unconditional (no prior restart to rate-limit).
    func testShouldRestartStaleDaemonWhenRequiredGrantDenied() {
        XCTAssertTrue(RecorderController.shouldRestartStaleDaemon(
            anyRequiredDenied: true,
            isRecording: false,
            transportIsDaemon: true,
            secondsSinceLastRestart: nil,
            cooldown: RecorderController.staleDaemonRestartCooldown
        ))
    }

    /// Never kickstart the daemon during a recording — the restart kills capture.
    func testShouldNotRestartStaleDaemonWhileRecording() {
        XCTAssertFalse(RecorderController.shouldRestartStaleDaemon(
            anyRequiredDenied: true,
            isRecording: true,
            transportIsDaemon: true,
            secondsSinceLastRestart: nil,
            cooldown: RecorderController.staleDaemonRestartCooldown
        ))
    }

    /// Only the daemon transport suffers the stale grant probe; on CLI-fallback
    /// the install flow owns bring-up, so a stale-grant restart must not fire.
    func testShouldNotRestartStaleDaemonOnCLIFallback() {
        XCTAssertFalse(RecorderController.shouldRestartStaleDaemon(
            anyRequiredDenied: true,
            isRecording: false,
            transportIsDaemon: false,
            secondsSinceLastRestart: nil,
            cooldown: RecorderController.staleDaemonRestartCooldown
        ))
    }

    /// Nothing to defeat once every required grant reads granted.
    func testShouldNotRestartStaleDaemonWhenAllGranted() {
        XCTAssertFalse(RecorderController.shouldRestartStaleDaemon(
            anyRequiredDenied: false,
            isRecording: false,
            transportIsDaemon: true,
            secondsSinceLastRestart: nil,
            cooldown: RecorderController.staleDaemonRestartCooldown
        ))
    }

    /// Rate limit: a coincident activation + timer tick must not double-restart
    /// within the cooldown, but a later tick past it may restart again.
    func testStaleDaemonRestartIsRateLimitedByCooldown() {
        XCTAssertFalse(RecorderController.shouldRestartStaleDaemon(
            anyRequiredDenied: true,
            isRecording: false,
            transportIsDaemon: true,
            secondsSinceLastRestart: 3,
            cooldown: 8
        ))
        XCTAssertTrue(RecorderController.shouldRestartStaleDaemon(
            anyRequiredDenied: true,
            isRecording: false,
            transportIsDaemon: true,
            secondsSinceLastRestart: 9,
            cooldown: 8
        ))
    }

    /// End-to-end wiring: a denied daemon grant snapshot drives exactly one
    /// kickstart, and the post-restart re-read surfaces the now-live grant into
    /// PermissionController — the fix for the "granted but still waiting" strand.
    func testDefeatingStalenessRestartsDaemonAndPicksUpLiveGrant() async {
        let denied = DaemonPermissionGrants(
            screenRecording: .denied, accessibility: .denied, inputMonitoring: .denied
        )
        let granted = DaemonPermissionGrants(
            screenRecording: .granted, accessibility: .granted, inputMonitoring: .granted
        )
        let daemon = StaleGrantDaemonSessionService(grants: denied)
        daemon.grantsAfterReload = granted

        let suite = "StaleDaemonRestartTests"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        let permissions = PermissionController(defaults: defaults)

        let recorder = RecorderController(daemonService: daemon)
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)
        permissions.updateDaemonGrants(denied)

        await recorder.refreshDaemonGrantsDefeatingStaleness()

        XCTAssertEqual(daemon.reloadCount, 1, "a stale denied grant must trigger exactly one kickstart")
        XCTAssertTrue(
            permissions.daemonGrants.allRequiredGranted,
            "the post-restart re-read must surface the live grant"
        )
    }

    /// End-to-end rate limit: with grants that stay denied (never granted), the
    /// wired path restarts once, suppresses a second restart inside the cooldown,
    /// and restarts again past it — pinning `lastDaemonRestartAt` gating through
    /// the async method, not just the pure `shouldRestartStaleDaemon`.
    func testDefeatingStalenessRateLimitsRestartsAcrossCooldown() async {
        let denied = DaemonPermissionGrants(
            screenRecording: .denied, accessibility: .denied, inputMonitoring: .denied
        )
        // No grantsAfterReload: grants stay denied so only the cooldown — not a
        // flip to granted — can suppress the second restart.
        let daemon = StaleGrantDaemonSessionService(grants: denied)

        let suite = "StaleDaemonRestartCooldownTests"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        let permissions = PermissionController(defaults: defaults)

        let recorder = RecorderController(daemonService: daemon)
        recorder.bindPermissions(permissions)
        recorder._testSetTransport(.daemon)
        permissions.updateDaemonGrants(denied)

        let t0 = Date(timeIntervalSince1970: 1_000_000)
        await recorder.refreshDaemonGrantsDefeatingStaleness(now: t0)
        XCTAssertEqual(daemon.reloadCount, 1, "first stale refresh restarts")

        await recorder.refreshDaemonGrantsDefeatingStaleness(now: t0.addingTimeInterval(3))
        XCTAssertEqual(daemon.reloadCount, 1, "a refresh within the cooldown must not restart again")

        await recorder.refreshDaemonGrantsDefeatingStaleness(
            now: t0.addingTimeInterval(RecorderController.staleDaemonRestartCooldown + 1)
        )
        XCTAssertEqual(daemon.reloadCount, 2, "a refresh past the cooldown restarts again")
    }

    /// While a staleness-defeating kickstart is in flight, a start() must refuse
    /// rather than dispatch to a daemon that is mid-relaunch — it surfaces a clear
    /// transient reason and does NOT enter .starting or reach the daemon.
    func testStartRefusesWhileDefeatingStalenessKickstartInFlight() {
        let daemon = CapturingDaemonSessionService(
            startResult: .init(cursor: 0, sessionID: "x", audioEcho: nil)
        )
        let recorder = RecorderController(daemonService: daemon)
        recorder._testSetTransport(.daemon)
        recorder._testSetDefeatingStaleness(true)

        recorder.start(name: "x", audio: nil)

        XCTAssertFalse(recorder.state.isRecording, "start must not enter .starting during a kickstart")
        XCTAssertFalse(daemon.startRecordingCalled, "start must not reach a daemon that is mid-relaunch")
        XCTAssertNotNil(recorder.lastError, "start must surface a clear transient reason")
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
    /// SCR-254 U7: the snapshot outcome `probeDaemon`'s `syncDaemonSnapshot` reads,
    /// so a test can drive the mute-hydration-on-attach path.
    var snapshotResult: DaemonSession.SnapshotOutcome = .noActiveSession
    /// SCR-254 U7: when set, `setMuted` throws it, modelling a mute-verb failure.
    var setMutedError: Error?
    private(set) var capturedAudio: Bool?
    private(set) var startRecordingCalled = false
    private(set) var setMutedCalled = false
    private(set) var capturedMuted: Bool?
    private(set) var setMutedCallCount = 0

    init(startResult: DaemonSession.StartedRecording) {
        self.startResult = startResult
    }

    func probe() async -> DaemonSession.ProbeOutcome { .daemon(grants: .allIndeterminate) }
    func snapshot() async -> DaemonSession.SnapshotOutcome { snapshotResult }

    func startRecording(name: String?, audio: Bool?) async throws -> DaemonSession.StartedRecording {
        startRecordingCalled = true
        capturedAudio = audio
        return startResult
    }

    func stopRecording(force: Bool) async throws {}

    func setMuted(_ muted: Bool) async throws -> Bool {
        setMutedCalled = true
        setMutedCallCount += 1
        capturedMuted = muted
        if let setMutedError { throw setMutedError }
        // Echo the requested state, as the daemon does.
        return muted
    }

    func translateFailure(_ error: Error) -> DaemonSession.FailureOutcome { .other(localizedDescription: "") }
    func reload() async -> Result<Void, DaemonSession.ReloadError> { .success(()) }
    func consumeEventStream(callbacks: DaemonSession.EventStreamCallbacks) async -> DaemonSession.AttachOutcome {
        .shutdown
    }
}

/// Fake daemon service whose grant snapshot flips only on `reload()`, modelling
/// the launch-time-pinned grant probe that a fresh daemon defeats: every `probe`
/// reports `grants` unchanged until a kickstart swaps in `grantsAfterReload`.
/// Counts restarts so the staleness-defeating refresh can be pinned end-to-end.
@MainActor
private final class StaleGrantDaemonSessionService: DaemonSessionService {
    var grants: DaemonPermissionGrants
    /// Grants the daemon reports once restarted (the live state a fresh process reads).
    var grantsAfterReload: DaemonPermissionGrants?
    private(set) var reloadCount = 0

    init(grants: DaemonPermissionGrants) { self.grants = grants }

    func probe() async -> DaemonSession.ProbeOutcome { .daemon(grants: grants) }
    func snapshot() async -> DaemonSession.SnapshotOutcome { .noActiveSession }
    func startRecording(name: String?, audio: Bool?) async throws -> DaemonSession.StartedRecording {
        DaemonSession.StartedRecording(cursor: 0, sessionID: "stale", audioEcho: nil)
    }
    func stopRecording(force: Bool) async throws {}
    func setMuted(_ muted: Bool) async throws -> Bool { muted }
    func translateFailure(_ error: Error) -> DaemonSession.FailureOutcome { .other(localizedDescription: "") }
    func reload() async -> Result<Void, DaemonSession.ReloadError> {
        reloadCount += 1
        if let grantsAfterReload { grants = grantsAfterReload }
        return .success(())
    }
    func consumeEventStream(callbacks: DaemonSession.EventStreamCallbacks) async -> DaemonSession.AttachOutcome {
        .shutdown
    }
}

/// SCR-254 U9: drives the unmute permission gate across the authorized /
/// undetermined / denied branches without a live TCC subject. `requestAccess`
/// returns the configured grant and records that it was invoked.
@MainActor
final class FakeMicAuthorizer: MicAuthorizing {
    var status: AVAuthorizationStatus
    var requestAccessResult: Bool
    private(set) var requestAccessCalled = false

    init(status: AVAuthorizationStatus, requestAccessResult: Bool = false) {
        self.status = status
        self.requestAccessResult = requestAccessResult
    }

    func authorizationStatus() -> AVAuthorizationStatus { status }

    func requestAccess() async -> Bool {
        requestAccessCalled = true
        return requestAccessResult
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
    private(set) var presentHideHintCount = 0
    private(set) var dismissHideHintCount = 0
    /// Captured, not auto-invoked, so a test can simulate the hint being shown +
    /// dismissed — the store flag must only be set after the hint actually shows.
    private(set) var lastHintCompletion: (() -> Void)?

    func showHUD(for recorder: RecorderController) { showHUDCount += 1 }
    func hideHUD() { hideHUDCount += 1 }
    func hideMainWindow() { hideMainWindowCount += 1 }
    func restoreMainWindow() { restoreMainWindowCount += 1 }
    func presentHideHint(onComplete: @escaping () -> Void) {
        presentHideHintCount += 1
        lastHintCompletion = onComplete
    }
    func dismissHideHint() { dismissHideHintCount += 1 }
}

/// U2/U3: records the input-monitor lifecycle so controller tests can assert the
/// ⌘⇧H hotkey + peek detector start on the recording-start edge and stop on every
/// teardown, without registering a real Carbon hotkey or spawning a poll timer.
@MainActor
final class FakeHUDInputMonitor: HUDInputMonitor {
    private(set) var startCount = 0
    private(set) var stopCount = 0
    private(set) var isMonitoring = false
    private(set) weak var boundRecorder: RecorderController?

    func startMonitoring(for recorder: RecorderController) {
        startCount += 1
        isMonitoring = true
        boundRecorder = recorder
    }

    func stopMonitoring() {
        stopCount += 1
        isMonitoring = false
    }
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
