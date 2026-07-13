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
        // Ignore SIGPIPE process-wide. `CLIClient.runOneShot` writes a BYO-key
        // secret to the child's stdin on a background queue; if the child has
        // already closed its read end (early exit / crash), that write would
        // otherwise raise SIGPIPE and terminate the whole app. Handling the
        // write error at the call site isn't enough — the signal fires first.
        signal(SIGPIPE, SIG_IGN)

        // The design system is light-only (U1: the warm palette is authored as
        // universal color sets with no dark variant), so pin the Aqua
        // appearance app-wide — otherwise native chrome (title bars, menus,
        // sheets, alerts) renders dark against the fixed warm-cream content
        // on Dark-mode machines (U14 fidelity pass). Unpin if the design ever
        // gains a dark variant.
        NSApp.appearance = NSAppearance(named: .aqua)
        NSApp.setActivationPolicy(.regular)
        DaemonInstallController.registerDaemonOnFirstLaunchIfNeeded()
        // The launch-time stale-daemon restart (post-update helper swap) now
        // runs inside the window's launch task via
        // `RecorderController.runLaunchDaemonCheck()` — sequenced BEFORE the
        // first daemon probe, so the probe can't adopt a daemon the kickstart
        // is about to kill and the SCR-262 convergence interstitial can key on
        // the restart decision.
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
