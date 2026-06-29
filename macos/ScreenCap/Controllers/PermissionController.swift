import AppKit
import AVFoundation
import ApplicationServices
import Combine
import CoreGraphics
import Foundation
import IOKit
import IOKit.hid
import Security
import os

private let permissionLogger = Logger(subsystem: "com.screencap.macos", category: "permission")

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

    /// The canonical permission string the daemon's `permission.request` verb
    /// (and `daemon.info` grant block) use — the inverse of
    /// `from(permissionString:)`. Microphone is not a daemon-registered
    /// permission and has no canonical string; it returns `nil` so the daemon
    /// registration path never fires for it.
    var permissionString: String? {
        switch self {
        case .screenRecording: return "screen_recording"
        case .accessibility:   return "accessibility"
        case .inputMonitoring: return "input_monitoring"
        case .microphone:      return nil
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
    /// process actually exits. Surfaced to the UI so the "Restart to apply
    /// permissions" button can be disabled, preventing a double-click from
    /// stacking multiple new instances.
    @Published private(set) var isRelaunching: Bool = false
    /// Persisted "permission setup dismissed" flag (U4). Once the user taps
    /// "Skip for now" with a grant still missing, the state-driven walkthrough
    /// gate stops re-popping on every launch. The **start-block is independent**
    /// of this flag (R4) — a dismissed sheet never lets a broken recording
    /// start silently. Auto-cleared when all required daemon grants land (see
    /// `updateDaemonGrants`) so a later loss (recovery / F2) re-arms the sheet.
    @Published private(set) var setupDismissed: Bool
    /// Phase 1c (SCR-49): true until the one-time daemon-helper migration banner
    /// (`DaemonMigrationView`) has been shown and migration completed. Backed by
    /// the `~/.screencap/.tcc-migrated-v1` marker (`MigrationMarkerStore`), read
    /// once at construction. Drives the first-run sheet to present the upgrade
    /// banner once — even when the daemon already reports grants, and even if the
    /// walkthrough was previously skipped (it overrides `setupDismissed` for that
    /// one-time banner). UX-only: never a grant oracle — the daemon's startup TCC
    /// preflight remains the authority on actual grants (R4).
    @Published private(set) var migrationNeeded: Bool
    /// Panes with an outstanding daemon registration round-trip (U8). While a
    /// pane is in this set the Grant button shows a disabled/spinner state and
    /// a repeat tap is a no-op — one in-flight registration per pane. Published
    /// so the walkthrough view reacts without owning the guard itself.
    @Published private(set) var daemonRegistrationInFlight: Set<PrivacyPane> = []

    /// True when the running app is ad-hoc signed (no Team Identifier). On such
    /// builds macOS re-keys TCC on every rebuild's changing signature, so prior
    /// Screen Recording / Accessibility / Input Monitoring grants are orphaned
    /// and the app reads "denied" even while System Settings still shows an old
    /// build as granted. Used purely to surface a dev-only hint — it never
    /// affects gating or the real OS-enforced permission state. Signed builds
    /// (Apple Development / Developer ID / App Store) always have a team id, so
    /// this is false for every release artifact. Injected for tests.
    let isAdHocBuild: Bool

    private let defaults: UserDefaults
    private static let setupDismissedDefaultsKey = "com.screencap.macos.permissionSetupDismissed"
    /// Backing store for `migrationNeeded` (Phase 1c). Injectable so tests point
    /// at a temp directory instead of the real `~/.screencap`.
    private let migrationMarker: MigrationMarkerStore

    /// Issues the daemon `permission.request` round-trip for a canonical
    /// permission string. Injected (nil → the live `DaemonClient` call) so tests
    /// exercise the Grant flow (ordering, the per-pane in-flight guard) without a
    /// `UnixHTTPTestServer`.
    typealias DaemonPermissionRegistrar = @MainActor (String) async throws -> Void
    private let injectedDaemonRegistrar: DaemonPermissionRegistrar?
    /// Opens the System Settings pane after the daemon ack. Injected (nil → the
    /// real `openSystemSettings`) so tests assert ack-before-open ordering
    /// without launching System Settings.
    private let injectedDaemonSettingsOpener: (@MainActor (PrivacyPane) -> Void)?

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

    /// Whether the Privacy tab's "Finish setup" recovery banner should show
    /// (SCR-143). Decouples "did the user skip the walkthrough" (`setupDismissed`,
    /// the launch-gate concern) from "can this machine record" (the banner's
    /// claim). Keying on `setupDismissed` alone strands the banner forever on the
    /// CLI-fallback path, where the daemon-grant auto-clear (`updateDaemonGrants`)
    /// never fires — so the orange "can't record" claim persists even after the
    /// user has granted permissions and recording works. Requiring
    /// `!allRequiredGranted` too mirrors the CLI-fallback recording-start gate
    /// (`RecorderController.start`), so the banner clears exactly when the app's
    /// own contract permits recording.
    var shouldShowFinishSetupBanner: Bool {
        setupDismissed && !allRequiredGranted
    }

    /// Dev-only hint shown to explain the "System Settings says granted but the
    /// app says denied" confusion that ad-hoc builds cause. Gated on an *actual*
    /// denial (daemon- or app-process-reported) so a fully-granted ad-hoc build
    /// stays quiet. Never shown on signed builds. See `adHocDevBuildWarning`.
    var showAdHocDevBuildWarning: Bool {
        isAdHocBuild && (daemonGrants.anyRequiredDenied || anyDenied)
    }

    /// Copy for the ad-hoc dev-build hint. Names the cause (signature changes on
    /// every rebuild orphan the grant) and both fixes (stable `DEVELOPMENT_TEAM`
    /// signing, or reset + re-grant). Intentionally developer-facing — it only
    /// ever renders on an unsigned local build.
    static let adHocDevBuildWarning =
        "Ad-hoc dev build: macOS ties permission grants to the app's code "
        + "signature, which changes on every rebuild — so System Settings may "
        + "show an earlier build as granted while this one reads denied. Build "
        + "with DEVELOPMENT_TEAM set for stable signing, or run "
        + "`tccutil reset All com.screencap.macos` and grant again."

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

    /// Set when the user explicitly asks to re-open the first-run walkthrough
    /// from a recovery affordance (the Privacy tab's "Finish setup" entry point),
    /// as opposed to the launch gate, which `setupDismissed` suppresses. Without
    /// this, a mistaken "Skip for now" is unrecoverable: the launch gate never
    /// re-pops, and the daemon-grant auto-clear (`updateDaemonGrants`) can never
    /// fire while the daemon stays uninstalled (the CLI-fallback path you land on
    /// when you skip the install step). MainWindow observes this, presents the
    /// sheet, then calls `consumeReopenSetupRequest()`.
    @Published private(set) var reopenSetupRequested: Bool = false

    /// User asked to re-open the walkthrough (recovery entry point). Deliberately
    /// does NOT clear `setupDismissed`: only *completing* setup re-arms the gate
    /// (daemon grants landing → `updateDaemonGrants`), so the launch behavior is
    /// unchanged for users who never touch this affordance.
    func requestReopenSetup() {
        reopenSetupRequested = true
    }

    /// MainWindow calls this once it has presented the sheet, so the latched flag
    /// doesn't re-present on a later view update.
    func consumeReopenSetupRequest() {
        reopenSetupRequested = false
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
    /// isolated suite instead of polluting `UserDefaults.standard`. The daemon
    /// registrar / settings-opener seams default to the live implementations and
    /// are overridden in tests.
    init(
        defaults: UserDefaults = .standard,
        daemonRegistrar: DaemonPermissionRegistrar? = nil,
        daemonSettingsOpener: (@MainActor (PrivacyPane) -> Void)? = nil,
        isAdHocBuild: Bool = PermissionController.detectAdHocSigned(),
        migrationMarker: MigrationMarkerStore = MigrationMarkerStore()
    ) {
        self.defaults = defaults
        self.setupDismissed = defaults.bool(forKey: Self.setupDismissedDefaultsKey)
        self.injectedDaemonRegistrar = daemonRegistrar
        self.injectedDaemonSettingsOpener = daemonSettingsOpener
        self.isAdHocBuild = isAdHocBuild
        self.migrationMarker = migrationMarker
        self.migrationNeeded = !migrationMarker.isMigrated()
    }

    /// Persist that the one-time Phase 1c migration banner has been acknowledged
    /// and migration completed (marker written), so it never shows again. Called
    /// when the daemon install reports `installedAndRunning` during the upgrade
    /// flow (origin upgrade-flow step 4) — not on the banner's "Continue" tap, so
    /// a user who quits mid-install doesn't get a marker without an installed
    /// helper. A write failure is logged and leaves `migrationNeeded` true so the
    /// banner re-shows once next launch (harmless) rather than being lost (R4).
    func markMigrationComplete() {
        guard migrationNeeded else { return }
        do {
            try migrationMarker.markMigrated()
            migrationNeeded = false
        } catch {
            permissionLogger.info(
                "Failed to write TCC migration marker: \(String(describing: error), privacy: .public)"
            )
        }
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
        daemonGrantRefreshGeneration += 1
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
    /// launch does NOT show up here until the next process start. On the
    /// CLI-fallback path the walkthrough surfaces a "Restart to apply
    /// permissions" button to drive the restart explicitly.
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

    /// Triggers permission registration for `pane` and opens the matching
    /// Privacy & Security pane.
    ///
    /// Phase 1c (SCR-49): the SwiftUI app no longer registers itself as a TCC
    /// subject for Screen Recording / Accessibility / Input Monitoring — the
    /// daemon helper is the sole subject for those three on the recording path
    /// (R5). They route through the daemon registration round-trip below. The
    /// app still owns the **microphone**, which it requests in-process. The
    /// CLI-fallback path continues to *probe* the three via `refresh()` /
    /// `allRequiredGranted` (R6) but never prompts the user from here.
    ///
    /// Microphone uses an async callback; we don't block on it because the
    /// pane should open immediately either way.
    func requestAndOpenSettings(for pane: PrivacyPane, subject: PermissionSubject = .daemon) {
        guard subject == .screenCapApp else {
            // Daemon subject (U8): the daemon must perform the registration in
            // *its own* process so the Settings entry is attributed to the
            // daemon's TCC identity, not the app's (R5; U7 responsible-process
            // caveat). Kick the async round-trip — the published in-flight set
            // drives the Grant button, and the pane opens only after the daemon
            // acks. Replaces the old no-op (which just opened the pane with no
            // helper row, the AE2 failure mode).
            Task { await requestDaemonPermission(for: pane) }
            return
        }

        // App-process (`.screenCapApp`) subject: only the microphone remains
        // app-owned. The three heavy TCC services are intentionally NOT requested
        // in-process anymore — Phase 1c drops the app's role as their TCC subject
        // (R5). `CGRequestScreenCaptureAccess` / `AXIsProcessTrustedWithOptions`
        // (prompt) / `IOHIDRequestAccess` are deliberately gone from this path.
        if pane == .microphone {
            AVCaptureDevice.requestAccess(for: .audio) { _ in
                Task { @MainActor in self.refresh() }
            }
        }
        openSystemSettings(for: pane)
    }

    /// True while a daemon registration round-trip for `pane` is outstanding
    /// (U8). The Grant button reads this to show a disabled/spinner state.
    func isDaemonRegistering(_ pane: PrivacyPane) -> Bool {
        daemonRegistrationInFlight.contains(pane)
    }

    /// Drive the daemon-side registration for `pane` (U8): ask the daemon to
    /// register itself for the permission in its own process, await the ack,
    /// then open the matching Settings pane. Ordering matters — the pane opens
    /// only *after* the daemon has registered, so the user doesn't land on a
    /// pane with no helper row (AE2). A per-pane in-flight guard collapses
    /// repeat taps to a single round-trip. The pane is opened even when the
    /// daemon call fails: registration is best-effort, and the user must always
    /// be able to reach Settings to enable the helper manually.
    func requestDaemonPermission(for pane: PrivacyPane) async {
        guard let permission = pane.permissionString else {
            // Not a daemon-registered permission (microphone) — just open.
            resolvedDaemonSettingsOpener()(pane)
            return
        }
        guard !daemonRegistrationInFlight.contains(pane) else { return }
        daemonRegistrationInFlight.insert(pane)
        defer { daemonRegistrationInFlight.remove(pane) }

        // Capture whether this request originated from the visible walkthrough.
        // The daemon-grant watch is started in the sheet's onAppear and stopped
        // in onDisappear, so it is the walkthrough's visibility signal — and the
        // walkthrough is this path's only caller today.
        let gatedOnWalkthrough = isDaemonGrantWatching

        do {
            try await resolvedDaemonRegistrar()(permission)
        } catch {
            permissionLogger.info(
                "Daemon permission registration for \(permission, privacy: .public) failed: \(String(describing: error), privacy: .public)"
            )
        }

        // The registrar round-trip can take up to the client timeout (~10s). If
        // the user dismissed the walkthrough while it was in flight, opening
        // System Settings now would pop a pane out of nowhere long after they
        // moved on. Suppress the open when a walkthrough-originated request finds
        // the walkthrough already gone. A non-walkthrough caller (not watching at
        // start) always opens, preserving prior behavior.
        if gatedOnWalkthrough, !isDaemonGrantWatching {
            permissionLogger.info(
                "Walkthrough dismissed during daemon registration for \(permission, privacy: .public); skipping Settings open"
            )
            return
        }
        resolvedDaemonSettingsOpener()(pane)
    }

    private func resolvedDaemonRegistrar() -> DaemonPermissionRegistrar {
        injectedDaemonRegistrar ?? { permission in
            _ = try await DaemonClient.permissionRequest(permission)
        }
    }

    private func resolvedDaemonSettingsOpener() -> @MainActor (PrivacyPane) -> Void {
        injectedDaemonSettingsOpener ?? { [weak self] pane in
            self?.openSystemSettings(for: pane)
        }
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
    // These cache at process start. On the CLI-fallback path the walkthrough's
    // "Restart to apply permissions" button is what gets the user to a fresh
    // process when they've granted a permission post-launch.

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

    /// True when the running bundle has no Team Identifier — i.e. it is ad-hoc
    /// signed or unsigned. Apple Development, Developer ID, and App Store builds
    /// all carry a team id, so this is false for every release artifact and any
    /// dev build signed with `DEVELOPMENT_TEAM` set.
    ///
    /// Fails closed to `false` (assume properly signed) on any Security API
    /// error so a probe hiccup never shows the dev hint on a real user's
    /// machine. Crucially, the fail-closed path is distinct from the genuine
    /// "ad-hoc" verdict: we return `true` ONLY when the signing information was
    /// read successfully AND `kSecCodeInfoTeamIdentifier` is genuinely absent.
    /// Any SecCode* guard failure (a transient Security-API error, an
    /// unreadable record) returns `false` rather than mislabelling a properly
    /// signed build as ad-hoc.
    nonisolated static func detectAdHocSigned() -> Bool {
        var codeRef: SecCode?
        guard SecCodeCopySelf(SecCSFlags(), &codeRef) == errSecSuccess,
              let codeRef else { return false }
        var staticRef: SecStaticCode?
        guard SecCodeCopyStaticCode(codeRef, SecCSFlags(), &staticRef) == errSecSuccess,
              let staticRef else { return false }
        var infoRef: CFDictionary?
        let flags = SecCSFlags(rawValue: UInt32(kSecCSSigningInformation))
        guard SecCodeCopySigningInformation(staticRef, flags, &infoRef) == errSecSuccess,
              let info = infoRef as? [String: Any] else { return false }
        // Signing info read successfully: a missing team id is a genuine ad-hoc
        // verdict (true); a present one is a properly signed build (false).
        return (info[kSecCodeInfoTeamIdentifier as String] as? String) == nil
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
