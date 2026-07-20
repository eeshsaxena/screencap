import SwiftUI

/// One-time Phase 1c migration banner (SCR-49). Shown once to existing users on
/// upgrade — before the daemon-install + TCC permissions screen — to explain
/// that recording permissions now belong to the background helper and will
/// persist across future updates. Tapping Continue advances to the
/// install/grant flow; the `MigrationMarkerStore` marker ensures the banner
/// never shows again once migration completes.
///
/// This view carries **no permission logic of its own** — the marker suppresses
/// only the banner, never the daemon's independent TCC verification (R4). It is
/// presented as the leading step of the permission-setup takeover
/// (`PermissionSetupTakeover`, U14), centered in the window.
struct DaemonMigrationView: View {
    /// Invoked when the user taps Continue. The host
    /// (`PermissionSetupTakeover`) advances to the daemon-install + TCC
    /// permissions screen; the marker is written later, when the install
    /// completes.
    let onContinue: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "checkmark.shield.fill")
                    .font(.system(size: 30))
                    .foregroundStyle(Color.scTeal)
                    // Decorative — the adjacent heading + body convey the full
                    // meaning, so keep VoiceOver from announcing the symbol as a
                    // separate, content-free focus stop (mirrors the
                    // adHocDevBuildCallout / finishSetupBanner convention).
                    .accessibilityHidden(true)
                    .padding(.top, 2)

                VStack(alignment: .leading, spacing: 8) {
                    Text("Grant recording permissions once")
                        .font(SCTypography.serif(size: 24))
                        .foregroundStyle(Color.scInk)
                        .fixedSize(horizontal: false, vertical: true)
                    // Copy is written to suit both an upgrading user (whose grants
                    // moved to the helper) and a first-time user — it states how
                    // permissions work now without implying a prior re-granting
                    // pain a fresh install never had.
                    Text(
                        "Screencap records through a background helper that holds "
                        + "the screen recording, accessibility, and input-monitoring "
                        + "permissions. Grant them once and they'll keep working "
                        + "across every future Screencap update."
                    )
                    .font(SCTypography.sans(size: 13.5))
                    .foregroundStyle(Color.scInkSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                }
            }

            HStack {
                Spacer()
                OnboardingPrimaryButton(title: "Continue", shortcut: .defaultAction) {
                    onContinue()
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
