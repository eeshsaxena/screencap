import XCTest
@testable import ScreenCap

/// Account & Plan sheet policy tests (account-sheet plan U4) — pure
/// derivation, no rendering, mirroring `OnboardingStepPolicyTests`: state
/// mapping from controller-published values (HTD state chart), affordance
/// visibility (tiers / Manage Subscription / Sign Out), context framing
/// selection, and the string-level honesty pins for the sign-out
/// confirmation, reconcile, and no-terminal (R10) rules.
final class AccountSheetPolicyTests: XCTestCase {

    // MARK: - Helpers

    private func signedIn(stale: Bool = false) -> AuthStatus {
        .signedIn(email: "user@example.com", uid: "uid-1", stale: stale)
    }

    private func state(
        _ status: AuthStatus,
        _ trial: TrialState,
        checkoutPending: Bool = false
    ) -> AccountSheetState {
        AccountSheetPolicy.state(
            status: status, trialState: trial, checkoutPending: checkoutPending
        )
    }

    // MARK: - State mapping (HTD state chart)

    /// AE7 / R1: signed-in + stale (offline payer) → neutral. Stale
    /// entitlement never reads as lapsed, whatever the trial state says.
    func testSignedInStaleMapsToNeutral() {
        XCTAssertEqual(state(signedIn(stale: true), .indeterminate), .neutral)
        XCTAssertEqual(state(signedIn(stale: true), .lapsed), .neutral)
        // Stale outranks checkout-pending too — no plan claim to converge on.
        XCTAssertEqual(
            state(signedIn(stale: true), .indeterminate, checkoutPending: true),
            .neutral
        )
    }

    /// HTD note: `trialState == .indeterminate` alone is NEVER a neutral
    /// signal — it also covers signed-out, so a signed-out user renders
    /// signedOut, never neutral.
    func testSignedOutWithIndeterminateTrialMapsToSignedOut() {
        XCTAssertEqual(state(.signedOut, .indeterminate), .signedOut)
        // Signed-out wins over any trial-shaped leftover state.
        XCTAssertEqual(state(.signedOut, .lapsed), .signedOut)
        XCTAssertEqual(state(.signedOut, .indeterminate, checkoutPending: true), .signedOut)
    }

    /// Pre-resolution: no affordances until the lazy whoami lands.
    func testUnknownStatusMapsToUnknown() {
        XCTAssertEqual(state(.unknown, .indeterminate), .unknown)
    }

    /// The signed-in base states: live trial phases → trial; converted payer
    /// → subscribed; no tier + no live trial → lapsed.
    func testSignedInBaseStateMapping() {
        XCTAssertEqual(state(signedIn(), .active(daysLeft: 10)), .trial)
        XCTAssertEqual(state(signedIn(), .nearExpiry(daysLeft: 2)), .trial)
        XCTAssertEqual(state(signedIn(), .lastDay(hoursLeft: 3)), .trial)
        XCTAssertEqual(state(signedIn(), .subscribed), .subscribed)
        XCTAssertEqual(state(signedIn(), .lapsed), .lapsed)
    }

    /// Signed-in, non-stale, indeterminate (paywall off / unresolved claim):
    /// the safe no-gate no-tiers rendering, not lapsed.
    func testSignedInIndeterminateMapsToNeutral() {
        XCTAssertEqual(state(signedIn(), .indeterminate), .neutral)
    }

    /// KTD-6: a pending checkout round-trip overlays the trial/lapsed base
    /// state so the "unlocks automatically" surface survives the browser
    /// round-trip.
    func testCheckoutPendingOverlaysSignedInStates() {
        XCTAssertEqual(
            state(signedIn(), .lapsed, checkoutPending: true), .checkoutPending
        )
        XCTAssertEqual(
            state(signedIn(), .active(daysLeft: 5), checkoutPending: true),
            .checkoutPending
        )
    }

    // MARK: - Paywall-off account-only mode (R9 / AE2)

