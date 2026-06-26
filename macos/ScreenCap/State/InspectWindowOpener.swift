import Foundation

/// Test-injectable seam over the per-recording inspect window's
/// `openWindow(id:value:)` action — the read-only "just looking" sibling of
/// `ReviewWindowOpener`. The closure is registered by the zero-frame bridge
/// inside the main scene body, so AppKit-context callers (the Recordings-list
/// row click) and unit tests that have no `@Environment(\.openWindow)` can still
/// open an inspect window — or substitute a fake to assert the call.
@MainActor
final class InspectWindowOpener: ObservableObject {
    static let shared = InspectWindowOpener()

    /// Set by the bridge view inside the main window's body. Nil before the
    /// first window renders, so callsites must use optional chaining.
    var openInspect: ((String) -> Void)?

    /// Pending one-shot seek targets (absolute unix ms) keyed by recording name.
    /// A search-result tap sets this just before opening the window;
    /// `InspectWindow` reads and clears it when it reaches `.ready` (SCR-174's
    /// out-of-band delivery). Keeping the seek off the scene value lets the
    /// window stay keyed on the recording name, so opening the same recording at
    /// two different moments reuses one window instead of spawning duplicates.
    var pendingSeekMs: [String: Int] = [:]

    private init() {}

    /// Convenience entry point. Forwards to `openInspect` if registered,
    /// otherwise no-ops and returns `false` so callers can report a "not ready"
    /// state if they care. Mirrors `ReviewWindowOpener.open`.
    @discardableResult
    func open(recordingName: String) -> Bool {
        guard let openInspect else { return false }
        openInspect(recordingName)
        return true
    }
}
