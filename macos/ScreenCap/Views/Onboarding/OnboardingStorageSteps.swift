import SwiftUI

// U11 steps 3–5 — storage choice, account, and team setup (design 196–288).
// The storage cards render per the design minus pricing/billing/encryption
// claims (KTD-9; copy pinned in OnboardingCopy). The account step's three
// actions all invoke the existing browser sign-in — native per-provider flows
// are SCR-221/SCR-229 scope. The team step is a stub (SCR-221) with a
// functional skip so the wizard always completes.

/// Step 3 — storage (design 196–236).
struct OnboardingStorageStep: View {
    @Binding var tier: OnboardingStorageTier
    let onContinue: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Text(OnboardingCopy.storageHeadline)
                .font(SCTypography.serifHeading)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
                .padding(.bottom, 10)
            Text(OnboardingCopy.storageSub)
                .font(SCTypography.sans(size: 14))
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 460)
                .padding(.bottom, 28)

            HStack(alignment: .top, spacing: 14) {
                storageCard(
                    tier: .local,
                    title: OnboardingCopy.localCardTitle,
                    meta: OnboardingCopy.localCardMeta,
                    metaColor: .scTeal,
                    bullets: OnboardingCopy.localCardBullets
                )
                storageCard(
                    tier: .personalCloud,
                    title: OnboardingCopy.personalCardTitle,
                    meta: OnboardingCopy.personalCardMeta,
                    metaColor: .scInkMuted,
                    bullets: OnboardingCopy.personalCardBullets
                )
                storageCard(
                    tier: .teamCloud,
                    title: OnboardingCopy.teamCardTitle,
                    meta: OnboardingCopy.teamCardMeta,
                    metaColor: .scInkMuted,
                    bullets: OnboardingCopy.teamCardBullets
                )
            }
            .frame(maxWidth: 860)
            .padding(.bottom, 16)

            Text(OnboardingCopy.storageFootnote)
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
                .padding(.bottom, 22)

            OnboardingPrimaryButton(
                title: OnboardingStepPolicy.storageCTATitle(tier: tier),
                action: onContinue
            )
        }
        .padding(.horizontal, 100)
        .padding(.top, 14)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func storageCard(
        tier cardTier: OnboardingStorageTier,
        title: String,
        meta: String,
        metaColor: Color,
        bullets: [String]
    ) -> some View {
        let selected = tier == cardTier
        return Button {
            tier = cardTier
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                Text(title)
                    .font(SCTypography.grotesk(size: 16, weight: .semibold))
                    .foregroundStyle(Color.scInk)
                    .padding(.bottom, 2)
                Text(meta)
                    .font(SCTypography.metaMono)
                    .foregroundStyle(metaColor)
                    .padding(.bottom, 14)
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(bullets, id: \.self) { bullet in
                        HStack(alignment: .top, spacing: 8) {
                            Text("—").foregroundStyle(metaColor)
                            Text(bullet).foregroundStyle(Color.scInkSecondary)
                        }
                        .font(SCTypography.sans(size: 12.5))
                        .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(22)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusPanel))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusPanel)
                    .strokeBorder(selected ? Color.scTeal : Color.scBorderWarm, lineWidth: 2)
            )
            .overlay(alignment: .topTrailing) {
                Circle()
                    .fill(Color.scTeal)
                    .frame(width: 20, height: 20)
                    .overlay(
                        Image(systemName: "checkmark")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(Color.scCanvas)
                    )
                    .padding(16)
                    .opacity(selected ? 1 : 0)
            }
            .contentShape(RoundedRectangle(cornerRadius: SCMetrics.radiusPanel))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }
}

/// Step 4 — account (design 238–262). All three actions route to the existing
/// browser sign-in (`CloudAuthController` → `screencap login`); sign-in leaves
/// `upload_default` untouched (`ask`), so the Review window stays the upload
/// gate. A functional skip keeps the wizard completable without an account.
struct OnboardingAccountStep: View {
    @EnvironmentObject private var auth: CloudAuthController
    let onSignedIn: () -> Void
    let onSkip: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Text(OnboardingCopy.accountHeadline)
                .font(SCTypography.serifHeading)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
                .padding(.bottom, 10)
            Text(OnboardingCopy.accountSub)
                .font(SCTypography.sans(size: 14))
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
                .padding(.bottom, 30)

            if auth.signInFlow == .inProgress {
                signInWaiting
            } else {
                signInActions
            }

