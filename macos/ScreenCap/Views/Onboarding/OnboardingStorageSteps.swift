import AppKit
import SwiftUI

// U11 steps 3–5 — storage choice, account, and team setup (design 196–288).
// The storage cards render per the design minus pricing/billing/encryption
// claims (KTD-9; copy pinned in OnboardingCopy). The account step's three
// actions all invoke the existing browser sign-in — native per-provider flows
// are SCR-221/SCR-229 scope. The team step is a stub (SCR-221) with a
// functional skip so the wizard always completes.

/// Step 3 — storage (design 196–236).
struct OnboardingStorageStep: View {
    @EnvironmentObject private var auth: CloudAuthController
    @Binding var tier: OnboardingStorageTier
    let onContinue: () -> Void

    /// The Cloud card's meta line. Paywall off → the pre-billing label. Paywall
    /// on → the priced trial meta, OR the F3 "add cloud" upsell when the user
    /// already holds Local Pro (a Local Pro holder is upgrading, not choosing a
    /// fresh tier).
    private var cloudCardMeta: String {
        guard auth.paywallEnabled else { return OnboardingCopy.personalCardMetaFree }
        return auth.tier == .localPro
            ? OnboardingCopy.cloudUpgradeCardMeta
            : OnboardingCopy.cloudCardMeta
    }

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
                // Paid-only launch (U11): behind `paywallEnabled`, the two
                // storage tiers read as the two PAID subscriptions — Local Pro
                // (this Mac) and Cloud (adds upload/sync/AI) — each fronted by a
                // free trial. The `OnboardingStorageTier` values are unchanged
                // (`.local`/`.personalCloud`) so wizard routing + dot counts
                // (OnboardingStepPolicyTests) stay pinned; only the surfaced
                // title/meta/bullets differ. Paywall OFF → the pre-billing copy.
                storageCard(
                    tier: .local,
                    title: auth.paywallEnabled ? OnboardingCopy.localProCardTitle : OnboardingCopy.localCardTitle,
                    meta: auth.paywallEnabled ? OnboardingCopy.localProCardMeta : OnboardingCopy.localCardMeta,
                    metaColor: .scTeal,
                    bullets: auth.paywallEnabled ? OnboardingCopy.localProCardBullets : OnboardingCopy.localCardBullets,
                    subscriptionGated: auth.paywallEnabled
                )
                storageCard(
                    tier: .personalCloud,
                    title: auth.paywallEnabled ? OnboardingCopy.cloudCardTitle : OnboardingCopy.personalCardTitle,
                    // Show the priced meta only when the client paywall is on
                    // (KTD-6/KTD-7); otherwise fall back to the pre-billing label
                    // so the app makes no pricing claim it isn't enforcing. F3
                    // upgrade path: a Local Pro holder sees the Cloud card as an
                    // "add cloud" upsell rather than a fresh price.
                    meta: cloudCardMeta,
                    metaColor: .scInkMuted,
                    bullets: auth.paywallEnabled ? OnboardingCopy.cloudCardBullets : OnboardingCopy.personalCardBullets,
                    subscriptionGated: auth.paywallEnabled
                )
                storageCard(
                    tier: .teamCloud,
                    title: OnboardingCopy.teamCardTitle,
                    meta: OnboardingCopy.teamCardMeta,
                    metaColor: .scInkMuted,
                    bullets: OnboardingCopy.teamCardBullets,
                    subscriptionGated: false
                )
            }
            .frame(maxWidth: 860)
            .padding(.bottom, 16)

            // R12 — disclose the trial's auto-conversion + cancellation before
            // any charge, right under the priced cards (paywall on only).
            if auth.paywallEnabled {
                Text(OnboardingCopy.trialDisclosure)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 520)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 14)
                    .accessibilityLabel("Free trial terms")
                    .accessibilityHint(OnboardingCopy.trialDisclosure)
            }

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
        bullets: [String],
        subscriptionGated: Bool
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
        // U11 accessibility: a priced/gated card is more than a bare selectable
        // pixel — VoiceOver reads the tier, its price, and that it requires a
        // paid subscription with a free trial (R5/R12), not just "button". The
        // Button is already one a11y element, so we label it directly rather
        // than `.accessibilityElement(children: .combine)` (which would drop the
        // button's own trait/action).
        .accessibilityLabel("\(title). \(meta).")
        .accessibilityHint(
            subscriptionGated
                ? "Paid subscription with a free trial. \(OnboardingCopy.trialDisclosure)"
                : ""
        )
    }
}