    /// AE2: paywall off → no tier buttons in ANY state (account-only mode).
    func testPaywallOffHidesAllTierButtons() {
        let states: [AccountSheetState] = [
            .unknown, .signedOut, .neutral, .trial, .subscribed, .lapsed,
            .checkoutPending,
        ]
        for sheetState in states {
            for tier in [EntitlementTier.localPro, .cloud] {
                XCTAssertEqual(
                    AccountSheetPolicy.tierAffordance(
                        tier: tier, state: sheetState,
                        heldTier: .localPro, paywallEnabled: false
                    ),
                    .hidden,
                    "paywall off must hide \(tier) in \(sheetState)"
                )
            }
        }
    }

    /// R9's carve-out: Manage Subscription is independent of the paywall flag
    /// — `showsManageSubscription` keys on state only, so an account with a
    /// subscription keeps the manage affordance with the flag off.
    func testManageSubscriptionIsIndependentOfPaywallFlag() {
        XCTAssertTrue(AccountSheetPolicy.showsManageSubscription(state: .trial))
        XCTAssertTrue(AccountSheetPolicy.showsManageSubscription(state: .subscribed))
        // The affordance derives from state alone; the paywall flag only feeds
        // tier-button visibility (asserted hidden above).
    }

    // MARK: - Signed-out affordances (R4 / AE1)

    /// AE1 (state half): signed out → tiers render as a disabled preview and
    /// Sign In is the single primary action.
    func testSignedOutTiersDisabledAndSignInPrimary() {
        for tier in [EntitlementTier.localPro, .cloud] {
            XCTAssertEqual(
                AccountSheetPolicy.tierAffordance(
                    tier: tier, state: .signedOut,
                    heldTier: .none, paywallEnabled: true
                ),
                .disabledPreview
            )
        }
        XCTAssertTrue(AccountSheetPolicy.showsSignInPrimary(state: .signedOut))
        XCTAssertFalse(AccountSheetPolicy.showsSignInPrimary(state: .trial))
        XCTAssertFalse(AccountSheetPolicy.showsSignOut(state: .signedOut))
    }

    // MARK: - Manage Subscription visibility (R7 / AE6)

    /// AE6: trial AND subscribed expose Manage Subscription; signed-out and
    /// neutral do not (nothing confirmed to manage); lapsed does not (the
    /// backend answers no_subscription — the plans are the recourse).
    func testManageSubscriptionShownForTrialAndSubscribedOnly() {
        XCTAssertTrue(AccountSheetPolicy.showsManageSubscription(state: .trial))
        XCTAssertTrue(AccountSheetPolicy.showsManageSubscription(state: .subscribed))
        XCTAssertFalse(AccountSheetPolicy.showsManageSubscription(state: .signedOut))
        XCTAssertFalse(AccountSheetPolicy.showsManageSubscription(state: .neutral))
        XCTAssertFalse(AccountSheetPolicy.showsManageSubscription(state: .lapsed))
        XCTAssertFalse(AccountSheetPolicy.showsManageSubscription(state: .unknown))
    }

    // MARK: - Context framing (R3, KTD-3)

    /// R3: the gate context carries the urgent gate framing; the account
    /// context reads as a neutral settings surface. Pinned strings.
    func testGateContextSelectsGateFramingAccountContextNeutral() {
        XCTAssertEqual(
            AccountSheetPolicy.headline(context: .gate, state: .lapsed),
            "Subscribe to keep recording"
        )
        XCTAssertEqual(
            AccountSheetPolicy.subcopy(context: .gate, state: .lapsed),
            AccountSheetCopy.gateReassurance
        )
        // The gate reassurance carries the load-bearing data-stays-local line.
        XCTAssertTrue(AccountSheetCopy.gateReassurance.contains("safe on this Mac"))

        XCTAssertEqual(
            AccountSheetPolicy.headline(context: .account, state: .subscribed),
            AccountSheetCopy.accountHeadline
        )
        XCTAssertEqual(
            AccountSheetPolicy.headline(context: .onboarding, state: .lapsed),
            AccountSheetCopy.accountHeadline
        )
    }

    /// AE7: the neutral state suppresses gate framing even when the sheet was
    /// opened as a gate — an offline payer never sees "Subscribe to keep
    /// recording".
    func testNeutralStateSuppressesGateFraming() {
        XCTAssertEqual(
            AccountSheetPolicy.headline(context: .gate, state: .neutral),
            AccountSheetCopy.accountHeadline
        )
        XCTAssertEqual(
            AccountSheetPolicy.subcopy(context: .gate, state: .neutral),
            AccountSheetCopy.accountSub
        )
    }

