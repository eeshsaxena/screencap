import Foundation
import OSLog

private let daemonSessionLogger = Logger(subsystem: "com.screencap.macos", category: "daemon-session")

/// Wire-level daemon envelope error codes the controller branches on.
/// Mirrors `src/screencap/daemon/errors.py`'s string constants.
enum DaemonErrorCode {
    static let lockContended = "lock_contended"
    // SCR-276: recording.start refused because the PREVIOUS recording is still
    // draining its post-stop finalize work. Deliberately NOT folded into
    // `lockContended` — nothing is recording, so that case's copy is wrong.
    static let finalizeInProgress = "finalize_in_progress"
    static let notOwnedByDaemon = "not_owned_by_daemon"
    static let permissionRequired = "permission_required"
    // SCR-228: recording.start refused because a storage migration holds the daemon.
    static let migrationInProgress = "migration_in_progress"
}

/// Decodes the `missing` list off a `permission_required` error envelope (U6).
private struct PermissionRequiredPayload: Decodable {
    let missing: [String]
}

/// Decodes the draining recording's name off a `finalize_in_progress` envelope
/// (SCR-276). The field is omitted when the daemon does not know the name, so
/// it is optional and the copy degrades to an unnamed subject.
private struct FinalizeInProgressPayload: Decodable {
    let recordingName: String?

    enum CodingKeys: String, CodingKey {
        case recordingName = "recording_name"
    }
}

/// Typed outcomes returned by the daemon session service. Hoisted out of the
/// concrete class so the protocol can declare them in its surface without a
/// nested-type cycle.
enum DaemonSession {
    enum ProbeOutcome: Equatable {
        /// Daemon reachable. Carries its live TCC grant state (U3) so the
        /// orchestrator can gate the walkthrough / start-block on the daemon's
        /// own grants rather than on transport reachability alone. An older
        /// daemon that omits the block surfaces as `.allIndeterminate`.
        case daemon(grants: DaemonPermissionGrants)
        case schemaMismatch
        case unavailable
    }

    enum SnapshotOutcome: Equatable {
        case noActiveSession
        /// A daemon-owned recording is live. `muted` is the CONFIRMED mic-mute
        /// state from the snapshot overlay (SCR-254 U7); the service maps a
        /// missing field to `false` (unmuted) here so the controller hydrates a
        /// concrete value on attach / reconnect.
        case daemonOwnedSession(startedAt: Date, muted: Bool)
        /// SCR-276: a daemon-owned session the snapshot still reports as
        /// recording, but which has been STOPPED and is draining its finalize
        /// work. The pidfile lock is only released when the engine exits, so
        /// `is_recording` stays true for the whole drain — without this case an
        /// app relaunch mid-drain re-attaches to a recording that is stopping
        /// and shows a live HUD for it. Carries `recordingName` so a
        /// user-facing "still finishing" surface can name it (SCR-296).
        case finalizingSession(recordingName: String?)
        case foreignClaimant
        case unreachable
    }

    enum FailureOutcome: Equatable {
        case schemaMismatch
        case socketUnavailable
        case lockContended
        /// SCR-276: the start was refused because the previous recording is
        /// still finalizing. Retryable — the drain is bounded by the daemon's
        /// stop backstop — so the surface says "try again in a moment" rather
        /// than `lockContended`'s "already recording".
        case finalizeInProgress(recordingName: String?)
        /// A daemon-backed start was rejected before spawn because a required
        /// TCC grant is missing (U6). Carries the canonical permission strings
        /// the app maps to panes. Routed into the grant flow, never the CLI
        /// fallback.
        case permissionRequired(missing: [String])
        case other(localizedDescription: String)
    }

    enum AttachOutcome: Equatable {
        case shutdown
        case foreignClaimant
        case sessionEnded
        case lostContact
        case fatalError(FailureOutcome)
    }

    /// Outcome of a successful `/v0/recording.start`. Carries both the
    /// late-join `cursor` and the daemon-assigned `sessionID` so the
    /// orchestrator can bind subsequent snapshot-driven promotions to the
    /// session it actually started (SCR-68). `sessionID` is the daemon's
    /// `session_id`, which equals the `recording_name` by contract (see
    /// `supervisor.py`), so it is comparable against `SessionSnapshotResponse.recordingName`.
    struct StartedRecording: Equatable {
        let cursor: Int
        let sessionID: String
        /// U6: the effective audio state echoed by the daemon, or `nil` when a
        /// stale daemon omitted it — the orchestrator treats `nil` as audio-on.
        let audioEcho: Bool?
    }

