import Foundation
import OSLog

private let daemonSessionLogger = Logger(subsystem: "com.screencap.macos", category: "daemon-session")

/// Wire-level daemon envelope error codes the controller branches on.
/// Mirrors `src/screencap/daemon/errors.py`'s string constants.
enum DaemonErrorCode {
    static let lockContended = "lock_contended"
    static let notOwnedByDaemon = "not_owned_by_daemon"
}

/// Typed outcomes returned by the daemon session service. Hoisted out of the
/// concrete class so the protocol can declare them in its surface without a
/// nested-type cycle.
enum DaemonSession {
    enum ProbeOutcome: Equatable {
        case daemon
        case schemaMismatch
        case unavailable
    }

    enum SnapshotOutcome: Equatable {
        case noActiveSession
        case daemonOwnedSession(startedAt: Date)
        case foreignClaimant
        case unreachable
    }

    enum FailureOutcome: Equatable {
        case schemaMismatch
        case socketUnavailable
        case lockContended
        case other(localizedDescription: String)
    }

    enum AttachOutcome: Equatable {
        case shutdown
        case foreignClaimant
        case sessionEnded
        case lostContact
        case fatalError(FailureOutcome)
    }

    /// Error returned by `reload()` so the orchestrator can build the
    /// pre-refactor two-message split:
    ///   - `spawnFailed(Error)`     → "Failed to reload ScreenCap daemon: <desc>"
    ///   - `nonZeroExit(Int32, String?)` → "Failed to reload ScreenCap daemon."
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
        let isRecording: @MainActor () -> Bool
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
    func startRecording(name: String?) async throws -> Int
    func stopRecording(force: Bool) async throws
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
            _ = try await DaemonClient.daemonInfo()
            return .daemon
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
                let started = snap.startedAt.map(Date.init(timeIntervalSince1970:)) ?? Date()
                return .daemonOwnedSession(startedAt: started)
            } else {
                return .foreignClaimant
            }
        } catch {
            daemonSessionLogger.info("Could not sync daemon session snapshot: \(String(describing: error), privacy: .public)")
            return .unreachable
        }
    }

    func startRecording(name: String?) async throws -> Int {
        let response = try await DaemonClient.recordingStart(
            RecordingStartRequest(name: name, startedBy: "swiftui-via-daemon")
        )
        return response.cursor
    }

    func stopRecording(force: Bool) async throws {
        _ = try await DaemonClient.recordingStop(RecordingStopRequest(force: force))
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
        case DaemonClientError.envelopeError(let code, _)
            where code == DaemonErrorCode.lockContended || code == DaemonErrorCode.notOwnedByDaemon:
            return .lockContended
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
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["kickstart", "-kp", "gui/\(getuid())/com.screencap.daemon"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = Pipe()
        let stderrPipe = Pipe()
        process.standardError = stderrPipe
        do {
            try process.run()
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                DispatchQueue.global(qos: .userInitiated).async {
                    process.waitUntilExit()
                    continuation.resume()
                }
            }
            if process.terminationStatus == 0 {
                return .success(())
            }
            let stderrData = try? stderrPipe.fileHandleForReading.readToEnd()
            let stderrString = stderrData.flatMap { String(data: $0, encoding: .utf8) }?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if let stderrString, !stderrString.isEmpty {
                daemonSessionLogger.info("launchctl kickstart failed (exit \(process.terminationStatus, privacy: .public)): \(stderrString, privacy: .public)")
            }
            return .failure(.nonZeroExit(
                code: process.terminationStatus,
                stderr: stderrString?.isEmpty == false ? stderrString : nil
            ))
        } catch {
            return .failure(.spawnFailed(error))
        }
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
        let maxConsecutiveFailures = 10
        let baseBackoff: TimeInterval = 0.1
        let cappedBackoff: TimeInterval = 30.0

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
                    callbacks.onTransientWarning("ScreenCap daemon is recovering the previous recording session.")
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
                // The requested cursor was evicted from the daemon's replay
                // window between snapshot and subscribe. Refetch the snapshot
                // and resubscribe with a fresh cursor; leave state intact so
                // the UI does not flicker to `.idle`. Count this against the
                // failure budget and apply backoff so a persistently mismatched
                // replay window cannot become an infinite hot loop.
                callbacks.clearPendingStartCursor()
                daemonSessionLogger.info("Daemon event stream evicted cursor; refetching snapshot.")
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
            } catch {
                if Task.isCancelled { return .shutdown }
                return .fatalError(translateFailure(error))
            }

            if sawProgress {
                consecutiveFailures = 0
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