    /// R14: the upload context frames sign-in when signed out and
    /// upgrade-to-Cloud when signed in (a Local Pro user tapping Upload).
    func testUploadContextFramingVariants() {
        XCTAssertEqual(
            AccountSheetPolicy.headline(context: .upload, state: .signedOut),
            "Sign in to upload"
        )
        XCTAssertEqual(
            AccountSheetPolicy.headline(context: .upload, state: .trial),
            "Upgrade to Cloud to upload"
        )
        XCTAssertEqual(
            AccountSheetPolicy.subcopy(context: .upload, state: .subscribed),
            AccountSheetCopy.uploadUpgradeSub
        )
    }

    // MARK: - Upload entry decision (R14, U5)

    /// The review window's Upload tap: signed out → the sheet (sign-in
    /// framing); signed in on Local Pro with the paywall on → the sheet
    /// (upgrade-to-Cloud framing) instead of the raw signer refusal; a
    /// cloud-entitled account proceeds straight to the upload.
    func testUploadEntryActionBranches() {
        XCTAssertEqual(
            AccountSheetPolicy.uploadEntryAction(
                isSignedIn: false, tier: .none, trialState: .indeterminate,
                paywallEnabled: true
            ),
            .presentAccountSheet
        )
        XCTAssertEqual(
            AccountSheetPolicy.uploadEntryAction(
                isSignedIn: true, tier: .localPro, trialState: .subscribed,
                paywallEnabled: true
            ),
            .presentAccountSheet
        )
        XCTAssertEqual(
            AccountSheetPolicy.uploadEntryAction(
                isSignedIn: true, tier: .cloud, trialState: .subscribed,
                paywallEnabled: true
            ),
            .proceed
        )
    }

    /// A definitively lapsed signed-in account (`tier == .none`, `trialState
    /// == .lapsed`) presents the sheet too — proceeding would just hit the raw
    /// signer refusal the sheet exists to replace.
    func testUploadEntryActionLapsedPresentsSheet() {
        XCTAssertEqual(
            AccountSheetPolicy.uploadEntryAction(
                isSignedIn: true, tier: .none, trialState: .lapsed,
                paywallEnabled: true
            ),
            .presentAccountSheet
        )
        // Paywall off: lapsed has no upgrade surface to show (R9) — proceed.
        XCTAssertEqual(
            AccountSheetPolicy.uploadEntryAction(
                isSignedIn: true, tier: .none, trialState: .lapsed,
                paywallEnabled: false
            ),
            .proceed
        )
    }

    /// Paywall off is pre-billing behavior (R9 — there is no upgrade surface
    /// to show), and the stale/offline case resolves `tier == .none` +
    /// `trialState == .indeterminate` (KTD-4 — grace never gates): both
    /// proceed for a signed-in account.
    func testUploadEntryActionPaywallOffAndGraceProceed() {
        XCTAssertEqual(
            AccountSheetPolicy.uploadEntryAction(
                isSignedIn: true, tier: .localPro, trialState: .subscribed,
                paywallEnabled: false
            ),
            .proceed
        )
        XCTAssertEqual(
            AccountSheetPolicy.uploadEntryAction(
                isSignedIn: true, tier: .none, trialState: .indeterminate,
                paywallEnabled: true
            ),
            .proceed
        )
    }

    /// R3: the gate keeps an explicit dismiss affordance ("Not now") — the
    /// sheet frames, it does not trap.
    func testGateDismissTitleIsNotNow() {
        XCTAssertEqual(AccountSheetPolicy.dismissTitle(context: .gate), "Not now")
        XCTAssertEqual(AccountSheetPolicy.dismissTitle(context: .account), "Close")
        XCTAssertEqual(AccountSheetPolicy.dismissTitle(context: .upload), "Close")
    }

    // MARK: - Sign-out confirmation copy (R8 / AE5)

