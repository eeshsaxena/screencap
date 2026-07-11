import Foundation
import LocalAuthentication

// Search U6 (R4 / KTD4): present-user gating for user-facing corpus DISPLAY. Gates
// showing search-result stills / the truth pane / full-size stills behind Touch ID
// (or the password sheet on hardware without a sensor), with a per-session grace
// window so scrolling a thumbnail grid doesn't re-prompt every cell. Background
// daemon indexing is NEVER gated (a daemon cannot prompt); agents ride the
// same-EUID bar (OQ3). This is app-display-only.

/// Injectable present-user evaluator (protocol + fake, mirroring the
/// `VideoPlaybackEngine` fake seam) so the gate is testable without real biometrics.
protocol PresenceEvaluating {
    var isAvailable: Bool { get }
    func evaluatePresence(reason: String) async -> Bool
}

/// The real LocalAuthentication evaluator. `.deviceOwnerAuthentication` transparently
/// falls back from Touch ID to the password sheet, so it works on Macs without a
/// sensor; `isAvailable` is false only when neither biometrics nor a password exist.
struct LocalAuthPresence: PresenceEvaluating {
    var isAvailable: Bool {
        var error: NSError?
        return LAContext().canEvaluatePolicy(.deviceOwnerAuthentication, error: &error)
    }

    func evaluatePresence(reason: String) async -> Bool {
        let context = LAContext()
        return await withCheckedContinuation { continuation in
            context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, _ in
                continuation.resume(returning: success)
            }
        }
    }
}

@MainActor
final class PresenceGate: ObservableObject {
    static let defaultGraceWindow: TimeInterval = 15 * 60  // 15 minutes (OQ1 proposed)

    private let evaluator: PresenceEvaluating
    private let graceWindow: TimeInterval
    private let now: () -> Date
    private var lastGrantedAt: Date?

    init(
        evaluator: PresenceEvaluating = LocalAuthPresence(),
        graceWindow: TimeInterval = PresenceGate.defaultGraceWindow,
        now: @escaping () -> Date = Date.init
    ) {
        self.evaluator = evaluator
        self.graceWindow = graceWindow
        self.now = now
    }

    /// Whether corpus display is currently unlocked (a successful auth is still
    /// within the grace window).
    var isUnlocked: Bool {
        guard let last = lastGrantedAt else { return false }
        return now().timeIntervalSince(last) < graceWindow
    }

    /// Ensure present-user auth before revealing corpus content. Returns true
    /// immediately when still within the grace window; otherwise prompts and, on
    /// success, (re)opens the window. Fails CLOSED (returns false, content stays
    /// hidden) when no auth mechanism is available or the user cancels/fails.
    func ensurePresent(reason: String = "View your recording history") async -> Bool {
        if isUnlocked { return true }
        guard evaluator.isAvailable else { return false }
        let granted = await evaluator.evaluatePresence(reason: reason)
        if granted { lastGrantedAt = now() }
        return granted
    }

    /// Drop the grace window so the next corpus view re-prompts (e.g. on lock/logout).
    func lock() { lastGrantedAt = nil }
}
