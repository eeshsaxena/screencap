import Foundation

// Account & Plan sheet — the pure decision layer (account-sheet plan U4,
// KTD-3/KTD-7). The sheet's rendered state is always *derived* from
// `CloudAuthController`-published values — the view renders, it never computes
// auth state (HTD state chart). All copy the honesty gates constrain lives in
// `AccountSheetCopy` (sibling of `OnboardingCopy`/`PricingCatalog`) so
// `AccountSheetPolicyTests` can string-assert it without a render tree.

/// Where the sheet was opened from. Selects FRAMING COPY ONLY (KTD-3):
/// settle side-effects (upload auto-start, wizard advance, dismissal) stay
/// caller-owned via `AccountSheetView.onSettled` — baking side effects into
/// the context would couple the shared component to callers' models.
///
/// `Identifiable` so `MainWindow` can drive presentation with
/// `presentedAccountContext: AccountSheetContext?` + `.sheet(item:)` (U5).
enum AccountSheetContext: String, Equatable, Identifiable {
    /// The recording/search paywall gate (R3): urgent framing, "Not now" keeps
    /// it dismissible — enforcement blocks independently; the sheet frames,
    /// it does not trap.
    case gate
    /// Neutral account surface: menu bar, the Settings `.account` pane.
    case account
    /// The review window's Upload moment (R14): sign-in or upgrade-to-Cloud
    /// framing.
    case upload
    /// The onboarding wizard's account step (wizard chrome wraps the body).
    case onboarding

    var id: String { rawValue }
}

/// The sheet's rendered state, derived from controller-published values.
/// One state chart, presentation switches on top (gate framing and the
/// paywall-off account-only mode are NOT extra states — HTD note).
enum AccountSheetState: Equatable {
    /// Pre-resolution: the lazy `whoami` has not returned. No affordances yet
    /// (avoid flashing "Sign In" at a signed-in user).
    case unknown
    /// Positively signed out (including signed-out + `trialState ==
    /// .indeterminate` — indeterminate alone is never a neutral signal).
    case signedOut
    /// Signed in but the plan claim is unconfirmable — `AuthStatus
    /// .signedIn(stale: true)` (offline payer, KTD-4 grace) or a signed-in
    /// account with no plan claim to reason about. Identity if known, no gate
    /// framing, no tier buttons: stale entitlement must never read as lapsed
    /// (R1/AE7).
    case neutral
    /// Signed in, live trial (active / near-expiry / last-day).
    case trial
    /// Signed in with an active paid subscription.
    case subscribed
    /// Signed in, no active tier and no live trial — the re-subscribe surface.
    case lapsed
    /// A checkout browser round-trip is pending ("unlocks automatically").
    /// Tier buttons stay tappable — a re-tap replaces the target (KTD-6).
    case checkoutPending
}

enum AccountSheetPolicy {

    // MARK: - State derivation (HTD state chart)

    /// Derive the rendered state from the controller's published values.
    ///
    /// Order is load-bearing:
    /// - `.signedOut` wins over everything trial-shaped: `trialState ==
    ///   .indeterminate` also covers signed-out/paywall-off/pre-resolution, so
    ///   a signed-out user renders `signedOut`, never `neutral` (HTD note).
    /// - Stale wins over pending/trial: `signedIn(stale: true)` is the offline
    ///   payer whose entitlement must never read as lapsed (KTD-4 / AE7).
    /// - `checkoutPending` wins over the trial/lapsed base state so the
    ///   "unlocks automatically" surface survives the browser round-trip;
    ///   the controller clears it on mint failure and sign-out (KTD-6).
    /// - Signed-in + non-stale + `.indeterminate` (paywall off, or a claim not
    ///   yet resolved) has no plan state to render → `neutral`, the safe
    ///   no-gate no-tiers rendering.
    static func state(
        status: AuthStatus,
        trialState: TrialState,
        checkoutPending: Bool
    ) -> AccountSheetState {
        switch status {
        case .unknown:
            return .unknown
        case .signedOut:
            return .signedOut
        case .signedIn(_, _, let stale):
            if stale { return .neutral }
            if checkoutPending { return .checkoutPending }
            switch trialState {
            case .subscribed:
                return .subscribed
            case .active, .nearExpiry, .lastDay:
                return .trial
            case .lapsed:
                return .lapsed
            case .indeterminate:
                return .neutral
            }
        }
    }

