import AppKit
import Foundation

/// Periodic + activation-triggered TCC re-check used during CLI-fallback
/// recordings. The orchestrator owns the "should I check?" gating
/// (`transport == .cliFallback` and `state == .recording`); this collaborator
/// only owns the scheduling primitives.
@MainActor
protocol PermissionWatchdog {
    /// Begin invoking `check` on a recurring timer and whenever any
    /// application activates. Calling `start` again replaces the previous
    /// schedule.
    func start(check: @escaping @MainActor () -> Void)

    /// Tear down the timer and detach the activation observer.
    func stop()
}

/// Live implementation backed by `Timer` (`.common` mode) and an
/// `NSWorkspace.didActivateApplicationNotification` observer.
@MainActor
final class LivePermissionWatchdog: PermissionWatchdog {
    private let interval: TimeInterval
    private var timer: Timer?
    private var observer: NSObjectProtocol?

    init(interval: TimeInterval = 5.0) {
        self.interval = interval
    }

    deinit {
        timer?.invalidate()
        if let observer {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
        }
    }

    func start(check: @escaping @MainActor () -> Void) {
        // Re-calling start while running invalidates the previous timer and
        // observer to prevent leaks; the new schedule fully replaces them.
        stop()
        // `.common` mode for the same reason as the elapsed timer: a menu bar
        // dropdown or NSAlert must not pause permission revocation detection.
        let timer = Timer(timeInterval: interval, repeats: true) { _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { check() } }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
        // `DispatchQueue.main.async` rather than `Task { @MainActor }` to keep
        // ordering FIFO with the stderr/termination dispatches in CLIClient —
        // Tasks don't preserve order against GCD blocks.
        observer = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { check() } }
        }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        if let observer {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
            self.observer = nil
        }
    }
}
