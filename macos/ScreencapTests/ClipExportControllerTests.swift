import XCTest
@testable import Screencap

/// Fake `ClipExportService` that records the start invocation, hands the
/// callbacks back to the test, and lets the test drive stderr lines +
/// termination at its own pace. Mirrors `FakeUploadService`; reuses the shared
/// `FakeSpawnedProcessHandle` (defined in `RecorderControllerTests`, same test
/// target).
@MainActor
final class FakeClipExportService: ClipExportService {
    struct StartCall: Equatable {
        let name: String
        let range: ClipRange
        let outPath: String
    }

    private(set) var startCalls: [StartCall] = []
    var pendingError: Error?
    var fakeProcess = FakeSpawnedProcessHandle(isRunning: true, pid: 31337, forceKillReturnValue: true)
    private var onLine: ((String) -> Void)?
    private var onTerminated: ((Int32) -> Void)?

    func start(
        name: String,
        range: ClipRange,
        outPath: String,
        onLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle {
        startCalls.append(.init(name: name, range: range, outPath: outPath))
        if let err = pendingError { throw err }
        self.onLine = onLine
        self.onTerminated = onTerminated
        return fakeProcess
    }

    func emit(_ line: String) { onLine?(line) }
    func terminate(exitCode: Int32) {
        fakeProcess.isRunning = false
        onTerminated?(exitCode)
    }
}

enum FakeClipExportServiceError: Error, LocalizedError {
    case launchFailed
    var errorDescription: String? { "launch failed" }
}

@MainActor
final class ClipExportControllerTests: XCTestCase {
    private let range = ClipRange(startMs: 1_700_000_001_000, endMs: 1_700_000_004_000)

    private func makeController(
        service: ClipExportService,
        inactivityTimeoutSeconds: Double = ClipExportController.defaultInactivityTimeoutSeconds
    ) -> ClipExportController {
        ClipExportController(service: service, inactivityTimeoutSeconds: inactivityTimeoutSeconds)
    }

    // MARK: - argv construction (the epoch-ms pass-through contract)

    /// KTD5/KTD6: `range.startMs` / `range.endMs` (absolute unix-epoch ms) are
    /// passed straight through as `--start-ms` / `--end-ms`, `--json` is explicit,
    /// and the trailing `--` protects a name beginning with `--`. This pins the
    /// load-bearing "pass the ClipRange ms directly" contract.
    func testClipArgsPassesEpochMsDirectlyWithTrailingSeparator() {
        XCTAssertEqual(
            LiveClipExportService.clipArgs(
                name: "rec-001", range: range, outPath: "/tmp/out.mp4"),
            [
                "clip",
                "--start-ms", "1700000001000",
                "--end-ms", "1700000004000",
                "--out", "/tmp/out.mp4",
                "--lock-timeout", "30",
                "--json",
                "--", "rec-001",
            ]
        )
    }

    /// KTD6: the interactive `--lock-timeout` must stay strictly under the
    /// inactivity watchdog, so a contended eviction lock surfaces as retryable
    /// `clip_busy` before the watchdog SIGTERMs the child. Mirrors the upload
    /// invariant.
    func testInteractiveLockTimeoutIsUnderTheWatchdog() {
        XCTAssertLessThan(
            Double(LiveClipExportService.interactiveLockTimeoutSeconds),
            ClipExportController.defaultInactivityTimeoutSeconds,
            "clip lock timeout must stay under the inactivity watchdog"
        )
    }

    // MARK: - ClipEventLine parsing