    /// Error returned by `reload()` so the orchestrator can build the
    /// pre-refactor two-message split:
    ///   - `spawnFailed(Error)`     → "Failed to reload Screencap daemon: <desc>"
    ///   - `nonZeroExit(Int32, String?)` → "Failed to reload Screencap daemon."
    /// The `String?` carries the captured launchctl stderr so callers
    /// (or Console.app via OSLog) can surface "Could not find service" distinctly
    /// from a generic non-zero exit.
    enum ReloadError: Error {
        case spawnFailed(Error)
        case nonZeroExit(code: Int32, stderr: String?)
    }

    /// Callbacks the event-stream consumer needs from the orchestrator. The
    /// service does not own `pendingStartCursor` or the recording state; it
    /// reads/clears them via these hooks so behavior matches the pre-extraction
    /// `consumeDaemonEvents`.
    struct EventStreamCallbacks: Sendable {
        let onEvent: @MainActor (RecorderEventLine) -> Void
        let onTransientWarning: @MainActor (String) -> Void
        let getPendingStartCursor: @MainActor () -> Int?
        let clearPendingStartCursor: @MainActor () -> Void
        /// The `session_id` of the recording this controller started, or nil
        /// when the stream is attached to a pre-existing daemon session
        /// (`syncDaemonSnapshot`) for which no started identity exists. Used to
        /// reject a `cursor_unknown` snapshot promotion onto a *foreign* session
        /// (SCR-68). Unlike `pendingStartCursor`, it survives a 410 eviction.
        let getStartedSessionID: @MainActor () -> String?
        let isRecording: @MainActor () -> Bool
        /// Signal that the in-scope snapshot reports an active daemon-owned
        /// recording during `cursor_unknown` recovery. The orchestrator
        /// decides what to do — today it promotes `.starting → .recording`
        /// and ignores the call past `.starting`. Safe to invoke on every
        /// matching iteration. Carries the snapshot's CONFIRMED `muted` state
        /// (SCR-254 U7) so the controller re-hydrates mute state on reconnect
        /// (R6/AE4), independent of the lifecycle promotion.
        let onSnapshotConfirmedActiveRecording: @MainActor (Date, Bool) -> Void
    }
}

/// Daemon-backed recording lifecycle: probe, snapshot, start, stop, event
/// stream, and the typed-failure translation that drives the orchestrator's
/// transport fallback. Protocol seam so the orchestrator can inject a fake
/// in tests without spinning up `UnixHTTPTestServer`.
@MainActor
protocol DaemonSessionService {
    func probe() async -> DaemonSession.ProbeOutcome
    func snapshot() async -> DaemonSession.SnapshotOutcome
    func startRecording(name: String?, audio: Bool?) async throws -> DaemonSession.StartedRecording
    func stopRecording(force: Bool) async throws
    /// Forward a mute/unmute request for the running recording (SCR-254 U7).
    /// Returns the daemon's echo of the REQUESTED state — a transport ack, NOT
    /// confirmation (the confirming event flips the controller's `muted`, KTD4).
    func setMuted(_ muted: Bool) async throws -> Bool
    func translateFailure(_ error: Error) -> DaemonSession.FailureOutcome
    func reload() async -> Result<Void, DaemonSession.ReloadError>
    func consumeEventStream(callbacks: DaemonSession.EventStreamCallbacks) async -> DaemonSession.AttachOutcome
}

extension DaemonSessionService {
    /// Convenience overload mirroring the pre-protocol `stopRecording(force:)`
    /// default — production callers do not pass `force`.
    func stopRecording() async throws {
        try await stopRecording(force: false)
    }
}

/// Live implementation backed by `DaemonClient`. Wraps the raw client so the
/// orchestrator deals in typed outcomes rather than raw error switches.
@MainActor
final class LiveDaemonSessionService: DaemonSessionService {
    // MARK: - Probe / snapshot / lifecycle calls

    func probe() async -> DaemonSession.ProbeOutcome {
        do {
            let info = try await DaemonClient.daemonInfo()
            return .daemon(grants: info.permissions ?? .allIndeterminate)
        } catch DaemonClientError.schemaMismatch {
            return .schemaMismatch
        } catch {
            daemonSessionLogger.info("Daemon not reachable; using CLI fallback. Error: \(String(describing: error), privacy: .public)")
            return .unavailable
        }
    }

