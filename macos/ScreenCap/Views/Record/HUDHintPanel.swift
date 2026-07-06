import AppKit
import SwiftUI

// U5 — the one-time "your controls live in the menu bar now" hint (design 8a,
// state 2). Shown the first time the user ever hides the recording toolbar, then
// never again (gated by `HUDHintStore` + `HUDHintPolicy`). Like the HUD it is a
// non-activating, capture-excluded (`sharingType = .none`) floating panel (KTD-7)
// so it never lands in the recording. Anchored to the top-right menu-bar region
// of the active display — SwiftUI's `MenuBarExtra` cannot host an anchored
// popover, so the hint approximates the menu-bar location (Risk Analysis).

/// The hint bubble copy (design 8a, state 2's tooltip). Teaches where status/Stop
/// went AND the ⌘⇧H restore shortcut (design's own tooltip names it).
struct HUDHintView: View {
    let onDismiss: () -> Void

    var body: some View {
        Button(action: onDismiss) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Still recording — the controls live up here now.")
                    .font(SCTypography.sans(size: 13, weight: .semibold))
                    .foregroundStyle(Color.scPaper)
                    .fixedSize(horizontal: false, vertical: true)
                Text("⌘⇧H brings the toolbar back")
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scHUDMuted)
            }
            .multilineTextAlignment(.leading)
            .padding(.horizontal, 16)
            .padding(.vertical, 13)
            .frame(width: 300, alignment: .leading)
            .background(Color.scDarkCanvas, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5)
            )
            .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .padding(12)
        .fixedSize()
        .help("Dismiss")
        .accessibilityLabel("Recording controls are hidden. Status and Stop are in the menu bar. Press Command Shift H to bring the toolbar back.")
        .accessibilityAddTraits(.isButton)
        .accessibilityHint("Dismiss this hint")
    }
}

/// Owns the one-time hint's floating `NSPanel`. Presents once per invocation,
/// auto-dismisses after a dwell OR on click, and calls `onComplete` exactly once
/// **after** the hint has actually been shown — so the caller marks the store
/// only when the user genuinely had a chance to see it (not at presentation).
@MainActor
final class HUDHintPanelController {
    private var panel: NSPanel?
    private var dismissTask: Task<Void, Never>?
    private var onComplete: (() -> Void)?

    /// How long the hint lingers before auto-dismissing if the user doesn't click
    /// it. Deliberately generous (not "a few seconds") so a distracted first-time
    /// user still sees it.
    private static let dwell: TimeInterval = 8

    func present(onComplete: @escaping () -> Void) {
        // Idempotent: a second present() while one is up does nothing.
        guard panel == nil else { return }
        self.onComplete = onComplete

        let hosting = NSHostingView(rootView: HUDHintView(onDismiss: { [weak self] in
            self?.dismiss()
        }))
        let panel = HUDPanel.captureExcluded(
            contentRect: NSRect(x: 0, y: 0, width: 340, height: 100),
            contentView: hosting
        )
        self.panel = panel

        panel.layoutIfNeeded()
        if let content = panel.contentView {
            let fit = content.fittingSize
            if fit.width > 1, fit.height > 1 {
                panel.setContentSize(fit)
            }
        }
        reposition(panel)
        panel.orderFrontRegardless()

        // VoiceOver: announce the guidance, since a visual-only bubble is invisible
        // to non-sighted users (design accessibility parity with the HUD controls).
        NSAccessibility.post(
            element: NSApp,
            notification: .announcementRequested,
            userInfo: [
                .announcement: "Recording controls hidden. Status and Stop are in the menu bar. Press Command Shift H to bring the toolbar back.",
                .priority: NSAccessibilityPriorityLevel.high.rawValue
            ]
        )

        dismissTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.dwell * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.dismiss()
        }
    }

    /// Tear the panel down and fire `onComplete` once. Safe to call repeatedly
    /// (click, auto-timeout, and recording-end teardown can all land). Callable by
    /// `WindowLifecycle.dismissHideHint()` so the hint dies with the recording
    /// instead of lingering with now-false "Still recording" copy.
    func dismiss() {
        dismissTask?.cancel()
        dismissTask = nil
        panel?.orderOut(nil)
        panel = nil
        let complete = onComplete
        onComplete = nil
        complete?()
    }

    /// Top-right of the main display, just below the menu bar — the region the
    /// menu-bar recording glyph lives in, which the hint points at.
    private func reposition(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(
            x: visible.maxX - size.width - 12,
            y: visible.maxY - size.height - 8
        ))
    }
}
