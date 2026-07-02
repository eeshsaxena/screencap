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
        DaemonInstallController.registerDaemonOnFirstLaunchIfNeeded()
        // After an app update the old daemon process keeps serving the previous
        // on-disk bundle; its lazy imports then break (HTTP 500 "Couldn't load
        // recordings"). Restart it if it predates the freshly installed bundle —
        // the SCR-121 version gate can't see this because the daemon version
        // string is unchanged across an app update.
        Task { await DaemonInstallController.restartStaleDaemonIfNeeded() }
        // Switch to .accessory whenever the main window closes; keep the menu
        // bar item alive so the user can reopen the app from there.
        windowCloseObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification,
            object: nil,
            queue: .main
        ) { notification in
            // Filter out transient windows: NSAlert uses NSPanel, SwiftUI
            // sheets attach via `sheetParent`, and HUD/utility windows lack
            // `.titled`. Without this guard an alert dismissal or a sheet
            // close could flip the app to `.accessory` while the user is
            // still actively using the main window.
            guard let window = notification.object as? NSWindow,
                  !(window is NSPanel),
                  window.styleMask.contains(.titled),
                  window.sheetParent == nil
            else { return }
            // Defer past the current run-loop cycle so AppKit has removed the
            // closing window from `NSApp.windows`. `DispatchQueue.main.async`
            // is guaranteed to run after the synchronous notification dispatch
            // returns to the run loop; an unstructured `Task { @MainActor }` is
            // not (Apple gives no FIFO guarantee relative to AppKit's internal
            // bookkeeping).
            DispatchQueue.main.async {
                let visible = NSApp.windows.contains {
                    $0.isVisible
                        && $0.contentViewController != nil
                        && !($0 is NSPanel)
                        && $0.styleMask.contains(.titled)
                }
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
            // Fast path: a real titled main window survived. Bring it forward
            // instead of asking SwiftUI to instantiate a new one.
            for window in NSApp.windows
            where window.contentViewController != nil
                && !(window is NSPanel)
                && window.styleMask.contains(.titled) {
                window.makeKeyAndOrderFront(nil)
                return true
            }
            // WindowGroup has torn down its window — ask SwiftUI to materialize
            // a fresh one via the OpenWindowBridge captured during the last
            // window's lifecycle. Safe no-op if the bridge has never armed.
            WindowOpener.shared.openMain?()
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
