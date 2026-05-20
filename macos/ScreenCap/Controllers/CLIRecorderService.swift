import Foundation

/// Subprocess lifecycle for the CLI-fallback recording transport. Wraps
/// `CLIClient.spawn` so the orchestrator deals in `RecorderEventLine` and
/// exit codes, not raw stderr bytes.
@MainActor
protocol CLIRecorderService {
    /// Currently-spawned process (`nil` if not running). Read by the stop
    /// policy / orchestrator for the SIGKILL-on-Cmd+Q-timeout guard. Exposed
    /// as the `SpawnedProcessHandle` protocol so tests can substitute a fake
    /// without spawning a real subprocess.
    var currentProcess: SpawnedProcessHandle? { get }

    /// Spawn `screencap` with the given args, decode stderr lines as
    /// `RecorderEventLine`, and dispatch them via `onEvent`. `onTerminated`
    /// fires after the subprocess exits.
    func start(
        args: [String],
        onEvent: @escaping @MainActor @Sendable (RecorderEventLine) -> Void,
        onTerminated: @escaping @MainActor @Sendable (Int32) -> Void
    ) throws
}

extension RecorderEventLine {
    /// Decode a single stderr line emitted by `screencap start`. Returns nil
    /// for blank, non-JSON, or malformed input — drift-resilient by design so
    /// a future field doesn't brick the SwiftUI parser.
    static func parse(stderrLine: String) -> RecorderEventLine? {
        let trimmed = stderrLine.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.hasPrefix("{") else { return nil }
        guard let data = trimmed.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(RecorderEventLine.self, from: data)
    }
}

/// Live implementation backed by `CLIClient.spawn`. Owns the
/// `SpawnedProcess` handle and the stderr → JSON decode step.
@MainActor
final class LiveCLIRecorderService: CLIRecorderService {
    private(set) var currentProcess: SpawnedProcessHandle?

    func start(
        args: [String],
        onEvent: @escaping @MainActor @Sendable (RecorderEventLine) -> Void,
        onTerminated: @escaping @MainActor @Sendable (Int32) -> Void
    ) throws {
        let proc = try CLIClient.spawn(
            args: args,
            // Use DispatchQueue.main.async (not Task { @MainActor }) for both
            // dispatch sites: GCD's main queue is strictly FIFO, so a final
            // `recording_finalized` line dispatched from `terminationHandler`'s
            // drain step is guaranteed to land on MainActor before the
            // subsequent `onTerminated` block. Mixing `Task { @MainActor }`
            // for one side and DispatchQueue for the other gives no FIFO
            // guarantee, allowing handleProcessTerminated to resolve the
            // awaiting continuation with `false` before the in-flight event
            // ran. See /rf:review finding #4.
            onStderrLine: { line in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        if let event = RecorderEventLine.parse(stderrLine: line) {
                            onEvent(event)
                        }
                    }
                }
            },
            onTerminated: { [weak self] exitCode in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated {
                        self?.currentProcess = nil
                        onTerminated(exitCode)
                    }
                }
            }
        )
        currentProcess = proc
    }
}
