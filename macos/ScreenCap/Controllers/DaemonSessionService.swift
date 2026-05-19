import Foundation
import OSLog

private let daemonSessionLogger = Logger(subsystem: "com.screencap.macos", category: "daemon-session")

/// Daemon-backed recording lifecycle: probe, snapshot, start, stop, event
/// stream, and the typed-failure translation that drives the orchestrator's
/// transport fallback. Wraps `DaemonClient` so the orchestrator deals in
/// typed outcomes rather than raw error switches.
@MainActor
final class DaemonSessionService {
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

    /// Callbacks the event-stream consumer needs from the orchestrator. The
    /// service does not own `pendingStartCursor` or the recording state; it
    /// reads/clears them via these hooks so behavior matches the pre-extraction
    /// `consumeDaemonEvents`.
    struct EventStreamCallbacks {
        let onEvent: @MainActor (RecorderEventLine) -> Void
        let onTransientWarning: @MainActor (String) -> Void
        let getPendingStartCursor: @MainActor () -> Int?
        let clearPendingStartCursor: @MainActor () -> Void
        let isRecording: @MainActor () -> Bool
    }

    // MARK: - Probe / snapshot / lifecycle calls

    func probe() async -> ProbeOutcome {
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

    func snapshot() async -> SnapshotOutcome {
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

    func stopRecording(force: Bool = false) async throws {
        _ = try await DaemonClient.recordingStop(RecordingStopRequest(force: force))
    }

    func translateFailure(_ error: Error) -> FailureOutcome {
        switch error {
        case DaemonClientError.schemaMismatch:
            return .schemaMismatch
        case DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed:
            return .socketUnavailable
        case DaemonClientError.envelopeError(let code, _)
            where code == DaemonErrorCode.lockContended || code == DaemonErrorCode.notOwnedByDaemon:
            return .lockContended
        default:
            return .other(localizedDescription: error.localizedDescription)
        }
    }

    /// Reload the daemon via `launchctl kickstart -kp`. Returns true on
    /// successful kickstart.
    func reload() async -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["kickstart", "-kp", "gui/\(getuid())/com.screencap.daemon"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                DispatchQueue.global(qos: .userInitiated).async {
                    process.waitUntilExit()
                    continuation.resume()
                }
            }
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }

    // MARK: - Event stream

    /// Consume the daemon event stream with capped exponential backoff for
    /// reconnects. Loops while `callbacks.isRecording()` returns true; exits
    /// via an `AttachOutcome` value the orchestrator translates into state
    /// transitions.
    func consumeEventStream(callbacks: EventStreamCallbacks) async -> AttachOutcome {
        // Capped exponential backoff for reconnects: a flat 100ms sleep would
        // hammer a daemon that is genuinely down, and a successful pass should
        // reset the dial. After `maxConsecutiveFailures` we give up and
        // surface the loss to the UI so the user can act.
        var consecutiveFailures = 0
        let maxConsecutiveFailures = 10
        let baseBackoff: TimeInterval = 0.1
        let cappedBackoff: TimeInterval = 30.0

        while !Task.isCancelled, callbacks.isRecording() {
            var sawProgress = false
            do {
                let snapshot = try await DaemonClient.sessionSnapshot()
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
                // the UI does not flicker to `.idle`.
                callbacks.clearPendingStartCursor()
                daemonSessionLogger.info("Daemon event stream evicted cursor; refetching snapshot.")
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