/// Step 4 — account. The account/plans body is the shared `AccountSheetView`
/// in `.onboarding` context (account-sheet U5): sign-in, plan state, checkout,
/// and the checkout-pending/reconcile machinery are all controller-owned
/// (KTD-6), so this step keeps no bespoke duplicate. The wizard retains its
/// chrome — the skip link and the footer progress dots stay wizard-owned; the
/// sheet's own header serves as the step title. Sign-in leaves
/// `upload_default` untouched (`ask`), so the Review window stays the upload
/// gate. A functional skip keeps the wizard completable without an account.
struct OnboardingAccountStep: View {
    @EnvironmentObject private var auth: CloudAuthController
    let tier: OnboardingStorageTier
    let onSignedIn: () -> Void
    let onSkip: () -> Void

    /// The paid tier this step's storage selection maps to (U11): Local Pro on
    /// the `.local` storage tier, Cloud on `.personalCloud`. Used only for the
    /// wizard-advance gate — checkout itself runs inside the shared sheet, and
    /// the webhook is the entitlement authority (KTD-2).
    private var checkoutTier: EntitlementTier {
        switch tier {
        case .local: return .localPro
        case .personalCloud: return .cloud
        case .teamCloud: return .none // Team is waitlist-only (R4), never checkout.
        }
    }

    /// Whether the wizard must hold on this step after sign-in: with the
    /// paywall on, a paid tier stays until the trial / subscription is active
    /// (the sheet's plan buttons drive checkout). Off → no soft gate.
    ///
    /// Grace/stale (KTD-4): `trialState == .indeterminate` captures the stale
    /// case (and pre-resolution), so an offline payer is never held on a
    /// spurious paywall.
    private var mustStayForEntitlement: Bool {
        auth.paywallEnabled
            && auth.isSignedIn
            && checkoutTier != .none
            && !auth.isSubscribed
            && auth.trialState != .indeterminate
            && auth.trialState != .subscribed
    }

    var body: some View {
        VStack(spacing: 0) {
            AccountSheetView(
                auth: auth,
                context: .onboarding,
                onSettled: {
                    // Sign-in just completed on this step. Paywall-off, Team,
                    // already-entitled, and offline-stale accounts advance
                    // immediately; a paid tier holds for entitlement — the
                    // onChange wiring below advances once it resolves.
                    if !mustStayForEntitlement { onSignedIn() }
                }
                // No onDismiss: embedded in the wizard chrome, the skip link
                // below is the way past this step.
            )

            OnboardingLinkButton(title: "Skip for now", action: onSkip)
                .padding(.top, 24)
        }
        .padding(.horizontal, 100)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        // Trial/subscription just became active (webhook granted it) → finish.
        // Cloud maps to `isSubscribed`; Local Pro converts once the tier resolves
        // (not `isSubscribed`, which is cloud-only), so also finish when the
        // resolved entitlement tier now matches what this step checked out.
        .onChange(of: auth.isSubscribed) { subscribed in
            if subscribed, checkoutTier == .cloud { onSignedIn() }
        }
        .onChange(of: auth.tier) { newTier in
            if newTier == checkoutTier, checkoutTier != .none { onSignedIn() }
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
                // U10: Team cloud is coming soon — the primary action captures
                // interest via the hosted waitlist form rather than a dead
                // "Create team" (the team backend is deferred).
                OnboardingPrimaryButton(title: "Join the Team cloud waitlist") {
                    if let url = URL(string: OnboardingCopy.teamWaitlistURL) {
                        NSWorkspace.shared.open(url)
                    }
                }
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
