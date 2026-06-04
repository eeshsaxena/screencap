import SwiftUI

/// "Sign in to upload" prompt (plan U6). Presented from the review window's
/// Upload affordance when the user is signed out, so an upload attempt surfaces
/// a sign-in step instead of an opaque CLI refusal.
///
/// Drives off `CloudAuthController.signInFlow` for the three design-review
/// states: idle (the initial prompt), in-progress (a non-blocking
/// "Waiting for sign-in in your browser…" with Cancel), and failed (the reason
/// plus a Try Again). The browser round-trip runs in Python via a cancellable
/// shell-out — this view never blocks the main thread waiting on it.
struct SignInPromptView: View {
    @ObservedObject var auth: CloudAuthController
    /// Called once sign-in succeeds — the review window dismisses the sheet and
    /// proceeds to the upload it was gating.
    let onSignedIn: () -> Void
    /// Called when the user dismisses the prompt (Cancel/Close); aborts any
    /// in-flight login.
    let onDismiss: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "icloud.and.arrow.up")
                .font(.largeTitle)
                .foregroundStyle(.secondary)
            Text("Sign in to upload")
                .font(.headline)
            Text("Uploading to the cloud requires a ScreenCap account. Local recording and playback never need sign-in.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)

            actions
        }
        .padding(24)
        .frame(minWidth: 380)
    }

    @ViewBuilder
    private var actions: some View {
        switch auth.signInFlow {
        case .inProgress:
            ProgressView("Waiting for sign-in in your browser…")
                .controlSize(.small)
            // Route through onDismiss so Cancel both aborts the login and closes
            // the sheet — calling cancelSignIn() alone would reset to .idle and
            // leave the sheet open on the initial prompt.
            Button("Cancel") { onDismiss() }
                .keyboardShortcut(.cancelAction)
        case .failed(let reason):
            Text(reason)
                .font(.caption)
                .foregroundStyle(.red)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
            HStack {
                Button("Cancel") { onDismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Try Again") { beginSignIn() }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        case .idle:
            HStack {
                Button("Cancel") { onDismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Sign In…") { beginSignIn() }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        }
    }

    private func beginSignIn() {
        auth.startSignIn { success in
            if success { onSignedIn() }
        }
    }
}