    func testEventLineDecodesSnakeCaseFields() {
        let done = ClipEventLine.parse(
            stderrLine: #"{"type":"clip_progress","ts":1.5,"schema_version":1,"recording":"r","frames_done":12,"frames_total":40}"#)
        XCTAssertEqual(done?.type, "clip_progress")
        XCTAssertEqual(done?.framesDone, 12)
        XCTAssertEqual(done?.framesTotal, 40)

        let failed = ClipEventLine.parse(
            stderrLine: #"{"type":"clip_failed","schema_version":1,"reason":"clip_busy","retryable":true}"#)
        XCTAssertEqual(failed?.reason, "clip_busy")
        XCTAssertEqual(failed?.retryable, true)
    }

    func testEventLineReturnsNilOnNonJSON() {
        XCTAssertNil(ClipEventLine.parse(stderrLine: "Encoding clip 12/40…"))
        XCTAssertNil(ClipEventLine.parse(stderrLine: ""))
        XCTAssertNil(ClipEventLine.parse(stderrLine: "   "))
    }

    // MARK: - start / event → state

    func testStartTransitionsIdleToExportingIndeterminate() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")

        XCTAssertEqual(service.startCalls, [
            .init(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        ])
        guard case .exporting(let progress) = controller.state else {
            return XCTFail("expected exporting, got \(controller.state)")
        }
        XCTAssertEqual(progress.framesTotal, 0, "no counts yet → indeterminate")
        XCTAssertEqual(progress.fraction, 0)
    }

    func testClipStartedKeepsExportingState() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_started","schema_version":1,"recording":"rec-001","start_ms":1,"end_ms":2}"#)

        if case .exporting = controller.state {} else {
            XCTFail("clip_started must keep exporting, got \(controller.state)")
        }
    }

    /// clip_progress drives a DETERMINATE state whose fraction tracks
    /// frames_done / frames_total (R10).
    func testClipProgressDrivesDeterminateFraction() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_started","schema_version":1}"#)
        service.emit(#"{"type":"clip_progress","schema_version":1,"frames_done":10,"frames_total":40}"#)

        guard case .exporting(let progress) = controller.state else {
            return XCTFail("expected exporting, got \(controller.state)")
        }
        XCTAssertEqual(progress.framesDone, 10)
        XCTAssertEqual(progress.framesTotal, 40)
        XCTAssertEqual(progress.fraction, 0.25, accuracy: 0.001)
    }

    /// Happy path: clip_started → clip_progress×N → clip_done → succeeded(path).
    func testHappyPathDrivesToSucceededWithPath() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_started","schema_version":1}"#)
        service.emit(#"{"type":"clip_progress","schema_version":1,"frames_done":20,"frames_total":40}"#)
        service.emit(#"{"type":"clip_progress","schema_version":1,"frames_done":40,"frames_total":40}"#)
        service.emit(#"{"type":"clip_done","schema_version":1,"path":"/tmp/out.mp4"}"#)
        service.terminate(exitCode: 0)

        if case .succeeded(let path) = controller.state {
            XCTAssertEqual(path, "/tmp/out.mp4")
        } else {
            XCTFail("expected succeeded, got \(controller.state)")
        }
    }

    /// clip_done without a `path` falls back to the chosen out path.
    func testClipDoneWithoutPathFallsBackToChosenOut() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/chosen.mp4")
        service.emit(#"{"type":"clip_done","schema_version":1}"#)

        if case .succeeded(let path) = controller.state {
            XCTAssertEqual(path, "/tmp/chosen.mp4")
        } else {
            XCTFail("expected succeeded, got \(controller.state)")
        }
    }

    // MARK: - reason → typed / retryable state mapping

    /// clip_busy is the ONLY retryable reason (KTD6); its typed state carries
    /// `retryable = true`.
    func testClipBusyMapsToRetryableFailure() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_failed","schema_version":1,"reason":"clip_busy","retryable":true}"#)
        // The child exits 0 after a busy skip; the terminationHandler must defer.
        service.terminate(exitCode: 0)

        guard case .failed(let failure) = controller.state else {
            return XCTFail("expected failed, got \(controller.state)")
        }
        XCTAssertEqual(failure.reason, .clipBusy)
        XCTAssertTrue(failure.retryable, "clip_busy is retryable")
        XCTAssertFalse(failure.message.isEmpty)
    }

    /// A flag-ON recording fails closed (AE3) with a NON-retryable typed reason.
    func testMaskedVideoRequiredMapsToNonRetryableFailure() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_failed","schema_version":1,"reason":"masked_video_required","retryable":false}"#)

        guard case .failed(let failure) = controller.state else {
            return XCTFail("expected failed, got \(controller.state)")
        }
        XCTAssertEqual(failure.reason, .maskedVideoRequired)
        XCTAssertFalse(failure.retryable)
    }

    func testNoFramesInRangeMapsToNonRetryableFailure() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_failed","schema_version":1,"reason":"no_frames_in_range","retryable":false}"#)

        guard case .failed(let failure) = controller.state else {
            return XCTFail("expected failed, got \(controller.state)")
        }
        XCTAssertEqual(failure.reason, .noFramesInRange)
        XCTAssertFalse(failure.retryable)
    }

    /// An unrecognized reason maps to `.unknown` rather than crashing the decoder.
    func testUnknownReasonMapsToUnknownFailure() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_failed","schema_version":1,"reason":"some_future_reason"}"#)

        guard case .failed(let failure) = controller.state else {
            return XCTFail("expected failed, got \(controller.state)")
        }
        XCTAssertEqual(failure.reason, .unknown)
        XCTAssertFalse(failure.retryable, "an unknown reason without retryable defaults to non-retryable")
    }

    /// When `clip_failed` omits `retryable`, the reason-derived default is used
    /// (only clip_busy retryable) — an older CLI can't accidentally read as
    /// retryable.
    func testRetryableDefaultsFromReasonWhenFieldAbsent() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_failed","schema_version":1,"reason":"clip_busy"}"#)

