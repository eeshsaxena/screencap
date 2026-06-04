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
    /// Called when this sheet actually launches a `login` (Sign In / Try Again),
    /// so the presenting window can record that IT owns the in-flight flow and
    /// is the one allowed to cancel it on close. Optional — surfaces that don't
    /// track ownership (or previews) can omit it.
    var onStartSignIn: () -> Void = {}

    /// One-shot latch so the signed-in settle (`onChange`) calls `onSignedIn`
    /// exactly once even if `isSignedIn` republishes — prevents a double upload.
    @State private var didSignIn = false

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "icloud.and.arrow.up")
                .font(.largeTitle)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
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
        // One app-wide controller backs every window/menu, so sign-in can
        // complete on another surface (or this one) while this sheet is open.
        // `status` flipping signed-in is the single settle trigger: it fires for
        // this window's own login AND for a sign-in that completed on another
        // surface first, so the sheet never strands on a now-stale prompt. Routed
        // through one flag so the two callers can't double-fire `onSignedIn`
        // (which would double-start the gated upload). `startSignIn`
        // short-circuits to `true` when already signed in, so an already-signed-in
        // tap never relaunches the browser.
        .onChange(of: auth.isSignedIn) { signedIn in
            if signedIn { settleSignedIn() }
        }
        // Covers the narrow race where the controller is already signed-in by
        // the time this sheet appears (another surface won first, so `onChange`
        // sees no transition to react to).
        .onAppear {
            if auth.isSignedIn { settleSignedIn() }
        }
    }

    /// Fire `onSignedIn` exactly once across all settle paths (onChange / onAppear
    /// / a completion), so the gated upload starts a single time.
    private func settleSignedIn() {
        guard !didSignIn else { return }
        didSignIn = true
        onSignedIn()
    }

    @ViewBuilder
    private var actions: some View {
        // This switch over `SignInFlowState` parallels `MenuBarMenu.accountSection`,
        // but renders different controls (a sheet with ProgressView + shortcut
        // buttons vs. menu items), so the two intentionally stay separate. Keep
        // the case coverage in sync when `SignInFlowState` changes.
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
        onStartSignIn()
        // The success settle is driven by `.onChange(of: auth.isSignedIn)` (the
        // single trigger, so a sign-in completed elsewhere also dismisses this
        // sheet). The completion is intentionally empty here — acting on it too
        // would call `onSignedIn` twice and double-start the gated upload. A
        // failure stays on screen via `signInFlow == .failed` for the re-prompt.
        auth.startSignIn { _ in }
    }
}
