import SwiftUI

/// U11 step 2 — the pre-blocked app rules preview (design 153–194). The list
/// seeds from `screencap apps --json` matrix-excluded rows — the real
/// always-blocked apps on this machine, not the prototype's hardcoded three —
/// closed by the "everything else · recorded" row. "Edit the list" deep-links
/// to the App rules pane after the wizard finishes (the prototype's step-3
/// target is a design bug; see the plan's assumptions).
struct OnboardingAppRulesStep: View {
    let apps: [InstalledApp]
    let isLoading: Bool
    let onLooksRight: () -> Void
    let onEditList: () -> Void

    private var preBlocked: [InstalledApp] {
        apps.filter(\.isMatrixExclude).sorted { $0.displayName < $1.displayName }
    }

    var body: some View {
        VStack(spacing: 0) {
            Text(OnboardingCopy.appRulesHeadline)
                .font(SCTypography.serifHeading)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
                .padding(.bottom, 10)
            Text(OnboardingCopy.appRulesSub)
                .font(SCTypography.sans(size: 14))
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 470)
                .padding(.bottom, 24)

            list
                .frame(maxWidth: 560)
                .padding(.bottom, 14)

            Text(OnboardingCopy.appRulesFooter)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .padding(.bottom, 20)

            HStack(spacing: 18) {
                OnboardingPrimaryButton(title: "Looks right", action: onLooksRight)
                OnboardingLinkButton(title: "Edit the list", action: onEditList)
            }
        }
        .padding(.horizontal, 100)
        .padding(.top, 10)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private var list: some View {
        VStack(spacing: 0) {
            if isLoading, preBlocked.isEmpty {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text("Checking your installed apps…")
                        .font(SCTypography.sans(size: 13))
                        .foregroundStyle(Color.scInkMuted)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 14)
            } else if preBlocked.isEmpty {
                HStack {
                    Text("No always-blocked apps found on this Mac — password managers are blocked automatically when installed.")
                        .font(SCTypography.sans(size: 12.5))
                        .foregroundStyle(Color.scInkMuted)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 14)
            } else {
                ForEach(preBlocked) { app in
                    row(
                        initials: AppInitialsTile.initials(for: app.displayName),
                        tile: SwiftUI.Color.tileColor(for: app.bundleId),
                        name: app.displayName,
                        chip: ("blocked", Color.scRust)
                    )
                    Rectangle().fill(Color.scFillSubtle).frame(height: 1)
                }
            }
            row(
                initials: "✓",
                tile: Color.scTeal,
                name: "Everything else",
                chip: ("recorded", Color.scTeal)
            )
        }
        .background(Color.scPaper)
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusPanel)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusPanel))
    }

    private func row(
        initials: String, tile: Color, name: String, chip: (label: String, color: Color)
    ) -> some View {
        HStack(spacing: 12) {
            AppInitialsTile(text: initials, color: tile, size: 30)
            Text(name)
                .font(SCTypography.sans(size: 13.5, weight: .semibold))
                .foregroundStyle(Color.scInk)
            Spacer(minLength: 8)
            Text(chip.label)
                .font(SCTypography.metaMonoSmall)
                .foregroundStyle(chip.color)
                .padding(.horizontal, 10)
                .padding(.vertical, 3)
                .overlay(Capsule().strokeBorder(chip.color.opacity(0.35), lineWidth: 1))
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }
}

/// The design's 30px initials tile (App rules rows + onboarding step 2),
/// colored by a stable hash of the app identity (`Color.tileColor(for:)`).
struct AppInitialsTile: View {
    let text: String
    let color: Color
    var size: CGFloat = 30

    var body: some View {
        RoundedRectangle(cornerRadius: SCMetrics.radiusInner)
            .fill(color)
            .frame(width: size, height: size)
            .overlay(
                Text(text)
                    .font(SCTypography.grotesk(size: size * 0.42, weight: .bold))
                    .foregroundStyle(Color.scPaper)
            )
            .accessibilityHidden(true)
    }

    /// Two-character initials the design's way ("1Password" → "1P",
    /// "Messages" → "Me"): first character plus the second word's initial when
    /// one exists, else the second character.
    static func initials(for name: String) -> String {
        let words = name.split(separator: " ").filter { !$0.isEmpty }
        guard let first = words.first, let firstChar = first.first else { return "?" }
        if words.count > 1, let secondWordChar = words[1].first {
            return String(firstChar).uppercased() + String(secondWordChar).uppercased()
        }
        let second = first.count > 1 ? String(first[first.index(after: first.startIndex)]) : ""
        return String(firstChar).uppercased() + second.lowercased()
    }
}
