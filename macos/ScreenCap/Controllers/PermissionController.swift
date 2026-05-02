import AppKit
import AVFoundation
import ApplicationServices
import Combine
import CoreGraphics
import Foundation
import IOKit
import IOKit.hid

/// macOS Privacy & Security panes targeted by the deep-link helpers.
/// macOS 13+ uses the `.extension` URL form; older forms hit a generic page on macOS 26.
///
/// All four panes the recorder cares about — Screen Recording, Accessibility,
/// Input Monitoring (`Privacy_ListenEvent`), and Microphone — are modeled here.
/// Skipping Input Monitoring is a real bug: the spawned `screencap start`
/// re-checks it via `recorder.py:_check_macos_permissions` and the SwiftUI
/// shell would otherwise stall in `.starting` waiting on console prompts that
/// nobody can answer.
enum PrivacyPane: String, CaseIterable {
    case screenRecording
    case accessibility
    case inputMonitoring
    case microphone

    var deepLinkURL: URL {
        let host = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension"
        switch self {
        case .screenRecording:  return URL(string: "\(host)?Privacy_ScreenCapture")!
        case .accessibility:    return URL(string: "\(host)?Privacy_Accessibility")!
        case .inputMonitoring:  return URL(string: "\(host)?Privacy_ListenEvent")!
        case .microphone:       return URL(string: "\(host)?Privacy_Microphone")!
        }
    }

    /// Fallback URL when the .extension form is unavailable on the target macOS.
    /// Same for every pane — the legacy form only lands on the top-level page.
    static let fallbackURL = URL(string: "x-apple.systempreferences:com.apple.preference.security")!

    var displayName: String {
        switch self {
        case .screenRecording:  return "Screen Recording"
        case .accessibility:    return "Accessibility"
        case .inputMonitoring:  return "Input Monitoring"
        case .microphone:       return "Microphone"
        }
    }

    var rationale: String {
        switch self {
        case .screenRecording:  return "Required to capture your screen."
        case .accessibility:    return "Required to associate keystrokes and clicks with the active window."
        case .inputMonitoring:  return "Required to record keyboard and mouse events."
        case .microphone:       return "Optional. Enables audio recording alongside the screen."
        }
    }

    var isRequired: Bool {
        self != .microphone
    }

    /// Maps the `permission` string emitted by `_check_permissions_now`
    /// (recorder.py) — one of `screen_recording` / `accessibility` /
    /// `input_monitoring` — to the matching pane. Microphone is intentionally
    /// not polled (audio loss should not abort a video-only capture). Falls
    /// back to `.screenRecording` for unknown strings so a future Python
    /// rename still opens *something* useful.
    static func from(permissionString perm: String) -> PrivacyPane {
        switch perm.lowercased() {
        case let s where s.contains("screen"): return .screenRecording
        case let s where s.contains("accessibility"): return .accessibility
        case let s where s.contains("input"): return .inputMonitoring
        default: return .screenRecording
        }
    }
}

enum PermissionStatus: Equatable {
    case granted
    case denied
    case notDetermined

    var isGranted: Bool { self == .granted }
}

@MainActor
final class PermissionController: ObservableObject {
    @Published private(set) var screenRecording: PermissionStatus = .notDetermined
    @Published private(set) var accessibility: PermissionStatus = .notDetermined
    @Published private(set) var inputMonitoring: PermissionStatus = .notDetermined
    @Published private(set) var microphone: PermissionStatus = .notDetermined
    /// True between the moment `relaunchApplication()` is invoked and the
    /// process actually exits. Surfaced to the UI so the Quit & Relaunch
    /// button can be disabled, preventing a double-click from stacking
    /// multiple new instances.
    @Published private(set) var isRelaunching: Bool = false

    nonisolated private static let relaunchMaxPollCount = 100
    nonisolated private static let relaunchPollIntervalSeconds = 0.1
    // Give the detached helper's 10 s poll budget one extra second to either
    // open the replacement app or time out before we re-enable the UI.
    nonisolated private static let relaunchWatchdogDelayNanoseconds: UInt64 = 11_000_000_000

    private var pollTimer: Timer?
    private var workspaceObserver: NSObjectProtocol?
    private var relaunchWatchdog: Task<Void, Never>?

