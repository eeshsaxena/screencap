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

#if DEBUG
extension PermissionController {
    func _testSetRequiredPermissionsGranted(_ granted: Bool) {
        let status: PermissionStatus = granted ? .granted : .denied
        screenRecording = status
        accessibility = status
        inputMonitoring = status
    }
}
#endif

enum PermissionStatus: Equatable {
    case granted
    case denied
    case notDetermined

    var isGranted: Bool { self == .granted }
}

enum PermissionSubject: Equatable {
    case screenCapApp
    case daemon

    var bundleIdentifier: String {
        switch self {
        case .screenCapApp: return "com.screencap.macos"
        case .daemon: return "com.screencap.daemon"
        }
    }
}

@MainActor
final class PermissionController: ObservableObject {
    @Published private(set) var screenRecording: PermissionStatus = .notDetermined
    @Published private(set) var accessibility: PermissionStatus = .notDetermined
    @Published private(set) var inputMonitoring: PermissionStatus = .notDetermined
    @Published private(set) var microphone: PermissionStatus = .notDetermined
    /// The *daemon's* live TCC grant state (the TCC subject), as reported over
    /// `daemon.info` (U2/U3). Kept distinct from the four app-process statuses
    /// above because they answer a different question — app identity vs. daemon
    /// identity. Onboarding (U4/U5) and the daemon start-block gate on this, not
    /// on the app-process state. Written only by `RecorderController.probeDaemon`
    /// (the single wire reader); defaults to all-indeterminate until the first
    /// probe lands or when the daemon is unreachable.
    @Published private(set) var daemonGrants: DaemonPermissionGrants = .allIndeterminate
    /// True between the moment `relaunchApplication()` is invoked and the
    /// process actually exits. Surfaced to the UI so the Quit & Relaunch
    /// button can be disabled, preventing a double-click from stacking
    /// multiple new instances.
    @Published private(set) var isRelaunching: Bool = false
    /// Persisted "permission setup dismissed" flag (U4). Once the user taps
    /// "Skip for now" with a grant still missing, the state-driven walkthrough
    /// gate stops re-popping on every launch. The **start-block is independent**
    /// of this flag (R4) — a dismissed sheet never lets a broken recording
    /// start silently. Auto-cleared when all required daemon grants land (see
    /// `updateDaemonGrants`) so a later loss (recovery / F2) re-arms the sheet.
    @Published private(set) var setupDismissed: Bool

    private let defaults: UserDefaults
    private static let setupDismissedDefaultsKey = "com.screencap.macos.permissionSetupDismissed"

    nonisolated private static let relaunchMaxPollCount = 100
    nonisolated private static let relaunchPollIntervalSeconds = 0.1
    // Give the detached helper's 10 s poll budget one extra second to either
    // open the replacement app or time out before we re-enable the UI.
    nonisolated private static let relaunchWatchdogDelayNanoseconds: UInt64 = 11_000_000_000

    private var pollTimer: Timer?
    private var workspaceObserver: NSObjectProtocol?
    private var relaunchWatchdog: Task<Void, Never>?
    // U5: separate, slower (~5s) daemon-grant refresh lifecycle. Distinct from
    // the 1Hz app-process `startWatching` poll because each daemon refresh costs
    // a daemon-side subprocess probe (macos-foundation-process-pipe-pitfalls.md).
    private var daemonGrantTimer: Timer?
    private var daemonGrantWorkspaceObserver: NSObjectProtocol?
    private var daemonGrantRefreshInFlight = false
    // Bumped on stop so an in-flight refresh that completes after the watch was
    // torn down (or restarted) cannot clear the in-flight guard belonging to a
    // newer watch session.
    private var daemonGrantRefreshGeneration = 0
    private static let daemonGrantRefreshInterval: TimeInterval = 5.0

    /// True while the daemon-grant refresh lifecycle is active (sheet visible).
    var isDaemonGrantWatching: Bool { daemonGrantTimer != nil }

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

