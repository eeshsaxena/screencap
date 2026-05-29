import XCTest
@testable import ScreenCap

/// Fake `UploadService` that records the start invocation, hands the
/// callbacks back to the test, and lets the test drive lines + termination
/// at its own pace.
@MainActor
final class FakeUploadService: UploadService {
    private(set) var startedNames: [String] = []
    var pendingError: Error?
    var fakeProcess = FakeSpawnedProcessHandle(isRunning: true, pid: 42424, forceKillReturnValue: true)
    private var onLine: ((String) -> Void)?
    private var onTerminated: ((Int32) -> Void)?

    func start(
        name: String,
        onLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle {
        startedNames.append(name)
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

enum FakeUploadServiceError: Error, LocalizedError {
    case launchFailed
    var errorDescription: String? { "launch failed" }
}

@MainActor
final class UploadControllerTests: XCTestCase {
    func testStartTransitionsIdleToUploading() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")

        XCTAssertEqual(service.startedNames, ["rec-001"])
        guard case .uploading(let progress) = controller.state else {
            return XCTFail("expected uploading state, got \(controller.state)")
        }
        XCTAssertEqual(progress.filesDone, 0)
        XCTAssertEqual(progress.filesTotal, 0)
    }

    /// Happy-path sequence from the plan: upload_started → upload_file_done×3
    /// → upload_finished walks the state machine through uploading-with-
    /// progress → succeeded(summary).
    func testHappyPathDrivesIdleToSucceeded() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 3}"#)
        service.emit(#"{"type": "upload_file_done", "schema_version": 1, "files_done": 1, "files_total": 3}"#)
        service.emit(#"{"type": "upload_file_done", "schema_version": 1, "files_done": 2, "files_total": 3}"#)
        service.emit(#"{"type": "upload_file_done", "schema_version": 1, "files_done": 3, "files_total": 3}"#)
        service.emit(#"{"type": "upload_finished", "schema_version": 1, "uploaded": 3, "skipped": 0, "failed": 0}"#)
        service.terminate(exitCode: 0)

        if case .succeeded(let summary) = controller.state {
            XCTAssertEqual(summary.uploaded, 3)
            XCTAssertEqual(summary.skipped, 0)
            XCTAssertEqual(summary.failed, 0)
        } else {
            XCTFail("expected succeeded state, got \(controller.state)")
        }
    }

    func testProgressFractionTracksFilesDoneOverFilesTotal() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 4}"#)
        service.emit(#"{"type": "upload_file_done", "schema_version": 1, "files_done": 1, "files_total": 4}"#)

        if case .uploading(let progress) = controller.state {
            XCTAssertEqual(progress.filesDone, 1)
            XCTAssertEqual(progress.filesTotal, 4)
            XCTAssertEqual(progress.fraction, 0.25, accuracy: 0.001)
        } else {
            XCTFail("expected uploading state with progress, got \(controller.state)")
        }
    }

    /// Covers AE5 — explicit upload_failed event transitions to failed(error).
    func testUploadFailedEventTransitionsToFailedWithMessage() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_failed", "schema_version": 1, "error": "network reset"}"#)

        if case .failed(let msg) = controller.state {
            XCTAssertEqual(msg, "network reset")
        } else {
            XCTFail("expected failed state, got \(controller.state)")
        }
    }

    /// Plan contract: if the subprocess exits non-zero without emitting a
    /// terminal event, the controller surfaces a failure rather than
    /// silently staying in `uploading`.
    func testProcessExitsNonZeroWithoutTerminalEventTransitionsToFailed() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.terminate(exitCode: 1)

        if case .failed(let msg) = controller.state {
            XCTAssertTrue(msg.contains("code 1"), "got: \(msg)")
        } else {
            XCTFail("expected failed state, got \(controller.state)")
        }
    }

    /// Covers AE4 — cancel mid-upload sends SIGTERM via terminate(), Python
    /// emits upload_failed(error: "interrupted"), state lands on failed.
    func testCancelMidUploadTerminatesProcessAndStateLandsOnFailedInterrupted() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 3}"#)

