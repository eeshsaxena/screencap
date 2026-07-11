import AppKit
import SwiftUI

extension Notification.Name {
    /// U10 — posted by the menu-bar "Search…" item after focusing/opening the
    /// main window; MainWindow observes it and opens the Recall palette
    /// (KTD-13: the ⌘⇧F shortcut itself stays window-scoped, no global tap).
    static let screenCapOpenRecallPalette = Notification.Name("com.screencap.recallPalette.open")

    /// U12 / account-sheet U5 — posted by the gated record/search affordances
    /// (menu-bar gated Start, New-recording sheet, Recall palette) after
    /// focusing/opening the main window; MainWindow observes it and presents
    /// the shared Account & Plan sheet in `.gate` context. Same pattern as the
    /// Search item, so every gated affordance surfaces the same sheet.
    /// (Replaces the retired `.screenCapOpenUpgradePrompt` /
    /// `UpgradePromptView` round-trip.)
    static let screenCapOpenAccountGate = Notification.Name("com.screencap.accountSheet.gate")

    /// Account-sheet U5 (KTD-4) — posted by the menu-bar "Account…" item after
    /// focusing/opening the main window; MainWindow observes it and selects the
    /// `.account` sidebar route (the embedded Account & Plan pane — no
    /// menu-bar sheet).
    static let screenCapOpenAccountPane = Notification.Name("com.screencap.accountPane.open")
}

/// Menu bar dropdown — Start Recording, Stop, account, Open ScreenCap,
/// Search… (U14 parity with the in-window surfaces). Icon swap (idle vs
/// recording) lives in `ScreenCapApp`. While a Cmd+Q-driven shutdown is
/// finalizing, the menu surfaces a countdown line instead of the Stop button
/// so the user sees progress.
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
            // Restore the HUD pill after the user hid it from the pill itself.
            // Gated so it only shows while the pill is actually hidden.
            if MenuBarMenuPolicy.showRecordingControlsVisible(
                state: recorder.state, hudHidden: recorder.hudHidden
            ) {
                Button("Show recording controls") { recorder.showRecordingHUD() }
            }
        } else if recorder.state.isRecording {
            // .starting or .stopping — surface progress, don't offer an action
            // that would re-enter the state machine.
            Text(recorder.state.isStopping ? "Stopping…" : "Starting…")
        } else if auth.isGatedForLapse {
            // U12: a lapsed / not-entitled user. The Start control gates to an
            // upgrade prompt rather than a silent no-op (or a start that the
            // daemon would 402). Opens the main window and surfaces the same
            // upgrade sheet as the in-window affordances. The lock glyph + label
            // state the subscription requirement (VoiceOver reads the title).
            Button {
                openMainWindow()
                NotificationCenter.default.post(name: .screenCapOpenAccountGate, object: nil)
            } label: {
                Label("Start Recording — Subscription Required", systemImage: "lock.fill")
            }
            .keyboardShortcut("r", modifiers: [.command, .shift])
        } else {
            // U14: starts with the New-recording sheet's last-used options —
            // `audio: nil` defers to the persisted `audio_default`, which is
            // exactly the sheet's initial mic state (NewRecordingSheetPolicy
            // .initialAudioOn), so the two Start paths cannot drift.
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

        // Cloud account (plan U6, collapsed by account-sheet U5): a status
        // line plus the "Account…" entry into the shared Account & Plan pane.
        // Local recording is never gated on this.
        //
        // Sign-in state is resolved lazily on first menu open (not at app
        // launch): `refreshIfNeeded` decrypts the Keychain only when this
        // account surface actually appears, so the macOS "screencap-auth"
        // authorization prompt can't fire the instant the app opens. It
        // coalesces + no-ops once resolved, so opening the menu repeatedly
        // re-decrypts nothing. See CloudAuthController.refreshIfNeeded / SCR-241.
        accountSection
            .onAppear { Task { await auth.refreshIfNeeded() } }

        Divider()

        Button("Open ScreenCap") { openMainWindow() }

        Button("Search…") {
            // U10: open (or focus) the main window with the palette pre-opened.
            openMainWindow()
            NotificationCenter.default.post(name: .screenCapOpenRecallPalette, object: nil)
        }
        .keyboardShortcut("f", modifiers: [.command, .shift])

        Divider()

        Button("Quit ScreenCap") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    /// Collapsed account section (account-sheet U5): one status line + the
    /// "Account…" item that opens the main window's Account & Plan pane
    /// (KTD-4 — the `.account` route, no menu-bar sheet). Sign In / Sign Out
    /// live in the shared sheet/pane now, so the menu never runs auth flows
    /// itself; the status line still surfaces an in-flight browser round-trip
    /// so the user sees what's happening. The line copy is policy-derived
    /// (`MenuBarMenuPolicy.accountStatusLine`) so it stays unit-testable.
    @ViewBuilder
    private var accountSection: some View {
        Text(MenuBarMenuPolicy.accountStatusLine(
            status: auth.status, signInFlow: auth.signInFlow
        ))
        Button(MenuBarMenuPolicy.accountItemTitle) {
            openMainWindow()
            NotificationCenter.default.post(name: .screenCapOpenAccountPane, object: nil)
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