    func snapshot() async -> DaemonSession.SnapshotOutcome {
        do {
            let snap = try await DaemonClient.sessionSnapshot()
            guard snap.isRecording == true else { return .noActiveSession }
            if snap.daemonOwned {
                // SCR-276: check the finalizing overlay BEFORE promoting to a
                // live session. `is_recording` alone cannot distinguish the two
                // — the lock outlives the stop for the whole drain.
                if snap.finalizing == true {
                    return .finalizingSession(recordingName: snap.recordingName)
                }
                let started = snap.startedAt.map(Date.init(timeIntervalSince1970:)) ?? Date()
                // Additive `muted` overlay (U7): absent → unmuted (back-compat
                // with a stale daemon that omits it), mirroring the audio-echo rule.
                return .daemonOwnedSession(startedAt: started, muted: snap.muted ?? false)
            } else {
                return .foreignClaimant
            }
        } catch {
            daemonSessionLogger.info("Could not sync daemon session snapshot: \(String(describing: error), privacy: .public)")
            return .unreachable
        }
    }

    func startRecording(name: String?, audio: Bool?) async throws -> DaemonSession.StartedRecording {
        let response = try await DaemonClient.recordingStart(
            RecordingStartRequest(name: name, startedBy: "swiftui-via-daemon", audio: audio)
        )
        return DaemonSession.StartedRecording(
            cursor: response.cursor,
            sessionID: response.sessionID,
            audioEcho: response.audio
        )
    }

    func stopRecording(force: Bool) async throws {
        _ = try await DaemonClient.recordingStop(RecordingStopRequest(force: force))
    }

    func setMuted(_ muted: Bool) async throws -> Bool {
        let response = try await DaemonClient.recordingMute(RecordingMuteRequest(muted: muted))
        return response.muted
    }

    func translateFailure(_ error: Error) -> DaemonSession.FailureOutcome {
        switch error {
        case DaemonClientError.schemaMismatch:
            return .schemaMismatch
        case DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed:
            // Log the underlying error description here because `FailureOutcome`
            // collapses both transport-level failures into the same case and
            // the orchestrator's CLI-fallback log line would otherwise drop
            // the diagnostic detail (regressed log-content from pre-refactor).
            daemonSessionLogger.info("Daemon transport failed; falling back to CLI. Error: \(String(describing: error), privacy: .public)")
            return .socketUnavailable
        case DaemonClientError.envelopeError(let code, let rawBody)
            where code == DaemonErrorCode.finalizeInProgress:
            // SCR-276: routed BEFORE `lockContended` and never merged into it —
            // the whole point is that "already recording" is the wrong story
            // here. The name is a best-effort decode; the copy reads fine
            // without it.
            let name = (try? JSONDecoder().decode(FinalizeInProgressPayload.self, from: rawBody))?
                .recordingName
            return .finalizeInProgress(recordingName: name)
        case DaemonClientError.envelopeError(let code, _)
            where code == DaemonErrorCode.lockContended || code == DaemonErrorCode.notOwnedByDaemon:
            return .lockContended
        case DaemonClientError.envelopeError(let code, let rawBody)
            where code == DaemonErrorCode.permissionRequired:
            let missing = (try? JSONDecoder().decode(PermissionRequiredPayload.self, from: rawBody))?
                .missing ?? []
            return .permissionRequired(missing: missing)
        case DaemonClientError.envelopeError(let code, _)
            where code == DaemonErrorCode.migrationInProgress:
            // SCR-228: reuse `.other` with the honest copy so no render-site
            // change is needed — a recording can't start mid-migration.
            return .other(localizedDescription:
                PrivacySettingsPolicy.migrationFailureFallback(reason: "migration_in_progress"))
        default:
            return .other(localizedDescription: error.localizedDescription)
        }
    }

