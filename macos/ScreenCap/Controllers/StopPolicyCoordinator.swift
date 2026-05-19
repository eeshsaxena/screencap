import Foundation

/// Orchestrates the stop policy: dispatch the stop signal, await the matching
/// stderr/daemon event (`recording_finalized` for in-app Stop,
/// `stopped` for Cmd+Q), and report the outcome. The orchestrator
/// translates the outcome into state transitions, `NSApp.reply`, and any
/// SIGKILL / `lastError` updates.
///
/// Owns the awaiting-continuation arrays and the timeout race; this is the
/// single client of `waitForOneShot`, so the abstraction lives with it.
@MainActor
final class StopPolicyCoordinator {
    enum Outcome {
        case sendSignalFailed(Error)
        case completed
        case timedOut

        var isCompleted: Bool { if case .completed = self { return true }; return false }
        var isTimedOut: Bool { if case .timedOut = self { return true }; return false }
        var sendSignalError: Error? {
            if case .sendSignalFailed(let error) = self { return error }
            return nil
        }
    }

    /// Pending awaits keyed by event type. Resolved when the matching event
    /// arrives or when the timeout fires.
    private var awaitingFinalized: [(Bool) -> Void] = []
    private var awaitingStopped: [(Bool) -> Void] = []

    /// Called from the state-machine `.resolveAwaiting(.finalized, success:)`
    /// effect. Flushes any pending finalized-await continuations.
    func resolveFinalized(_ success: Bool) {
        drainResumes(into: \.awaitingFinalized, value: success)
    }

    /// Called from the state-machine `.resolveAwaiting(.stopped, success:)`
    /// effect. Flushes any pending stopped-await continuations.
    func resolveStopped(_ success: Bool) {
        drainResumes(into: \.awaitingStopped, value: success)
    }

    /// Resolve every pending continuation with `false`. Use on tear-down so
    /// callers cannot deadlock.
    func cancelAll() {
        drainResumes(into: \.awaitingFinalized, value: false)
        drainResumes(into: \.awaitingStopped, value: false)
    }

    /// Runs the stop policy. The orchestrator picks `sendStopSignal` per
    /// transport (`CLIClient.runDetached(["stop"])` vs
    /// `DaemonClient.recordingStop(...)`).
    /// - Parameters:
    ///   - quitting: `false` uses the 30s in-app wait against
    ///     `recording_finalized`; `true` uses the 300s Cmd+Q wait against
    ///     `stopped` and drives the quit-progress countdown via `onTickQuitProgress`.
    ///   - timeout: optional override (used by tests). When `nil`, uses the
    ///     production defaults (30s / 300s).
    ///   - sendStopSignal: async closure dispatching the platform-specific
    ///     stop request.
    ///   - onTickQuitProgress: called once per second of the Cmd+Q wait so the
    ///     orchestrator can update `quitProgressSecondsRemaining`.
    func runStop(
        quitting: Bool,
        timeout: TimeInterval? = nil,
        sendStopSignal: () async throws -> Void,
        onTickQuitProgress: @escaping @MainActor (Int) async -> Void = { _ in }
    ) async -> Outcome {
        do {
            try await sendStopSignal()
        } catch {
            // The stop subprocess never launched — the recorder never
            // received SIGTERM, so waiting 30s/300s for stderr events would
            // surface a false "still finalizing" message. Bail out so the
            // orchestrator can roll state back and (for Cmd+Q) tell AppKit
            // to abort the quit so the app doesn't hang on `.terminateLater`.
            return .sendSignalFailed(error)
        }

        let effective = timeout ?? (quitting ? 300 : 30)
        let success: Bool
        if quitting {
            success = await waitForOneShot(
                into: \.awaitingStopped,
                timeout: effective,
                tickQuitProgress: onTickQuitProgress
            )
        } else {
            success = await waitForOneShot(
                into: \.awaitingFinalized,
                timeout: effective,
                tickQuitProgress: nil
            )
        }
        return success ? .completed : .timedOut
    }

    private func drainResumes(
        into keyPath: ReferenceWritableKeyPath<StopPolicyCoordinator, [(Bool) -> Void]>,
        value: Bool
    ) {
        let resumes = self[keyPath: keyPath]
        self[keyPath: keyPath] = []
        for resume in resumes { resume(value) }
    }

    private func waitForOneShot(
        into keyPath: ReferenceWritableKeyPath<StopPolicyCoordinator, [(Bool) -> Void]>,
        timeout: TimeInterval,
        tickQuitProgress: (@MainActor (Int) async -> Void)?
    ) async -> Bool {
        await withCheckedContinuation { continuation in
            var resumed = false
            var timeoutTask: Task<Void, Never>?
            let resume: (Bool) -> Void = { value in
                Task { @MainActor in
                    if resumed { return }
                    resumed = true
                    // Cancel the timeout sleep so it doesn't sit for the full
                    // 30s/300s wall-clock after a successful event.
                    timeoutTask?.cancel()
                    continuation.resume(returning: value)
                }
            }
            // If the timeout path resumes the continuation first, this closure
            // stays in the array as a no-op (the `resumed` flag prevents
            // double-resume) until the next drain runs. A stop cycle that
            // times out without any subsequent successful event would leak
            // one closure per timeout — vanishingly rare in practice and
            // self-cleaning on the next event. Tracked as a residual cleanup;
            // deferred until usage shows it bites.
            self[keyPath: keyPath].append(resume)
            timeoutTask = Task { @MainActor in
                if let tickQuitProgress {
                    await QuitProgressCountdown.run(totalSeconds: Int(timeout)) { remaining in
                        await tickQuitProgress(remaining)
                    } sleep: {
                        try await Task.sleep(nanoseconds: 1_000_000_000)
                    }
                } else {
                    try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                }
                if Task.isCancelled { return }
                resume(false)
            }
        }
    }
}