    /// All three required *daemon* grants are confirmed granted (microphone
    /// excluded). Drives the walkthrough's "all done" / auto-close path (U5).
    var allRequiredDaemonGrantsGranted: Bool {
        daemonGrants.allRequiredGranted
    }

    /// Replace the daemon grant snapshot. Called by `RecorderController` after a
    /// `daemon.info` probe (the daemon's grants) or with `.allIndeterminate`
    /// when the daemon is unreachable — indeterminate never blocks or nags.
    func updateDaemonGrants(_ grants: DaemonPermissionGrants) {
        daemonGrants = grants
        // Re-arm the walkthrough once everything is granted, so a later loss
        // (recovery / F2, including a reinstall that re-grants then re-orphans)
        // surfaces the sheet again instead of staying suppressed by a stale
        // "Skip for now".
        if grants.allRequiredGranted {
            clearSetupDismissed()
        }
    }

    /// Persist that the user dismissed the permission walkthrough ("Skip for
    /// now"). Suppresses the launch gate; does NOT affect the start-block.
    func markSetupDismissed() {
        guard !setupDismissed else { return }
        setupDismissed = true
        defaults.set(true, forKey: Self.setupDismissedDefaultsKey)
    }

    private func clearSetupDismissed() {
        guard setupDismissed else { return }
        setupDismissed = false
        defaults.set(false, forKey: Self.setupDismissedDefaultsKey)
    }

    /// The daemon's grant state for a given Privacy pane. Microphone is not a
    /// daemon-tracked permission, so it reports `.indeterminate` (never shown in
    /// the required daemon rows).
    func daemonGrant(for pane: PrivacyPane) -> DaemonGrantState {
        switch pane {
        case .screenRecording: return daemonGrants.screenRecording
        case .accessibility:   return daemonGrants.accessibility
        case .inputMonitoring: return daemonGrants.inputMonitoring
        case .microphone:      return .indeterminate
        }
    }

