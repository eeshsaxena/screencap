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

    init(isRunning: Bool, pid: Int32, forceKillReturnValue: Bool) {
        self.isRunning = isRunning
        self.processIdentifier = pid
        self.forceKillReturnValue = forceKillReturnValue
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