    // MARK: - Affordance visibility

    /// R9: with the paywall flag off the sheet is account-only — no pricing
    /// copy, no tier buttons, no checkout routing. Manage Subscription is
    /// deliberately NOT gated here (the flag gates pricing and checkout, not
    /// management of an existing subscription).
    static func isAccountOnly(paywallEnabled: Bool) -> Bool {
        !paywallEnabled
    }

    /// R7/AE6: any state with a Stripe subscription — trial or subscribed —
    /// offers Manage Subscription. Signed-out and neutral do not (no confirmed
    /// subscription to manage); lapsed does not (the backend would answer
    /// `no_subscription` — the plans are the recourse).
    static func showsManageSubscription(state: AccountSheetState) -> Bool {
        state == .trial || state == .subscribed
    }

    /// Sign Out renders on every signed-in state (its enabled/disabled state
    /// rides `CloudAuthController.canSignOut`, R8).
    static func showsSignOut(state: AccountSheetState) -> Bool {
        switch state {
        case .neutral, .trial, .subscribed, .lapsed, .checkoutPending:
            return true
        case .unknown, .signedOut:
            return false
        }
    }

    /// R4: signed out → the single primary action is Sign In.
    static func showsSignInPrimary(state: AccountSheetState) -> Bool {
        state == .signedOut
    }

    /// The trial's pre-conversion cancel affordance copy (R7 honesty: the app
    /// promises cancellation before conversion, so the trial state points at
    /// Manage Subscription explicitly).
    static func showsTrialCancelAffordance(state: AccountSheetState) -> Bool {
        state == .trial
    }

    /// R10/AE4 seam placement: the shared `errorSection` renders `accountError`
    /// in every state EXCEPT signed-out — there `signInSection` already owns
    /// the sign-in failure display (message + a working Try Again), so the
    /// error seam rendering on top would stack a second copy of the failure
    /// with a retry wired to a billing action that never ran.
    static func showsErrorSection(state: AccountSheetState) -> Bool {
        state != .signedOut
    }

    /// The gate-context success rendering: an already-entitled account (active
    /// trial or paid subscription) is looking at the gate. KTD-1: the gate only
    /// *opens* for an unentitled account, so gate+entitled is the
    /// just-subscribed-in-place (or out-of-band-resolved) success moment. The
    /// safety property holds — this only ever renders for an entitled account —
    /// but it is not exclusively an in-place subscribe, so the copy that keys on
    /// it must be state-asserting, never event-asserting (R1).
    static func isEntitledSuccess(_ state: AccountSheetState) -> Bool {
        state == .trial || state == .subscribed
    }

    /// R5/R6/R11: the cross-tier switch is the prominent upsell only when it is
    /// an upgrade (held Local Pro → Cloud). A Cloud holder's switch to Local Pro
    /// is a downgrade and renders quiet, as does an unresolved held tier
    /// (`.none` — no confident upgrade to push).
    static func switchIsProminent(heldTier: EntitlementTier) -> Bool {
        heldTier == .localPro
    }

    // MARK: - Tier buttons

    /// How one tier row renders in a given state.
    enum TierAffordance: Equatable {
        /// Tappable buy button → `startCheckout(tier:)`.
        case buy
        /// The already-held tier (trialing or subscribed): a current-plan
        /// label, NOT a buy button — checkout must never mint a second
        /// subscription for a tier the account already holds (U4 approach);
        /// tier switching goes through Manage Subscription.
        case currentPlan
        /// The OTHER tier while the account already holds a subscription
        /// (trial / subscribed / checkout-pending with a held tier): a
        /// secondary button routing to the Stripe portal
        /// (`startManageSubscription`), NOT a buy button — a fresh checkout
        /// for an existing subscriber would mint a SECOND subscription (U4:
        /// "tier switching for existing subscribers goes through Manage
        /// Subscription").
        case switchViaPortal
        /// Signed-out plans preview (R4): visible with pricing, not tappable.
        case disabledPreview
        /// Not rendered at all (neutral/unknown states, paywall off).
        case hidden
    }

