import Foundation

/// Test-injectable seam over the per-recording review window's
/// `openWindow(id:value:)` action. The closure is registered by a zero-frame
/// SwiftUI bridge inside the main scene body (mirroring
/// `WindowOpener` / `OpenWindowBridge`), so AppKit-context callers or unit
/// tests that have no `@Environment(\.openWindow)` available can still open
/// a review window — or substitute a fake to assert the call.
///
/// The Library card's "Review & upload…" action (U5) has the SwiftUI
/// environment available and calls `openWindow(...)` directly; the Inspect
/// window's Review deep link goes through this seam. The singleton also
/// exists for parity with `WindowOpener` and for the unit-test observability
/// the plan calls out under U3.
@MainActor
final class ReviewWindowOpener: ObservableObject {
    static let shared = ReviewWindowOpener()

    /// Set by the bridge view inside the main window's body. Nil before the
    /// first window renders, so callsites must use optional chaining.
    var openReview: ((String) -> Void)?

    /// SCR-174: pending one-shot seek targets (absolute unix ms) keyed by
    /// recording name. A search-result tap sets this just before opening the
    /// window; `ReviewWindow` reads and clears it when it reaches `.ready`.
    /// Delivering the seek out-of-band (rather than via the scene value) keeps
    /// the window keyed on the recording name, so opening the same recording at
    /// two different moments reuses one window instead of spawning duplicates.
    var pendingSeekMs: [String: Int] = [:]

    /// SCR-219 (U5): pending one-shot **clip ranges** keyed by recording name —
    /// the clip-mode sibling of `pendingSeekMs`. The Day timeline's clip-bounds
    /// overlay sets this just before opening the Review window; `ReviewWindow`
    /// (U6) reads and clears it when it reaches `.ready`, then scopes the
    /// consent surface + local-file export to `[startMs, endMs)`. Carried
    /// out-of-band (not through the scene value) for exactly the reason
    /// `pendingSeekMs` is: the review `WindowGroup` scene stays keyed on the
    /// recording name (a `String`, KTD5), so a clip review and a whole-recording
    /// review of the same recording address one window key without changing the
    /// scene's value type. Absence of an entry means "not a clip" — the window
    /// behaves as a normal whole-recording review.
    var pendingClipRange: [String: ClipRange] = [:]

    private init() {}

    /// Convenience entry point. Forwards to `openReview` if registered,
    /// otherwise no-ops. Safe to call before the bridge has registered the
    /// closure (returns false to let callers report a "not ready" state if
    /// they care).
    @discardableResult
    func open(recordingName: String) -> Bool {
        guard let openReview else { return false }
        openReview(recordingName)
        return true
    }
}
