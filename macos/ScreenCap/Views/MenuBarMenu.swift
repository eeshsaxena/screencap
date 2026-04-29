import AppKit
import SwiftUI

/// Menu bar dropdown. Unit 9 stubs the actions; Unit 13 wires Start/Stop to
/// `RecorderController`. The icon swap (idle vs recording) lives in `ScreenCapApp`.
struct MenuBarMenu: View {
    @EnvironmentObject private var recorder: RecorderController

    var body: some View {
        if recorder.state.isRecording {
            Button("Stop Recording") { recorder.stop() }
                .keyboardShortcut("s", modifiers: [.command, .shift])
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
