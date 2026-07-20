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

    // MARK: - Encrypted-store Lock / Unlock (SCR-258 U10)

    /// Whether the menu-bar "Lock" item should appear. Shown only when the store is
    /// MOUNTED (there is something to seal), the encrypted container is enabled
    /// (locking a plaintext install would just be refused), and no lock/unlock is
    /// already in flight. Hidden when the store is absent or a plaintext-only build.
    static func lockItemVisible(
        storeState: StoreState, containerEnabled: Bool, phase: StoreController.Phase
    ) -> Bool {
        storeState.isMounted && containerEnabled && phase == .idle
    }

    /// Whether the menu-bar "Unlock" item should appear — only for a sealed store.
    /// A locked store always implies a container, so no container-enabled check is
    /// needed here.
    static func unlockItemVisible(storeState: StoreState, phase: StoreController.Phase) -> Bool {
        if case .locked = storeState { return phase == .idle }
        return false
    }

    // MARK: - Mute mic item (SCR-254 U8)

    /// Whether the menu-bar mute item should appear. Gated on `.recording` — it
    /// sits beside Stop in the `.recording` block, and there is no live mic to
    /// toggle in `.starting` / `.stopping` / `.idle`.
    static func muteItemVisible(state: RecordingState) -> Bool {
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

/// Shared mute-control presentation (SCR-254 U8) — ONE grammar for the HUD pill
/// and the menu-bar item so "Muted" never reads ambiguously as "tap to mute". The
/// control shows the CURRENT mic status ("Mic on" / "Muted"), a transitional
/// label while a toggle is in flight, plus the SF Symbol + VoiceOver label; both
/// surfaces derive their affordance from here so they can never drift. Pure, so
/// it is unit-testable and reused by `RecordingHUDModel`,
/// `RecorderController.toggleMute`, and the menu-bar item.
enum MuteControlPresentation {
    /// The mic's effective OFF state for both display and toggling: explicitly
    /// muted, OR a recording that started audio-off and has not been unmuted (its
    /// mic is not capturing). Reading this everywhere keeps "Muted" meaning "tap to
    /// turn the mic on" — so tapping the control on a `--no-audio` recording sends
    /// an UNMUTE (R2), not a redundant mute.
    static func effectivelyMuted(muted: Bool, audioEnabled: Bool) -> Bool {
        muted || !audioEnabled
    }

    /// The status label. While a toggle is in flight it shows the DIRECTION derived
    /// from the current confirmed state (since `muted` has not flipped yet —
    /// confirmed-state, KTD4), never an optimistic target.
    static func statusLabel(effectivelyMuted: Bool, inFlight: Bool) -> String {
        if inFlight { return effectivelyMuted ? "Unmuting…" : "Muting…" }
        return effectivelyMuted ? "Muted" : "Mic on"
    }

    /// The SF Symbol name — a slashed mic when off so the state reads at a glance,
    /// not by label alone (the distinct-visual requirement, U8).
    static func iconName(effectivelyMuted: Bool) -> String {
        effectivelyMuted ? "mic.slash.fill" : "mic.fill"
    }

    /// VoiceOver label — spells out the action so "Muted" is never read as a bare
    /// command. In-flight announces the direction under way.
    static func accessibilityLabel(effectivelyMuted: Bool, inFlight: Bool) -> String {
        if inFlight { return effectivelyMuted ? "Unmuting microphone" : "Muting microphone" }
        return effectivelyMuted
            ? "Microphone muted, tap to unmute"
            : "Microphone on, tap to mute"
    }
}
