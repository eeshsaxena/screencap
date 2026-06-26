import Foundation

// SCR-174 U7 — the pure decision logic behind the in-app on-screen-text
// indexing consent flow, extracted from `SearchView` so its invariants are
// unit-testable without the SwiftUI/CLIClient surface (plan U7).
//
// The state is two independent booleans persisted as settings:
//   - `indexEnabled`           — `content_index_enabled`
//   - `declined`               — `content_index_consent_declined`
// "never asked" = both false; "consented" = indexEnabled; "declined" = declined.
//
// The UI applies each transition OPTIMISTICALLY (flip the in-memory flag, then
// persist), and reverts on a write failure so the on-disk truth and the UI
// never diverge — the "success-latch" the review calls out (#10). A transient
// settings-read failure must NOT advance or clear either flag.

/// The two-bool consent state shared by the Search consent banner.
struct ContentIndexConsentState: Equatable, Sendable {
    var indexEnabled: Bool
    var declined: Bool

    /// Drives the consent CTA: free-text was queried but indexing is off and the
    /// user hasn't dismissed the prompt.
    func bannerVisible(hasFreeText: Bool) -> Bool {
        hasFreeText && !indexEnabled && !declined
    }
}

/// Pure transitions for the consent flow. Each `optimistic*` produces the state
/// to show immediately; the matching `*DidFail` reverts it if the persist write
/// fails (so a failed write can never falsely advance the latch).
enum ContentIndexConsent {
    /// "Turn on" tapped: optimistically enable indexing.
    static func optimisticEnable(_ s: ContentIndexConsentState) -> ContentIndexConsentState {
        var next = s
        next.indexEnabled = true
        return next
    }

    /// The enable write failed: revert to the pre-tap (disabled) value.
    static func enableDidFail(_ s: ContentIndexConsentState) -> ContentIndexConsentState {
        var next = s
        next.indexEnabled = false
        return next
    }

    /// "Not now" tapped: optimistically record the decline.
    static func optimisticDecline(_ s: ContentIndexConsentState) -> ContentIndexConsentState {
        var next = s
        next.declined = true
        return next
    }

    /// The decline write failed: revert so the banner can re-appear (the user's
    /// decline never persisted).
    static func declineDidFail(_ s: ContentIndexConsentState) -> ContentIndexConsentState {
        var next = s
        next.declined = false
        return next
    }

    /// Apply a successful settings read. A `nil` field means the daemon omitted
    /// it (older daemon) → treat as the conservative default (off / not
    /// declined).
    static func applyLoaded(
        indexEnabled: Bool?, declined: Bool?, into s: ContentIndexConsentState
    ) -> ContentIndexConsentState {
        ContentIndexConsentState(
            indexEnabled: indexEnabled ?? false,
            declined: declined ?? false
        )
    }

    /// A settings READ failed: assume indexing off (conservative for the CTA)
    /// but PRESERVE the prior `declined` decision — a transient read failure
    /// must not resurface a banner the user already dismissed (#10).
    static func loadDidFail(_ s: ContentIndexConsentState) -> ContentIndexConsentState {
        ContentIndexConsentState(indexEnabled: false, declined: s.declined)
    }
}
