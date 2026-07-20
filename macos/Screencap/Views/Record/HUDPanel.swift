import AppKit

/// Shared factory for the recording HUD's floating surfaces (the peek bar and the
/// one-time hint). Every surface shown over recorded content MUST be excluded from
/// the capture (`sharingType = .none`) so it never appears in the user's recording
/// — a leak is a blocker, not a styling bug (R9 / KTD-7). Centralizing the panel
/// configuration keeps that invariant in one place and makes it unit-testable
/// (`HUDPanelTests`), instead of relying on each call site re-setting it correctly.
enum HUDPanel {
    /// A borderless, non-activating floating panel excluded from screen capture.
    /// Non-activating so clicking it never steals focus from the recorded app;
    /// `.floating` + all-Spaces so it rides over full-screen apps; `sharingType =
    /// .none` so it is never captured. Mirrors the shipped HUD pill's panel setup
    /// (`RecordingHUDPanelController.ensurePanel`).
    @MainActor
    static func captureExcluded(contentRect: NSRect, contentView: NSView) -> NSPanel {
        let panel = NSPanel(
            contentRect: contentRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        // Never appears inside the recording (R9 / KTD-7).
        panel.sharingType = .none
        panel.contentView = contentView
        return panel
    }
}
