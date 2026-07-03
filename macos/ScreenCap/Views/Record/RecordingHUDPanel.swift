import AppKit
import SwiftUI

// U7 — the floating recording HUD (design 29–41): a bottom-center dark pill with
// a pulsing amber elapsed clock, the provisional recording title, Draw/Mute stubs,
// and a teal "Stop & save", plus the "recording to this Mac" sub-caption. It lives
// in a separate non-activating floating `NSPanel` (KTD-4) so it survives the
// main-window hide and floats over full-screen apps on every Space, and is marked
// capture-excluded (`sharingType = .none`).

/// The HUD's pure presentation model — elapsed formatting, the honesty-substituted
/// footer copy (KTD-9), and the accessibility labels — factored out so it is
/// testable without a render (RecordingHUDModelTests).
struct RecordingHUDModel: Equatable {
    let elapsed: TimeInterval
    let title: String
    let audioEnabled: Bool

    /// KTD-9: the design's "recording to this Mac · encrypted" drops "· encrypted"
    /// (no encryption exists until SCR-220).
    static let footerText = "recording to this Mac"

    /// `MM:SS`, growing to `H:MM:SS` past an hour (design's mono clock).
    var elapsedText: String {
        let total = max(0, Int(elapsed.rounded(.down)))
        let hours = total / 3600, minutes = (total % 3600) / 60, seconds = total % 60
        return hours > 0
            ? String(format: "%d:%02d:%02d", hours, minutes, seconds)
            : String(format: "%02d:%02d", minutes, seconds)
    }

    /// A spoken elapsed for VoiceOver — the raw `MM:SS` reads as digits otherwise.
    var elapsedAccessibilityLabel: String {
        let total = max(0, Int(elapsed.rounded(.down)))
        let hours = total / 3600, minutes = (total % 3600) / 60, seconds = total % 60
        var parts: [String] = []
        if hours > 0 { parts.append("\(hours) hour\(hours == 1 ? "" : "s")") }
        if minutes > 0 { parts.append("\(minutes) minute\(minutes == 1 ? "" : "s")") }
        parts.append("\(seconds) second\(seconds == 1 ? "" : "s")")
        return "Recording time " + parts.joined(separator: " ")
    }

    var titleAccessibilityLabel: String { "Recording \(title)" }
    var stopAccessibilityLabel: String { "Stop and save recording" }
}

/// The SwiftUI HUD content, bound to the live recorder for elapsed / title / audio.
struct RecordingHUDView: View {
    @ObservedObject var recorder: RecorderController

    private var model: RecordingHUDModel {
        RecordingHUDModel(
            elapsed: recorder.state.elapsed,
            title: recorder.currentRecordingName ?? "Recording",
            audioEnabled: recorder.audioEnabled
        )
    }

    var body: some View {
        VStack(spacing: 6) {
            pill
            Text(RecordingHUDModel.footerText)
                .font(SCTypography.mono(size: 9.5))
                .foregroundStyle(Color.scInkMuted)
                .accessibilityHidden(true)
        }
        .fixedSize()
    }

    private var pill: some View {
        HStack(spacing: 4) {
            elapsedGroup
            titleChip
            divider
            stub("Draw", ticket: "SCR-217")
            stub(model.audioEnabled ? "Mute" : "Muted", ticket: "SCR-218")
            divider
            stopButton
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color.scDarkCanvas, in: Capsule())
        .shadow(color: .black.opacity(0.4), radius: 16, y: 12)
    }

    private var elapsedGroup: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(Color.scAmberHUD)
                .frame(width: 9, height: 9)
                .overlay(Circle().stroke(Color.scAmberHUD.opacity(0.25), lineWidth: 4))
            Text(model.elapsedText)
                .font(SCTypography.mono(size: 13))
                .foregroundStyle(Color.scAmberHUD)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .accessibilityElement()
        .accessibilityLabel(model.elapsedAccessibilityLabel)
    }

    private var titleChip: some View {
        Text(model.title)
            .font(SCTypography.sans(size: 13))
            .foregroundStyle(Color.scFillSubtle)
            .lineLimit(1)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(Color.scHUDSurface, in: Capsule())
            .accessibilityLabel(model.titleAccessibilityLabel)
    }

    private var divider: some View {
        Rectangle()
            .fill(Color.scHUDSurfaceRaised)
            .frame(width: 1, height: 22)
            .padding(.horizontal, 4)
            .accessibilityHidden(true)
    }

    /// A disabled HUD control stub (Draw SCR-217 / Mute SCR-218), rendered per the
    /// design but non-functional (KTD-8).
    private func stub(_ label: String, ticket: String) -> some View {
        Text(label)
            .font(SCTypography.sans(size: 13))
            .foregroundStyle(Color.scHUDMuted)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .help("Coming soon — \(ticket)")
            .accessibilityLabel("\(label), coming soon")
    }

    private var stopButton: some View {
        Button {
            recorder.stop()
        } label: {
            Text("Stop & save")
                .font(SCTypography.sans(size: 13, weight: .bold))
                .foregroundStyle(Color.scPaper)
                .padding(.horizontal, 18)
                .padding(.vertical, 9)
                .background(Color.scTeal, in: Capsule())
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(model.stopAccessibilityLabel)
    }
}

/// Owns the floating recording HUD `NSPanel`. Non-activating so clicking Stop
/// never steals focus from the app being recorded; `.floating` + all-Spaces so
/// it rides over full-screen apps; `sharingType = .none` so it is excluded from
/// the recording itself.
///
/// NOTE (KTD-4, manual QA): capture-exclusion via `sharingType = .none` must be
/// verified against the real `screencapture`-CLI capture path — see
/// `docs/runbooks/new-ui-manual-qa.md` (U14). If a captured frame ever contains
/// the pill, that is a blocker to record in KTD-4, not a styling tweak.
@MainActor
final class RecordingHUDPanelController {
    private var panel: NSPanel?

    func show(recorder: RecorderController) {
        let panel = ensurePanel(recorder: recorder)
        reposition(panel)
        panel.orderFrontRegardless()
    }

    func hide() {
        panel?.orderOut(nil)
        // Load-bearing: the panel's hosting view strongly retains the
        // RecorderController (passed into RecordingHUDView), which transitively
        // owns this controller — so niling the panel is what breaks that cycle.
        // Every show() is paired with a hide() on teardown (RecorderController's
        // effect/transitionToIdle paths), so the cycle is transient.
        panel = nil
    }

    private func ensurePanel(recorder: RecorderController) -> NSPanel {
        if let panel { return panel }
        let hosting = NSHostingView(rootView: RecordingHUDView(recorder: recorder))
        hosting.setFrameSize(hosting.fittingSize)

        let panel = NSPanel(
            contentRect: NSRect(origin: .zero, size: hosting.fittingSize),
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
        panel.isMovableByWindowBackground = true
        panel.isReleasedWhenClosed = false
        // Exclude the HUD from screen capture so it never appears in the recording.
        panel.sharingType = .none
        panel.contentView = hosting
        panel.setContentSize(hosting.fittingSize)
        self.panel = panel
        return panel
    }

    /// Bottom-center of the main display, 34pt up from the visible-frame bottom
    /// (design line 29).
    private func reposition(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(
            x: visible.midX - size.width / 2,
            y: visible.minY + 34
        ))
    }
}
