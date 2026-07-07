import AppKit
import SwiftUI

// U3 — the bottom-edge "Show controls" peek (design 8a, state 3). While the pill
// is hidden, resting the cursor at the bottom screen edge reveals this slim bar;
// clicking it (or ⌘⇧H) pins the toolbar back. Like the HUD pill it lives in a
// non-activating, capture-excluded (`sharingType = .none`) floating `NSPanel`
// (KTD-7) so it never appears in the recording and never steals focus from the
// recorded app. Reveal/hide timing is driven by `LiveHUDInputMonitor`; this file
// only owns the bar's appearance and its panel.

/// The slim peek bar: a pulsing amber clock and a "Show controls ⌘⇧H" affordance.
/// The whole bar is the restore control (design shows one tappable strip).
struct HUDPeekView: View {
    @ObservedObject var recorder: RecorderController
    let onActivate: () -> Void

    private var elapsedText: String {
        RecordingHUDModel(
            elapsed: recorder.state.elapsed,
            title: recorder.currentRecordingName ?? "Recording",
            audioEnabled: recorder.audioEnabled
        ).elapsedText
    }

    var body: some View {
        Button(action: onActivate) {
            HStack(spacing: 10) {
                Circle()
                    .fill(Color.scAmberHUD)
                    .frame(width: 8, height: 8)
                    .overlay(Circle().stroke(Color.scAmberHUD.opacity(0.25), lineWidth: 3))
                Text(elapsedText)
                    .font(SCTypography.mono(size: 12))
                    .foregroundStyle(Color.scAmberHUD)
                Rectangle()
                    .fill(Color.scHUDSurfaceRaised)
                    .frame(width: 1, height: 16)
                Text("Show controls")
                    .font(SCTypography.sans(size: 12.5, weight: .medium))
                    .foregroundStyle(Color.scFillSubtle)
                Text("⌘⇧H")
                    .font(SCTypography.mono(size: 10.5))
                    .foregroundStyle(Color.scHUDMuted)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(Color.scDarkCanvas, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .padding(10)
        .fixedSize()
        .help("Show recording controls (⌘⇧H)")
        .accessibilityLabel("Show recording controls")
        .accessibilityHint("Restores the recording toolbar")
    }
}

/// Owns the peek bar's floating `NSPanel`. Same non-activating, all-Spaces,
/// capture-excluded configuration as `RecordingHUDPanelController` (KTD-7).
@MainActor
final class HUDPeekPanelController {
    private var panel: NSPanel?

    var isVisible: Bool { panel != nil }

    /// The panel's current screen frame (global coordinates), or nil when hidden.
    /// The input monitor uses this as part of the peek's keep-alive region so the
    /// bar doesn't dismiss the instant the cursor moves up off the edge band to
    /// click it.
    var frameOnScreen: CGRect? { panel?.frame }

    func show(recorder: RecorderController, onActivate: @escaping () -> Void) {
        guard panel == nil else { return }
        let hosting = NSHostingView(
            rootView: HUDPeekView(recorder: recorder, onActivate: onActivate)
        )
        let panel = HUDPanel.captureExcluded(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 60),
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
    }

    func hide() {
        panel?.orderOut(nil)
        // Load-bearing (mirrors RecordingHUDPanelController.hide): the hosting view
        // strongly retains the RecorderController passed into HUDPeekView, which
        // transitively owns this controller through the input monitor — so niling
        // the panel is what breaks that transient cycle. The monitor's `recorder`
        // is weak, keeping the monitor itself out of the cycle.
        panel = nil
    }

    /// Bottom-center of the main display, pinned just above the visible-frame
    /// bottom so it clears the Dock (mirrors the pill's `visibleFrame.minY + 34`).
    private func reposition(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(
            x: visible.midX - size.width / 2,
            y: visible.minY + 12
        ))
    }
}