    /// R8 honesty pins: "new recording" wording (an in-flight recording is
    /// never claimed to stop) plus the data-stays-local reassurance; the
    /// paywall-off variant makes no recording-pause claim at all (sign-out
    /// does not gate recording with the flag off).
    func testSignOutConfirmationCopyPinned() {
        let gated = AccountSheetCopy.signOutConfirmBody(paywallEnabled: true)
        XCTAssertEqual(gated, AccountSheetCopy.signOutConfirmBodyGated)
        XCTAssertTrue(gated.contains("New recording"))
        XCTAssertTrue(gated.contains("stay on this Mac"))
        // Never claims the in-flight recording stops.
        XCTAssertFalse(gated.lowercased().contains("current recording"))
        XCTAssertFalse(gated.lowercased().contains("stops immediately"))

        let ungated = AccountSheetCopy.signOutConfirmBody(paywallEnabled: false)
        XCTAssertEqual(ungated, AccountSheetCopy.signOutConfirmBodyUngated)
        XCTAssertTrue(ungated.contains("stay on this Mac"))
        XCTAssertFalse(ungated.lowercased().contains("recording and search will pause"))

        XCTAssertTrue(AccountSheetCopy.signOutDisabledUploadInFlight.contains("upload"))
    }

    /// Sign Out renders on every signed-in state and never on signed-out /
    /// unknown (its enabled state rides `canSignOut` at the view).
    func testSignOutVisibilityByState() {
        for shown in [AccountSheetState.neutral, .trial, .subscribed, .lapsed, .checkoutPending] {
            XCTAssertTrue(AccountSheetPolicy.showsSignOut(state: shown), "\(shown)")
        }
        XCTAssertFalse(AccountSheetPolicy.showsSignOut(state: .signedOut))
        XCTAssertFalse(AccountSheetPolicy.showsSignOut(state: .unknown))
    }

    // MARK: - Current-plan rendering (U4: held tier is never a buy button)

    /// The already-held tier (trialing or subscribed) renders as current-plan
    /// — checkout can never mint a second subscription for a tier the account
    /// already holds — while the OTHER tier is switch-routed through the
    /// Stripe portal (`switchViaPortal`), never a buy button: a fresh checkout
    /// for an existing subscriber would mint a second subscription (U4: tier
    /// switching goes through Manage Subscription).
    func testHeldTierRendersCurrentPlanOtherTierSwitchRouted() {
        // Trialing Local Pro: Local Pro is current, Cloud is portal-routed.
        XCTAssertEqual(
            AccountSheetPolicy.tierAffordance(
                tier: .localPro, state: .trial, heldTier: .localPro, paywallEnabled: true
            ),
            .currentPlan
        )
        XCTAssertEqual(
            AccountSheetPolicy.tierAffordance(
                tier: .cloud, state: .trial, heldTier: .localPro, paywallEnabled: true
            ),
            .switchViaPortal
        )
        // Subscribed Cloud: Cloud is current, Local Pro portal-routed.
        XCTAssertEqual(
            AccountSheetPolicy.tierAffordance(
                tier: .cloud, state: .subscribed, heldTier: .cloud, paywallEnabled: true
            ),
            .currentPlan
        )
        XCTAssertEqual(
            AccountSheetPolicy.tierAffordance(
                tier: .localPro, state: .subscribed, heldTier: .cloud, paywallEnabled: true
            ),
            .switchViaPortal
        )
    }

    /// KTD-6 within the no-second-subscription rule: during checkout-pending
    /// with a held tier, the held tier stays current-plan and the other tier
    /// is portal-routed; a pending checkout with NO held tier keeps both tiers
    /// buyable (the re-tap-replaces-target recovery), as does lapsed.
    func testCheckoutPendingAffordancesAndLapsedBuysBoth() {
        XCTAssertEqual(
            AccountSheetPolicy.tierAffordance(
                tier: .cloud, state: .checkoutPending, heldTier: .localPro, paywallEnabled: true
            ),
            .switchViaPortal
        )
        XCTAssertEqual(
            AccountSheetPolicy.tierAffordance(
                tier: .localPro, state: .checkoutPending, heldTier: .localPro, paywallEnabled: true
            ),
            .currentPlan
        )
        for tier in [EntitlementTier.localPro, .cloud] {
            // No held tier (e.g. a lapsed account's pending checkout): the
            // abandoned-Stripe-tab recovery — both tiers stay buyable.
            XCTAssertEqual(
                AccountSheetPolicy.tierAffordance(
                    tier: tier, state: .checkoutPending, heldTier: .none, paywallEnabled: true
                ),
                .buy
            )
            XCTAssertEqual(
                AccountSheetPolicy.tierAffordance(
                    tier: tier, state: .lapsed, heldTier: .none, paywallEnabled: true
                ),
                .buy
            )
        }
    }

