import AppKit
import SwiftUI

/// Menu bar dropdown. Start / Stop bind to `RecorderController` (wired in
/// Unit 13); icon swap (idle vs recording) lives in `ScreenCapApp`. While a
/// Cmd+Q-driven shutdown is finalizing, the menu surfaces a countdown line
/// instead of the Stop button so the user sees progress.
struct MenuBarMenu: View {
    @EnvironmentObject private var recorder: RecorderController
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
        }

        Divider()

        Button("Open ScreenCap") { openMainWindow() }

        Divider()

        Button("Quit ScreenCap") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
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