    var allRequiredGranted: Bool {
        screenRecording == .granted
            && accessibility == .granted
            && inputMonitoring == .granted
    }

    var anyDenied: Bool {
        screenRecording == .denied
            || accessibility == .denied
            || inputMonitoring == .denied
    }

    init() {
        refresh()
    }

    nonisolated deinit {
        // The class is @MainActor but deinit runs on whichever thread drops
        // the last reference. `MainActor.assumeIsolated` jumps back to the
        // actor for the cleanup so accessing the @Published timer / observer
        // properties stays well-defined under Swift 6's strict concurrency.
        // (Timer.invalidate and NSWorkspace.removeObserver tolerate cross-
        // thread calls in practice, but the compiler is strict for a reason.)
        MainActor.assumeIsolated {
            pollTimer?.invalidate()
            relaunchWatchdog?.cancel()
            if let workspaceObserver {
                NSWorkspace.shared.notificationCenter.removeObserver(workspaceObserver)
            }
        }
    }

    /// Starts the live polling loop used by FirstRunPermissionsView while it is visible.
    func startWatching() {
        stopWatching()
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        pollTimer = timer

        workspaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func stopWatching() {
        pollTimer?.invalidate()
        pollTimer = nil
        if let workspaceObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(workspaceObserver)
            self.workspaceObserver = nil
        }
    }

    /// Recomputes all four permission statuses without prompting.
    ///
    /// These APIs cache their result at process launch — a grant made after
    /// launch does NOT show up here until the next process start. The walkthrough
    /// surfaces a "Quit & Relaunch" button to drive the restart explicitly.
    func refresh() {
        screenRecording = Self.checkScreenRecording()
        accessibility = Self.checkAccessibility()
        inputMonitoring = Self.checkInputMonitoring()
        microphone = Self.checkMicrophone()
    }

    /// Quits the current process and spawns a fresh ScreenCap process so the
    /// new instance reads the latest TCC state at launch. Order matters:
    /// terminate first, then relaunch after the parent has actually exited.
    /// If we relaunched first and terminated from a callback, an unsigned dev
    /// build can stack multiple instances when the terminate callback lags.
    ///
    /// We schedule the relaunch via `/bin/sh` and pass the executable path as
    /// a positional argument so spaces / metacharacters bypass the shell
    /// parser entirely. The helper polls our PID until it's gone (with a 10 s
    /// safety cap) instead of guessing a fixed sleep — under Xcode's LLDB
    /// attachment, `NSApp.terminate(nil)` can take noticeably longer than
    /// 0.6 s to actually exit the process. If that cap fires we abort the
    /// relaunch instead of forcing a new instance against a still-live parent,
    /// because stacking a second instance is worse than leaving the current
    /// one up and asking the user to reopen manually.
    ///
    /// The relaunch uses the app binary directly, not `open -n`. That keeps
    /// the current process environment intact for the fresh instance, which is
    /// critical for dev runs where PATH / SCREENCAP_DEV_REPO_ROOT / pyenv
    /// shims are only present because the app was launched from Xcode or a
    /// shell wrapper.
    func relaunchApplication() {
        guard !isRelaunching else { return }
        isRelaunching = true
        guard let executablePath = Bundle.main.executableURL?.path else {
            isRelaunching = false
            let alert = NSAlert()
            alert.messageText = "Couldn't relaunch ScreenCap"
            alert.informativeText = "Couldn't determine the app executable path. Quit and reopen ScreenCap manually to apply granted permissions."
            alert.alertStyle = .warning
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }
        let parentPid = ProcessInfo.processInfo.processIdentifier
        let detach = Process()
        detach.executableURL = URL(fileURLWithPath: "/bin/sh")
        // Preserve the current dev environment so the fresh app instance sees
        // the same PATH / SCREENCAP_DEV_REPO_ROOT / pyenv shims after relaunch.
        detach.environment = ProcessInfo.processInfo.environment
        detach.arguments = [
            "-c",
            Self.relaunchHelperShellScript(),
            "screencap-relaunch",     // $0
            String(parentPid),        // $1 — current process's PID
            executablePath,           // $2 — passed unescaped through argv
        ]
        do {
            try detach.run()
        } catch {
            // If the relaunch helper can't even start, don't terminate —
            // surface the failure so the user isn't left with a vanished app.
            isRelaunching = false
            let alert = NSAlert()
            alert.messageText = "Couldn't relaunch ScreenCap"
            alert.informativeText = "Quit and reopen the app manually to apply granted permissions.\n\n\(error.localizedDescription)"
            alert.alertStyle = .warning
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }
        armRelaunchWatchdog()
        NSApp.terminate(nil)
    }

