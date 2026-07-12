import SwiftUI

/// SCR-262: every string the update-convergence surfaces render, in one place
/// so `UpdateConvergenceTests` can string-assert the copy (the honesty-gate
/// convention). Status only — no permission claims: the whole point of the
/// interstitial is NOT implying grants were lost while they can't be verified.
enum UpdateConvergenceCopy {
    static let headline = "Finishing update…"
    static let body =
        "ScreenCap is restarting its recording helper to pick up the update. "
        + "This usually takes under a minute — no action needed."

    /// Elapsed seconds after which the prolonged-wait line joins the card, so a
    /// long swap never reads as hung (mirrors `HelperInstallCard.statusText`'s
    /// phase-varied convention).
    static let prolongedWaitThreshold: TimeInterval = 30
    static let prolongedWait = "Still working — this can take a little longer on some Macs."

    /// KTD-6: the one-line acknowledgment the permission wall carries when it
    /// was reached via deadline expiry, bridging "Finishing update…" to the
    /// repair surface instead of dropping the user on a checklist unexplained.
    static let deadlineFallbackNotice =
        "The update didn't finish cleanly, so ScreenCap needs to check its recording helper."

    /// The card's secondary status line for the given elapsed wait, or nil while
    /// the wait is still ordinary. Pure so the elapsed→line decision is
    /// assertable without a render tree.
    static func statusLine(elapsedSeconds: TimeInterval) -> String? {
        elapsedSeconds >= prolongedWaitThreshold ? prolongedWait : nil
    }
}

/// SCR-262: the "Finishing update…" interstitial — the main window's content
/// while a helper swap converges after an app update. Replaces the spurious
/// permission wall for that window (the wall's rows can't be verified until the
/// swapped-in helper is up, so they read as lost grants and invite TCC
/// fiddling). Self-dismissing: `MainWindow` swaps it out the moment the
/// convergence loop verifies a fresh daemon or the deadline expires.
///
/// Styling mirrors `DaemonMigrationView`'s centered 520pt card; the spinner is
/// `HelperInstallCard`'s convergence idiom. The window-controls slot keeps the
/// real traffic lights (`.hiddenTitleBar`) usable during the wait.
struct UpdateConvergenceView: View {
    /// When the helper swap was triggered (`RecorderController`'s convergence
    /// anchor) — drives the prolonged-wait line. Nil renders as a fresh wait.
    let anchor: Date?

    var body: some View {
        VStack(spacing: 0) {
            windowControlsSlot
            Spacer()
            card
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.scCanvas)
    }

    private var card: some View {
        // Periodic redraw so the prolonged-wait line joins without any state:
        // the elapsed→line decision is pure (UpdateConvergenceCopy.statusLine).
        TimelineView(.periodic(from: .now, by: 5)) { context in
            VStack(alignment: .leading, spacing: 20) {
                HStack(alignment: .top, spacing: 14) {
                    ProgressView()
                        .controlSize(.regular)
                        // Decorative alongside the headline (the
                        // DaemonMigrationView icon convention).
                        .accessibilityHidden(true)
                        .padding(.top, 4)

                    VStack(alignment: .leading, spacing: 8) {
                        Text(UpdateConvergenceCopy.headline)
                            .font(SCTypography.serif(size: 24))
                            .foregroundStyle(Color.scInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(UpdateConvergenceCopy.body)
                            .font(SCTypography.sans(size: 13.5))
                            .foregroundStyle(Color.scInkSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                        if let line = UpdateConvergenceCopy.statusLine(
                            elapsedSeconds: elapsedSeconds(at: context.date)
                        ) {
                            Text(line)
                                .font(SCTypography.sans(size: 12.5))
                                .foregroundStyle(Color.scInkSecondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            .padding(28)
            .frame(width: 520)
            .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusPanel))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusPanel)
                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
            )
        }
    }

    private func elapsedSeconds(at date: Date) -> TimeInterval {
        guard let anchor else { return 0 }
        return max(0, date.timeIntervalSince(anchor))
    }

    /// Reserve the real traffic lights' overlay slot (`.hiddenTitleBar`),
    /// mirroring `PermissionSetupTakeover` and the onboarding wizard.
    private var windowControlsSlot: some View {
        Color.clear
            .frame(height: 12)
            .padding(.vertical, 18)
    }
}
