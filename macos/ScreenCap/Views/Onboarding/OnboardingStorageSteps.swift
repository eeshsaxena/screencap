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

/// Step 4 — account (design 238–262). All three actions route to the existing
/// browser sign-in (`CloudAuthController` → `screencap login`); sign-in leaves
/// `upload_default` untouched (`ask`), so the Review window stays the upload
/// gate. A functional skip keeps the wizard completable without an account.
struct OnboardingAccountStep: View {
    @EnvironmentObject private var auth: CloudAuthController
    let tier: OnboardingStorageTier
    let onSignedIn: () -> Void
    let onSkip: () -> Void

    /// Short reason surfaced when minting the checkout URL fails (U9).
    @State private var upgradeError: String?
    /// Post-checkout pending: set true when the user opens Stripe Checkout, so
    /// the return path shows "unlocks automatically" (a spinner + reconcile),
    /// never a fresh "start trial" that reads like a failed payment (U11).
    @State private var checkoutPending = false

    /// The paid tier this step checks out — the storage selection mapped to the
    /// entitlement/price tier (U11). Local Pro on the `.local` storage tier,
    /// Cloud on `.personalCloud`. Passed to `startCheckout` for PRICE selection
    /// only; the webhook is the entitlement authority (KTD-2).
    private var checkoutTier: EntitlementTier {
        switch tier {
        case .local: return .localPro
        case .personalCloud: return .cloud
        case .teamCloud: return .none // Team is waitlist-only (R4), never checkout.
        }
    }

    /// After sign-in, a paid cloud tier stays on this step until the trial /
    /// subscription is active — the upgrade panel drives checkout (U8/U9/U11).
    /// Only when the client paywall is on (KTD-6); off → no soft gate.
    ///
    /// Grace/stale (KTD-4): while `whoami` is stale the daemon grace-allows via
    /// the lease, so we must NOT show the upgrade/"expired" surface then — an
    /// offline payer would see a spurious paywall. `trialState == .indeterminate`
    /// captures the stale case (and the pre-resolution case), so gating on
    /// "not indeterminate" keeps the offline payer out of the upgrade panel.
    private var showUpgrade: Bool {
        auth.paywallEnabled
            && auth.isSignedIn
            && checkoutTier != .none
            && !auth.isSubscribed
            && auth.trialState != .indeterminate
            && auth.trialState != .subscribed
    }

    /// The lapsed / never-entitled re-entry renders a gated RE-subscribe surface
    /// (not a fresh chooser) per U11.
    private var isLapsed: Bool {
        showUpgrade && auth.trialState == .lapsed
    }

    var body: some View {
        VStack(spacing: 0) {
            Text(upgradeHeadline)
                .font(SCTypography.serifHeading)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
                .padding(.bottom, 10)
            Text(upgradeSubcopy)
                .font(SCTypography.sans(size: 14))
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
                .padding(.bottom, 30)

            if auth.signInFlow == .inProgress {
                signInWaiting
            } else if showUpgrade {
                upgradePanel
            } else {
                signInActions
            }

            OnboardingLinkButton(title: "Skip for now", action: onSkip)
                .padding(.top, 24)
        }
        .padding(.horizontal, 100)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        // Resolve sign-in state lazily when this account step appears (sign-in
        // is no longer probed at app launch — SCR-241). Without this, a user who
        // replays onboarding while already signed in would see `status ==
        // .unknown` (→ `isSignedIn == false`) and be offered a redundant sign-in
        // instead of the upgrade panel. The account step is an explicit cloud
        // surface, so decrypting the Keychain (and any prompt) here is expected.
        .task { await auth.refreshIfNeeded() }
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
        // Returning from the browser checkout → re-check entitlement (both
        // tiers). The pending flag survives the app-switch so the return shows
        // the pending panel, not "start trial".
        .onReceive(NotificationCenter.default.publisher(
            for: NSApplication.didBecomeActiveNotification)) { _ in
            if showUpgrade || checkoutPending { Task { await auth.refreshEntitlement() } }
        }
    }

    /// Headline reflects the entitlement state: lapsed re-subscribe, the paid
    /// upgrade, or the pre-billing account creation.
    private var upgradeHeadline: String {
        if isLapsed { return OnboardingCopy.lapsedHeadline }
        if showUpgrade { return "One more step to unlock \(checkoutTier == .cloud ? "cloud" : "Local Pro")." }
        return OnboardingCopy.accountHeadline
    }

    private var upgradeSubcopy: String {
        if isLapsed { return OnboardingCopy.lapsedSub }
        if showUpgrade { return OnboardingCopy.upgradeSub }
        return OnboardingCopy.accountSub
    }