    /// Triggers the system permission prompt for `pane` and then opens the
    /// matching Privacy & Security pane. The request API is what registers
    /// the app in the TCC database — without it, the app won't appear in the
    /// pane's app list, so the user can't toggle anything on. After the user
    /// returns from System Settings the 1Hz poll picks up the new state.
    ///
    /// Microphone uses an async callback; we don't block on it because the
    /// pane should open immediately either way.
    func requestAndOpenSettings(for pane: PrivacyPane) {
        switch pane {
        case .screenRecording:
            // Triggers the "<App> would like to record this computer's screen"
            // prompt the first time. No-op once the user has answered.
            _ = CGRequestScreenCaptureAccess()
        case .accessibility:
            let options: NSDictionary = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
            _ = AXIsProcessTrustedWithOptions(options)
        case .inputMonitoring:
            // IOHIDRequestAccess shows the prompt and registers the app in
            // the Input Monitoring list. Returns true if granted.
            _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
        case .microphone:
            AVCaptureDevice.requestAccess(for: .audio) { _ in
                Task { @MainActor in self.refresh() }
            }
        }
        openSystemSettings(for: pane)
    }

    /// Opens System Settings to the requested pane. Tries the macOS 13+ .extension
    /// URL first; falls back to the generic Privacy & Security page if unavailable.
    func openSystemSettings(for pane: PrivacyPane) {
        if !NSWorkspace.shared.open(pane.deepLinkURL) {
            NSWorkspace.shared.open(PrivacyPane.fallbackURL)
        }
    }

    // MARK: - Silent in-process checks
    //
    // These cache at process start. The walkthrough's `Quit & Relaunch`
    // button is what gets the user to a fresh process when they've granted
    // a permission post-launch.

    private static func checkScreenRecording() -> PermissionStatus {
        CGPreflightScreenCaptureAccess() ? .granted : .denied
    }

    private static func checkAccessibility() -> PermissionStatus {
        let options: NSDictionary = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: false]
        return AXIsProcessTrustedWithOptions(options) ? .granted : .denied
    }

    private static func checkInputMonitoring() -> PermissionStatus {
        // IOHIDAccessType is imported as Int32, not a Swift enum, so the
        // switch needs a non-`@unknown` default to satisfy exhaustiveness.
        switch IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) {
        case kIOHIDAccessTypeGranted: return .granted
        case kIOHIDAccessTypeDenied:  return .denied
        case kIOHIDAccessTypeUnknown: return .notDetermined
        default:                      return .notDetermined
        }
    }

    private static func checkMicrophone() -> PermissionStatus {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:        return .granted
        case .denied, .restricted: return .denied
        case .notDetermined:     return .notDetermined
        @unknown default:        return .notDetermined
        }
    }

    nonisolated static func relaunchHelperShellScript(
        maxPollCount: Int = relaunchMaxPollCount,
        pollIntervalSeconds: Double = relaunchPollIntervalSeconds
    ) -> String {
        "i=0; while kill -0 \"$1\" 2>/dev/null; do i=$((i+1)); [ $i -ge \(maxPollCount) ] && exit 0; sleep \(pollIntervalSeconds); done; exec \"$2\""
    }

    private func armRelaunchWatchdog() {
        relaunchWatchdog?.cancel()
        relaunchWatchdog = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: Self.relaunchWatchdogDelayNanoseconds)
            guard let self, self.isRelaunching else { return }

            isRelaunching = false

            let alert = NSAlert()
            alert.messageText = "ScreenCap is still running"
            alert.informativeText =
                "A new instance was not opened because the current app never finished quitting. Quit and reopen ScreenCap manually to apply granted permissions."
            alert.alertStyle = .warning
            alert.addButton(withTitle: "OK")
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
    }
}
