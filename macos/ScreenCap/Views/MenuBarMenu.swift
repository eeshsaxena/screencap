import AppKit
import SwiftUI

/// Menu bar dropdown. Unit 9 stubs the actions; Unit 13 wires Start/Stop to
/// `RecorderController`. The icon swap (idle vs recording) lives in `ScreenCapApp`.
struct MenuBarMenu: View {
    @EnvironmentObject private var recorder: RecorderController

    var body: some View {
        // Recording controls are stubbed in PR1 — Unit 13 wires them up
        // (and re-attaches Cmd+Shift+R / Cmd+Shift+S). We render them
        // disabled so the menu doesn't present a broken action, but keep
        // the keyboard shortcuts off until they actually do something.
        if recorder.state.isRecording {
            Button("Stop Recording") { recorder.stop() }
                .disabled(true)
        } else {
            Button("Start Recording") { recorder.start() }
                .disabled(true)
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