    /// Affordance for `tier` given the sheet state, the account's held tier
    /// (`CloudAuthController.tier` — during a trial it carries the tier the
    /// trial converts into), and the paywall flag.
    ///
    /// `checkoutPending` keeps tiers tappable (KTD-6): a re-tap replaces the
    /// remembered target and re-runs checkout, so an abandoned Stripe tab is
    /// always recoverable in place — but only while NO tier is held; once a
    /// subscription exists, the cross-tier affordance is portal-routed
    /// (`switchViaPortal`), never a second checkout.
    static func tierAffordance(
        tier: EntitlementTier,
        state: AccountSheetState,
        heldTier: EntitlementTier,
        paywallEnabled: Bool
    ) -> TierAffordance {
        guard paywallEnabled else { return .hidden } // R9 account-only mode.
        switch state {
        case .unknown, .neutral:
            return .hidden // AE7: no tier buttons on the neutral view.
        case .signedOut:
            return .disabledPreview
        case .trial, .subscribed, .checkoutPending:
            guard heldTier != .none else { return .buy }
            return tier == heldTier ? .currentPlan : .switchViaPortal
        case .lapsed:
            return .buy
        }
    }

    // MARK: - Upload entry decision (R14, U5)

    /// What the review window's Upload tap does.
    enum UploadEntryAction: Equatable {
        /// Start the upload — signed in and not on a tier the signer would
        /// refuse.
        case proceed
        /// Present the Account & Plan sheet in `.upload` context: sign-in
        /// framing when signed out, upgrade-to-Cloud framing for a signed-in
        /// Local Pro holder (today's raw signer-refusal path).
        case presentAccountSheet
    }

    /// The review window's Upload branch (R14). Signed out → the sheet
    /// (sign-in framing). Signed in on Local Pro with the paywall on → the
    /// sheet (upgrade-to-Cloud framing) instead of a raw signer error. A
    /// definitively lapsed signed-in account (`trialState == .lapsed`) → the
    /// sheet too — proceeding would also just hit the raw signer refusal.
    /// Everything else proceeds — including paywall-off (pre-billing behavior:
    /// no upgrade surface exists to show, R9) and the stale/offline case
    /// (`tier` resolves `.none` and `trialState` resolves `.indeterminate`
    /// when stale; grace must never gate, KTD-4).
    static func uploadEntryAction(
        isSignedIn: Bool,
        tier: EntitlementTier,
        trialState: TrialState,
        paywallEnabled: Bool
    ) -> UploadEntryAction {
        guard isSignedIn else { return .presentAccountSheet }
        if paywallEnabled, tier == .localPro || trialState == .lapsed {
            return .presentAccountSheet
        }
        return .proceed
    }

    // MARK: - Framing (context selects copy only, KTD-3)

    /// The sheet headline. The neutral state suppresses gate/upload framing
    /// entirely (AE7: no gate framing while entitlement is unconfirmable).
    static func headline(
        context: AccountSheetContext,
        state: AccountSheetState
    ) -> String {
        guard state != .neutral else { return AccountSheetCopy.accountHeadline }
        switch context {
        case .gate:
            return isEntitledSuccess(state)
                ? AccountSheetCopy.successHeadline
                : AccountSheetCopy.gateHeadline
        case .upload:
            return state == .signedOut
                ? AccountSheetCopy.uploadSignInHeadline
                : AccountSheetCopy.uploadUpgradeHeadline
        case .account, .onboarding:
            return AccountSheetCopy.accountHeadline
        }
    }

    /// The framing subcopy under the headline (same selection rules).
    static func subcopy(
        context: AccountSheetContext,
        state: AccountSheetState
    ) -> String {
        guard state != .neutral else { return AccountSheetCopy.accountSub }
        switch context {
        case .gate:
            if isEntitledSuccess(state) {
                return state == .trial
                    ? AccountSheetCopy.successSubTrial
                    : AccountSheetCopy.successSubSubscribed
            }
            return AccountSheetCopy.gateReassurance
        case .upload:
            return state == .signedOut
                ? AccountSheetCopy.uploadSignInSub
                : AccountSheetCopy.uploadUpgradeSub
        case .account, .onboarding:
            return AccountSheetCopy.accountSub
        }
    }

