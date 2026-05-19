import XCTest
@testable import ScreenCap

/// Focused tests for the pure value-type state machine. None of these
/// instantiate `RecorderController`, AppKit, Combine, or any test server —
/// the state machine is a pure mapping from `(state, event)` to
/// `(state', effects)`.
final class RecordingStateMachineTests: XCTestCase {
    // MARK: - Lifecycle transitions

    func testEnterStartingFromIdleEmitsClearError() {
        var machine = RecordingStateMachine()
        let effects = machine.enterStarting()

        XCTAssertEqual(machine.state, .starting)
        XCTAssertEqual(effects, [.clearError])
    }

    func testEnterStartingWhileRecordingIsNoOp() {
        var machine = RecordingStateMachine()
        machine.forceState(.recording(elapsed: 5))

        let effects = machine.enterStarting()

        XCTAssertEqual(machine.state, .recording(elapsed: 5))
        XCTAssertEqual(effects, [])
    }

    func testEnterStoppingFromRecordingTransitionsState() {
        var machine = RecordingStateMachine()
        machine.forceState(.recording(elapsed: 3))

        let effects = machine.enterStopping(quitting: false)

        XCTAssertEqual(machine.state, .stopping(quitting: false))
        XCTAssertEqual(effects, [])
    }

    func testEnterStoppingFromIdleIsNoOp() {
        var machine = RecordingStateMachine()

        let effects = machine.enterStopping(quitting: false)

        XCTAssertEqual(machine.state, .idle)
        XCTAssertEqual(effects, [])
    }

    func testObserveActiveDaemonSessionEntersRecordingWithComputedElapsed() {
        var machine = RecordingStateMachine()
        let startedAt = Date(timeIntervalSince1970: 100)
        let now = Date(timeIntervalSince1970: 112)

        let effects = machine.observeActiveDaemonSession(startedAt: startedAt, now: now)

        XCTAssertEqual(machine.state, .recording(elapsed: 12))
        XCTAssertEqual(machine.recordingStartedAt, startedAt)
        XCTAssertEqual(effects, [.startElapsedTimer])
    }

    func testRestoreRecordingAfterStopFailureUsesStartedAt() {
        var machine = RecordingStateMachine()
        let startedAt = Date(timeIntervalSince1970: 50)
        _ = machine.observeActiveDaemonSession(startedAt: startedAt, now: Date(timeIntervalSince1970: 60))
        _ = machine.enterStopping(quitting: false)

        machine.restoreRecordingAfterStopFailure(now: Date(timeIntervalSince1970: 70))

        XCTAssertEqual(machine.state, .recording(elapsed: 20))
    }

    func testTickElapsedUpdatesRecordingElapsed() {
        var machine = RecordingStateMachine()
        let startedAt = Date(timeIntervalSince1970: 100)
        _ = machine.observeActiveDaemonSession(startedAt: startedAt, now: Date(timeIntervalSince1970: 101))

        machine.tickElapsed(now: Date(timeIntervalSince1970: 105))

        XCTAssertEqual(machine.state, .recording(elapsed: 5))
    }

    func testTickElapsedIsNoOpOutsideRecording() {
        var machine = RecordingStateMachine()

        machine.tickElapsed(now: Date(timeIntervalSince1970: 100))

        XCTAssertEqual(machine.state, .idle)
    }

    func testForceStateToIdleClearsLedgerFields() {
        var machine = RecordingStateMachine()
        machine.pendingStartCursor = 42
        _ = machine.observeActiveDaemonSession(startedAt: Date(), now: Date())

        machine.forceState(.idle)

        XCTAssertEqual(machine.state, .idle)
        XCTAssertNil(machine.pendingStartCursor)
        XCTAssertNil(machine.recordingStartedAt)
    }

    // MARK: - Event: started

