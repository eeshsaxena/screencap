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

    // MARK: - Collapsed account section (account-sheet U5)

    /// The "Account…" menu item title — always present; it opens the main
    /// window's Account & Plan pane (KTD-4).
    static let accountItemTitle = "Account…"

    /// The single account status line above the "Account…" item. An in-flight
    /// or failed sign-in takes priority over the persistent status so the user
    /// always sees what the browser round-trip is doing; the failed line is
    /// deliberately static copy (no raw reason string — R10) since the pane is
    /// where retry + details live now.
    static func accountStatusLine(
        status: AuthStatus,
        signInFlow: SignInFlowState
    ) -> String {
        switch signInFlow {
        case .inProgress:
            return "Signing in… check your browser"
        case .failed:
            return "Sign-in failed — open Account to retry"
        case .idle:
            switch status {
            case .signedIn:
                return status.accountLabel.map { "Signed in: \($0)" } ?? "Signed in (offline)"
            case .signedOut:
                return "Not signed in"
            case .unknown:
                return "Checking sign-in…"
            }
        }
    }
}
