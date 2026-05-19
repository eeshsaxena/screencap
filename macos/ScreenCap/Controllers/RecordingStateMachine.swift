import Foundation
import OSLog

private let stateMachineLogger = Logger(subsystem: "com.screencap.macos", category: "recorder")

/// Stderr event schema version the Swift parser is pinned to. Mirrors the
/// constant published by `src/screencap/_stderr_events.py`.
let SUPPORTED_EVENT_SCHEMA_VERSION = 1

/// Wire-level daemon envelope error codes the controller branches on.
/// Mirrors `src/screencap/daemon/errors.py`'s string constants.
enum DaemonErrorCode {
    static let lockContended = "lock_contended"
    static let notOwnedByDaemon = "not_owned_by_daemon"
}

/// State machine for the recording lifecycle. Mirrors the stderr event contract
/// from `src/screencap/_stderr_events.py` (Unit 8a).
enum RecordingState: Equatable {
    case idle
    case starting
    case recording(elapsed: TimeInterval)
    case stopping(quitting: Bool)

    var isRecording: Bool {
        switch self {
        case .recording, .stopping, .starting: return true
        case .idle: return false
        }
    }

    var isStopping: Bool {
        if case .stopping = self { return true }
        return false
    }

    var elapsed: TimeInterval {
        if case .recording(let e) = self { return e }
        return 0
    }
}

/// Pure state machine for the recording lifecycle. Maps `(state, event)` to
/// `(newState, effects)` without any UI, concurrency, or transport dependency.
/// The orchestrator (`RecorderController`) applies the returned effects.
struct RecordingStateMachine {
    enum AwaitKind: Equatable {
        case finalized
        case stopped
    }

    /// Side-effects emitted by state transitions. The controller drains the
    /// effect list and routes each value to the appropriate collaborator.
    enum Effect: Equatable {
        case startElapsedTimer
        case stopElapsedTimer
        case startPermissionWatchdog
        case stopPermissionWatchdog
        case resolveAwaiting(AwaitKind, success: Bool)
        case surfaceError(String)
        case clearError
        case setMatrixDisclosure(PrivacyMatrixDisclosure)
        case refreshIndex
        case handlePermissionLost(permission: String?)
    }

    private(set) var state: RecordingState = .idle
    /// Cursor returned by `/v0/recording.start`, used so initial subscribes
    /// are keyed to the start-response boundary (which precedes `started`)
    /// instead of the later `session.snapshot` cursor. Kept until we observe a
    /// start outcome, so a dropped initial stream cannot skip the boundary.
    var pendingStartCursor: Int?
    private(set) var recordingStartedAt: Date?

    /// Transition `.idle → .starting`. No-op if already recording.
    mutating func enterStarting() -> [Effect] {
        guard !state.isRecording else { return [] }
        state = .starting
        return [.clearError]
    }

    /// Attach to a daemon-owned session discovered via snapshot. Skips the
    /// `.starting` intermediate state since the recording is already running.
    mutating func observeActiveDaemonSession(startedAt: Date, now: Date = Date()) -> [Effect] {
        pendingStartCursor = nil
        recordingStartedAt = startedAt
        state = .recording(elapsed: now.timeIntervalSince(startedAt))
        return [.startElapsedTimer]
    }

    /// Transition into a stopping state (in-app Stop or Cmd+Q).
    mutating func enterStopping(quitting: Bool) -> [Effect] {
        guard case .recording = state else { return [] }
        state = .stopping(quitting: quitting)
        return []
    }

    /// Stop was requested but the stop signal failed to dispatch. Restore the
    /// previous `.recording(elapsed:)` so the UI doesn't hang in `.stopping`.
    mutating func restoreRecordingAfterStopFailure(now: Date = Date()) {
        guard state.isStopping else { return }
        let elapsed = recordingStartedAt.map { now.timeIntervalSince($0) } ?? 0
        state = .recording(elapsed: elapsed)
    }

    /// Final transition to `.idle` after a stop completes. Idempotent.
    mutating func enterIdle() {
        state = .idle
    }

    /// Update the displayed elapsed seconds. No-op outside `.recording`.
    mutating func tickElapsed(now: Date = Date()) {
        guard case .recording = state, let start = recordingStartedAt else { return }
        state = .recording(elapsed: now.timeIntervalSince(start))
    }

    /// Override the underlying recording state. Used for transport-level
    /// rollbacks (foreign claimant, daemon failure, connection loss) where the
    /// orchestrator computes the new state from transport context the machine
    /// doesn't model.
    mutating func forceState(_ newState: RecordingState) {
        if case .idle = newState {
            recordingStartedAt = nil
            pendingStartCursor = nil
        }
        state = newState
    }

