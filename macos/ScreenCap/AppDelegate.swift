import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private weak var recorder: RecorderController?

    func bind(recorder: RecorderController) {
        self.recorder = recorder
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
    }

    func applicationDidResignActive(_ notification: Notification) {
        if NSApp.windows.allSatisfy({ !$0.isVisible || $0.isMiniaturized }) {
            NSApp.setActivationPolicy(.accessory)
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let recorder, recorder.state.isRecording else {
            return .terminateNow
        }
        return recorder.confirmQuitWhileRecording()
    }
}
