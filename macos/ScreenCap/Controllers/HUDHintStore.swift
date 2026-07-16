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
    /// SCR-239 — the standing "enable local intelligence" sidebar hint, dismissed
    /// once (persisted so it never reappears — R7 no intrusive re-prompt).
    static let localModelHintDismissedKey = "com.screencap.macos.localModelHintDismissed"
    /// Honest status U7 — set the first time a recording is started, so the
    /// one-time first-recording beat fires at most once.
    static let hasRecordedOnceKey = "com.screencap.macos.hasRecordedOnce"
    /// Honest status U7 — set at the onboarding download-model step (on BOTH a
    /// choice and a skip). Its absence is what makes the beat "catch-up": a user
    /// who never reached that step (existing users) has it unset and is eligible.
    static let intelligenceChoiceSeenKey = "com.screencap.macos.intelligenceChoiceSeen"

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

    var hasDismissedLocalModelHint: Bool {
        defaults.bool(forKey: Self.localModelHintDismissedKey)
    }

    func markLocalModelHintDismissed() {
        defaults.set(true, forKey: Self.localModelHintDismissedKey)
    }

    /// Whether a recording has ever been started (U7 — gates the one-time beat).
    var hasRecordedOnce: Bool {
        defaults.bool(forKey: Self.hasRecordedOnceKey)
    }

    func markRecordedOnce() {
        defaults.set(true, forKey: Self.hasRecordedOnceKey)
    }

    /// Whether the intelligence choice was made or seen in onboarding (U7 catch-up).
    var intelligenceChoiceSeen: Bool {
        defaults.bool(forKey: Self.intelligenceChoiceSeenKey)
    }

    func markIntelligenceChoiceSeen() {
        defaults.set(true, forKey: Self.intelligenceChoiceSeenKey)
    }
}
