import Foundation

/// Hide control — pure gating for the menu-bar "Show recording controls" item.
/// The item restores the recording HUD pill after the user dismissed it, so it
/// is offered only while a recording is live AND the pill is currently hidden.
/// Factored out of `MenuBarMenu` so the gate is unit-testable (the SwiftUI menu
/// view itself is not), matching the app's other `*Policy` structs.
enum MenuBarMenuPolicy {
    /// Whether the "Show recording controls" menu item should appear.
    /// Gated on the `.recording` case specifically — `.starting` / `.stopping`
    /// have no stable pill to restore, and `hudHidden` is only ever set during
    /// `.recording` anyway (`RecorderController.hideRecordingHUD` guards on it).
    static func showRecordingControlsVisible(state: RecordingState, hudHidden: Bool) -> Bool {
        guard hudHidden else { return false }
        if case .recording = state { return true }
        return false
    }
}
