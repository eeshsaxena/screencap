import SwiftUI

/// The unified, state-adaptive Account & Plan sheet (account-sheet plan U4).
/// One surface for sign-in, plan state, checkout, manage-subscription, and
/// sign-out — replacing `SignInPromptView` and `UpgradePromptView` (U5 rewires
/// the entry points).
///
/// A thin view over `AccountSheetPolicy`: every rendered state is derived from
/// `CloudAuthController`-published values (the view renders, it never computes
/// auth state), and `context` selects framing copy only (KTD-3). Settle
/// side-effects — upload auto-start, wizard advance, dismissal — stay
/// caller-owned via the optional `onSettled` closure with the one-shot latch.
///
/// The body renders embeddable: no window-chrome assumptions, so sheets
/// (gate/upload), the Settings `.account` pane, and the onboarding wizard
/// chrome all wrap the same content. Visual idioms mirror
/// `UpgradePromptView`/`SignInPromptView` (padding, title font, button styles).
struct AccountSheetView: View {
    @ObservedObject var auth: CloudAuthController
    /// Which entry point presented this instance — framing copy only (KTD-3).
    let context: AccountSheetContext
    /// Fired exactly once when sign-in settles while this instance is up
    /// (one-shot latch, mirroring `SignInPromptView`'s `didSignIn`). The upload
    /// entry uses it to dismiss and auto-start the gated upload (R14).
    ///
    /// Deliberately fires only on the signed-out → signed-in TRANSITION, never
    /// on appear-already-signed-in: the upload entry also presents this sheet
    /// to signed-in Local Pro users in upgrade-to-Cloud framing (R14), and an
    /// on-appear settle would insta-dismiss it and auto-fire the upload.
    var onSettled: (() -> Void)? = nil
    /// Dismisses the presenting container ("Not now" on the gate, Close
    /// elsewhere). Embedded wrappers (Settings pane, onboarding chrome) omit
    /// it and render no dismiss affordance. The presenting window owns
    /// cancellation of an in-flight sign-in on dismissal (R14).
    var onDismiss: (() -> Void)? = nil

    /// One-shot latch so the sign-in settle calls `onSettled` exactly once
    /// even if `isSignedIn` republishes — prevents a double upload.
    @State private var didSettle = false
    @State private var showingSignOutConfirm = false
    /// Transient "still not showing up" copy after a manual reconcile that
    /// came back unresolved — never a silent return to the same view (U4).
    @State private var reconcileCameBackUnresolved = false
    /// The last billing action this instance ran, so an `accountError` with
    /// `.retry` re-runs the thing that actually failed (checkout for the
    /// tapped tier, or the portal fetch) rather than a generic no-op.
    @State private var retryLastAction: (() -> Void)?

    /// The policy-derived rendered state (HTD state chart).
    private var sheetState: AccountSheetState {
        AccountSheetPolicy.state(
            status: auth.status,
            trialState: auth.trialState,
            checkoutPending: auth.checkoutPending
        )
    }

    var body: some View {
        VStack(spacing: 16) {
            header
            stateContent
            errorSection
            dismissFooter
        }
        .padding(24)
        .frame(minWidth: 400)
        // R4: sign-in in flight is cancellable but not swipe-dismissable —
        // an accidental dismiss mid-round-trip would strand the browser tab.
        .interactiveDismissDisabled(auth.signInFlow == .inProgress)
        // Resolve sign-in state lazily on appear — never `refresh()`, never a
        // launch-path probe (KTD-1: decrypt-at-most-once Keychain discipline).
        .onAppear { Task { await auth.refreshIfNeeded() } }
        // One app-wide controller backs every surface, so sign-in can complete
        // on another surface while this sheet is open; the status flip is the
        // single settle trigger either way.
        .onChange(of: auth.isSignedIn) { signedIn in
            if signedIn { settleSignedIn() }
        }
        .confirmationDialog(
            AccountSheetCopy.signOutConfirmTitle,
            isPresented: $showingSignOutConfirm,
            titleVisibility: .visible
        ) {
            Button(AccountSheetCopy.signOutConfirmAction, role: .destructive) {
                Task { await auth.signOut() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            // R8 honesty: "new recording" wording only — never claims an
            // in-flight recording stops — and paywall-aware, since with the
            // paywall off sign-out does not pause recording.
            Text(AccountSheetCopy.signOutConfirmBody(paywallEnabled: auth.paywallEnabled))
        }
    }

    // MARK: - Header (context framing, KTD-3)

    private var header: some View {
        VStack(spacing: 8) {
            Image(systemName: headerSymbol)
                .font(.largeTitle)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text(AccountSheetPolicy.headline(context: context, state: sheetState))
                .font(.headline)
            Text(AccountSheetPolicy.subcopy(context: context, state: sheetState))
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)
        }
    }

    private var headerSymbol: String {
        switch context {
        case .gate: return "lock.circle"
        case .upload: return "icloud.and.arrow.up"
        case .account, .onboarding: return "person.crop.circle"
        }
    }

    // MARK: - State content

    @ViewBuilder
    private var stateContent: some View {
        switch sheetState {
        case .unknown:
            ProgressView(AccountSheetCopy.checkingAccount)
                .controlSize(.small)
        case .signedOut:
            plansSection
            signInSection
        case .neutral:
            identitySection
            Text(AccountSheetCopy.neutralPlanNote)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)
            accountActionsSection
        case .trial, .subscribed:
            identitySection
            planStatusSection
            plansSection
            accountActionsSection
        case .lapsed:
            identitySection
            plansSection
            accountActionsSection
        case .checkoutPending:
            identitySection
            checkoutPendingSection
            plansSection
            accountActionsSection
        }
    }

