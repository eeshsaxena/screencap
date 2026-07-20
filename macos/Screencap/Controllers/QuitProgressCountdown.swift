import Foundation

/// Drives the Cmd+Q finalization countdown after the initial value has already
/// been published to the UI. Updates begin one second later at N-1, which
/// avoids an immediate duplicate of the initial number and keeps cancellation
/// from sprinting the countdown to zero.
enum QuitProgressCountdown {
    static func run(
        totalSeconds: Int,
        onUpdate: (Int) async -> Void,
        sleep: () async throws -> Void
    ) async {
        guard totalSeconds > 0 else { return }

        for remaining in stride(from: totalSeconds - 1, through: 0, by: -1) {
            do {
                try await sleep()
            } catch {
                return
            }
            if Task.isCancelled { return }
            await onUpdate(remaining)
        }
    }
}
