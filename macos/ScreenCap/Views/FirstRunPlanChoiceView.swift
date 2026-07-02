import SwiftUI

/// The first-run hybrid plan choice (U6, R1/R2): pitch cloud, but make
/// "keep everything local (free)" the default, low-friction path. Choosing cloud
/// is opt-in and funnels into the one shared setup flow (`CloudSetupView`, R4).
///
/// Presented once, after the permission steps; the caller marks the decision so
/// it is never re-shown (the `cloudDecisionMade` axis). This view owns no
/// persistence — it calls back so the caller can persist the destination (U5),
/// mark the decision, and, for cloud, present the shared setup flow.
struct FirstRunPlanChoiceView: View {
    /// "Keep everything local" — persist local + finish (no account).
    let onKeepLocal: () -> Void
    /// "Set up cloud" — mark decided + open the shared cloud-setup flow.
    let onChooseCloud: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Where should your recordings live?")
                    .font(.title2.bold())
                Text("ScreenCap is local-first and free. You can add cloud anytime to reach your history anywhere and share with teammates.")
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(spacing: 12) {
                planCard(
                    icon: "internaldrive",
                    title: "Keep everything local",
                    subtitle: "Free, private, no account. Recordings stay on this Mac.",
                    prominent: true,
                    action: onKeepLocal,
                    cta: "Start free"
                )
                planCard(
                    icon: "cloud",
                    title: "Set up cloud",
                    subtitle: "Sign in and join as a founding member — free while we build the web experience.",
                    prominent: false,
                    action: onChooseCloud,
                    cta: "Set up cloud"
                )
            }
        }
        .padding(28)
        .frame(width: 480)
    }

    @ViewBuilder
    private func planCard(
        icon: String,
        title: String,
        subtitle: String,
        prominent: Bool,
        action: @escaping () -> Void,
        cta: String
    ) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .font(.title)
                .foregroundStyle(prominent ? Color.accentColor : .secondary)
                .frame(width: 28)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            if prominent {
                Button(cta, action: action).buttonStyle(.borderedProminent)
            } else {
                Button(cta, action: action).buttonStyle(.bordered)
            }
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .fill(Color.scSurfaceElevated)
        )
    }
}