    // MARK: - Identity + plan status

    /// The account line: email preferred, uid fallback, or an offline label
    /// when signed-in-but-stale carries no cached identity.
    private var identitySection: some View {
        Text(auth.status.accountLabel.map { "Signed in: \($0)" } ?? "Signed in (offline)")
            .font(.callout)
            .foregroundStyle(.primary)
    }

    @ViewBuilder
    private var planStatusSection: some View {
        VStack(spacing: 6) {
            // Plan name only outside account-only mode when it exists; the
            // trial banner is shared with onboarding (single source of the
            // trial-days copy + auto-conversion disclosure).
            if auth.tier != .none {
                Text(AccountSheetCopy.planName(auth.tier))
                    .font(.callout.weight(.semibold))
            }
            if let banner = OnboardingCopy.trialBanner(for: auth.trialState) {
                Text(banner)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 380)
            }
            if AccountSheetPolicy.showsTrialCancelAffordance(state: sheetState) {
                Text(AccountSheetCopy.trialCancelAffordance)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 380)
            }
        }
    }

    // MARK: - Plans (R4/R6/R9)

    /// The two tier rows. Each row's affordance is policy-derived: buy /
    /// current-plan / disabled preview / hidden (paywall off hides all, R9).
    @ViewBuilder
    private var plansSection: some View {
        VStack(spacing: 10) {
            tierRow(.localPro)
            tierRow(.cloud)
        }
        .frame(maxWidth: 360)
    }

    @ViewBuilder
    private func tierRow(_ tier: EntitlementTier) -> some View {
        let affordance = AccountSheetPolicy.tierAffordance(
            tier: tier,
            state: sheetState,
            heldTier: auth.tier,
            paywallEnabled: auth.paywallEnabled
        )
        switch affordance {
        case .hidden:
            EmptyView()
        case .currentPlan:
            HStack {
                Text(AccountSheetCopy.tierButtonTitle(tier) ?? "")
                Spacer()
                Text(AccountSheetCopy.currentPlanBadge)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 10)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(.quaternary)
            )
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(AccountSheetCopy.planName(tier)). \(AccountSheetCopy.currentPlanBadge).")
        case .buy:
            tierBuyButton(tier, enabled: true)
        case .disabledPreview:
            // R4: plans preview while signed out — visible, priced, inactive.
            tierBuyButton(tier, enabled: false)
        }
    }

    @ViewBuilder
    private func tierBuyButton(_ tier: EntitlementTier, enabled: Bool) -> some View {
        VStack(spacing: 2) {
            Button {
                beginCheckout(tier)
            } label: {
                Text(AccountSheetCopy.tierButtonTitle(tier) ?? "")
                    .frame(maxWidth: .infinity)
            }
            .disabled(!enabled)
            .accessibilityHint(
                enabled
                    ? "Opens checkout for the \(AccountSheetCopy.planName(tier)) subscription in your browser."
                    : "Sign in first to subscribe."
            )
            if let detail = AccountSheetCopy.tierDetail(tier) {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Sign-in (R4)

    /// The signed-out action block: Sign In primary, the cancellable
    /// in-progress spinner, and the failure re-prompt. Parallels the retired
    /// `SignInPromptView.actions` switch over `SignInFlowState`.
    @ViewBuilder
    private var signInSection: some View {
        switch auth.signInFlow {
        case .inProgress:
            VStack(spacing: 8) {
                ProgressView(AccountSheetCopy.signInWaiting)
                    .controlSize(.small)
                Button("Cancel") { auth.cancelSignIn() }
            }
        case .failed(let reason):
            VStack(spacing: 8) {
                Text(reason)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 360)
                Button("Try Again") { auth.startSignIn() }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        case .idle:
            Button(AccountSheetCopy.signInButton) { auth.startSignIn() }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
        }
    }

    // MARK: - Checkout pending + reconcile (KTD-6 / AE3)

    private var checkoutPendingSection: some View {
        VStack(spacing: 8) {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(AccountSheetCopy.checkoutPendingBanner)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Waiting for payment to complete. Your subscription unlocks automatically.")

            Button {
                reconcileNow()
            } label: {
                HStack(spacing: 6) {
                    if auth.isReconciling {
                        ProgressView().controlSize(.small)
                    }
                    Text(AccountSheetCopy.reconcileCheckNow)
                }
            }
            .disabled(auth.isReconciling)

            if reconcileCameBackUnresolved && !auth.isReconciling {
                Text(AccountSheetCopy.reconcileStillProcessing)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 360)
            }
        }
    }

    private func reconcileNow() {
        reconcileCameBackUnresolved = false
        Task {
            await auth.reconcileEntitlement()
            // Still pending after the reconcile + refresh → say so (never a
            // silent return to the same view); Stripe's search is eventually
            // consistent, so the copy suggests a minute, not a re-buy.
            if auth.checkoutPending {
                reconcileCameBackUnresolved = true
            }
        }
    }

    // MARK: - Manage Subscription + Sign Out (R7/R8)

    @ViewBuilder
    private var accountActionsSection: some View {
        VStack(spacing: 8) {
            if AccountSheetPolicy.showsManageSubscription(state: sheetState) {
                Button(AccountSheetCopy.manageSubscription) { beginManage() }
                    .accessibilityHint("Opens your subscription management page in your browser.")
            }
            if AccountSheetPolicy.showsSignOut(state: sheetState) {
                Button("Sign Out") { showingSignOutConfirm = true }
                    .disabled(!auth.canSignOut)
                if auth.isSignedIn && !auth.canSignOut {
                    // R8: disabled-with-reason while an upload is in flight.
                    Text(AccountSheetCopy.signOutDisabledUploadInFlight)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - Error seam rendering (R10 / AE4)

    /// Renders the controller's published `AccountErrorCopy` — always the
    /// mapped static copy, never an envelope string — with its recovery
    /// action: retry re-runs the billing action that failed, sign-in starts
    /// the browser flow.
    @ViewBuilder
    private var errorSection: some View {
        if let error = auth.accountError {
            VStack(spacing: 8) {
                Text(error.message)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 380)
                switch error.action {
                case .retry:
                    Button("Try Again") {
                        auth.clearAccountError()
                        retryLastAction?()
                    }
                case .signIn:
                    Button(AccountSheetCopy.signInButton) {
                        auth.clearAccountError()
                        auth.startSignIn()
                    }
                }
            }
        }
    }

    // MARK: - Dismissal (R3)

    @ViewBuilder
    private var dismissFooter: some View {
        if let onDismiss {
            Button(AccountSheetPolicy.dismissTitle(context: context)) {
                onDismiss()
            }
            .keyboardShortcut(.cancelAction)
        }
    }

    // MARK: - Actions

    private func beginCheckout(_ tier: EntitlementTier) {
        reconcileCameBackUnresolved = false
        retryLastAction = { beginCheckout(tier) }
        // Failure publishes `accountError` (the seam); the String callback is
        // redundant here, so it stays empty — rendering rides the published
        // copy only (R10).
        auth.startCheckout(tier: tier) { _ in }
    }

    private func beginManage() {
        retryLastAction = { beginManage() }
        auth.startManageSubscription { _ in }
    }

    /// Fire `onSettled` exactly once across republishes (one-shot latch).
    private func settleSignedIn() {
        guard !didSettle else { return }
        didSettle = true
        onSettled?()
    }
}