            OnboardingLinkButton(title: "Skip for now", action: onSkip)
                .padding(.top, 24)
        }
        .padding(.horizontal, 100)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var signInWaiting: some View {
        VStack(spacing: 12) {
            ProgressView()
                .controlSize(.small)
            Text("Waiting for sign-in in your browser…")
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scInkSecondary)
            Button("Cancel") { auth.cancelSignIn() }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12.5))
                .underline()
                .foregroundStyle(Color.scInkMuted)
        }
        .frame(width: 340)
    }

    private var signInActions: some View {
        VStack(spacing: 10) {
            if case .failed(let reason) = auth.signInFlow {
                Text(reason)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scRust)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 4)
            }
            providerButton(label: "Continue with Google", monogram: "G")
            appleButton
            HStack(spacing: 12) {
                Rectangle().fill(Color.scBorderWarm).frame(height: 1)
                Text("or")
                    .font(SCTypography.metaMonoSmall)
                    .foregroundStyle(Color.scInkMuted)
                Rectangle().fill(Color.scBorderWarm).frame(height: 1)
            }
            .padding(.vertical, 6)
            workEmailRow
        }
        .frame(width: 340)
    }

    /// Every provider action invokes the same browser flow — the sign-in page
    /// is where the provider choice actually happens until SCR-221/SCR-229
    /// bring native per-provider flows.
    private func startSignIn() {
        auth.startSignIn { success in
            if success { onSignedIn() }
        }
    }

    private func providerButton(label: String, monogram: String) -> some View {
        Button(action: startSignIn) {
            HStack(spacing: 10) {
                Text(monogram)
                    .font(SCTypography.grotesk(size: 15, weight: .bold))
                    .foregroundStyle(Color.scTeal)
                Text(label)
                    .font(SCTypography.sans(size: 14, weight: .semibold))
                    .foregroundStyle(Color.scInk)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .background(Color.scPaper, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    private var appleButton: some View {
        Button(action: startSignIn) {
            HStack(spacing: 10) {
                Image(systemName: "apple.logo")
                    .font(.system(size: 13))
                Text("Continue with Apple")
                    .font(SCTypography.sans(size: 14, weight: .semibold))
            }
            .foregroundStyle(Color.scCanvas)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .background(Color.scInk, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    private var workEmailRow: some View {
        HStack(spacing: 8) {
            // Decorative field per the design; email entry happens on the
            // browser sign-in page, so typing here would be silently ignored —
            // disabled keeps the row honest.
            Text("work email…")
                .font(SCTypography.sans(size: 13.5))
                .foregroundStyle(Color.scInkMuted)
                .padding(.horizontal, 18)
                .padding(.vertical, 11)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.scPaper, in: Capsule())
                .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
                .help("Email sign-in continues in your browser")
            Button(action: startSignIn) {
                Text("Continue")
                    .font(SCTypography.sans(size: 13.5, weight: .semibold))
                    .foregroundStyle(Color.scCanvas)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 11)
                    .background(Color.scTeal, in: Capsule())
                    .contentShape(Capsule())
            }
            .buttonStyle(.plain)
        }
    }
}

/// Step 5 — team setup (design 264–288). Renders per the design with the
/// fields and both team actions disabled (stub SCR-221, KTD-8); the skip path
/// is functional so the wizard always completes and persists its marker.
struct OnboardingTeamStep: View {
    let onSkip: () -> Void

    private static let stubHelp = "Coming soon — SCR-221"

    var body: some View {
        VStack(spacing: 0) {
            Text(OnboardingCopy.teamHeadline)
                .font(SCTypography.serifHeading)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
                .padding(.bottom, 10)
            Text(OnboardingCopy.teamSub)
                .font(SCTypography.sans(size: 14))
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
                .padding(.bottom, 30)

            // Stub: SCR-221 team cloud — fields render per the design,
            // disabled, with neutral placeholders (the prototype's sample
            // names are mock data the U14 sweep forbids).
            VStack(alignment: .leading, spacing: 12) {
                fieldGroup(label: "TEAM NAME", placeholder: "your team name…")
                fieldGroup(label: "INVITE TEAMMATES", placeholder: "teammate@company.com…")
                domainRow
            }
            .frame(width: 400)
            .opacity(0.7)
            .help(Self.stubHelp)
            .padding(.bottom, 28)

            HStack(spacing: 18) {
                OnboardingPrimaryButton(title: "Create team", enabled: false, action: {})
                    .help(Self.stubHelp)
                OnboardingLinkButton(title: "I have an invite link", enabled: false, action: {})
                    .help(Self.stubHelp)
            }
            .padding(.bottom, 20)

            OnboardingLinkButton(title: "Skip for now", action: onSkip)
        }
        .padding(.horizontal, 100)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func fieldGroup(label: String, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label)
                .font(SCTypography.metaMonoSmall)
                .tracking(0.84)
                .foregroundStyle(Color.scInkMuted)
            Text(placeholder)
                .font(SCTypography.sans(size: 13.5))
                .foregroundStyle(Color.scInkMuted)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
                .overlay(
                    RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                        .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                )
        }
    }

    private var domainRow: some View {
        HStack {
            Text("Anyone with a company email can join")
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scInkSecondary)
            Spacer(minLength: 8)
            SettingsToggle(on: true, onFill: .scTealSoft, action: nil)
                .accessibilityHidden(true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color.scPaper, in: RoundedRectangle(cornerRadius: SCMetrics.radiusChip))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusChip)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
    }
}