    /// Signed-out stays a disabled preview and lapsed stays buy — the
    /// portal-routing applies only where a subscription already exists.
    func testSwitchRoutingLeavesSignedOutAndLapsedUnchanged() {
        XCTAssertEqual(
            AccountSheetPolicy.tierAffordance(
                tier: .cloud, state: .signedOut, heldTier: .none, paywallEnabled: true
            ),
            .disabledPreview
        )
        XCTAssertEqual(
            AccountSheetPolicy.tierAffordance(
                tier: .cloud, state: .lapsed, heldTier: .none, paywallEnabled: true
            ),
            .buy
        )
    }

    // MARK: - Error seam visibility (dead-retry dedup)

    /// The shared error section is suppressed while the sheet renders
    /// signed-out: there `signInSection` owns the failure display (message +
    /// working retry), and rendering `accountError` on top stacked two copies
    /// of the failure plus a dead "Try Again". Every other state renders it.
    func testErrorSectionHiddenWhileSignedOut() {
        XCTAssertFalse(AccountSheetPolicy.showsErrorSection(state: .signedOut))
        for shown in [AccountSheetState.unknown, .neutral, .trial, .subscribed,
                      .lapsed, .checkoutPending] {
            XCTAssertTrue(AccountSheetPolicy.showsErrorSection(state: shown), "\(shown)")
        }
    }

    // MARK: - Reconcile / pending copy (AE3)

    /// AE3: the pending banner promises the automatic unlock, and the
    /// unresolved-reconcile copy takes the eventual-consistency shape ("try
    /// again in a minute") — it never asserts no subscription exists, so a
    /// just-paid user is never told to buy again.
    func testReconcileAndPendingCopyPinned() {
        XCTAssertEqual(
            AccountSheetCopy.checkoutPendingBanner,
            "Unlocks automatically once payment completes."
        )
        XCTAssertEqual(AccountSheetCopy.reconcileCheckNow, "I've paid — check now")
        XCTAssertTrue(
            AccountSheetCopy.reconcileStillProcessing.contains("try again in a minute")
        )
        XCTAssertFalse(
            AccountSheetCopy.reconcileStillProcessing.lowercased().contains("no subscription")
        )
        XCTAssertTrue(AccountSheetPolicy.showsTrialCancelAffordance(state: .trial))
        XCTAssertFalse(AccountSheetPolicy.showsTrialCancelAffordance(state: .subscribed))
    }

    /// The sign-in progress copy matches the app's established browser-handoff
    /// wording.
    func testSignInWaitingCopyPinned() {
        XCTAssertEqual(
            AccountSheetCopy.signInWaiting,
            "Waiting for sign-in in your browser…"
        )
    }

    // MARK: - No-terminal rule over the catalog (R10; U6 sweep is the durable lock)

    /// No rendered account-sheet string instructs terminal use or names the
    /// CLI invocation. Policy-level early warning; `TerminalStringSweepTests`
    /// (U6) is the tree-wide enforcement.
    func testNoRenderedStringInstructsTerminalUse() {
        let banned = ["screencap login", "run `", "command line", "terminal"]
        for string in AccountSheetCopy.renderedStrings {
            let lower = string.lowercased()
            for phrase in banned {
                XCTAssertFalse(
                    lower.contains(phrase),
                    "banned phrase \"\(phrase)\" in: \(string)"
                )
            }
        }
        // Vacuity guard: the audit list actually covers the catalog.
        XCTAssertGreaterThan(AccountSheetCopy.renderedStrings.count, 20)
    }
}
