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
}