    /// Reload the daemon via `launchctl kickstart -kp`. Returns `.success`
    /// on a clean kickstart, `.failure(.spawnFailed)` if the subprocess
    /// could not launch, and `.failure(.nonZeroExit)` if launchctl returned
    /// a non-zero status (carries stderr for actionable diagnostics — the
    /// headless / no-LaunchAgent case prints "Could not find service…" here).
    func reload() async -> Result<Void, DaemonSession.ReloadError> {
        let result: LaunchctlResult
        do {
            result = try await runLaunchctl(["kickstart", "-kp", "gui/\(getuid())/com.screencap.daemon"])
        } catch {
            return .failure(.spawnFailed(error))
        }
        if result.terminationStatus == 0 {
            return .success(())
        }
        let stderrString = result.stderr.isEmpty ? nil : result.stderr
        if let stderrString {
            daemonSessionLogger.info("launchctl kickstart failed (exit \(result.terminationStatus, privacy: .public)): \(stderrString, privacy: .public)")
        }
        return .failure(.nonZeroExit(
            code: result.terminationStatus,
            stderr: stderrString
        ))
    }

    // MARK: - Event stream

    /// Consume the daemon event stream with capped exponential backoff for
    /// reconnects. Loops while `callbacks.isRecording()` returns true; exits
    /// via an `AttachOutcome` value the orchestrator translates into state
    /// transitions.
    func consumeEventStream(callbacks: DaemonSession.EventStreamCallbacks) async -> DaemonSession.AttachOutcome {
        // Capped exponential backoff for reconnects: a flat 100ms sleep would
        // hammer a daemon that is genuinely down, and a successful pass should
        // reset the dial. After `maxConsecutiveFailures` we give up and
        // surface the loss to the UI so the user can act. The 10-failure
        // budget covers transient transport errors *and* snapshot-fetch
        // errors — both can recover across a daemon restart, so a single
        // failure should not tear down the recording.
        var consecutiveFailures = 0
        // Separate streak for `cursor_unknown` recoveries where the snapshot
        // confirms the recording is alive (SCR-59). Does NOT arm `.lostContact`
        // — repeated evictions against a healthy recording are recovery-success,
        // not transport failure — but escalates backoff so a pathological
        // 410-only daemon cannot hot-loop at `baseBackoff` indefinitely.
        var consecutiveSnapshotRecoveries = 0
        let maxConsecutiveFailures = 10
        let baseBackoff: TimeInterval = 0.1
        let cappedBackoff: TimeInterval = 30.0
        // Lower cap for the recovery streak — at 5s a wedged daemon polls
        // ~once every 5s, low enough to be near-invisible but high enough
        // that recovery resumes promptly when the daemon comes back.
        let recoveryCappedBackoff: TimeInterval = 5.0

        while !Task.isCancelled, callbacks.isRecording() {
            var sawProgress = false

            // Snapshot fetch is in its own do/catch so a transient snapshot
            // failure doesn't fall through to the catch-all `.fatalError`
            // branch on the subscribe block. Pre-refactor `consumeDaemonEvents`
            // tolerated 10 consecutive snapshot errors before giving up; we
            // restore that here by counting them against the same budget as
            // stream drops.
            let snapshot: SessionSnapshotResponse
            do {
                snapshot = try await DaemonClient.sessionSnapshot()
            } catch {
                if Task.isCancelled { return .shutdown }
                daemonSessionLogger.info("Daemon snapshot failed; will retry. Error: \(String(describing: error), privacy: .public)")
                consecutiveFailures += 1
                if consecutiveFailures >= maxConsecutiveFailures {
                    return .lostContact
                }
                if callbacks.isRecording() {
                    let attempt = max(0, consecutiveFailures - 1)
                    let backoff = min(baseBackoff * pow(2.0, Double(attempt)), cappedBackoff)
                    try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
                }
                continue
            }

            do {
                if snapshot.isRecording == true, snapshot.daemonOwned == false {
                    return .foreignClaimant
                }
                if snapshot.recovering {
                    callbacks.onTransientWarning("Screencap daemon is recovering the previous recording session.")
                }
                // If the daemon snapshot says recording stopped while our local
                // state still says recording, the previous run terminated
                // outside this controller's awareness (engine crash, external
                // `screencap stop`, daemon restart that lost session). Without
                // this branch the `subscribe` below would wait forever on a
                // dead session and the UI would stay stuck in `.recording`
                // until the 10×backoff cap fires.
                if snapshot.isRecording == false, callbacks.isRecording() {
                    return .sessionEnded
                }

                // Until we receive `started` or `recording_failed`, keep using
                // the start boundary. If the first stream drops before the
                // boundary event is delivered, a reconnect from snapshot.cursor
                // could skip the event that moves the UI out of `.starting`.
                let sinceCursor = callbacks.getPendingStartCursor() ?? snapshot.cursor
                for try await event in DaemonClient.subscribe(sinceCursor: sinceCursor) {
                    if Task.isCancelled { return .shutdown }
                    sawProgress = true
                    callbacks.onEvent(event)
                    if event.type == "_close", event.reason == "shutdown" {
                        return .shutdown
                    }
                }
            } catch DaemonClientError.streamClosed(let reason) {
                if Task.isCancelled { return .shutdown }
                daemonSessionLogger.info("Daemon event stream dropped; reconnecting. Reason: \(reason, privacy: .public)")
            } catch DaemonClientError.envelopeError(let code, _) where code == "cursor_unknown" {
                if Task.isCancelled { return .shutdown }
                // Daemon replay window aged past the requested cursor (410).
                // If the snapshot confirms the recording is alive, signal
                // the orchestrator to promote `.starting → .recording`
                // (SCR-59) and skip the `consecutiveFailures` increment —
                // recovery-success must not drive `.lostContact`. Backoff
                // on a separate streak (`consecutiveSnapshotRecoveries`,
                // capped at 5s) prevents a 410-only daemon from hot-looping.
                // The `streamClosed` branch above keeps `pendingStartCursor`,
                // so a pre-`started` stream drop cascades into this branch.
                callbacks.clearPendingStartCursor()
                daemonSessionLogger.info("Daemon event stream evicted cursor; refetching snapshot.")
                // Cross-session identity guard (SCR-68): only treat the
                // in-scope snapshot as *our* recording when its `recording_name`
                // matches the `session_id` we started. The two are the same
                // value by the daemon contract (`supervisor.py` sets
                // `session_id == recording_name == name`). If the daemon
                // restarted or a foreign claimant took over between our start
                // and this 410, the snapshot may report a *different* healthy
                // daemon-owned session — promoting onto it would attribute the
                // UI to a recording we never started. A nil `startedSessionID`
                // means we attached to a pre-existing session and have no
                // identity to bind, so any healthy snapshot is accepted
                // (preserves the `syncDaemonSnapshot` attach path).
                let startedSessionID = callbacks.getStartedSessionID()
                let snapshotIsOurSession =
                    snapshot.isRecording == true
                    && snapshot.daemonOwned
                    && (startedSessionID == nil || snapshot.recordingName == startedSessionID)
                let recoveryBackoff: TimeInterval
                if snapshotIsOurSession {
                    let startedAt = snapshot.startedAt.map(Date.init(timeIntervalSince1970:)) ?? Date()
                    // Additive `muted` overlay (U7): absent → unmuted, so the
                    // controller re-hydrates a concrete confirmed value on reconnect.
                    callbacks.onSnapshotConfirmedActiveRecording(startedAt, snapshot.muted ?? false)
                    // Healthy snapshot is positive evidence the recording is
                    // alive — equivalent recovery signal to `sawProgress` at
                    // the bottom of the loop. Reset `consecutiveFailures` so
                    // accumulated drops from before this confirmation can't
                    // push us toward `.lostContact` on the next transient.
                    consecutiveFailures = 0
                    consecutiveSnapshotRecoveries += 1
                    let attempt = max(0, consecutiveSnapshotRecoveries - 1)
                    recoveryBackoff = min(baseBackoff * pow(2.0, Double(attempt)), recoveryCappedBackoff)
                } else {
                    consecutiveFailures += 1
                    if consecutiveFailures >= maxConsecutiveFailures {
                        return .lostContact
                    }
                    let attempt = max(0, consecutiveFailures - 1)
                    recoveryBackoff = min(baseBackoff * pow(2.0, Double(attempt)), cappedBackoff)
                }
                if callbacks.isRecording() {
                    try? await Task.sleep(nanoseconds: UInt64(recoveryBackoff * 1_000_000_000))
                }
                continue
            } catch {
                if Task.isCancelled { return .shutdown }
                return .fatalError(translateFailure(error))
            }

            if sawProgress {
                consecutiveFailures = 0
                consecutiveSnapshotRecoveries = 0
            } else {
                consecutiveFailures += 1
                if consecutiveFailures >= maxConsecutiveFailures {
                    return .lostContact
                }
            }

            if callbacks.isRecording() {
                let attempt = max(0, consecutiveFailures - 1)
                let backoff = min(baseBackoff * pow(2.0, Double(attempt)), cappedBackoff)
                try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
            }
        }
        return .shutdown
    }
}
