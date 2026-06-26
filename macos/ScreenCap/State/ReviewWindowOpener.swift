import Foundation

/// Test-injectable seam over the per-recording review window's
/// `openWindow(id:value:)` action. The closure is registered by a zero-frame
/// SwiftUI bridge inside the main scene body (mirroring
/// `WindowOpener` / `OpenWindowBridge`), so AppKit-context callers or unit
/// tests that have no `@Environment(\.openWindow)` available can still open
/// a review window — or substitute a fake to assert the call.
///
/// Production callsite is the Upload button in `RecordingsListView`, which
/// has the SwiftUI environment available and calls `openWindow(...)` directly.
/// This singleton exists for parity with `WindowOpener` and for the unit-test
/// observability the plan calls out under U3.
@MainActor
final class ReviewWindowOpener: ObservableObject {
    static let shared = ReviewWindowOpener()

    /// Set by the bridge view inside the main window's body. Nil before the
    /// first window renders, so callsites must use optional chaining.
    var openReview: ((String) -> Void)?

    /// SCR-174: pending one-shot seek targets (absolute unix ms) keyed by
    /// recording name. A search-result tap sets this just before opening the
    /// window; `ReviewWindow` consumes it (read-and-clear) when it reaches
    /// `.ready`. Delivering the seek out-of-band (rather than via the scene
    /// value) keeps the window keyed on the recording name, so opening the same
    /// recording at two different moments reuses one window instead of spawning
    /// duplicates. Private storage — go through `setPendingSeek` /
    /// `consumePendingSeek` so the one-shot contract is enforced in one place.
    private var pendingSeekMs: [String: Int] = [:]

    private init() {}

    /// Record a one-shot seek target for `recording`, to be consumed by the
    /// Review window when it reaches `.ready`.
    func setPendingSeek(_ anchorMs: Int, for recording: String) {
        pendingSeekMs[recording] = anchorMs
    }

    /// Read-and-clear the pending seek for `recording` (one-shot). Returns nil
    /// when none is pending.
    func consumePendingSeek(for recording: String) -> Int? {
        defer { pendingSeekMs[recording] = nil }
        return pendingSeekMs[recording]
    }

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
