import Foundation
import OSLog

private let stateMachineLogger = Logger(subsystem: "com.screencap.macos", category: "recorder")

/// Stderr event schema version the Swift parser is pinned to. Mirrors the
/// constant published by `src/screencap/_stderr_events.py`.
let SUPPORTED_EVENT_SCHEMA_VERSION = 1

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
    ///
    /// Note: starting the permission watchdog is intentionally not modelled as
    /// an effect because the orchestrator must gate on `transport == .cliFallback`
    /// before arming — a check the pure state machine has no visibility into.
    /// The orchestrator therefore arms the watchdog imperatively at each
    /// transport-aware callsite (start/CLI fallback paths). Stopping the
    /// watchdog *is* an effect because it is unconditional on transport.
    enum Effect: Equatable {
        case startElapsedTimer
        case stopElapsedTimer
        case stopPermissionWatchdog
        case resolveAwaiting(AwaitKind, success: Bool)
        case surfaceError(String)
        case clearError
        case setMatrixDisclosure(PrivacyMatrixDisclosure)
        case refreshIndex
        case handlePermissionLost(permission: String?)
        case handlePermissionRequired(missing: [String])
        case handleCaptureUnhealthy(reason: String?, reader: String?)
        case handleCaptureRecovered(reader: String?)
    }

    private(set) var state: RecordingState = .idle
    /// Cursor returned by `/v0/recording.start`, used so initial subscribes
    /// are keyed to the start-response boundary (which precedes `started`)
    /// instead of the later `session.snapshot` cursor. Kept until we observe a
    /// start outcome, so a dropped initial stream cannot skip the boundary.
    private(set) var pendingStartCursor: Int?
    private(set) var recordingStartedAt: Date?
    /// Set when a `permission_required` (SCR-142) start-time block was already
    /// routed into the grant flow this attempt, so the subsequent non-zero
    /// process exit (3) does not ALSO surface a generic "permission was revoked"
    /// message that would clobber the precise grant-flow alert. Reset at the
    /// start of each attempt (`enterStarting`) and consumed in `processTerminated`.
    private(set) var permissionRequiredRouted = false

    /// Set the start-response cursor from the orchestrator after a successful
    /// `/v0/recording.start`. Kept as a named mutating method so the field
    /// stays `private(set)` and writes are localised.
    mutating func setPendingStartCursor(_ cursor: Int?) {
        pendingStartCursor = cursor
    }

    /// Clear the start-response cursor when the daemon has evicted it from
    /// the replay window (orchestrator must refetch from snapshot).
    mutating func clearPendingStartCursor() {
        pendingStartCursor = nil
    }

    /// Transition `.idle → .starting`. No-op if already recording.
    mutating func enterStarting() -> [Effect] {
        guard !state.isRecording else { return [] }
        state = .starting
        permissionRequiredRouted = false
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
    ///
    /// In-app stop (`quitting == false`) only fires from `.recording` —
    /// pre-`.recording` states have no active engine to stop. Cmd+Q
    /// (`quitting == true`) is broader: it must also work from `.starting`
    /// (user hit Cmd+Q before the first `started` event arrived) and from
    /// `.stopping(quitting: false)` (an in-app Stop was already in flight
    /// when the user pressed Cmd+Q — the upgrade-to-quit path). Pre-refactor
    /// this was a single unconditional assignment that didn't gate on the
    /// source state at all; widening the guard restores that behavior for
    /// the quit path while keeping in-app Stop narrow.
    mutating func enterStopping(quitting: Bool) -> [Effect] {
        switch state {
        case .recording:
            state = .stopping(quitting: quitting)
            return []
        case .starting where quitting,
             .stopping(quitting: false) where quitting:
            state = .stopping(quitting: quitting)
            return []
        default:
            return []
        }
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
            permissionRequiredRouted = false
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

        case "permission_required":
            // SCR-142: the daemon's pre-spawn permission gate blocked the start
            // and `screencap start` re-emitted the full `missing` list. Route
            // into the SAME precise grant flow the daemon transport uses, naming
            // every denied permission. Mark it routed so the imminent exit-3
            // process termination doesn't clobber the alert with a generic
            // "permission was revoked" message (which is also wrong here — the
            // recording never started).
            permissionRequiredRouted = true
            return [.handlePermissionRequired(missing: event.missing ?? [])]

        case "capture_unhealthy":
            // Advisory, NON-terminal (SCR-76). The controller surfaces a
            // distinct, non-blocking hint and does NOT stop the recording —
            // the cause is non-TCC or could not be attributed. (A TCC denial
            // the engine could attribute arrives as `permission_lost` instead.)
            return [.handleCaptureUnhealthy(reason: event.reason, reader: event.reader)]

        case "capture_recovered":
            // Paired clear for capture_unhealthy (SCR-100). The engine emits
            // this once when a reader that previously went unhealthy returns
            // healthy mid-recording, so the controller can drop the stale
            // advisory for that reader instead of holding it until `.idle`.
            return [.handleCaptureRecovered(reader: event.reader)]

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
        // Capture whether the engine was already mid-recording BEFORE we idle
        // below. `.recording` / `.stopping` mean a `started` event was already
        // observed, so a non-zero exit now is a force-quit / mid-recording
        // crash — NOT a start-time failure. `.starting` (and `.idle`, the spawn
        // window before `started`) is the genuine start-time window where the
        // permission preflight is the dominant cause. Used by the exit-1 branch
        // below to avoid a false start-time permission message.
        let wasActivelyRecording: Bool
        switch state {
        case .recording, .stopping: wasActivelyRecording = true
        case .idle, .starting: wasActivelyRecording = false
        }

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
        } else if permissionRequiredRouted && exitCode == 3 {
            // SCR-142: a start-time `permission_required` block already routed
            // the user into the precise grant flow this attempt (and named every
            // missing permission). The process now exits 3, but the generic
            // case-3 "permission was revoked" copy would be both redundant and
            // wrong (nothing was recording to revoke), so suppress it and let the
            // grant-flow alert stand. The suppression is scoped to exit 3: a
            // non-3 exit after a `permission_required` event is a different
            // failure (e.g. disk_full → 4) whose real message must still surface.
        } else {
            switch exitCode {
            case 1:
                // Exit 1 is the engine's "generic failure" code. At START TIME on
                // the CLI-fallback path it means the start failed before any
                // `started` event for a reason we could NOT attribute precisely:
                // a genuine non-permission startup crash (import failure, encoder
                // fault), or a permission denial the daemon's grant probe left
                // indeterminate so the pre-spawn gate never fired. A *precise*
                // permission block now arrives instead as `permission_required`
                // (handled above, exit 3) — so this branch is the residual
                // catch-all, and the hedged "usually means … isn't granted"
                // wording stays a safe non-false fallback.
                //
                // Exit 1 ALSO fires when a process that was already recording
                // dies (force-quit, mid-recording crash). There the start-time
                // permission story is wrong — the recorder already produced a
                // `started` event — and last-writer-wins on `lastError` would
                // clobber the real reason a prior event (e.g. `permission_lost`,
                // `disk_full`) may have surfaced this session. So gate the
                // permission hint on the start-time window and fall back to a
                // neutral message otherwise.
                //
                // The copy points at "the Privacy tab" generically rather than
                // naming the conditional "Finish setup" button, which renders
                // only when first-run setup was explicitly skipped — so a user
                // who never skipped (ad-hoc-orphaned grant, or any indeterminate
                // CLI-fallback exit-1) isn't pointed at a control that isn't on
                // screen. The Privacy tab is always reachable from the sidebar.
                if wasActivelyRecording {
                    effects.append(.surfaceError("Recorder exited with code 1."))
                } else {
                    effects.append(.surfaceError(
                        "Recording couldn't start. This usually means Screen Recording, "
                        + "Accessibility, or Input Monitoring isn't granted to the recorder — "
                        + "open the Privacy tab to review and grant them."
                    ))
                }
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

        // Consume the SCR-142 routed flag so it cannot leak into a later
        // termination; the next attempt re-arms it via `enterStarting`.
        permissionRequiredRouted = false

        if state.isRecording {
            state = .idle
        }
        return effects
    }
}
