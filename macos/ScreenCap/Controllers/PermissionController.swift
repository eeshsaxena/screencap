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

    private var pollTimer: Timer?
    private var workspaceObserver: NSObjectProtocol?

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

    /// Quits the current process and spawns a fresh ScreenCap.app via
    /// LaunchServices so the new instance reads the latest TCC state at
    /// launch. Order matters: terminate first, then `open` after a short
    /// delay. If we did `openApplication` first and `terminate` from its
    /// callback, an unsigned dev build can stack multiple instances when
    /// LaunchServices delays the terminate callback.
    ///
    /// We schedule the relaunch via a detached `/usr/bin/open --args` invocation
    /// rather than `sh -c`, so a bundle path containing spaces or shell
    /// metacharacters is passed verbatim through arguments rather than the
    /// shell parser. `open --wait-apps` parks the helper for ~600ms via a
    /// short `sleep` chain — but using `Process.run()` with arguments avoids
    /// any shell entirely.
    func relaunchApplication() {
        guard !isRelaunching else { return }
        isRelaunching = true
        let bundlePath = Bundle.main.bundlePath
        // Two staged Processes: a `sleep 0.6` to give the kernel time to reap
        // this process, then `open -n <bundle>`. We run them via a single
        // /bin/sh invocation but pass the bundle path as a positional
        // argument so $0 / "$1" carry it without shell-escape concerns.
        let detach = Process()
        detach.executableURL = URL(fileURLWithPath: "/bin/sh")
        detach.arguments = [
            "-c",
            "sleep 0.6 && exec /usr/bin/open -n \"$1\"",
            "screencap-relaunch", // $0
            bundlePath,           // $1 — passed unescaped through argv
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
}