    /// The dismiss affordance title. In gate context an already-entitled
    /// account (the success moment) gets "Done"; an unentitled account keeps the
    /// explicit "Not now" (R3 — dismissible while unresolved). Other presented
    /// contexts use a plain Close. Embedded wrappers (Settings pane, onboarding
    /// chrome) simply omit the affordance by passing no `onDismiss`.
    static func dismissTitle(
        context: AccountSheetContext,
        state: AccountSheetState
    ) -> String {
        guard context == .gate else { return AccountSheetCopy.close }
        return isEntitledSuccess(state) ? AccountSheetCopy.done : AccountSheetCopy.notNow
    }
}

/// Account-sheet copy catalog (KTD-7): every honesty-gated string the sheet
/// renders, string-assertable in `AccountSheetPolicyTests` without a render
/// tree. No terminal-instruction phrasing anywhere (R10) — the U6 sweep is the
/// durable enforcement; the policy tests audit `renderedStrings` here too.
enum AccountSheetCopy {

    // MARK: - Gate framing (R3)

    static let gateHeadline = "Subscribe to keep recording"
    /// The load-bearing local-data reassurance: a lapse gates NEW recording and
    /// search; captured recordings stay on this Mac, browsable and exportable.
    static let gateReassurance =
        "Recording and search need an active subscription. Your existing "
        + "recordings are safe on this Mac — you can still browse and export "
        + "them anytime."

    // MARK: - Success framing (gate + entitled, R1)

    /// Shown when an already-entitled account reaches the gate — the
    /// just-subscribed-in-place success moment. State-asserting, never
    /// event-asserting: gate+entitled is not exclusively an in-place subscribe
    /// (see AccountSheetPolicy KTD-1), so no "you just subscribed" phrasing.
    static let successHeadline = "You're all set"
    /// Paid-subscription success subcopy.
    static let successSubSubscribed =
        "Your subscription is active — recording and search stay unlocked."
    /// Trial success subcopy: keeps the cancel-before-charge caveat in the
    /// PRIMARY framing (R1 honesty), not the card line alone, so a not-yet-charged
    /// trial user is never told they are simply "all set".
    static let successSubTrial =
        "Your free trial is active — cancel before it ends in Manage "
        + "Subscription and you won't be charged."
    static let done = "Done"

    // MARK: - Account framing

    static let accountHeadline = "Account & Plan"
    static let accountSub =
        "Your Screencap account and subscription, in one place."
    /// Neutral-state plan note (AE7): identity if known, plan unconfirmable —
    /// usually offline. Never reads as lapsed, never gates.
    static let neutralPlanNote =
        "Your plan details couldn't be confirmed right now — this usually "
        + "means this Mac is offline. Your recordings and settings are "
        + "unaffected."

    // MARK: - Upload framing (R14)

    static let uploadSignInHeadline = "Sign in to upload"
    static let uploadSignInSub =
        "Uploading to the cloud requires a Screencap account. Local recording "
        + "and playback never need sign-in."
    static let uploadUpgradeHeadline = "Upgrade to Cloud to upload"
    static let uploadUpgradeSub =
        "Uploading needs the Cloud plan. Local Pro keeps everything on this "
        + "Mac — Cloud adds upload and sync for the recordings you approve."

    // MARK: - Sign-in flow

    static let checkingAccount = "Checking sign-in…"
    static let signInButton = "Sign In…"
    static let signInWaiting = "Waiting for sign-in in your browser…"

    // MARK: - Sign out (R8)

    static let signOutConfirmTitle = "Sign out of Screencap?"
    /// Consequence copy, honesty-gated: "new recording" wording only — an
    /// in-flight recording is never claimed to stop — plus the data-stays-local
    /// reassurance. Paywall-aware: with the paywall off, sign-out does NOT
    /// pause recording, so claiming it would overstate the consequence.
    static func signOutConfirmBody(paywallEnabled: Bool) -> String {
        paywallEnabled ? signOutConfirmBodyGated : signOutConfirmBodyUngated
    }
    static let signOutConfirmBodyGated =
        "New recording and search will pause until you sign in again. Your "
        + "existing recordings stay on this Mac — you can browse and export "
        + "them anytime."
    static let signOutConfirmBodyUngated =
        "Uploading will need a sign-in again. Your recordings stay on this "
        + "Mac — you can browse and export them anytime."
    static let signOutConfirmAction = "Sign Out"
    /// Shown while `canSignOut` is false with an upload in flight (R8).
    static let signOutDisabledUploadInFlight =
        "Sign Out is unavailable while an upload is in progress."

