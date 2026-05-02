import AppKit
import SwiftUI

/// Menu bar dropdown. Start / Stop bind to `RecorderController` (wired in
/// Unit 13); icon swap (idle vs recording) lives in `ScreenCapApp`. While a
/// Cmd+Q-driven shutdown is finalizing, the menu surfaces a countdown line
/// instead of the Stop button so the user sees progress.
struct MenuBarMenu: View {
    @EnvironmentObject private var recorder: RecorderController

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

        Divider()

        Button("Open ScreenCap") { openMainWindow() }

        Divider()

        Button("Quit ScreenCap") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private func openMainWindow() {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        for window in NSApp.windows where window.title == "ScreenCap" {
            window.makeKeyAndOrderFront(nil)
            return
        }
        // No matching window — bring whatever exists to the front.
        if let window = NSApp.windows.first {
            window.makeKeyAndOrderFront(nil)
        }
    }
}