    func testStartedEventFromStartingTransitionsToRecording() {
        var machine = RecordingStateMachine()
        machine.pendingStartCursor = 7
        _ = machine.enterStarting()

        let now = Date(timeIntervalSince1970: 200)
        let effects = machine.handle(event: event(type: "started"), now: now)

        XCTAssertEqual(machine.state, .recording(elapsed: 0))
        XCTAssertNil(machine.pendingStartCursor)
        XCTAssertEqual(machine.recordingStartedAt, now)
        XCTAssertEqual(effects, [.startElapsedTimer])
    }

    func testDuplicateStartedEventWhileRecordingIsIgnored() {
        var machine = RecordingStateMachine()
        _ = machine.enterStarting()
        _ = machine.handle(event: event(type: "started"))

        let effects = machine.handle(event: event(type: "started"))

        if case .recording = machine.state {
            // expected
        } else {
            XCTFail("Expected .recording, got \(machine.state)")
        }
        XCTAssertEqual(effects, [])
    }

    // MARK: - Event: recording_finalized

    func testRecordingFinalizedResolvesFinalizedAndRefreshesIndex() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "recording_finalized"))

        XCTAssertEqual(effects, [
            .resolveAwaiting(.finalized, success: true),
            .refreshIndex,
        ])
    }

    func testRecordingFinalizedWithForceStoppedSurfacesUploadRetryWarning() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "recording_finalized", forceStopped: true))

        XCTAssertEqual(effects, [
            .resolveAwaiting(.finalized, success: true),
            .surfaceError("Recording stopped, but some data may not have uploaded. Run `screencap upload` to retry."),
            .refreshIndex,
        ])
    }

    // MARK: - Event: recording_failed

    func testRecordingFailedTransitionsToIdleAndResolvesAwaits() {
        var machine = RecordingStateMachine()
        machine.pendingStartCursor = 9
        _ = machine.enterStarting()

        let effects = machine.handle(event: event(type: "recording_failed", reason: "engine crashed"))

        XCTAssertEqual(machine.state, .idle)
        XCTAssertNil(machine.pendingStartCursor)
        XCTAssertEqual(effects, [
            .surfaceError("engine crashed"),
            .resolveAwaiting(.finalized, success: true),
            .resolveAwaiting(.stopped, success: false),
        ])
    }

    func testRecordingFailedWithoutReasonUsesGenericMessage() {
        var machine = RecordingStateMachine()
        _ = machine.enterStarting()

        let effects = machine.handle(event: event(type: "recording_failed"))

        XCTAssertEqual(effects, [
            .surfaceError("Recording failed."),
            .resolveAwaiting(.finalized, success: true),
            .resolveAwaiting(.stopped, success: false),
        ])
    }

    // MARK: - Event: permission_lost, disk_full, stopped

    func testPermissionLostEmitsHandlePermissionLostEffect() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "permission_lost", permission: "Screen Recording"))

        XCTAssertEqual(effects, [.handlePermissionLost(permission: "Screen Recording")])
    }

    func testDiskFullEmitsSurfaceError() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "disk_full"))

        XCTAssertEqual(effects, [.surfaceError("Disk is full — recording stopped.")])
    }

    func testStoppedEventResolvesBothAwaits() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "stopped"))

        XCTAssertEqual(effects, [
            .resolveAwaiting(.finalized, success: true),
            .resolveAwaiting(.stopped, success: true),
        ])
    }

    // MARK: - Event: matrix_disclosure_required

    func testMatrixDisclosureEventEmitsSetMatrixDisclosureWithChanges() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(
            type: "matrix_disclosure_required",
            changes: ["chat_email_calendar_video_call_mask_window"],
            optOutCommandExamples: ["screencap settings privacy exclude_apps add com.openai.chat"]
        ))

        XCTAssertEqual(effects, [
            .setMatrixDisclosure(PrivacyMatrixDisclosure(
                changes: ["chat_email_calendar_video_call_mask_window"],
                optOutCommandExamples: ["screencap settings privacy exclude_apps add com.openai.chat"]
            ))
        ])
    }

    // MARK: - Event: unknown / _close / chunk_finalized

    func testChunkFinalizedIsInformationalNoOp() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "chunk_finalized"))

        XCTAssertEqual(effects, [])
    }

    func testUnknownEventTypeProducesNoEffects() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "lock_contended"))

        XCTAssertEqual(effects, [])
    }

    func testCloseEventProducesNoEffects() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "_close", reason: "shutdown"))

        XCTAssertEqual(effects, [])
    }

    func testSchemaDriftDoesNotPreventEventProcessing() {
        var machine = RecordingStateMachine()
        _ = machine.enterStarting()

        // schema_version=99 is mismatched but the event still drives the state machine.
        let effects = machine.handle(event: event(type: "started", schemaVersion: 99))

        XCTAssertEqual(machine.state, .recording(elapsed: 0))
        XCTAssertEqual(effects, [.startElapsedTimer])
    }

    // MARK: - Process termination

    func testProcessTerminatedCleanExitPreservesPriorError() {
        var machine = RecordingStateMachine()
        machine.forceState(.stopping(quitting: false))

        let effects = machine.processTerminated(exitCode: 0)

        XCTAssertEqual(machine.state, .idle)
        XCTAssertEqual(effects, [
            .stopElapsedTimer,
            .stopPermissionWatchdog,
            .resolveAwaiting(.finalized, success: false),
            .resolveAwaiting(.stopped, success: false),
        ])
    }

    func testProcessTerminatedSIGINTAndSIGTERMPreservePriorError() {
        for exitCode in [Int32(130), Int32(143)] {
            var machine = RecordingStateMachine()
            machine.forceState(.recording(elapsed: 5))

            let effects = machine.processTerminated(exitCode: exitCode)

            XCTAssertEqual(machine.state, .idle, "exit code \(exitCode) should idle")
            XCTAssertFalse(
                effects.contains(where: { if case .surfaceError = $0 { return true }; return false }),
                "exit code \(exitCode) should not surface error"
            )
        }
    }

    func testProcessTerminatedExitCode2SurfacesAlreadyRecording() {
        var machine = RecordingStateMachine()

        let effects = machine.processTerminated(exitCode: 2)

        XCTAssertTrue(effects.contains(.surfaceError("ScreenCap is already recording.")))
    }

    func testProcessTerminatedExitCode3SurfacesPermissionRevoked() {
        var machine = RecordingStateMachine()

        let effects = machine.processTerminated(exitCode: 3)

        XCTAssertTrue(effects.contains(.surfaceError("Recording stopped because a required permission was revoked.")))
    }

    func testProcessTerminatedExitCode4SurfacesDiskFull() {
        var machine = RecordingStateMachine()

        let effects = machine.processTerminated(exitCode: 4)

        XCTAssertTrue(effects.contains(.surfaceError("Disk is full — recording stopped.")))
    }

    func testProcessTerminatedUnknownExitCodeSurfacesGenericMessage() {
        var machine = RecordingStateMachine()

        let effects = machine.processTerminated(exitCode: 99)

        XCTAssertTrue(effects.contains(.surfaceError("Recorder exited with code 99.")))
    }

    // MARK: - Helpers

    /// Build a `RecorderEventLine` test value. Defaults match the most common
    /// schema_version=1 happy-path payload; pass overrides per scenario.
    private func event(
        type: String,
        schemaVersion: Int? = 1,
        forceStopped: Bool? = nil,
        permission: String? = nil,
        changes: [String]? = nil,
        optOutCommandExamples: [String]? = nil,
        cursor: Int? = nil,
        reason: String? = nil,
        ts: Double? = nil
    ) -> RecorderEventLine {
        RecorderEventLine(
            type: type,
            schemaVersion: schemaVersion,
            forceStopped: forceStopped,
            permission: permission,
            changes: changes,
            optOutCommandExamples: optOutCommandExamples,
            cursor: cursor,
            reason: reason,
            ts: ts
        )
    }
}
