import AppKit

// U7 — the window-lifecycle seam the recording state machine drives through
// `RecorderController`. The live implementation manages the floating HUD panel
// and hides / restores the main window; the no-op implementation lets unit tests
// exercise the state transitions without touching AppKit. The default factory
// resolves to the no-op under XCTest and the live one in the app, so neither the
// app scene nor the test suites need to inject it explicitly.

@MainActor
protocol WindowLifecycle: AnyObject {
    /// Show the floating recording HUD, bound to `recorder` for live elapsed /
    /// title / audio.
    func showHUD(for recorder: RecorderController)
    /// Close the recording HUD.
    func hideHUD()
    /// Hide (orderOut) the main window while recording.
    func hideMainWindow()
    /// Bring the main window back after a recording ends.
    func restoreMainWindow()
    /// Present the one-time first-hide menu-bar hint (U5). `onComplete` fires once
    /// after the hint has actually been shown (dismissed or timed out), so the
    /// caller marks the "shown" flag only when the user had a chance to see it.
    func presentHideHint(onComplete: @escaping () -> Void)
}

enum WindowLifecycleFactory {
    /// No-op under the XCTest host (mirrors `ScreenCapApp.isRunningUnderTests`) so
    /// controller tests that drive `started` / `stopped` transitions never spawn a
    /// real panel or orderOut the test host's windows; live otherwise.
    /// `@MainActor` because it constructs main-actor-isolated implementations; it
    /// is only ever evaluated as a default arg of `RecorderController.init`, which
    /// is itself `@MainActor`.
    @MainActor
    static func makeDefault() -> WindowLifecycle {
        if ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil {
            return NoopWindowLifecycle()
        }
        return LiveWindowLifecycle()
    }
}

/// Live AppKit implementation.
@MainActor
final class LiveWindowLifecycle: WindowLifecycle {
    private let hud = RecordingHUDPanelController()
    private let hintPanel = HUDHintPanelController()

    func showHUD(for recorder: RecorderController) {
        hud.show(recorder: recorder)
    }

    func hideHUD() {
        hud.hide()
    }

    func presentHideHint(onComplete: @escaping () -> Void) {
        hintPanel.present(onComplete: onComplete)
    }

    func hideMainWindow() {
        mainWindow()?.orderOut(nil)
    }

    func restoreMainWindow() {
        // The main-window close observer may have flipped the app to .accessory;
        // return to .regular before bringing the window forward (mirrors
        // MenuBarMenu.openMainWindow / applicationShouldHandleReopen).
        NSApp.setActivationPolicy(.regular)
        if let window = mainWindow() {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        } else {
            // SwiftUI tore the window down — ask it to materialize a fresh one.
            WindowOpener.shared.openMain?()
        }
    }

    /// The app's titled main window (not a panel, not an attached sheet) — the same
    /// filter `AppDelegate.applicationShouldHandleReopen` and `MenuBarMenu` use.
    private func mainWindow() -> NSWindow? {
        NSApp.windows.first { window in
            window.contentViewController != nil
                && !(window is NSPanel)
                && window.styleMask.contains(.titled)
                && window.sheetParent == nil
        }
    }
}

/// No-op implementation for tests.
@MainActor
final class NoopWindowLifecycle: WindowLifecycle {
    func showHUD(for recorder: RecorderController) {}
    func hideHUD() {}
    func hideMainWindow() {}
    func restoreMainWindow() {}
    func presentHideHint(onComplete: @escaping () -> Void) {}
}
