import AppKit
import SwiftUI

// U7 — the floating recording HUD (design 29–41): a bottom-center dark pill with
// a pulsing amber elapsed clock, the provisional recording title, a functional
// Mute control (SCR-254), Hide, and a teal "Stop & save", plus the
// "recording to this Mac" sub-caption. It lives
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
    /// SCR-254 (U8): the confirmed live mute state + pending flag driving the Mute
    /// control's status/icon/a11y. Default so pre-mute call sites and tests that
    /// only care about elapsed/title/footer stay terse.
    var muted: Bool = false
    var muteInFlight: Bool = false

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
    var hideAccessibilityLabel: String { "Hide recording controls" }

    // MARK: - Mute control (SCR-254 U8) — derived from the shared grammar so the
    // HUD pill and the menu-bar item can never drift.

    /// The mic's effective OFF state: explicitly muted, or a recording that started
    /// audio-off and has not been unmuted (its mic isn't capturing).
    var micEffectivelyMuted: Bool {
        MuteControlPresentation.effectivelyMuted(muted: muted, audioEnabled: audioEnabled)
    }
    /// The status label — "Mic on" / "Muted", or "Muting…" / "Unmuting…" in flight.
    var micStatusLabel: String {
        MuteControlPresentation.statusLabel(effectivelyMuted: micEffectivelyMuted, inFlight: muteInFlight)
    }
    /// The SF Symbol — a slashed mic when off.
    var micIconName: String {
        MuteControlPresentation.iconName(effectivelyMuted: micEffectivelyMuted)
    }
    /// VoiceOver label spelling out the toggle action.
    var micAccessibilityLabel: String {
        MuteControlPresentation.accessibilityLabel(effectivelyMuted: micEffectivelyMuted, inFlight: muteInFlight)
    }
}

/// The SwiftUI HUD content, bound to the live recorder for elapsed / title / audio.
struct RecordingHUDView: View {
    @ObservedObject var recorder: RecorderController

    private var model: RecordingHUDModel {
        RecordingHUDModel(
            elapsed: recorder.state.elapsed,
            title: recorder.currentRecordingName ?? "Recording",
            audioEnabled: recorder.audioEnabled,
            muted: recorder.muted,
            muteInFlight: recorder.muteInFlight
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
        // Transparent room for the pill's soft shadow so the fittingSize-based
        // panel sizing in `show()` doesn't clip the blur (U4).
        .padding(14)
        .fixedSize()
    }

    private var pill: some View {
        HStack(spacing: 4) {
            elapsedGroup
            titleChip
            divider
            muteButton
            hideButton
            divider
            stopButton
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color.scDarkCanvas, in: Capsule())
        // A hairline edge so the dark capsule separates from any background even
        // with a much lighter shadow than the old radius-16 / opacity-0.4 halo.
        .overlay(Capsule().strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5))
        // Softer, tighter lift — the old shadow read as a grey cloud on light
        // content. `body`'s `.padding(14)` gives this blur transparent room so
        // the fittingSize-based panel sizing in `show()` doesn't clip it (U4).
        .shadow(color: .black.opacity(0.28), radius: 10, y: 4)
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

    /// The functional Mute control (SCR-254 U8). The muted state gets a DISTINCT
    /// visual — a filled rust pill + `mic.slash.fill` — so the mic being off reads
    /// at a glance, not by label alone. While a toggle is in flight it shows a
    /// transitional "Muting…"/"Unmuting…" and disables, so the control never shows
    /// an optimistic target state before capture actually changed (KTD4).
    private var muteButton: some View {
        Button {
            recorder.toggleMute(source: .hud)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: model.micIconName)
                Text(model.micStatusLabel)
            }
            .font(SCTypography.sans(size: 13))
            .foregroundStyle(model.micEffectivelyMuted ? Color.scPaper : Color.scHUDMuted)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(model.micEffectivelyMuted ? Color.scRust : Color.clear, in: Capsule())
            .opacity(model.muteInFlight ? 0.6 : 1)
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(model.muteInFlight)
        .help(model.micEffectivelyMuted ? "Turn the microphone on" : "Mute the microphone")
        .accessibilityLabel(model.micAccessibilityLabel)
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

    /// Dismisses the pill for the rest of the recording (design 8a). A plain
    /// "Hide" label beside the Mute control; restored via ⌘⇧H, the bottom-edge
    /// peek, or the menu-bar "Show recording controls" item.
    private var hideButton: some View {
        Button {
            recorder.hideRecordingHUD()
        } label: {
            Text("Hide")
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scHUDMuted)
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Hide recording controls (⌘⇧H)")
        .accessibilityLabel(model.hideAccessibilityLabel)
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
        // Size the panel to the pill ONCE, after the hosting view has laid out
        // inside the window (its `fittingSize` is degenerate before that — the
        // cause of the earlier collapsed-pill). A single measure-then-set is
        // safe because the content is `.fixedSize()` (width-independent), so the
        // resize can't feed back into another layout pass. (An
        // NSHostingController with `.preferredContentSize` DID feed back — an
        // infinite Auto Layout recursion that overflowed the stack.)
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

        // A generous initial size the pill lays out within; `show()` shrinks the
        // panel to the pill's measured `fittingSize` after the first layout pass.
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 100),
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
