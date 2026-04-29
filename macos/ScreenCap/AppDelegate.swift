import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private weak var recorder: RecorderController?
    private var windowCloseObserver: NSObjectProtocol?

    func bind(recorder: RecorderController) {
        self.recorder = recorder
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        // Switch to .accessory whenever the main window closes; keep the menu
        // bar item alive so the user can reopen the app from there.
        windowCloseObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification,
            object: nil,
            queue: .main
        ) { _ in
            Task { @MainActor in
                // Defer one tick so SwiftUI has updated `NSApp.windows`.
                let visible = NSApp.windows.contains { $0.isVisible && $0.contentViewController != nil }
                if !visible {
                    NSApp.setActivationPolicy(.accessory)
                }
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    /// Re-show the main window when the user clicks the dock icon, the menu
    /// bar's "Open ScreenCap", or relaunches the .app while it's idle in
    /// .accessory mode.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        if !hasVisibleWindows {
            for window in NSApp.windows where window.contentViewController != nil {
                window.makeKeyAndOrderFront(nil)
                return true
            }
        }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let recorder, recorder.state.isRecording else {
            return .terminateNow
        }
        return recorder.confirmQuitWhileRecording()
    }

    deinit {
        if let windowCloseObserver {
            NotificationCenter.default.removeObserver(windowCloseObserver)
        }
    }
}
