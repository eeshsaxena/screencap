import Foundation

/// Persists whether the user has made the first-run cloud plan choice (U6).
/// Once decided (either "keep local" or "set up cloud"), the onboarding
/// plan-choice step is never re-shown — `FirstRunSetupPresentationPolicy`
/// keys on `decisionMade`. The contextual upsell + settings pane remain the
/// re-entry points afterward (R3).
///
/// Backed by `UserDefaults` (mirrors `PermissionController.setupDismissed`).
/// Injectable so tests point at an ephemeral suite instead of the shared domain.
@MainActor
final class CloudDecisionStore: ObservableObject {
    @Published private(set) var decisionMade: Bool

    private let defaults: UserDefaults
    private static let key = "com.screencap.macos.cloudDecisionMade"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.decisionMade = defaults.bool(forKey: Self.key)
    }

    /// Record that the user resolved the plan choice (local or cloud). Idempotent.
    func markDecided() {
        guard !decisionMade else { return }
        decisionMade = true
        defaults.set(true, forKey: Self.key)
    }

    /// Test seam: clear the persisted decision.
    func resetForTests() {
        decisionMade = false
        defaults.removeObject(forKey: Self.key)
    }
}