    // MARK: - Plans & checkout

    /// Tier button title with the catalog price (single price source, KTD-7).
    static func tierButtonTitle(_ tier: EntitlementTier) -> String? {
        switch tier {
        case .localPro:
            return "Local Pro — \(PricingCatalog.localProPriceLine)"
        case .cloud:
            return "Cloud — \(PricingCatalog.cloudPriceLine)"
        case .none:
            return nil
        }
    }

    /// One-line tier descriptor (honesty: the Cloud line states server-side
    /// storage; no E2EE / "we can't watch" claims).
    static func tierDetail(_ tier: EntitlementTier) -> String? {
        switch tier {
        case .localPro:
            return "Unlimited recording & search on this Mac"
        case .cloud:
            return "Adds upload, sync & AI (stored server-side)"
        case .none:
            return nil
        }
    }

    /// The bare price line for the consolidated plan card. The card renders the
    /// plan name separately, so this omits the tier name that `tierButtonTitle`
    /// prepends.
    static func planPriceLine(_ tier: EntitlementTier) -> String? {
        switch tier {
        case .localPro: return PricingCatalog.localProPriceLine
        case .cloud: return PricingCatalog.cloudPriceLine
        case .none: return nil
        }
    }

    /// Plan display name for the signed-in plan line.
    static func planName(_ tier: EntitlementTier) -> String {
        switch tier {
        case .localPro: return "Local Pro"
        case .cloud: return "Cloud"
        case .none: return "No plan"
        }
    }

    static let currentPlanBadge = "Current plan"
    static let manageSubscription = "Manage Subscription"
    /// The cross-tier affordance for an existing subscriber (`switchViaPortal`):
    /// routes to Manage Subscription rather than checkout — never a second
    /// subscription, no terminal phrasing (R10).
    static func switchPlanButtonTitle(_ tier: EntitlementTier) -> String? {
        switch tier {
        case .localPro, .cloud:
            return "Switch to \(planName(tier))…"
        case .none:
            return nil
        }
    }
    /// Caption under the switch button: honest about where the switch happens.
    static let switchPlanDetail = "Plan changes happen in Manage Subscription"
    /// Trial cancel affordance (R7): the app promises pre-conversion
    /// cancellation, and Manage Subscription is where it happens.
    static let trialCancelAffordance =
        "Cancel anytime before your trial ends in Manage Subscription and "
        + "you won't be charged."

    // MARK: - Checkout pending / reconcile (KTD-6, AE3)

    static let checkoutPendingBanner =
        "Unlocks automatically once payment completes."
    static let reconcileCheckNow = "I've paid — check now"
    /// Shown when a manual reconcile returns still-unresolved — never a silent
    /// return to the same view. Mirrors the error seam's eventual-consistency
    /// honesty: never asserts nothing exists.
    static let reconcileStillProcessing =
        "Your subscription still isn't showing up — try again in a minute."

    // MARK: - Dismissal

    static let notNow = "Not now"
    static let close = "Close"

    /// Every string the sheet renders, for the honesty / no-terminal audits
    /// (policy-test level; the U6 literal sweep is the durable enforcement).
    static var renderedStrings: [String] {
        [
            gateHeadline, gateReassurance,
            successHeadline, successSubSubscribed, successSubTrial, done,
            accountHeadline, accountSub, neutralPlanNote,
            uploadSignInHeadline, uploadSignInSub,
            uploadUpgradeHeadline, uploadUpgradeSub,
            checkingAccount, signInButton, signInWaiting,
            signOutConfirmTitle, signOutConfirmBodyGated,
            signOutConfirmBodyUngated, signOutConfirmAction,
            signOutDisabledUploadInFlight,
            currentPlanBadge, manageSubscription, trialCancelAffordance,
            switchPlanDetail,
            checkoutPendingBanner, reconcileCheckNow, reconcileStillProcessing,
            notNow, close,
        ]
        + [EntitlementTier.localPro, .cloud].compactMap(tierButtonTitle)
        + [EntitlementTier.localPro, .cloud].compactMap(switchPlanButtonTitle)
        + [EntitlementTier.localPro, .cloud].compactMap(tierDetail)
        + [EntitlementTier.localPro, .cloud].compactMap(planPriceLine)
        + [EntitlementTier.localPro, .cloud, .none].map(planName)
    }
}
