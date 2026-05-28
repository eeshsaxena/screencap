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
