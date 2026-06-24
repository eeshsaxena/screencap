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
        machine.setPendingStartCursor(42)
        _ = machine.observeActiveDaemonSession(startedAt: Date(), now: Date())

        machine.forceState(.idle)

        XCTAssertEqual(machine.state, .idle)
        XCTAssertNil(machine.pendingStartCursor)
        XCTAssertNil(machine.recordingStartedAt)
    }

    // MARK: - Event: started

    func testStartedEventFromStartingTransitionsToRecording() {
        var machine = RecordingStateMachine()
        machine.setPendingStartCursor(7)
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
        machine.setPendingStartCursor(9)
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

    // MARK: - Event: capture_unhealthy (SCR-76 advisory)

    func testCaptureUnhealthyEmitsAdvisoryEffectWithReasonAndReader() {
        var machine = RecordingStateMachine()
        machine.forceState(.recording(elapsed: 7))

        let effects = machine.handle(event: event(
            type: "capture_unhealthy", reason: "reader_stalled", reader: "screen",
        ))

        XCTAssertEqual(effects, [.handleCaptureUnhealthy(reason: "reader_stalled", reader: "screen")])
        // Advisory & non-terminal: the state machine does NOT change state.
        XCTAssertEqual(machine.state, .recording(elapsed: 7))
    }

    func testCaptureRecoveredEmitsHandleCaptureRecoveredEffect() {
        // SCR-100: the paired recovery event maps to the clear effect, carrying
        // the reader, and — like its unhealthy counterpart — does not change state.
        var machine = RecordingStateMachine()
        machine.forceState(.recording(elapsed: 7))

        let effects = machine.handle(event: event(type: "capture_recovered", reader: "screen"))

        XCTAssertEqual(effects, [.handleCaptureRecovered(reader: "screen")])
        XCTAssertEqual(machine.state, .recording(elapsed: 7))
    }

    func testPermissionLostStillRoutesToHandlePermissionLost() {
        // Regression guard: capture_unhealthy must not perturb the existing
        // permission_lost routing (TCC denials still take the terminal path).
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "permission_lost", permission: "screen_recording"))

        XCTAssertEqual(effects, [.handlePermissionLost(permission: "screen_recording")])
    }

    func testUnknownEventTypeStillNoOps() {
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(type: "some_future_event"))

        XCTAssertEqual(effects, [])
    }

    // MARK: - RecorderEventLine decoding (the `reader` field)

    func testCaptureUnhealthyLineDecodesReaderField() throws {
        let json = #"{"type":"capture_unhealthy","reason":"reader_stalled","reader":"screen","elapsed":12.5,"schema_version":1}"#
        let line = try JSONDecoder().decode(RecorderEventLine.self, from: Data(json.utf8))

        XCTAssertEqual(line.type, "capture_unhealthy")
        XCTAssertEqual(line.reason, "reader_stalled")
        XCTAssertEqual(line.reader, "screen")
    }

    func testExistingEventDecodingUnaffectedByReaderField() throws {
        // A permission_lost line carries no `reader` — decoding must still
        // succeed with reader == nil (no breaking change to existing events).
        let json = #"{"type":"permission_lost","permission":"screen_recording","elapsed":1.0,"schema_version":1}"#
        let line = try JSONDecoder().decode(RecorderEventLine.self, from: Data(json.utf8))

        XCTAssertEqual(line.permission, "screen_recording")
        XCTAssertNil(line.reader)
    }

    func testPermissionRequiredLineDecodesMissingList() throws {
        // SCR-142: the CLI re-emits the daemon's `missing` list as a JSON array.
        let json = #"{"type":"permission_required","missing":["screen_recording","accessibility"],"schema_version":1}"#
        let line = try JSONDecoder().decode(RecorderEventLine.self, from: Data(json.utf8))

        XCTAssertEqual(line.type, "permission_required")
        XCTAssertEqual(line.missing, ["screen_recording", "accessibility"])
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
            XCTAssertEqual(effects, [
                .stopElapsedTimer,
                .stopPermissionWatchdog,
                .resolveAwaiting(.finalized, success: false),
                .resolveAwaiting(.stopped, success: false),
            ], "exit code \(exitCode) should match graceful-exit effect list")
        }
    }

    func testProcessTerminatedExitCode1SurfacesActionablePermissionHint() {
        var machine = RecordingStateMachine()
        // Start-time window: a `started` event was never observed, so the
        // machine is in `.starting` (or `.idle`). Exit 1 here is overwhelmingly
        // the start-time permission preflight bailing; the surfaced message must
        // be self-actionable — naming the permissions and pointing at the always
        // -reachable Privacy tab — rather than the old dead-end "Recorder exited
        // with code 1".
        _ = machine.enterStarting()

        let effects = machine.processTerminated(exitCode: 1)

        let surfaced = effects.compactMap { effect -> String? in
            if case let .surfaceError(message) = effect { return message }
            return nil
        }
        XCTAssertEqual(surfaced.count, 1)
        XCTAssertTrue(surfaced[0].contains("Screen Recording"))
        XCTAssertTrue(surfaced[0].contains("Privacy tab"))
        XCTAssertFalse(surfaced[0].contains("exited with code"))
    }

    func testProcessTerminatedExitCode1WhileRecordingDoesNotSurfacePermissionHint() {
        // A process that exits 1 while ALREADY recording (force-quit /
        // mid-recording crash) is NOT a start-time failure — it already cleared
        // the permission preflight and produced a `started` event. The
        // start-time permission hint would be a false claim, so we surface a
        // neutral message instead.
        var machine = RecordingStateMachine()
        machine.forceState(.recording(elapsed: 12))

        let effects = machine.processTerminated(exitCode: 1)

        XCTAssertEqual(machine.state, .idle)
        let surfaced = effects.compactMap { effect -> String? in
            if case let .surfaceError(message) = effect { return message }
            return nil
        }
        XCTAssertEqual(surfaced, ["Recorder exited with code 1."])
        XCTAssertFalse(surfaced[0].contains("Screen Recording"))
        XCTAssertFalse(surfaced[0].contains("Privacy tab"))
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

    func testPermissionRequiredEmitsHandlePermissionRequiredWithMissingList() {
        // SCR-142: the CLI-fallback start-time block names every denied
        // permission, so the event routes the full `missing` list (not the
        // single-permission `permission_lost` path) into the grant flow.
        var machine = RecordingStateMachine()

        let effects = machine.handle(event: event(
            type: "permission_required",
            missing: ["screen_recording", "accessibility"]
        ))

        XCTAssertEqual(
            effects,
            [.handlePermissionRequired(missing: ["screen_recording", "accessibility"])]
        )
    }

    func testPermissionRequiredThenExit3SuppressesGenericRevokedMessage() {
        // SCR-142 de-dup: once `permission_required` routed the precise grant
        // flow, the process exits 3 — but the generic "permission was revoked"
        // copy must NOT also fire (it's redundant and wrong: nothing was
        // recording to revoke). The FIFO stderr→termination ordering guarantees
        // the event is processed before `processTerminated`.
        var machine = RecordingStateMachine()
        _ = machine.enterStarting()
        _ = machine.handle(event: event(
            type: "permission_required",
            missing: ["screen_recording"]
        ))

        let effects = machine.processTerminated(exitCode: 3)

        XCTAssertFalse(
            effects.contains(.surfaceError("Recording stopped because a required permission was revoked.")),
            "exit-3 message must be suppressed after a permission_required block"
        )
    }

    func testExit3WithoutPriorPermissionRequiredStillSurfacesRevoked() {
        // Guard the de-dup is scoped: a bare exit 3 (mid-recording revocation,
        // no preceding `permission_required`) still surfaces its message.
        var machine = RecordingStateMachine()
        _ = machine.enterStarting()

        let effects = machine.processTerminated(exitCode: 3)

        XCTAssertTrue(
            effects.contains(.surfaceError("Recording stopped because a required permission was revoked."))
        )
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
        missing: [String]? = nil,
        changes: [String]? = nil,
        optOutCommandExamples: [String]? = nil,
        cursor: Int? = nil,
        reason: String? = nil,
        reader: String? = nil,
        ts: Double? = nil
    ) -> RecorderEventLine {
        RecorderEventLine(
            type: type,
            schemaVersion: schemaVersion,
            forceStopped: forceStopped,
            permission: permission,
            missing: missing,
            changes: changes,
            optOutCommandExamples: optOutCommandExamples,
            cursor: cursor,
            reason: reason,
            reader: reader,
            ts: ts
        )
    }
}
