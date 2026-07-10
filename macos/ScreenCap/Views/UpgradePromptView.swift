import SwiftUI

/// "Resubscribe to keep recording & searching" prompt (paid-only launch, U12).
/// Presented when a lapsed / not-entitled user (`CloudAuthController.isGatedForLapse`)
/// attempts to record or search, so the gated affordance surfaces a concrete
/// upgrade path — a sheet mirroring `SignInPromptView` — rather than a silent
/// no-op.
///
/// The load-bearing reassurance (R8): a lapse gates *new* recording and the
/// searchable recall surface, but the user's already-captured recordings stay on
/// this Mac, browsable and exportable. The copy states that up front so gated
/// never reads as "you lost your data".
///
/// Checkout runs through `CloudAuthController.startCheckout(tier:)`, which mints a
/// hosted Stripe URL in Python and opens the browser — the webhook is the sole
/// entitlement authority (KTD-2), so the tier passed here only selects the price.
/// No card claims E2EE / "we can't watch" (R12): the Cloud line is honest about
/// server-readable storage.
struct UpgradePromptView: View {
    @ObservedObject var auth: CloudAuthController
    /// Called when the user dismisses the prompt (Cancel / Close) without acting.
    let onDismiss: () -> Void

    /// A checkout-mint failure reason (offline, CLI error). Rendered inline so a
    /// tap that can't open the browser explains itself rather than doing nothing.
    @State private var checkoutError: String?

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "lock.circle")
                .font(.largeTitle)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text("Subscribe to keep recording")
                .font(.headline)
            Text("Recording and search need an active subscription. Your existing recordings are safe on this Mac — you can still browse and export them anytime.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)

            if let checkoutError {
                Text(checkoutError)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 380)
            }

            actions
        }
        .padding(24)
        .frame(minWidth: 400)
        // If entitlement is restored on any surface while this sheet is open
        // (resubscribed elsewhere, or a stale token refreshed into grace), the
        // gate lifts — dismiss so the caller can proceed instead of stranding on
        // a now-stale prompt. Mirrors SignInPromptView's cross-surface settle.
        .onChange(of: auth.isGatedForLapse) { gated in
            if !gated { onDismiss() }
        }
    }

    @ViewBuilder
    private var actions: some View {
        VStack(spacing: 10) {
            Button {
                beginCheckout(.localPro)
            } label: {
                Text("Local Pro — unlimited recording & search")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .accessibilityHint("Opens checkout for the Local Pro subscription in your browser.")

            Button {
                beginCheckout(.cloud)
            } label: {
                Text("Cloud — adds upload, sync & AI (stored server-side)")
                    .frame(maxWidth: .infinity)
            }
            .accessibilityHint("Opens checkout for the Cloud subscription, which stores recordings server-side, in your browser.")

            Button("Not now") { onDismiss() }
                .keyboardShortcut(.cancelAction)
        }
        .frame(maxWidth: 360)
    }

    private func beginCheckout(_ tier: EntitlementTier) {
        checkoutError = nil
        auth.startCheckout(tier: tier) { reason in
            checkoutError = reason
        }
    }
}
