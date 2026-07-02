import SwiftUI

/// The single shared cloud-setup flow (U7, R4): Google sign-in → confirm a
/// founding plan with the training-contribution toggle → grant the entitlement.
/// Reused verbatim by onboarding (U6) and the settings surface (U8) so there is
/// never a second, divergent setup path. All token handling stays in Python; this
/// view only drives `CloudAuthController` and reads its published state.
///
/// State machine, read off the controller:
///  - not signed in  → the sign-in leg (`signInFlow`: idle/inProgress/failed).
///  - signed in       → the founding-plan confirmation + training toggle
///                       (`cloudSetupState`: idle/working/failed/done).
///  - done / already active → `onComplete`.
struct CloudSetupView: View {
    @ObservedObject var auth: CloudAuthController
    /// Fired once the account is entitled and authorized to upload.
    let onComplete: () -> Void
    /// Fired when the user cancels — leaves no partial entitlement.
    let onDismiss: () -> Void

    @State private var trainingOptIn = false
    @State private var didComplete = false

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "cloud.and.arrow.up")
                .font(.largeTitle)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text("Set up cloud")
                .font(.headline)

            content
        }
        .padding(24)
        .frame(minWidth: 400)
        .onAppear { settleIfEntitled() }
        .onChange(of: auth.entitlementStatus) { _ in settleIfEntitled() }
        .onChange(of: auth.cloudSetupState) { state in
            if state == .done { settle() }
        }
    }

    @ViewBuilder
    private var content: some View {
        if auth.status.isSignedIn {
            foundingConfirmation
        } else {
            signInLeg
        }
    }

    // MARK: - Sign-in leg

    @ViewBuilder
    private var signInLeg: some View {
        switch auth.signInFlow {
        case .inProgress:
            ProgressView("Waiting for sign-in in your browser…")
                .controlSize(.small)
            Button("Cancel") { cancel() }
                .keyboardShortcut(.cancelAction)
        case .failed(let reason):
            Text(reason)
                .font(.caption)
                .foregroundStyle(.red)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 340)
            HStack {
                Button("Cancel") { cancel() }
                    .keyboardShortcut(.cancelAction)
                Button("Try Again") { auth.startSignIn { _ in } }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        case .idle:
            Text("Sign in to keep your recordings in the cloud and reach them anywhere. Local recording never needs an account.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
            HStack {
                Button("Cancel") { cancel() }
                    .keyboardShortcut(.cancelAction)
                Button("Continue with Google") { auth.startSignIn { _ in } }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        }
    }

    // MARK: - Founding confirmation

    @ViewBuilder
    private var foundingConfirmation: some View {
        switch auth.cloudSetupState {
        case .working:
            ProgressView("Setting up your cloud plan…")
                .controlSize(.small)
        case .failed(let reason):
            Text(reason)
                .font(.caption)
                .foregroundStyle(.red)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 340)
            HStack {
                Button("Cancel") { cancel() }
                    .keyboardShortcut(.cancelAction)
                Button("Try Again") {
                    Task { await auth.completeCloudSetup(trainingOptIn: trainingOptIn) }
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
            }
        case .idle, .done:
            VStack(alignment: .leading, spacing: 12) {
                if let account = auth.status.accountLabel {
                    Text("Signed in as \(account)")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Text("You're joining as a founding cloud member — free while we build out the web experience.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Toggle(isOn: $trainingOptIn) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Contribute to training (optional)")
                        Text("Only scrubbed, masked data — for a subscription discount. You can change this anytime.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .toggleStyle(.checkbox)
            }
            .frame(maxWidth: 360, alignment: .leading)
            HStack {
                Button("Cancel") { cancel() }
                    .keyboardShortcut(.cancelAction)
                Button("Set up cloud") {
                    Task { await auth.completeCloudSetup(trainingOptIn: trainingOptIn) }
                }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
            }
        }
    }

    // MARK: - Settle / cancel

    private func settleIfEntitled() {
        if auth.entitlementStatus.isActive { settle() }
    }

    private func settle() {
        guard !didComplete else { return }
        didComplete = true
        onComplete()
    }

    private func cancel() {
        // Abort an in-flight sign-in and clear any partial setup state so a
        // re-entry starts clean and no partial entitlement is left behind.
        auth.cancelSignIn()
        auth.resetCloudSetup()
        onDismiss()
    }
}
