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
    /// Honest status U7 — set only after the first-recording beat sheet actually
    /// presented and was dismissed (the hide-hint idiom above), so a start that
    /// nothing can host (menu-bar start, window closed) never burns the one-shot.
    static let firstRecordingBeatSeenKey = "com.screencap.macos.firstRecordingBeatSeen"
    /// Honest status U7 — set at the onboarding download-model step (on BOTH a
    /// choice and a skip). Its absence is what makes the beat "catch-up": a user
    /// who never reached that step (existing users) has it unset and is eligible.
    static let intelligenceChoiceSeenKey = "com.screencap.macos.intelligenceChoiceSeen"
    /// Day-diary U9 — the morning resume card's dismissals, KEYED by "day|thread"
    /// (unlike the single-boolean local-model hint): dismissing one day's thread
    /// must not suppress a later day's card (a new day re-arms — R11/KTD-11).
    /// Persisted as an ordered `[String]` (most-recent last) so growth is bounded
    /// by `resumeCardDismissalCap`; exposed as a `Set` for membership checks.
    static let resumeCardDismissedKeysKey = "com.screencap.macos.resumeCardDismissedKeys"
    /// Cap on retained resume dismissals — the set is tiny (one per resumed day)
    /// but the key space is unbounded over time, so keep only the most recent N.
    static let resumeCardDismissalCap = 60

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

    /// Whether the first-recording beat was actually seen (U7 — gates the one-time beat).
    var firstRecordingBeatSeen: Bool {
        defaults.bool(forKey: Self.firstRecordingBeatSeenKey)
    }

    func markFirstRecordingBeatSeen() {
        defaults.set(true, forKey: Self.firstRecordingBeatSeenKey)
    }

    /// Whether the intelligence choice was made or seen in onboarding (U7 catch-up).
    var intelligenceChoiceSeen: Bool {
        defaults.bool(forKey: Self.intelligenceChoiceSeenKey)
    }

    func markIntelligenceChoiceSeen() {
        defaults.set(true, forKey: Self.intelligenceChoiceSeenKey)
    }

    /// The set of dismissed morning-resume-card keys ("day|thread" — U9/KTD-11).
    /// Read as a Set so the gating predicate can do O(1) membership checks; the
    /// underlying storage is an ordered array for bounded pruning.
    var dismissedResumeKeys: Set<String> {
        Set(defaults.stringArray(forKey: Self.resumeCardDismissedKeysKey) ?? [])
    }

    /// Persist a dismissed resume-card key. Re-appends (most-recent last), dedupes,
    /// and prunes to the most recent `resumeCardDismissalCap` keys so the store
    /// never grows without bound as the user resumes across many days.
    func markResumeCardDismissed(_ key: String) {
        var keys = defaults.stringArray(forKey: Self.resumeCardDismissedKeysKey) ?? []
        keys.removeAll { $0 == key }
        keys.append(key)
        if keys.count > Self.resumeCardDismissalCap {
            keys = Array(keys.suffix(Self.resumeCardDismissalCap))
        }
        defaults.set(keys, forKey: Self.resumeCardDismissedKeysKey)
    }
}