        controller.cancel()
        // Simulate the Python side's SIGTERM handler reaction.
        service.emit(#"{"type": "upload_failed", "schema_version": 1, "error": "interrupted"}"#)
        service.terminate(exitCode: 130)

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 1)
        if case .failed(let msg) = controller.state {
            XCTAssertEqual(msg, "interrupted")
        } else {
            XCTFail("expected failed state, got \(controller.state)")
        }
    }

    /// Safe-on-idle invariant: cancel before start (or after success) must
    /// not crash and must not dispatch terminate.
    func testCancelOnIdleIsSafeNoOp() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.cancel()

        XCTAssertEqual(service.fakeProcess.terminateInvocations, 0)
        if case .idle = controller.state {
            // pass
        } else {
            XCTFail("expected idle state, got \(controller.state)")
        }
    }

    func testCancelAfterSucceededIsSafeNoOp() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_finished", "schema_version": 1, "uploaded": 1, "skipped": 0, "failed": 0}"#)
        service.terminate(exitCode: 0)

        controller.cancel()

        // terminate is gated on isRunning, which the fake flips to false on
        // service.terminate, so cancel after success is a no-op.
        XCTAssertEqual(service.fakeProcess.terminateInvocations, 0)
        if case .succeeded = controller.state {
            // pass
        } else {
            XCTFail("expected succeeded state, got \(controller.state)")
        }
    }

    func testNonJSONLineDoesNotChangeState() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 2}"#)
        let stateBefore = controller.state
        service.emit("[INFO] Uploading file 1 of 2…")
        XCTAssertEqual(controller.state, stateBefore)
    }

    func testUnknownEventTypeDoesNotChangeState() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 2}"#)
        let stateBefore = controller.state
        service.emit(#"{"type": "upload_progress_future", "schema_version": 1, "percent": 50}"#)
        XCTAssertEqual(controller.state, stateBefore)
    }

    /// Spawn failure (binary not found, launch error) maps to a terminal
    /// `failed` state immediately so the UI doesn't hang waiting for a
    /// process that never started.
    func testStartFailsWhenServiceThrowsAndStateLandsOnFailed() {
        let service = FakeUploadService()
        service.pendingError = FakeUploadServiceError.launchFailed
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")

        if case .failed(let msg) = controller.state {
            XCTAssertEqual(msg, "launch failed")
        } else {
            XCTFail("expected failed state, got \(controller.state)")
        }
    }

    /// Double-start guard: a second start while already uploading is a
    /// no-op (review window owns one upload at a time per window).
    func testSecondStartWhileUploadingIsNoOp() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        controller.start(name: "rec-002")

        XCTAssertEqual(service.startedNames, ["rec-001"])
    }

    /// Retry path: after a failure, start() should accept a new attempt
    /// (this is what the Retry button in U8 wires up to).
    func testRetryAfterFailureStartsAgain() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_failed", "schema_version": 1, "error": "first try"}"#)
        service.terminate(exitCode: 1)
        // After terminate, isRunning is false so cancel/start guards are clean.
        service.fakeProcess = FakeSpawnedProcessHandle(isRunning: true, pid: 99999, forceKillReturnValue: true)

        controller.start(name: "rec-001")

        XCTAssertEqual(service.startedNames, ["rec-001", "rec-001"])
        if case .uploading = controller.state {
            // pass
        } else {
            XCTFail("expected uploading state on retry, got \(controller.state)")
        }
    }

    /// Todo #004 — `upload_file_done` without an explicit `files_done`
    /// falls through to `filesDone += 1`. Every other test supplies
    /// `files_done`; this one exercises the increment fallback so a
    /// regression there ships visible.
    func testUploadFileDoneWithoutFilesDoneIncrementsCounter() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 3}"#)
        // No `files_done` field — exercise the fallback branch.
        service.emit(#"{"type": "upload_file_done", "schema_version": 1, "name": "video.mp4"}"#)
        service.emit(#"{"type": "upload_file_done", "schema_version": 1, "name": "events.jsonl"}"#)

        guard case .uploading(let progress) = controller.state else {
            return XCTFail("expected uploading, got \(controller.state)")
        }
        XCTAssertEqual(progress.filesDone, 2)
        XCTAssertEqual(progress.filesTotal, 3)
    }

    /// Todo #005 — `upload_failed` without an explicit `error` lands on
    /// `.failed("upload failed")` (the default-message fallback).
    func testUploadFailedWithoutErrorUsesDefaultMessage() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        // No `error` field — exercise the default-message fallback.
        service.emit(#"{"type": "upload_failed", "schema_version": 1}"#)

        if case .failed(let msg) = controller.state {
            XCTAssertEqual(msg, "upload failed")
        } else {
            XCTFail("expected failed state, got \(controller.state)")
        }
    }

    /// Todo #010 — first-write-wins on terminal events. A second
    /// `upload_finished` after the first must not re-fire the state
    /// transition (which would re-trigger auto-close + index refresh in
    /// the viewmodel). Same for contradictory `upload_failed` after a
    /// successful terminal.
    func testSecondTerminalEventIsIgnored() {
        let service = FakeUploadService()
        let controller = UploadController(service: service)

        controller.start(name: "rec-001")
        service.emit(#"{"type": "upload_started", "schema_version": 1, "file_count": 1}"#)
        service.emit(#"{"type": "upload_finished", "schema_version": 1, "uploaded": 1, "skipped": 0, "failed": 0}"#)
        // Contradictory follow-up that must not flip the terminal state.
        service.emit(#"{"type": "upload_failed", "schema_version": 1, "error": "spurious"}"#)

        if case .succeeded(let summary) = controller.state {
            XCTAssertEqual(summary.uploaded, 1)
        } else {
            XCTFail("first terminal must win, got \(controller.state)")
        }
    }

    /// Todo #001 — inactivity timeout. With a short test bound, no events
    /// after start → controller transitions to `.failed("upload timed out")`
    /// and SIGTERMs the child.
    func testInactivityTimeoutTransitionsToFailed() async {
        let service = FakeUploadService()
        let controller = UploadController(service: service, inactivityTimeoutSeconds: 0.05)

        controller.start(name: "rec-001")
        // No events for longer than the bound.
        try? await Task.sleep(nanoseconds: 200_000_000)

        if case .failed(let msg) = controller.state {
            XCTAssertEqual(msg, "upload timed out")
        } else {
            XCTFail("expected timeout failure, got \(controller.state)")
        }
        XCTAssertGreaterThanOrEqual(service.fakeProcess.terminateInvocations, 1, "child must receive SIGTERM on timeout")
    }

    /// Todo #001 — the watchdog must reset on every parsed event, so a
    /// slow-but-progressing upload doesn't trip the timeout.
    func testWatchdogResetsOnEachEvent() async {
        let service = FakeUploadService()
        // Bound = 100ms. Events arrive every 30ms for ~200ms. The watchdog
        // should reset on each and never fire.
        let controller = UploadController(service: service, inactivityTimeoutSeconds: 0.1)

        controller.start(name: "rec-001")
        for _ in 0..<6 {
            try? await Task.sleep(nanoseconds: 30_000_000)
            service.emit(#"{"type": "upload_file_done", "schema_version": 1, "files_done": 1, "files_total": 3}"#)
        }

        // Still uploading after the steady stream of events.
        if case .uploading = controller.state {
            // pass
        } else {
            XCTFail("watchdog must reset; got \(controller.state)")
        }
    }
}