    /// Process a decoded stderr / daemon event. Returns the side-effects the
    /// orchestrator must apply (timers, awaits, error surface, etc.).
    mutating func handle(event: RecorderEventLine, now: Date = Date()) -> [Effect] {
        // Schema-drift guard: warn (don't fail) so we keep working under minor
        // additions while making major-version drift visible in Console.app.
        // Decision on user-facing behavior for a major bump tracked separately.
        if let v = event.schemaVersion, v != SUPPORTED_EVENT_SCHEMA_VERSION {
            stateMachineLogger.warning("Unexpected schema_version \(v, privacy: .public) on stderr event \(event.type, privacy: .public). Swift parser pinned to v\(SUPPORTED_EVENT_SCHEMA_VERSION, privacy: .public).")
        }

        switch event.type {
        case "started":
            // Only honour the transition when we're still in `.starting`. A
            // duplicate or out-of-order `started` arriving while we're already
            // `.recording` or `.stopping` would otherwise regress the state
            // machine and re-arm the elapsed timer.
            guard case .starting = state else { return [] }
            pendingStartCursor = nil
            recordingStartedAt = now
            state = .recording(elapsed: 0)
            return [.startElapsedTimer]

        case "chunk_finalized":
            // Informational — no UI change needed.
            return []

        case "recording_finalized":
            // Both stop policies care about this; the in-app path resolves on it.
            var effects: [Effect] = [.resolveAwaiting(.finalized, success: true)]
            if event.forceStopped == true {
                effects.append(.surfaceError("Recording stopped, but some data may not have uploaded. Run `screencap upload` to retry."))
            }
            effects.append(.refreshIndex)
            return effects

        case "recording_failed":
            // Engine reported a failure that prevents continuation (e.g. spawn
            // error, encoder fault). Without this branch the UI stays in
            // `.starting` / `.recording` until the user notices nothing is
            // happening. Release any in-flight stop callers, surface the
            // reason, and drop back to `.idle`.
            pendingStartCursor = nil
            state = .idle
            return [
                .surfaceError(event.reason ?? "Recording failed."),
                .resolveAwaiting(.finalized, success: true),
                .resolveAwaiting(.stopped, success: false),
            ]

        case "permission_lost":
            return [.handlePermissionLost(permission: event.permission)]

        case "disk_full":
            return [.surfaceError("Disk is full — recording stopped.")]

        case "stopped":
            return [
                .resolveAwaiting(.finalized, success: true),
                .resolveAwaiting(.stopped, success: true),
            ]

        case "matrix_disclosure_required":
            let disclosure = PrivacyMatrixDisclosure(
                changes: event.changes ?? [],
                optOutCommandExamples: event.optOutCommandExamples ?? []
            )
            return [.setMatrixDisclosure(disclosure)]

        case "_close":
            if event.reason == "shutdown" {
                stateMachineLogger.info("Daemon event stream closed for shutdown.")
            } else if let reason = event.reason {
                stateMachineLogger.info("Daemon event stream closed: \(reason, privacy: .public)")
            }
            return []

        default:
            // Active Python events the Swift consumer doesn't model (e.g.
            // lock_contended) — log so drift is detectable; the engine handles
            // user-facing fallout via exit codes so we don't surface here.
            stateMachineLogger.debug("Unhandled stderr event type: \(event.type, privacy: .public)")
            return []
        }
    }

    /// Process termination cleanup (CLI fallback transport). Resolves any
    /// awaiting one-shot continuations and maps exit codes to user-facing
    /// messages.
    mutating func processTerminated(exitCode: Int32) -> [Effect] {
        recordingStartedAt = nil

        var effects: [Effect] = [
            .stopElapsedTimer,
            .stopPermissionWatchdog,
            // If we never saw a `stopped` event and the process is gone, resolve
            // any in-flight awaits so the caller can transition out of stopping.
            .resolveAwaiting(.finalized, success: false),
            .resolveAwaiting(.stopped, success: false),
        ]

        // 0 = clean, 130 = SIGINT, 143 = SIGTERM (the engine's documented
        // graceful-shutdown signals). Treat all three as "no new terminal
        // error to surface." Intentionally preserve any warning already set
        // earlier in this session (for example the `forceStopped`
        // upload-retry note from `recording_finalized`) so the user can still
        // see it after the process exits. `start()` clears stale messages when
        // a new recording begins.
        if exitCode == 0 || exitCode == 130 || exitCode == 143 {
            // Keep any prior user-facing warning.
        } else {
            switch exitCode {
            case 2:
                effects.append(.surfaceError("ScreenCap is already recording."))
            case 3:
                effects.append(.surfaceError("Recording stopped because a required permission was revoked."))
            case 4:
                effects.append(.surfaceError("Disk is full — recording stopped."))
            default:
                effects.append(.surfaceError("Recorder exited with code \(exitCode)."))
            }
        }

        if state.isRecording {
            state = .idle
        }
        return effects
    }
}
