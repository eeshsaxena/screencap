import AppKit
import SwiftUI

/// Menu bar dropdown. Start / Stop bind to `RecorderController` (wired in
/// Unit 13); icon swap (idle vs recording) lives in `ScreenCapApp`. While a
/// Cmd+Q-driven shutdown is finalizing, the menu surfaces a countdown line
/// instead of the Stop button so the user sees progress.
struct MenuBarMenu: View {
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var auth: CloudAuthController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if let remaining = recorder.quitProgressSecondsRemaining {
            Text("Finalizing recording — \(remaining)s remaining")
            Divider()
        } else if case .recording = recorder.state {
            Button("Stop Recording") { recorder.stop() }
                .keyboardShortcut("s", modifiers: [.command, .shift])
        } else if recorder.state.isRecording {
            // .starting or .stopping — surface progress, don't offer an action
            // that would re-enter the state machine.
            Text(recorder.state.isStopping ? "Stopping…" : "Starting…")
        } else {
            Button("Start Recording") { recorder.start() }
                .keyboardShortcut("r", modifiers: [.command, .shift])
        }

        if let err = recorder.lastError {
            Divider()
            RecorderErrorMessage(message: err)
        } else if let advisory = recorder.captureAdvisory {
            // Advisory, non-terminal capture-health hint (SCR-76). Errors take
            // priority (else-if, matching MainWindow); the recording continues.
            Divider()
            RecorderErrorMessage(message: advisory)
        }

        Divider()

        // Cloud account (plan U6). The one consistent place to see sign-in
        // state and sign in / out. Local recording is never gated on this.
        accountSection

        Divider()

        Button("Open ScreenCap") { openMainWindow() }

        Divider()

        Button("Quit ScreenCap") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    /// Account status + Sign In / Sign Out. The in-progress and failed sign-in
    /// flow states take priority over the persistent status so the user always
    /// sees what the browser round-trip is doing (design-review states a/b).
    @ViewBuilder
    private var accountSection: some View {
        switch auth.signInFlow {
        case .inProgress:
            Text("Signing in… check your browser")
            Button("Cancel Sign-In") { auth.cancelSignIn() }
        case .failed(let reason):
            Text("Sign-in failed: \(reason)")
            Button("Sign In…") { auth.startSignIn() }
        case .idle:
            switch auth.status {
            case .signedIn:
                Text(auth.status.accountLabel.map { "Signed in: \($0)" } ?? "Signed in (offline)")
                Button("Sign Out") { Task { await auth.signOut() } }
                    // Disabled mid-upload so an in-flight signed-URL request
                    // can't hit NotSignedIn (design-review state c).
                    .disabled(!auth.canSignOut)
            case .signedOut:
                Button("Sign In…") { auth.startSignIn() }
            case .unknown:
                Text("Checking sign-in…")
            }
        }
    }

    private func openMainWindow() {
        // Activation policy must flip back to .regular before activating —
        // the close observer in AppDelegate sets .accessory when the last
        // titled window goes away, and openWindow(id:) alone won't bring
        // the app to the foreground.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        // Fast path: a titled main window already exists. Bring it forward
        // instead of asking SwiftUI to instantiate a duplicate — calling
        // openWindow(id:) from MenuBarExtra creates a second WindowGroup
        // instance even when one is already visible (see SCR-55 QA). The
        // filter mirrors AppDelegate.applicationShouldHandleReopen and adds
        // sheetParent == nil so we focus the parent, not an attached sheet
        // (e.g. the first-run permissions sheet).
        for window in NSApp.windows
        where window.contentViewController != nil
            && !(window is NSPanel)
            && window.styleMask.contains(.titled)
            && window.sheetParent == nil {
            window.makeKeyAndOrderFront(nil)
            return
        }
        openWindow(id: MainWindowID)
    }
}