        guard case .failed(let failure) = controller.state else {
            return XCTFail("expected failed, got \(controller.state)")
        }
        XCTAssertTrue(failure.retryable, "clip_busy without explicit retryable still defaults retryable")
    }

    // MARK: - cancel

    /// Cancel mid-export SIGTERMs the child and lands `.cancelled`. U1's atomic
    /// write means no partial/delivered file (asserted by the engine tests).
    func testCancelTerminatesProcessAndLandsCancelled() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_progress","schema_version":1,"frames_done":5,"frames_total":40}"#)

        controller.cancel()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 1, "cancel must SIGTERM the child")
        if case .cancelled = controller.state {} else {
            XCTFail("expected cancelled, got \(controller.state)")
        }
    }

    /// A late clip_done arriving after cancel must NOT resurrect success
    /// (first-write-wins via the terminal latch).
    func testLateDoneAfterCancelDoesNotResurrectSuccess() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        controller.cancel()
        service.emit(#"{"type":"clip_done","schema_version":1,"path":"/tmp/out.mp4"}"#)
        service.terminate(exitCode: 130)

        if case .cancelled = controller.state {} else {
            XCTFail("cancel must win over a late clip_done, got \(controller.state)")
        }
    }

    func testCancelOnIdleIsSafeNoOp() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.cancel()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 0)
        if case .idle = controller.state {} else {
            XCTFail("expected idle, got \(controller.state)")
        }
    }

    // MARK: - termination / contract violations

    /// The CLI always exits 0 with a terminal event; a bare non-zero exit with no
    /// event is a contract violation surfaced as a typed `.unknown` failure rather
    /// than a wedged `.exporting`.
    func testExitWithoutTerminalEventTransitionsToFailed() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_started","schema_version":1}"#)
        service.terminate(exitCode: 1)

        guard case .failed(let failure) = controller.state else {
            return XCTFail("expected failed, got \(controller.state)")
        }
        XCTAssertEqual(failure.reason, .unknown)
        XCTAssertTrue(failure.message.contains("code 1"), "got: \(failure.message)")
    }

    /// First-write-wins: a contradictory clip_failed after clip_done must not
    /// flip the settled success.
    func testSecondTerminalEventIsIgnored() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_done","schema_version":1,"path":"/tmp/out.mp4"}"#)
        service.emit(#"{"type":"clip_failed","schema_version":1,"reason":"trim_failed"}"#)

        if case .succeeded = controller.state {} else {
            XCTFail("first terminal must win, got \(controller.state)")
        }
    }

    func testNonJSONLineDoesNotChangeState() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        let before = controller.state
        service.emit("Encoding 12/40…")
        XCTAssertEqual(controller.state, before)
    }

    func testUnknownEventTypeDoesNotChangeState() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_started","schema_version":1}"#)
        let before = controller.state
        service.emit(#"{"type":"clip_future_event","schema_version":1,"foo":1}"#)
        XCTAssertEqual(controller.state, before)
    }

    /// Spawn failure (binary not found / launch error) maps to a terminal
    /// `.failed` immediately, so the modal doesn't hang on a process that never
    /// started.
    func testSpawnFailureLandsOnFailed() {
        let service = FakeClipExportService()
        service.pendingError = FakeClipExportServiceError.launchFailed
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")

        guard case .failed(let failure) = controller.state else {
            return XCTFail("expected failed, got \(controller.state)")
        }
        XCTAssertEqual(failure.message, "launch failed")
        XCTAssertFalse(failure.retryable)
    }

    /// Retry after a retryable failure re-spawns (the modal's "Try Again" path).
    func testRetryAfterFailureReSpawns() {
        let service = FakeClipExportService()
        let controller = makeController(service: service)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        service.emit(#"{"type":"clip_failed","schema_version":1,"reason":"clip_busy","retryable":true}"#)
        service.terminate(exitCode: 0)
        service.fakeProcess = FakeSpawnedProcessHandle(isRunning: true, pid: 99, forceKillReturnValue: true)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")

        XCTAssertEqual(service.startCalls.count, 2, "retry must re-spawn")
        if case .exporting = controller.state {} else {
            XCTFail("retry must return to exporting, got \(controller.state)")
        }
    }

    // MARK: - watchdog

    /// No events after start (past a short bound) → the watchdog SIGTERMs the
    /// child and surfaces a typed failure, so a wedged encode can't hang forever.
    func testInactivityTimeoutTransitionsToFailedAndTerminates() async {
        let service = FakeClipExportService()
        let controller = makeController(service: service, inactivityTimeoutSeconds: 0.05)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        try? await Task.sleep(nanoseconds: 200_000_000)

        guard case .failed(let failure) = controller.state else {
            return XCTFail("expected timeout failure, got \(controller.state)")
        }
        XCTAssertEqual(failure.reason, .trimFailed)
        XCTAssertGreaterThanOrEqual(
            service.fakeProcess.terminateInvocations, 1, "child must be SIGTERMed on timeout")
    }

    /// The watchdog resets on every parsed event, so a progressing encode never
    /// trips it.
    func testWatchdogResetsOnEachProgressEvent() async {
        let service = FakeClipExportService()
        let controller = makeController(service: service, inactivityTimeoutSeconds: 0.1)

        controller.start(name: "rec-001", range: range, outPath: "/tmp/out.mp4")
        for done in 1...6 {
            try? await Task.sleep(nanoseconds: 30_000_000)
            service.emit(#"{"type":"clip_progress","schema_version":1,"frames_done":\#(done),"frames_total":40}"#)
        }

        if case .exporting = controller.state {} else {
            XCTFail("watchdog must reset on progress; got \(controller.state)")
        }
    }
}
