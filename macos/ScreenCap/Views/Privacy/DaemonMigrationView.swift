import SwiftUI

/// One-time Phase 1c migration banner (SCR-49). Shown once to existing users on
/// upgrade — before the daemon-install + TCC walkthrough — to explain that
/// recording permissions now belong to the background helper and will persist
/// across future updates. Tapping Continue advances to the install/grant flow
/// (`FirstRunPermissionsView`); the `MigrationMarkerStore` marker ensures the
/// banner never shows again once migration completes.
///
/// This view carries **no permission logic of its own** — the marker suppresses
/// only the banner, never the daemon's independent TCC verification (R4). It is
/// presented as the leading step of the existing first-run sheet, so it adopts
/// the same `padding`/`frame` as `FirstRunPermissionsView`.
struct DaemonMigrationView: View {
    /// Invoked when the user taps Continue. The host
    /// (`FirstRunPermissionsView`) advances to the daemon-install + TCC
    /// walkthrough; the marker is written later, when the install completes.
    let onContinue: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "checkmark.shield.fill")
                    .font(.system(size: 30))
                    .foregroundStyle(.tint)
                    // Decorative — the adjacent heading + body convey the full
                    // meaning, so keep VoiceOver from announcing the symbol as a
                    // separate, content-free focus stop (mirrors the
                    // adHocDevBuildCallout / finishSetupBanner convention).
                    .accessibilityHidden(true)
                    .padding(.top, 2)

                VStack(alignment: .leading, spacing: 8) {
                    Text("Permissions now stay put across updates")
                        .font(.title2.bold())
                    Text(
                        "ScreenCap now uses a background helper to record your "
                        + "screen. Grant permissions once and they'll persist "
                        + "across all future ScreenCap updates — no more "
                        + "re-granting every time you update."
                    )
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                }
            }

            HStack {
                Spacer()
                Button("Continue") { onContinue() }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(28)
        .frame(width: 520)
    }
}
