import Foundation

/// Pure gating for the recording-anchored intelligence surfaces (U7 first-recording
/// beat, U8 dead-state banner). No SwiftUI, no I/O — the record path and views read
/// these so the fire/adapt decisions stay unit-testable (the repo's pure-policy
/// convention, mirroring `OnboardingStepPolicy`).
enum IntelligenceSurfacePolicy {
    /// The first-recording beat (R1/R3) fires **once for everyone** — adaptive content,
    /// not gated on the verdict — as **catch-up**: only when the intelligence choice
    /// wasn't already made or seen in onboarding. So a fresh install that made the
    /// choice in the wizard is never asked again, and an existing user who never saw
    /// the wizard still gets the one-time beat.
    static func shouldShowFirstRecordingBeat(hasRecordedOnce: Bool, onboardingChoiceSeen: Bool) -> Bool {
        !hasRecordedOnce && !onboardingChoiceSeen
    }

    /// The beat's adaptive content, resolved from the live verdict (U5 / KTD3). While
    /// the verdict is unresolved (config facts still loading), the beat **waits**
    /// (`.awaitVerdict`) rather than showing a choose-a-model prompt to a user whose
    /// model is actually ready — and the "has recorded once" marker must not be set
    /// until the verdict resolves or the user acts (AE2).
    enum BeatMode: Equatable, Sendable { case awaitVerdict, lightConfirm, chooseModel }
    static func beatMode(verdict: IntelligenceVerdict?) -> BeatMode {
        guard let v = verdict else { return .awaitVerdict }
        return v.usable ? .lightConfirm : .chooseModel
    }

    /// The dead-state banner (R2/R4) appears only when a **resolved** verdict is
    /// not-usable and the surface isn't suppressed for this recording (the user just
    /// skipped the beat, or dismissed the banner, on this recording). An unresolved
    /// verdict never shows the banner (fail quiet), so it never nags during the
    /// async config-facts load window.
    static func shouldShowDeadStateBanner(verdict: IntelligenceVerdict?, suppressedThisRecording: Bool) -> Bool {
        guard let v = verdict else { return false }
        return !v.usable && !suppressedThisRecording
    }
}
