import CoreGraphics

/// Bottom-edge peek — pure gating for the "Show controls" reveal (U3, R4-R6).
///
/// The reveal decision is factored out of the live cursor-poll detector so it is
/// unit-testable (the poll timer, panel presentation, and dwell/anti-flicker
/// timing live in `LiveHUDInputMonitor`; only the boolean geometry/state gate
/// lives here). Mirrors the app's other `*Policy` structs (`MenuBarMenuPolicy`,
/// `NewRecordingSheetPolicy`, `OnboardingStepPolicy`).
enum HUDPeekPolicy {
    /// Whether the peek bar may be revealed at all: only while a recording is live
    /// AND the pill is user-hidden AND the cursor is dwelling in the bottom edge
    /// band. The detector adds the dwell requirement on top of this gate.
    static func shouldReveal(isRecording: Bool, hudHidden: Bool, cursorInBand: Bool) -> Bool {
        isRecording && hudHidden && cursorInBand
    }

    /// Whether the global cursor location sits within the bottom edge band of the
    /// given screen's **visible** frame. Callers must pass `visibleFrame` (which
    /// excludes the Dock and menu bar), not the full `frame` — the peek must clear
    /// a bottom Dock, matching the shipped pill's `visibleFrame.minY + 34`
    /// anchoring (`RecordingHUDPanelController.reposition`). Coordinates are the
    /// global, bottom-left-origin screen space of `NSEvent.mouseLocation`.
    static func cursorInBottomBand(cursor: CGPoint, visibleFrame: CGRect, band: CGFloat) -> Bool {
        cursor.x >= visibleFrame.minX
            && cursor.x <= visibleFrame.maxX
            && cursor.y >= visibleFrame.minY
            && cursor.y <= visibleFrame.minY + band
    }
}