    /// `defaults` is injectable so tests exercise the dismissal flag against an
    /// isolated suite instead of polluting `UserDefaults.standard`.
    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.setupDismissed = defaults.bool(forKey: Self.setupDismissedDefaultsKey)
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
            daemonGrantTimer?.invalidate()
            relaunchWatchdog?.cancel()
            if let workspaceObserver {
                NSWorkspace.shared.notificationCenter.removeObserver(workspaceObserver)
            }
            if let daemonGrantWorkspaceObserver {
                NSWorkspace.shared.notificationCenter.removeObserver(daemonGrantWorkspaceObserver)
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

    /// Begin refreshing the *daemon* grant snapshot while the walkthrough sheet
    /// is visible (U5): on app re-activation and on a slow ~5s timer. `refresh`
    /// performs the actual `daemon.info` round-trip (owned by RecorderController);
    /// an in-flight guard collapses overlapping ticks so a re-activation landing
    /// on a timer tick can't double-spawn the daemon probe. An immediate refresh
    /// runs so the rows reflect current state the moment the sheet opens.
    func startDaemonGrantWatching(refresh: @escaping @MainActor () async -> Void) {
        stopDaemonGrantWatching()
        let timer = Timer.scheduledTimer(
            withTimeInterval: Self.daemonGrantRefreshInterval, repeats: true
        ) { [weak self] _ in
            Task { @MainActor in self?.runDaemonGrantRefresh(refresh) }
        }
        daemonGrantTimer = timer
        daemonGrantWorkspaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.runDaemonGrantRefresh(refresh) }
        }
        runDaemonGrantRefresh(refresh)
    }

    func stopDaemonGrantWatching() {
        daemonGrantTimer?.invalidate()
        daemonGrantTimer = nil
        if let daemonGrantWorkspaceObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(daemonGrantWorkspaceObserver)
            self.daemonGrantWorkspaceObserver = nil
        }
        // Clear the in-flight guard on stop so a re-open always kicks a fresh
        // immediate refresh. Bump the generation so a refresh still awaiting
        // when we stopped doesn't clear a *newer* session's guard on completion
        // (see runDaemonGrantRefresh).
        daemonGrantRefreshInFlight = false
        daemonGrantRefreshGeneration &+= 1
    }

    private func runDaemonGrantRefresh(_ refresh: @escaping @MainActor () async -> Void) {
        guard !daemonGrantRefreshInFlight else { return }
        daemonGrantRefreshInFlight = true
        let generation = daemonGrantRefreshGeneration
        Task { @MainActor in
            await refresh()
            // Only clear if no stop()/restart superseded this refresh; otherwise
            // a stale completion would reopen the guard mid-flight for the new
            // session.
            guard generation == daemonGrantRefreshGeneration else { return }
            daemonGrantRefreshInFlight = false
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
    /// We schedule the relaunch via `/bin/sh` and pass the app bundle path as
    /// a positional argument so spaces / metacharacters bypass the shell
    /// parser entirely. The helper polls our PID until it's gone (with a 10 s
    /// safety cap) instead of guessing a fixed sleep — under Xcode's LLDB
    /// attachment, `NSApp.terminate(nil)` can take noticeably longer than
    /// 0.6 s to actually exit the process. If that cap fires we abort the
    /// relaunch instead of forcing a new instance against a still-live parent,
    /// because stacking a second instance is worse than leaving the current
    /// one up and asking the user to reopen manually.
    ///
    /// The relaunch opens the signed app bundle through LaunchServices so TCC
    /// sees the same bundle identity that System Settings presents. To keep
    /// dev runs working, the helper publishes the current PATH and repo root
    /// into launchd before opening the bundle.
    func relaunchApplication() {
        guard !isRelaunching else { return }
        isRelaunching = true
        let bundlePath = Bundle.main.bundlePath
        guard !bundlePath.isEmpty else {
            isRelaunching = false
            let alert = NSAlert()
            alert.messageText = "Couldn't relaunch ScreenCap"
            alert.informativeText = "Couldn't determine the app bundle path. Quit and reopen ScreenCap manually to apply granted permissions."
            alert.alertStyle = .warning
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }
        let parentPid = ProcessInfo.processInfo.processIdentifier
        let environment = ProcessInfo.processInfo.environment
        let detach = Process()
        detach.executableURL = URL(fileURLWithPath: "/bin/sh")
        detach.environment = environment
        detach.arguments = [
            "-c",
            Self.relaunchHelperShellScript(),
            "screencap-relaunch",     // $0
            String(parentPid),        // $1 — current process's PID
            bundlePath,               // $2 — app bundle path passed through argv
            environment["PATH"] ?? "",
            environment["SCREENCAP_DEV_REPO_ROOT"] ?? "",
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
    func requestAndOpenSettings(for pane: PrivacyPane, subject: PermissionSubject = .screenCapApp) {
        guard subject == .screenCapApp else {
            openSystemSettings(for: pane)
            return
        }

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
    ///
    /// The destination URL is the same regardless of subject (TCC panes list
    /// every registered subject in a single list), so no `subject` param here —
    /// `requestAndOpenSettings` is where subject-specific branching lives.
    func openSystemSettings(for pane: PrivacyPane) {
        if !NSWorkspace.shared.open(Self.settingsURL(for: pane)) {
            NSWorkspace.shared.open(PrivacyPane.fallbackURL)
        }
    }

    nonisolated static func settingsURL(for pane: PrivacyPane) -> URL {
        pane.deepLinkURL
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
        "i=0; while kill -0 \"$1\" 2>/dev/null; do i=$((i+1)); [ $i -ge \(maxPollCount) ] && exit 0; sleep \(pollIntervalSeconds); done; [ -n \"$3\" ] && /bin/launchctl setenv PATH \"$3\"; [ -n \"$4\" ] && /bin/launchctl setenv SCREENCAP_DEV_REPO_ROOT \"$4\"; exec /usr/bin/open -n \"$2\""
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
