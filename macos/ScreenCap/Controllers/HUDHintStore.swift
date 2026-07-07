import Foundation

/// Persistence for the one-time "your controls live in the menu bar now" hint
/// shown on the user's first-ever toolbar hide (U5, R7).
///
/// Backed by `UserDefaults` with an injectable suite so tests never touch
/// `.standard` — the `OnboardingMarkerStore` / `RecentSearchesStore` idiom. The
/// flag is set only **after** the hint has actually been displayed (the live
/// panel calls back on dismissal/timeout), so a distracted first-time user is
/// not silently robbed of the one guidance moment.
struct HUDHintStore {
    static let hasShownHideHintKey = "com.screencap.macos.hasShownHideHint"

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    var hasShownHideHint: Bool {
        defaults.bool(forKey: Self.hasShownHideHintKey)
    }

    func markShown() {
        defaults.set(true, forKey: Self.hasShownHideHintKey)
    }
}