    /// The tier's price line, sourced from `PricingCatalog` (single source,
    /// KTD-7). Cloud gets the Cloud price; Local Pro the Local Pro price.
    private var upgradePriceLine: String {
        checkoutTier == .cloud
            ? "\(PricingCatalog.cloudPriceLine) · cancel anytime"
            : "\(PricingCatalog.localProPriceLine) · cancel anytime"
    }

    /// The tier's selling bullets — Cloud vs Local Pro card bullets (KTD-7).
    private var upgradeBullets: [String] {
        checkoutTier == .cloud ? OnboardingCopy.cloudCardBullets : OnboardingCopy.localProCardBullets
    }

    /// The checkout CTA label — "Resubscribe" on a lapsed re-entry, a trial
    /// "Start free trial" otherwise (R5). Both carry the tier's price.
    private var checkoutButtonTitle: String {
        let price = checkoutTier == .cloud ? PricingCatalog.cloudMonthly : PricingCatalog.localProMonthly
        return isLapsed
            ? "Resubscribe — \(price)/month"
            : "Start free trial — then \(price)/month"
    }

    private var upgradePanel: some View {
        VStack(spacing: 12) {
            Text(upgradePriceLine)
                .font(SCTypography.metaMono)
                .foregroundStyle(Color.scTeal)

            // Trial lifecycle banner (U11): calm "N days left" → escalated
            // near-expiry → urgent last-day. Color escalates with urgency; the
            // copy discloses auto-conversion (R12). No banner on lapsed (that is
            // the whole-panel re-subscribe framing) or subscribed.
            if let banner = OnboardingCopy.trialBanner(for: auth.trialState) {
                Text(banner)
                    .font(SCTypography.sans(size: 12.5, weight: .semibold))
                    .foregroundStyle(trialBannerColor)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.updatesFrequently)
            }

            ForEach(upgradeBullets, id: \.self) { bullet in
                HStack(alignment: .top, spacing: 8) {
                    Text("—").foregroundStyle(Color.scInkMuted)
                    Text(bullet).foregroundStyle(Color.scInkSecondary)
                }
                .font(SCTypography.sans(size: 12.5))
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            // R12 — auto-conversion + cancellation disclosed before any charge.
            Text(OnboardingCopy.trialDisclosure)
                .font(SCTypography.sans(size: 11.5))
                .foregroundStyle(Color.scInkMuted)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)

            if let upgradeError {
                Text(upgradeError)
                    .font(SCTypography.sans(size: 12.5))
                    .foregroundStyle(Color.scRust)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 2)
            }

            OnboardingPrimaryButton(title: checkoutButtonTitle) {
                upgradeError = nil
                checkoutPending = true
                // Pass the chosen tier so Checkout selects the right price; the
                // webhook re-derives the entitlement (KTD-2). Failure clears the
                // pending flag so the user can retry, not sit on a false spinner.
                auth.startCheckout(tier: checkoutTier) { reason in
                    upgradeError = reason
                    checkoutPending = false
                }
            }
            .padding(.top, 4)
            .accessibilityLabel(checkoutButtonTitle)
            .accessibilityHint("Requires a paid subscription. \(OnboardingCopy.trialDisclosure)")

            // Post-checkout pending (U11): once Checkout is opened, show the
            // "unlocks automatically" reassurance so the return from Stripe never
            // reads as a failed payment. Shown for BOTH tiers.
            if checkoutPending {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Unlocks automatically once payment completes.")
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scInkMuted)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Waiting for payment to complete. Your subscription unlocks automatically.")
            }

            OnboardingLinkButton(title: "I've paid — check now") {
                // Reconcile first (grant-only self-heal for a dropped checkout
                // webhook), then force-refresh — so a paid customer is never
                // stuck if the webhook never landed (U14).
                Task { await auth.reconcileEntitlement() }
            }
        }
        .frame(width: 360)
    }

    /// The trial banner color escalates with urgency — teal (calm) → amber
    /// (near-expiry) → rust (last day). Neutral for the non-banner states.
    private var trialBannerColor: Color {
        switch auth.trialState {
        case .active: return .scTeal
        case .nearExpiry: return .scAmber
        case .lastDay: return .scRust
        case .indeterminate, .subscribed, .lapsed: return .scInkMuted
        }
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
            if success { proceedAfterSignIn() }
        }
    }

    /// With the paywall on, a paid cloud tier requires an active trial /
    /// subscription before finishing — the upgrade panel (shown reactively when
    /// `showUpgrade`) drives it. Paywall-off, Team, already-entitled, and
    /// offline-stale accounts proceed immediately (U9 / KTD-6 / KTD-4). Uses the
    /// same `showUpgrade` gate as the panel so the "stay on this step" decision
    /// can't drift from what is actually rendered.
    private func proceedAfterSignIn() {
        if showUpgrade { return }
        onSignedIn()
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
