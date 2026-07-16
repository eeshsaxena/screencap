import AppKit
import AVFoundation
import Combine
import Foundation
import OSLog

/// Drift-detection log for the stderr event contract with `_stderr_events.py`.
/// Tail with: `log stream --predicate 'subsystem == "com.screencap.macos"'`.
private let recorderLogger = Logger(subsystem: "com.screencap.macos", category: "recorder")
let SUPPORTED_API_SCHEMA_VERSION = 1
/// The auth CLI envelopes (`whoami` / `checkout-url` / `portal-url` /
/// `reconcile-entitlement`) version independently of the daemon `/v0/*` API
/// above: Python's `_AUTH_SCHEMA_VERSION` was bumped 1 → 2 when the two-tier
/// `tier` + `trial_end` claims joined the envelope (U6), while the daemon
/// envelopes stayed at 1. `CloudAuthController`'s drift check compares against
/// this constant so an auth-side bump warns exactly once per real drift —
/// not spuriously on every envelope because it was measured against the
/// daemon's version.
let SUPPORTED_AUTH_SCHEMA_VERSION = 2

/// Status payload from `screencap status --json` (Unit 4 schema v1).
struct CLIStatus: Decodable {
    let ok: Bool
    let schemaVersion: Int
    let isRecording: Bool
    let startedAt: Double?
    let elapsed: Double?
    let captureDir: String?
    let claimant: String?
    let warning: String?
    let privacyConfigured: Bool?
    let nlpModelsCached: Bool?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case isRecording = "is_recording"
        case startedAt = "started_at"
        case elapsed
        case captureDir = "capture_dir"
        case claimant
        case warning
        case privacyConfigured = "privacy_configured"
        case nlpModelsCached = "nlp_models_cached"
    }
}

/// Decoded shape of a single stderr line from `screencap start`. Unknown event
/// types decode into `.unknown` rather than failing, so a future field doesn't
/// brick the SwiftUI parser.
struct RecorderEventLine: Decodable {
    let type: String
    let schemaVersion: Int?
    let forceStopped: Bool?
    let permission: String?
    /// Full list of denied permissions on a `permission_required` (SCR-142)
    /// start-time block. Unlike the single `permission` field (a mid-recording
    /// `permission_lost` revocation), this names every missing permission at
    /// once so the CLI-fallback shell can route into the precise grant flow.
    /// Absent on every other event type.
    let missing: [String]?
    let changes: [String]?
    let optOutCommandExamples: [String]?
    let cursor: Int?
    let reason: String?
    /// Which capture is affected on a `capture_unhealthy` (SCR-76) or
    /// `capture_recovered` (SCR-100) event: "screen" / "window" / "action".
    /// Absent on every other event type.
    let reader: String?
    /// The recording's frozen routing destination ("local" / "cloud" / "both")
    /// on a `recording_finalized` event, so the force-stop banner can tailor its
    /// copy — a local recording has nothing to upload. Absent (nil) on other
    /// events and on the daemon's crash-synthesized finalize, where the banner
    /// falls back to the upload-oriented copy (the conservative default).
    let destination: String?
    let ts: Double?

    enum CodingKeys: String, CodingKey {
        case type
        case schemaVersion = "schema_version"
        case forceStopped = "force_stopped"
        case permission
        case missing
        case changes
        case optOutCommandExamples = "opt_out_command_examples"
        case cursor
        case reason
        case reader
        case destination
        case ts
    }
}

enum RecorderTransport: Equatable {
    case daemon
    case cliFallback
}

/// Where a mute toggle was initiated (SCR-254 U8/U9). Routes the unmute
/// permission-denied surface: a menu-bar action means the user isn't looking at
/// the HUD pill, so the denial must reach a surface they can see (R3).
enum MuteToggleSource: Equatable, Sendable {
    case hud
    case menuBar
}

/// Microphone-authorization seam (SCR-254 U9). Wraps the two `AVCaptureDevice`
/// audio-permission calls the unmute path needs so `RecorderController` is
/// unit-testable across the authorized / undetermined / denied branches without a
/// live TCC subject. The live implementation mirrors `NewRecordingSheet`'s
/// non-prompting `authorizationStatus` + prompting `requestAccess` pair.
@MainActor
protocol MicAuthorizing {
    /// The current status WITHOUT prompting (`authorizationStatus(for: .audio)`).
    func authorizationStatus() -> AVAuthorizationStatus
    /// The PROMPTING request (`requestAccess(for: .audio)`); returns true on grant.
    func requestAccess() async -> Bool
}

/// Live implementation over `AVCaptureDevice`.
@MainActor
final class LiveMicAuthorizer: MicAuthorizing {
    func authorizationStatus() -> AVAuthorizationStatus {
        AVCaptureDevice.authorizationStatus(for: .audio)
    }

    func requestAccess() async -> Bool {
        await AVCaptureDevice.requestAccess(for: .audio)
    }
}

struct PrivacyMatrixDisclosure: Equatable, Identifiable {
    let id = "privacy-matrix-v2026-04"
    let changes: [String]
    let optOutCommandExamples: [String]
}

extension Notification.Name {
    /// U7: posted when a recording ends and the main window is restored, so
    /// `MainWindow` routes the reopened shell to Library.
    static let screenCapRecordingDidEnd = Notification.Name("com.screencap.recording.didEnd")
}

@MainActor
final class RecorderController: ObservableObject {
    static let requiredPermissionsErrorMessage =
        "Grant Screen Recording, Accessibility, and Input Monitoring permissions before recording."

    /// Minimum spacing between two staleness-defeating daemon restarts (see
    /// `refreshDaemonGrantsDefeatingStaleness`). The grant-watch fires both on
    /// app re-activation (immediate when the user returns from System Settings)
    /// and on a ~5s timer; 8s keeps a coincident activation + timer tick from
    /// double-restarting while still picking up a fresh grant within one cycle.
    static let staleDaemonRestartCooldown: TimeInterval = 8.0

    /// SCR-262: wall-clock budget for a helper swap to converge, anchored at the
    /// restart trigger. Must exceed both the 30s install convergence budget
    /// (launchd `ExitTimeOut=30`) and the observed 30–60s boot-out/re-register
    /// range — 60s came from a single observed update, so 90s buys headroom
    /// against slower machines. Past the deadline the permission wall (the
    /// repair surface) presents.
    static let updateConvergenceDeadline: TimeInterval = 90.0

    /// SCR-262: cadence of the convergence re-probe loop. Deliberately slower
    /// than `pollDaemon`'s interactive 0.5s wait: the swap takes 30–60s, so a
    /// background ~3s tick still converges within one tick of the daemon
    /// binding while keeping the dying socket quiet.
    static let updateConvergenceProbeInterval: TimeInterval = 3.0

    /// SCR-262: UserDefaults key holding the restart-trigger timestamp. The swap
    /// window outlives the app process that kickstarts it, so a quit-and-relaunch
    /// mid-swap re-enters convergence from this persisted anchor instead of
    /// presenting the spurious permission wall.
    static let updateConvergenceAnchorKey = "screencap.updateConvergenceTriggeredAt"

    /// Non-terminal advisory when the mute/unmute VERB itself could not be
    /// delivered (SCR-254 U7). Deliberately NOT routed through
    /// `handleDaemonOperationFailure` (which idles the recording) — the recording
    /// keeps running with its prior confirmed mic state; the user can retry.
    static let muteRequestFailedAdvisory =
        "Couldn't reach the recorder to change the microphone. The mic state is "
        + "unchanged — try again."

    /// Non-terminal advisory when the engine confirmed it could NOT acquire the
    /// mic on unmute (`audio_unmute_failed`, KTD4/R3). The recording keeps running
    /// MUTED; surfaced so the failure is never silent.
    static let microphoneUnmuteFailedAdvisory =
        "Couldn't turn the microphone on — it may be in use by another app. The "
        + "recording is still running, muted."

    /// Non-terminal advisory when the engine confirmed the MUTE toggle FAILED
    /// mid-recording (`audio_mute_failed`, SCR-271) — a DB write faulted or the
    /// stream stop raised, so capture did NOT reliably stop. A failed stop means
    /// the mic is still live, so the mic state reads unchanged and the user can
    /// retry; surfaced so the failure is never silent. Distinct from
    /// `muteRequestFailedAdvisory`, which is the TRANSPORT verb never landing.
    static let microphoneMuteFailedAdvisory =
        "Couldn't turn the microphone off. The recording is still running — try "
        + "again."

    /// Non-terminal advisory when the app's own mic permission is denied, so an
    /// unmute never reached the daemon (SCR-254 U9/R3). Paired with the modal for
    /// the menu-bar / HUD-hidden surfaces so the denial is always visible.
    static let microphoneAccessDeniedAdvisory =
        "Microphone access is denied, so the mic stayed off. Enable it in System "
        + "Settings to record audio."

    /// Decide whether to kickstart a fresh daemon to defeat a stale grant probe.
    ///
    /// The daemon's `daemon.info` grant probe reports TCC state as of the
    /// daemon's *process launch*, not live state: its "fresh subprocess" still
    /// inherits the daemon's launch-time TCC responsibility context (Screen
    /// Recording resolves through the responsible-app rollup, cached at launch),
    /// so a permission granted *after* the daemon started stays invisible until
    /// the daemon restarts. While a permission surface is visible and a required
    /// grant still reads denied, a bounded restart re-reads live state instead of
    /// stranding the user on a permission they have already granted.
    ///
    /// Gated so it never fires during a recording (the kickstart would kill
    /// capture), only when the daemon transport is actually live (never mid
    /// install / CLI-fallback, where the install flow owns bring-up), and
    /// rate-limited by `cooldown`.
    static func shouldRestartStaleDaemon(
        anyRequiredDenied: Bool,
        isRecording: Bool,
        transportIsDaemon: Bool,
        secondsSinceLastRestart: TimeInterval?,
        cooldown: TimeInterval
    ) -> Bool {
        guard anyRequiredDenied, !isRecording, transportIsDaemon else { return false }
        guard let secondsSinceLastRestart else { return true }
        return secondsSinceLastRestart >= cooldown
    }

    /// SCR-262: the anchor a launch should converge from, or nil when this launch
    /// has no swap window to wait out.
    ///
    /// A freshly-triggered restart anchors at `now`. Otherwise a persisted anchor
    /// still inside its deadline re-enters convergence with the ORIGINAL anchor —
    /// a quit-and-relaunch mid-swap must neither present the spurious wall (the
    /// relaunch finds a dead daemon and triggers no restart of its own) nor grant
    /// itself a fresh deadline. A future-dated persisted anchor can't be trusted
    /// (clock skew) — treat it as no window, mirroring the bundle-mtime guard in
    /// `DaemonInstallController.restartStaleDaemonIfNeeded`.
    static func convergenceAnchorForLaunch(
        restartTriggered: Bool,
        persistedAnchor: Date?,
        now: Date,
        deadline: TimeInterval
    ) -> Date? {
        if restartTriggered { return now }
        guard let persistedAnchor,
              persistedAnchor <= now,
              now.timeIntervalSince(persistedAnchor) < deadline
        else { return nil }
        return persistedAnchor
    }

    /// SCR-262: one step of the convergence re-probe loop.
    enum ConvergenceStep: Equatable {
        /// A probed daemon's process start postdates the trigger anchor — the
        /// swapped-in helper is up. Bare reachability is NOT success: the
        /// booted-out daemon can keep answering the socket for up to ~30s
        /// (launchd `ExitTimeOut=30`), and adopting it would dismiss the
        /// interstitial onto a transport about to die.
        case finishedFresh
        /// The deadline passed without a fresh daemon — fall through to the
        /// permission wall (the repair surface).
        case deadlineExpired
        /// No daemon, or only the pre-swap daemon, answered — keep probing.
        case keepWaiting
    }

    /// Pure termination decision for the convergence loop. Freshness wins at the
    /// deadline edge: a fresh daemon observed on the expiring tick still counts
    /// as convergence.
    static func convergenceStep(
        probeStartedAt: Double?,
        anchor: Date,
        now: Date,
        deadline: TimeInterval
    ) -> ConvergenceStep {
        if let probeStartedAt, probeStartedAt > anchor.timeIntervalSince1970 {
            return .finishedFresh
        }
        if now.timeIntervalSince(anchor) >= deadline {
            return .deadlineExpired
        }
        return .keepWaiting
    }

    /// SCR-264: whether a `state` transition is the recording→idle edge that
    /// should re-run the deferred stale-daemon check. True only when landing on
    /// `.idle` from an active state (`.recording`/`.stopping`/`.starting`), so a
    /// defensive idle→idle re-assignment never re-triggers the check.
    static func shouldRecheckStaleDaemonAfterRecording(
        from oldState: RecordingState, to newState: RecordingState
    ) -> Bool {
        guard case .idle = newState else { return false }
        return oldState.isRecording
    }

    @Published private(set) var state: RecordingState = .idle {
        didSet {
            // The capture-health advisory is scoped to an active recording.
            // Drop it on the return to .idle so a stale "may not be recording
            // correctly" hint never lingers on the idle menu/window (SCR-76).
            // handleCaptureUnhealthy's `.recording` guard prevents it from
            // being re-set outside a recording, so this single chokepoint
            // covers every idle-transition path (stop, Cmd+Q, termination).
            // Also reset the per-reader tracking so the next recording starts
            // clean (SCR-100).
            if case .idle = state {
                captureAdvisory = nil
                unhealthyReaders.removeAll()
                // Hide control: every teardown path (normal stop via enterIdle,
                // Cmd+Q, process termination, recording_failed, and the abnormal
                // transitionToIdle rollbacks) lands on `.idle` here — the same
                // single chokepoint the advisory uses — so hidden state never
                // survives a recording and the pill starts shown next time (R6).
                hudHidden = false
                // SCR-254: mute state is per-recording; clear it on every idle
                // transition through the same chokepoint so a fresh recording
                // starts unmuted and no stale in-flight flag survives.
                muted = false
                muteInFlight = false
            }
            // SCR-264: re-run the post-update stale-daemon swap on the
            // recording→idle edge. A launch that landed while a daemon-owned
            // recording was in flight defers the swap (restartStaleDaemonIfNeeded
            // no-ops during a recording) and, because the launch check is
            // launch-once, nothing else re-triggers it — the stale daemon would
            // otherwise squat the socket for the rest of the session, 500ing every
            // lazy-import verb ("Couldn't load recordings"). This single idle
            // chokepoint catches every teardown path, including the launch-attach
            // session end that never posts `.screenCapRecordingDidEnd` (its posts
            // are gated on `mainWindowHidden`, which the attach path leaves false).
            if Self.shouldRecheckStaleDaemonAfterRecording(from: oldValue, to: state) {
                Task { [weak self] in await self?.recheckStaleDaemonAfterRecordingEnd() }
            }
        }
    }
    @Published private(set) var lastError: String?
    /// Advisory, NON-terminal capture-health notice (SCR-76 `capture_unhealthy`).
    /// Kept distinct from `lastError` (terminal failures) so the UI can present
    /// it as a non-blocking hint that does not imply the recording has stopped.
    /// Cleared automatically on the return to `.idle` (see `state.didSet`).
    @Published private(set) var captureAdvisory: String?
    /// Readers currently flagged unhealthy by the engine, in recency order
    /// (last == most recent). Readers are independent (screen / window /
    /// action can stall simultaneously), so the advisory only clears when this
    /// empties — one reader recovering must not drop a hint another still
    /// warrants. The displayed message names the most-recent reader so it
    /// stays specific (SCR-100). Reset on the return to `.idle`.
    private var unhealthyReaders: [String] = []
    @Published private(set) var matrixDisclosure: PrivacyMatrixDisclosure?
    @Published private(set) var transport: RecorderTransport = .cliFallback
    @Published private(set) var daemonProbeCompleted = false
    @Published private(set) var schemaMismatchDetected = false
    /// SCR-262: true while a helper swap triggered by the stale-daemon check is
    /// converging (kickstart fired, swapped-in daemon not yet verified fresh).
    /// The launch gate shows the "Finishing update…" interstitial instead of the
    /// permission wall while this is up. Set/cleared only by the launch check and
    /// the convergence loop — gate on THIS signal, never on bare unreachability,
    /// or dead-registration users get a 90s lie before their repair wall.
    @Published private(set) var updateConverging = false
    /// SCR-262: true when convergence ended at the deadline without a fresh
    /// daemon. Marks the permission wall's provenance as "update path": the wall
    /// then carries the didn't-finish-cleanly notice, and dismissing it does not
    /// persist `setupDismissed` (KTD-8). Cleared once a reachable daemon makes
    /// the wall's content genuine again, and at the next launch check.
    @Published private(set) var updateConvergenceFailed = false
    /// SCR-262: when the in-flight swap was triggered, for the interstitial's
    /// elapsed-time copy. Non-nil only while `updateConverging`.
    @Published private(set) var updateConvergenceAnchor: Date?
    /// Surfaced in the menu bar dropdown during a Cmd+Q stop. Counts down
    /// from 300s while we wait for the `stopped` event.
    @Published private(set) var quitProgressSecondsRemaining: Int?
    /// U6/U7: the EFFECTIVE audio state of the current recording — read by the
    /// HUD's mic indicator. Set at start from the daemon's `audio` echo (or the
    /// CLI's deterministic `--no-audio` choice); a stale daemon that omits the
    /// echo is treated as audio-on. Defaults to `true` between recordings.
    @Published private(set) var audioEnabled: Bool = true
    /// SCR-254 (U7): the CONFIRMED mic-mute state of the current recording, the
    /// single source the HUD pill + menu-bar item both reflect (R7). Flipped ONLY
    /// by the engine's confirmed `audio_muted` / `audio_unmuted` events (or a
    /// reconnect snapshot), NEVER by the mute-verb response echo — so the UI never
    /// shows "Muted" before capture actually stopped (KTD4). Reset on the return to
    /// `.idle` via the `state.didSet` chokepoint.
    @Published private(set) var muted: Bool = false
    /// SCR-254 (U7): true while a mute/unmute request is dispatched and awaiting
    /// its confirming event. Drives the HUD's transitional "Muting…"/"Unmuting…"
    /// affordance so the control never optimistically shows the target state.
    /// Cleared by the confirming event, by a verb failure, or on `.idle`.
    @Published private(set) var muteInFlight: Bool = false
    /// U7: the provisional recording name shown on the HUD title (the daemon
    /// session id / CLI name — the directory slug until post-stop auto-naming
    /// renames it; user rename is SCR-223). `nil` → the HUD shows "Recording".
    @Published private(set) var currentRecordingName: String?
    /// Hide control: true while the user has dismissed the recording HUD pill via
    /// its hide control. Distinct from the recording lifecycle — the pill's hide
    /// button and the menu-bar "Show recording controls" item both read this.
    /// Only `hideRecordingHUD()` sets it (guarded to `.recording`), and every
    /// return to `.idle` resets it via the `state.didSet` chokepoint that also
    /// resets `captureAdvisory` — so no hidden state survives a recording and each
    /// one starts shown.
    @Published private(set) var hudHidden: Bool = false
    /// U7: true only after `.hideMainWindow` was actually applied (the live
    /// `started` path), so a teardown restores + routes to Library ONLY when the
    /// window was really hidden. Without this, a failure in `.starting` (which is
    /// `state.isRecording` but pre-HUD — e.g. the daemon→CLI fallback) or a stop
    /// of a HUD-only attach session would spuriously re-activate an already-visible
    /// window and yank the user to Library.
    private var mainWindowHidden = false

    private weak var index: RecordingsIndex?
    private weak var permissions: PermissionController?

    /// When the last staleness-defeating daemon restart fired, so
    /// `refreshDaemonGrantsDefeatingStaleness` can rate-limit itself across the
    /// grant-watch's activation + timer ticks. `nil` until the first restart.
    private var lastDaemonRestartAt: Date?

    /// SCR-262: true once `runLaunchDaemonCheck` has run in this process — the
    /// stale-check, provenance reset, and anchor mint are launch-once; window
    /// re-materializations fall through to a plain probe.
    private var launchDaemonCheckRan = false
    /// SCR-262: the running convergence re-probe loop, nil when idle. Single
    /// instance — `startConvergenceProbeLoopIfNeeded` guards on it, and a window
    /// re-materialization re-running the launch task never doubles the loop.
    private var convergenceLoopTask: Task<Void, Never>?
    /// The defaults store the current convergence anchor was persisted to, so
    /// `finishUpdateConvergence` clears the same store the launch check wrote
    /// (tests inject a suite-scoped instance).
    private var convergenceDefaults: UserDefaults = .standard

    /// True only while a staleness-defeating daemon kickstart is in flight (the
    /// `daemonService.reload()` + rebind window inside `restartDaemonToRefreshGrants`).
    /// `start()` refuses during this window so a recording launched from a permission
    /// surface doesn't dispatch to a daemon that is mid-relaunch (which would fail
    /// over to CLI and wrongly demand every permission). Cleared via `defer`, and the
    /// window is ~1s, so a retry succeeds immediately.
    private var isDefeatingStaleness = false

    /// Pure value-type state machine owning `state`, `pendingStartCursor`,
    /// and `recordingStartedAt`. All state transitions route through it; the
    /// orchestrator mirrors `machine.state` into the `@Published` surface and
    /// applies returned effects.
    private var machine = RecordingStateMachine()
    private let watchdog: PermissionWatchdog
    private let alertPresenter: RecorderAlertPresenter
    private let cliService: CLIRecorderService
    private let daemonService: DaemonSessionService
    private let stopPolicy: StopPolicyCoordinator
    /// U7: HUD panel + main-window hide/restore seam. Defaults to no-op under
    /// XCTest (see `WindowLifecycleFactory`) so controller tests never spawn a
    /// real panel; the app gets the live implementation.
    private let windowLifecycle: WindowLifecycle
    /// Global-input seam for the ⌘⇧H toggle and the bottom-edge peek, active only
    /// during a recording (U2/U3). No-op under XCTest via `HUDInputMonitorFactory`.
    private let inputMonitor: HUDInputMonitor
    /// Persistence for the one-time first-hide menu-bar hint (U5).
    private let hintStore: HUDHintStore
    /// SCR-254 (U9): non-prompting/prompting mic-authorization seam for the unmute
    /// permission gate. Injected so controller tests drive the authorized /
    /// undetermined / denied branches without a live TCC subject.
    private let micAuthorizer: MicAuthorizing

    private var daemonEventTask: Task<Void, Never>?
    private var elapsedTimer: Timer?
    private var daemonInstalledObserver: NSObjectProtocol?

    func bindIndex(_ index: RecordingsIndex) {
        self.index = index
    }

    func bindPermissions(_ permissions: PermissionController) {
        self.permissions = permissions
    }

    init(
        watchdog: PermissionWatchdog = LivePermissionWatchdog(),
        alertPresenter: RecorderAlertPresenter = LiveRecorderAlertPresenter(),
        cliService: CLIRecorderService = LiveCLIRecorderService(),
        daemonService: DaemonSessionService = LiveDaemonSessionService(),
        stopPolicy: StopPolicyCoordinator = LiveStopPolicyCoordinator(),
        windowLifecycle: WindowLifecycle = WindowLifecycleFactory.makeDefault(),
        inputMonitor: HUDInputMonitor = HUDInputMonitorFactory.makeDefault(),
        hintStore: HUDHintStore = HUDHintStore(),
        micAuthorizer: MicAuthorizing = LiveMicAuthorizer()
    ) {
        self.watchdog = watchdog
        self.alertPresenter = alertPresenter
        self.cliService = cliService
        self.daemonService = daemonService
        self.stopPolicy = stopPolicy
        self.windowLifecycle = windowLifecycle
        self.inputMonitor = inputMonitor
        self.hintStore = hintStore
        self.micAuthorizer = micAuthorizer
        daemonInstalledObserver = NotificationCenter.default.addObserver(
            forName: .screenCapDaemonInstalledAndRunning,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                await self?.probeDaemon()
            }
        }
    }

    deinit {
        daemonEventTask?.cancel()
        convergenceLoopTask?.cancel()
        elapsedTimer?.invalidate()
        // No `stopPolicy.cancelAll()` here: `cancelAll` is `@MainActor` and
        // `deinit` is nonisolated, so the synchronous call won't compile under
        // strict concurrency. The drain is also unnecessary — every in-flight
        // `runStop` is launched via `Task { await self.runStop(...) }` which
        // captures `self` strongly, so `deinit` cannot fire while a stop is
        // suspended on the coordinator. The 60s / 300s timeout in
        // `StopPolicyCoordinator.waitForOneShot` is the cancellation backstop
        // for any other corner case.
        if let daemonInstalledObserver {
            NotificationCenter.default.removeObserver(daemonInstalledObserver)
        }
    }

    // MARK: - Public surface

    /// Spawn `screencap start [<name>]` and start consuming stderr events.
    ///
    /// `audio` is the New-recording sheet's explicit choice (U6): `nil` defers to
    /// the persisted `audio_default` (the plain toolbar / menu-bar path), `false`
    /// forces audio off (daemon `audio:false` / CLI `--no-audio`), `true` forces
    /// it on via the daemon body.
    func start(name: String? = nil, audio: Bool? = nil) {
        guard !state.isRecording else { return }
        // A staleness-defeating daemon kickstart is in flight: the daemon is mid
        // relaunch, so dispatching a start now would fail over to CLI and wrongly
        // demand every permission. The window is ~1s — surface a clear transient
        // reason and let the user retry rather than start against a dying daemon.
        if isDefeatingStaleness {
            lastError = "ScreenCap is applying your updated permissions — try again in a moment."
            return
        }
        if transport == .cliFallback, let permissions, !permissions.allRequiredGranted {
            // SCR-263: during a post-update helper swap the probe lands mid-swap
            // as .cliFallback with grants unverifiable — the same shape as a
            // genuine missing grant. Keying on `updateConverging` tells the two
            // apart so this block reads like the window's "Finishing update…"
            // interstitial instead of demanding permissions that aren't missing.
            lastError = updateConverging
                ? UpdateConvergenceCopy.startBlockedDuringConvergence
                : Self.requiredPermissionsErrorMessage
            return
        }
        // Daemon path: hard-block start ONLY on a denied Screen Recording grant
        // — the one permission fatal to capture (U4 decision). Accessibility /
        // Input Monitoring denials are advisory (engine backstop + capture
        // health), so they warn-and-proceed rather than gate start.
        // Indeterminate never blocks. Keyed on daemon-reported state — this
        // reintroduces a daemon-path pre-block that SCR-54 removed, but
        // legitimately: SCR-54 removed the *app-process* pre-block; this keys on
        // the *daemon's* state. Independent of the dismissed flag (R4).
        if transport == .daemon, let permissions, permissions.daemonGrants.screenRecordingDenied {
            routeToPermissionGrant(missing: [.screenRecording])
            return
        }
        apply(machine.enterStarting())

        switch transport {
        case .daemon:
            Task { await startViaDaemon(name: name, audio: audio) }
        case .cliFallback:
            startViaCLI(name: name, audio: audio)
        }
    }

    /// U6: the inline pre-spawn block reason for the New-recording sheet, or
    /// `nil` if start would proceed. Mirrors `start()`'s pre-spawn permission
    /// gates WITHOUT side effects (no modal, no state change) so the sheet can
    /// render the failing permission inline (KTD-8) rather than popping a modal
    /// over itself. The daemon path blocks only on a denied Screen Recording
    /// grant (the one permission fatal to capture); CLI-fallback requires all.
    func newRecordingBlockReason() -> String? {
        switch transport {
        case .cliFallback:
            if let permissions, !permissions.allRequiredGranted {
                // SCR-263: mirror start()'s convergence-aware copy so the sheet's
                // inline block reason doesn't contradict the update interstitial.
                return updateConverging
                    ? UpdateConvergenceCopy.startBlockedDuringConvergence
                    : Self.requiredPermissionsErrorMessage
            }
        case .daemon:
            if let permissions, permissions.daemonGrants.screenRecordingDenied {
                return Self.permissionRequiredErrorMessage(for: [.screenRecording])
            }
        }
        return nil
    }

    /// SCR-262 (plan U1): the launch-time daemon bring-up, replacing the old
    /// fire-and-forget `restartStaleDaemonIfNeeded` in `AppDelegate` racing the
    /// window's `probeDaemon()`. Sequencing is the point — the stale-daemon
    /// decision resolves BEFORE the first probe, so the probe can never adopt a
    /// daemon the kickstart is about to kill (transport `.daemon` on a process
    /// seconds from death, with no re-probe driver left).
    ///
    /// When a restart was triggered (or a persisted anchor shows a swap window
    /// from a previous process is still open), convergence state comes up and
    /// the re-probe loop drives it; the presentation policy shows the
    /// "Finishing update…" interstitial off that state. One owner, one
    /// kickstart per launch.
    func runLaunchDaemonCheck(
        restartStaleDaemon: () async -> Bool = { await DaemonInstallController.restartStaleDaemonIfNeeded() },
        defaults: UserDefaults = .standard,
        now: () -> Date = Date.init
    ) async {
        // Launch-once: the window-content `.task` re-runs whenever the singleton
        // Window is re-materialized (accessory-mode teardown → menu-bar/Dock
        // reopen). A re-run must not fire a second kickstart into the old
        // daemon's exit grace, erase the wall's update provenance, or re-anchor
        // a deadline the running loop already captured — it just re-probes so a
        // reopened window still reflects current daemon state.
        guard !launchDaemonCheckRan else {
            await probeDaemon()
            return
        }
        launchDaemonCheckRan = true
        updateConvergenceFailed = false
        let triggered = await restartStaleDaemon()
        if let anchor = Self.convergenceAnchorForLaunch(
            restartTriggered: triggered,
            persistedAnchor: defaults.object(forKey: Self.updateConvergenceAnchorKey) as? Date,
            now: now(),
            deadline: Self.updateConvergenceDeadline
        ) {
            enterConvergence(anchor: anchor, defaults: defaults)
        } else {
            defaults.removeObject(forKey: Self.updateConvergenceAnchorKey)
        }
        await probeDaemon()
        startConvergenceProbeLoopIfNeeded()
    }

    /// SCR-264: re-run the stale-daemon check after a recording ends, entering the
    /// same convergence state the launch check uses. A launch that lands while a
    /// daemon-owned recording is in flight defers the post-update swap
    /// (`restartStaleDaemonIfNeeded` no-ops during a recording) and — because the
    /// launch check is launch-once — nothing re-triggers it, so the stale daemon
    /// squats the socket for the rest of the session, 500ing every lazy-import
    /// verb until the next app relaunch.
    ///
    /// Called off the single `state` idle chokepoint on the recording→idle edge.
    /// Idempotent and self-guarding: `restartStaleDaemonIfNeeded` no-ops on a
    /// fresh daemon, so a recording that ends with the helper already current does
    /// nothing; a stale one triggers the kickstart and the convergence loop drives
    /// the swap behind the "Finishing update…" interstitial, exactly as at launch.
    /// The interstitial surfaces here because the launch gate never latched
    /// `launchPresentationDecided` during the recording (its evaluator early-returns
    /// while `state.isRecording`). Firing on every recording end (not once) also
    /// self-heals the rare case where the daemon still reports the just-ended
    /// recording on the first probe: that recheck defers, the next end retries.
    func recheckStaleDaemonAfterRecordingEnd(
        restartStaleDaemon: () async -> Bool = { await DaemonInstallController.restartStaleDaemonIfNeeded() },
        defaults: UserDefaults = .standard,
        now: () -> Date = Date.init
    ) async {
        // Only once the launch sequencing has run (the convergence state is owned
        // from launch onward), and never stack a second loop over an in-flight swap.
        guard launchDaemonCheckRan, !updateConverging else { return }
        guard await restartStaleDaemon() else { return }
        updateConvergenceFailed = false
        enterConvergence(anchor: now(), defaults: defaults)
        await probeDaemon()
        startConvergenceProbeLoopIfNeeded()
    }

    /// SCR-262 (plan U3): while converging, re-probe until the swapped-in daemon
    /// is verifiably up or the deadline passes. This is the convergence driver
    /// the suppressed wall used to be (the spurious wall auto-fired `install()`,
    /// whose success re-probed) — without it, every update launch would ride the
    /// full deadline and land on the wall anyway.
    ///
    /// The freshness probe (`daemon.info.started_at` vs the trigger anchor) is
    /// read-only; `probeDaemon()` — the single grant/transport writer — runs
    /// once, after freshness confirms.
    func startConvergenceProbeLoopIfNeeded(
        freshProbe: @escaping () async -> Double? =
            DaemonInstallController.liveDaemonStartedAtProbe,
        now: @escaping () -> Date = Date.init,
        sleep: @escaping (TimeInterval) async -> Void = {
            try? await Task.sleep(nanoseconds: UInt64(max(0, $0) * 1_000_000_000))
        }
    ) {
        guard updateConverging, convergenceLoopTask == nil,
              let anchor = updateConvergenceAnchor else { return }
        convergenceLoopTask = Task { [weak self] in
            while !Task.isCancelled {
                let probeStartedAt = await freshProbe()
                guard let self, !Task.isCancelled else { return }
                switch Self.convergenceStep(
                    probeStartedAt: probeStartedAt,
                    anchor: anchor,
                    now: now(),
                    deadline: Self.updateConvergenceDeadline
                ) {
                case .finishedFresh:
                    await self.probeDaemon()
                    self.finishUpdateConvergence(failed: false)
                    return
                case .deadlineExpired:
                    recorderLogger.error(
                        "Helper swap did not converge within \(Self.updateConvergenceDeadline, privacy: .public)s; falling through to the permission wall"
                    )
                    // Re-read reality before falling through: the launch probe
                    // may have adopted the DYING pre-swap daemon (transport
                    // frozen at .daemon, grants intact), and without a fresh
                    // probe the policy would resolve to the shell on a dead
                    // transport instead of the repair wall. A dead daemon now
                    // reads .cliFallback → wall; a live one means the swap
                    // converged after all → not a failure.
                    await self.probeDaemon()
                    self.finishUpdateConvergence(failed: self.transport != .daemon)
                    return
                case .keepWaiting:
                    await sleep(Self.updateConvergenceProbeInterval)
                }
            }
        }
    }

    /// SCR-262: enter the update-convergence state — persist the trigger anchor
    /// and flip the observable flag the presentation policy shows the interstitial
    /// off. The single entry point (symmetric with `finishUpdateConvergence`) so
    /// the launch path (`runLaunchDaemonCheck`), the post-recording recheck
    /// (SCR-264), and the loop tests can't drift as convergence fields are added.
    /// Callers own the anchor derivation and starting the re-probe loop.
    private func enterConvergence(anchor: Date, defaults: UserDefaults) {
        defaults.set(anchor, forKey: Self.updateConvergenceAnchorKey)
        convergenceDefaults = defaults
        updateConvergenceAnchor = anchor
        updateConverging = true
    }

    private func finishUpdateConvergence(failed: Bool) {
        updateConverging = false
        updateConvergenceFailed = failed
        updateConvergenceAnchor = nil
        convergenceLoopTask = nil
        convergenceDefaults.removeObject(forKey: Self.updateConvergenceAnchorKey)
    }

    func probeDaemon() async {
        defer { daemonProbeCompleted = true }
        switch await daemonService.probe() {
        case .daemon(let grants):
            schemaMismatchDetected = false
            // SCR-262: a reachable daemon makes the wall's content genuine again —
            // its rows reflect real grant state, so the wall loses its
            // "update didn't finish" provenance (KTD-8/KTD-6).
            updateConvergenceFailed = false
            // probeDaemon is the single writer of the daemon-grant snapshot
            // (U3). The walkthrough rows, the launch gate (U4), and the
            // start-block all read it from PermissionController. Push grants
            // BEFORE flipping transport so MainWindow's transport onChange
            // re-evaluates the gate against fresh daemon grant state.
            permissions?.updateDaemonGrants(grants)
            transport = .daemon
            await syncDaemonSnapshot()
        case .schemaMismatch:
            schemaMismatchDetected = true
            transport = .cliFallback
            // Daemon grant state is unknowable when we can't speak its schema —
            // fall back to indeterminate so nothing blocks on a stale snapshot.
            permissions?.updateDaemonGrants(.allIndeterminate)
        case .unavailable:
            transport = .cliFallback
            permissions?.updateDaemonGrants(.allIndeterminate)
        }
    }

    /// Re-read just the daemon's grant snapshot (U5 refresh while the
    /// walkthrough is visible). Lighter than `probeDaemon` — it does not touch
    /// transport or sync the session snapshot, so it won't disturb an in-flight
    /// recording. A transient `.unavailable` / `.schemaMismatch` keeps the
    /// last-known grants rather than thrashing them to indeterminate.
    func refreshDaemonGrants() async {
        if case .daemon(let grants) = await daemonService.probe() {
            permissions?.updateDaemonGrants(grants)
        }
    }

    /// The grant-watch refresh used by the onboarding wizard and the
    /// permission-repair takeover. Re-reads the daemon's grant snapshot and, when
    /// a required grant still reads denied, kickstarts a fresh daemon to defeat
    /// the stale grant probe (see `shouldRestartStaleDaemon` for why a restart is
    /// the only way a granted-after-launch permission becomes visible). This is
    /// what turns the "listening for permission change…" footer into something
    /// that actually detects a change the running daemon would otherwise report
    /// as still-missing forever — the bug that stranded users who had already
    /// granted Screen Recording in System Settings.
    func refreshDaemonGrantsDefeatingStaleness(now: Date = Date()) async {
        await refreshDaemonGrants()
        guard let permissions else { return }
        let secondsSinceLastRestart = lastDaemonRestartAt.map { now.timeIntervalSince($0) }
        guard Self.shouldRestartStaleDaemon(
            anyRequiredDenied: permissions.daemonGrants.anyRequiredDenied,
            isRecording: state.isRecording,
            transportIsDaemon: transport == .daemon,
            secondsSinceLastRestart: secondsSinceLastRestart,
            cooldown: Self.staleDaemonRestartCooldown
        ) else { return }
        lastDaemonRestartAt = now
        await restartDaemonToRefreshGrants()
    }

    /// Best-effort daemon kickstart used purely to defeat the stale grant probe.
    /// Unlike `reloadDaemon()` it never surfaces a user-facing `lastError`: a
    /// failed staleness restart just leaves the last-known grants in place for
    /// the next watch tick to retry. After a successful kickstart the relaunched
    /// daemon needs a moment to rebind `api.sock`, so we wait briefly before the
    /// live re-read; `refreshDaemonGrants` preserves the last-known grants on a
    /// transient miss, so the rows never flicker to "couldn't verify" and the
    /// next timer tick is the backstop if the daemon is slow to return.
    private func restartDaemonToRefreshGrants() async {
        isDefeatingStaleness = true
        defer { isDefeatingStaleness = false }
        switch await daemonService.reload() {
        case .success:
            try? await Task.sleep(nanoseconds: 800_000_000)
            await refreshDaemonGrants()
        case .failure(let error):
            recorderLogger.info(
                "Stale-grant daemon restart failed: \(String(describing: error), privacy: .public)"
            )
        }
    }

    private func syncDaemonSnapshot() async {
        switch await daemonService.snapshot() {
        case .noActiveSession, .unreachable:
            return
        case .daemonOwnedSession(let startedAt, let muted):
            // We are attaching to a pre-existing daemon-owned session we did not
            // start, so we have no started identity to bind. Clear any stale one
            // from a prior recording so the "nil startedSessionID means attach"
            // invariant that the cursor_unknown gate relies on is structural,
            // not just positional (SCR-68).
            machine.setStartedSessionID(nil)
            // SCR-254 (R6/AE4): hydrate the CONFIRMED mute state from the snapshot
            // so re-attaching to a live recording shows the correct mic state
            // without waiting for a fresh toggle.
            self.muted = muted
            apply(machine.observeActiveDaemonSession(startedAt: startedAt))
            attachDaemonEventStream()
            // The watchdog only re-checks TCC for the app process during
            // CLI-fallback recordings (see checkPermissionsDuringRecording).
            // Skip arming it on the daemon transport so we don't wake the
            // Timer and NSWorkspace observer to immediately no-op.
            if transport == .cliFallback {
                startPermissionWatchdog()
            }
        case .foreignClaimant:
            lastError = "Another process is recording."
            transitionToIdle()
        }
    }

    func reloadDaemon() async {
        switch await daemonService.reload() {
        case .success:
            schemaMismatchDetected = false
            await probeDaemon()
        case .failure(.spawnFailed(let error)):
            // Pre-refactor: launchctl process never launched — include the
            // localized description so the user sees an actionable message.
            lastError = "Failed to reload ScreenCap daemon: \(error.localizedDescription)"
        case .failure(.nonZeroExit):
            // Pre-refactor: launchctl ran but returned non-zero. The stderr
            // detail is logged inside `reload()`; surface the same terse
            // user-facing string.
            lastError = "Failed to reload ScreenCap daemon."
        }
    }

    private func startViaCLI(name: String? = nil, audio: Bool? = nil) {
        // Same shape as the outer guard in start(name:), but it covers the
        // case where `handleDaemonOperationFailure` flips transport to
        // .cliFallback and invokes us as a fallback — at that point the
        // outer guard has already passed (it gated on the prior .daemon
        // transport) so we must re-check before spawning the CLI.
        if let permissions, !permissions.allRequiredGranted {
            transitionToIdle()
            // SCR-263: a daemon start that fails over to CLI mid-swap re-enters
            // this gate as .cliFallback with grants unverifiable; key on
            // `updateConverging` here too so the failover copy stays consistent
            // with the "Finishing update…" interstitial rather than the
            // permission-required error.
            lastError = updateConverging
                ? UpdateConvergenceCopy.startBlockedDuringConvergence
                : Self.requiredPermissionsErrorMessage
            return
        }

        var args = ["start"]
        if let name { args.append(name) }
        // The CLI can only force audio OFF (`--no-audio`); an unspecified or
        // `true` choice defers to `audio_default`. Reflect the deterministic
        // part: a `false` choice is a definite audio-off, everything else is
        // treated as audio-on for the HUD (U7) — matching the daemon path's
        // "missing echo → audio-on".
        if audio == false { args.append("--no-audio") }
        audioEnabled = (audio == false) ? false : true
        // Provisional HUD title (U7): the CLI name if supplied, else "Recording"
        // (the engine auto-names an unnamed CLI capture; we learn it post-stop).
        currentRecordingName = name

        do {
            try cliService.start(
                args: args,
                onEvent: { [weak self] event in
                    self?.handleRecorderEvent(event)
                },
                onTerminated: { [weak self] exitCode in
                    self?.handleProcessTerminated(exitCode: exitCode)
                }
            )
            // Watchdog is armed imperatively (not via the effect channel)
            // because it must be gated on transport == .cliFallback; the pure
            // state machine has no visibility into transport.
            startPermissionWatchdog()
        } catch {
            transitionToIdle()
            lastError = error.localizedDescription
        }
    }

    private func startViaDaemon(name: String? = nil, audio: Bool? = nil) async {
        do {
            let started = try await daemonService.startRecording(name: name, audio: audio)
            // Reflect the effective audio state for the HUD (U7). A stale daemon
            // omits the echo → treat as audio-on (it ignored the flag and
            // recorded with audio).
            audioEnabled = started.audioEcho ?? true
            // The daemon's session id is the recording directory name (contract in
            // supervisor.py) — the HUD's provisional title (U7).
            currentRecordingName = started.sessionID
            machine.setPendingStartCursor(started.cursor)
            // Bind the started session's identity so a `cursor_unknown`
            // snapshot promotion can't attribute the UI to a foreign session
            // that took over mid-start (SCR-68).
            machine.setStartedSessionID(started.sessionID)
            attachDaemonEventStream()
            // Same rationale as syncDaemonSnapshot: on the daemon transport
            // the watchdog's check is a guarded no-op, so don't arm it.
            if transport == .cliFallback {
                startPermissionWatchdog()
            }
        } catch {
            handleDaemonOperationFailure(error, fallback: {
                self.startViaCLI(name: name, audio: audio)
            })
        }
    }

    /// In-app Stop button path. Tears the recording HUD down immediately (via
    /// `enterStopping`) so the pill never freezes on screen, then finalizes in
    /// the background: SIGTERM via `screencap stop`, await `recording_finalized`
    /// with a 60s wall-clock fallback (headroom over the daemon's own 30s
    /// `SCREENCAP_DAEMON_STOP_TIMEOUT` + SIGKILL fallback; see
    /// `StopPolicyCoordinator.runStop`). The terminal transition concludes
    /// staying backgrounded (`enterIdleStayingBackgrounded`) so a clean Stop
    /// never pulls ScreenCap to the foreground — the user keeps working
    /// uninterrupted while finalization continues invisibly.
    ///
    /// Guard is intentionally narrower than `state.isRecording`: a second
    /// click while we're already `.stopping` would dispatch a duplicate
    /// `screencap stop` and a second `runStop` task, and a watchdog tick
    /// during a Cmd+Q quit would overwrite `.stopping(quitting:true)` with
    /// `.stopping(quitting:false)` and prematurely flip state to `.idle`.
    func stop() {
        guard case .recording = state else { return }
        apply(machine.enterStopping(quitting: false))
        Task { await self.runStop(quitting: false) }
    }

    /// Cmd+Q path. Shows the stop-and-quit alert; on "Stop & Quit", returns
    /// `.terminateLater` and runs the long-wait stop policy (5min for
    /// `stopped` event).
    func confirmQuitWhileRecording() -> NSApplication.TerminateReply {
        guard state.isRecording else { return .terminateNow }

        // Re-entry while any stop is already in flight: do not stack a second
        // modal or dispatch a second runStop. This covers both `.stopping(quitting: true)`
        // (a prior Cmd+Q in flight) and `.stopping(quitting: false)` (in-app
        // Stop already running when the user pressed Cmd+Q — the upgrade
        // path is owned by enterStopping(quitting: true) below if reached
        // via a different code path; here we hold AppKit so the in-flight
        // task can drive the eventual `NSApp.reply(toApplicationShouldTerminate:)`).
        // Telling AppKit `.terminateLater` is the correct hold reply.
        if case .stopping = state {
            return .terminateLater
        }

        switch alertPresenter.confirmStopAndQuit() {
        case .terminateLater:
            apply(machine.enterStopping(quitting: true))
            quitProgressSecondsRemaining = 300
            Task { await self.runStop(quitting: true) }
            return .terminateLater
        default:
            return .terminateCancel
        }
    }

    func smokeStatus() async -> CLIStatus? {
        do {
            return try await CLIClient.runJSON(["status", "--json"])
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }

    func dismissMatrixDisclosure() {
        matrixDisclosure = nil
    }

    // MARK: - Stop policy

    /// `quitting=false`: in-app Stop, 60s wait. The HUD was already dropped on
    /// `enterStopping`, so this just finalizes in the background and concludes
    /// staying backgrounded — the UI is never blocked on the wait.
    /// `quitting=true`:  Cmd+Q, 300s wait, then NSApp.reply(...). On timeout
    /// we SIGKILL the CLI recorder (daemon transport surfaces the
    /// "may still be running" message instead) and let
    /// `.upload_followup.json` surface on next launch.
    private func runStop(quitting: Bool) async {
        // Snapshot the transport at task entry. The send-signal closure must
        // also dispatch against the same transport even if `transport` flips
        // mid-await (e.g. the daemon event stream tore down to .cliFallback
        // while the user's stop was in flight) — otherwise we'd send the
        // stop signal to a transport that doesn't own the recording.
        let isDaemon = transport == .daemon
        let outcome = await stopPolicy.runStop(
            quitting: quitting,
            timeout: nil,
            sendStopSignal: { [daemonService, isDaemon] in
                if isDaemon {
                    try await daemonService.stopRecording(force: false)
                } else {
                    _ = try CLIClient.runDetached(["stop"])
                }
            },
            onTickQuitProgress: { [weak self] remaining in
                guard let self, self.quitProgressSecondsRemaining != nil else { return }
                self.quitProgressSecondsRemaining = remaining
            }
        )

        switch outcome {
        case .sendSignalFailed(let error):
            // Daemon and CLI failure paths diverge: the daemon path needs the
            // typed-error policy from `handleDaemonOperationFailure`
            // (schemaMismatch, socketUnavailable, envelopeError) while the CLI
            // path surfaces the generic SIGTERM-dispatch message.
            if isDaemon {
                handleDaemonOperationFailure(error)
            } else {
                lastError = "Failed to send stop signal: \(error.localizedDescription). Try stopping the recording again, or quit ScreenCap if it won't stop."
            }
            if quitting {
                quitProgressSecondsRemaining = nil
                NSApp.reply(toApplicationShouldTerminate: false)
            }
            // No-op for the daemon path when `handleDaemonOperationFailure`
            // already forced state to `.idle`; only the CLI path lands in
            // `.stopping` and gets rolled back to `.recording(elapsed:)`. The
            // rollback re-emits `.showHUD` because the in-app Stop already hid
            // the pill on `enterStopping` — the recording is still live, so the
            // HUD must come back (apply() also mirrors the machine state).
            apply(machine.restoreRecordingAfterStopFailure())
        case .completed:
            finalizeStop(quitting: quitting)
        case .timedOut:
            // Cmd+Q surfaces a terminal message (the window is restored on quit
            // so it's seen, and the app is exiting so it can't go stale). The
            // in-app path surfaces NOTHING: background finalization outlasting
            // the 60s wait is normal for a long recording, not a failure, and it
            // self-resolves. Writing it to `lastError` (the terminal-failure
            // channel, never auto-cleared — see the `state.didSet` idle
            // chokepoint) left a stale red banner over the Library long after the
            // recording finished uploading. The per-recording Library status
            // chips already carry the finalization signal.
            if quitting {
                if isDaemon {
                    lastError = "Stop timed out after 5 minutes; recorder finalization may still be running."
                } else if cliService.currentProcess?.forceKill() == true {
                    // `forceKill()` returns true only when the process was
                    // running with a valid PID — the same `isRunning` + `pid > 0`
                    // guard the inline `kill(...)` used to apply, now scoped
                    // to the SpawnedProcessHandle so a recycled PID from an
                    // unrelated process cannot be signalled.
                    lastError = "Stop timed out after 5 minutes; recorder force-killed."
                }
            }
            finalizeStop(quitting: quitting)
        }
    }

    /// Shared stop-completion sequence, transitioning the machine to `.idle`.
    /// The two stop paths conclude differently:
    /// - Cmd+Q restores + activates the window (`enterIdle`) so any final message
    ///   is visible, then tells AppKit it may finish terminating.
    /// - In-app Stop concludes staying backgrounded (`enterIdleStayingBackgrounded`)
    ///   so a clean Stop never steals focus from the app the user moved on to —
    ///   the HUD was already dropped on `enterStopping`, so this just routes the
    ///   still-hidden window to Library. apply() mirrors `.idle` into `state`.
    private func finalizeStop(quitting: Bool) {
        if quitting {
            quitProgressSecondsRemaining = nil
            apply(machine.enterIdle())
            NSApp.reply(toApplicationShouldTerminate: true)
        } else {
            apply(machine.enterIdleStayingBackgrounded())
        }
    }

    // MARK: - Event / process callbacks

    private func handleRecorderEvent(_ event: RecorderEventLine) {
        // SCR-254: mute is ORTHOGONAL to the recording lifecycle (idle/starting/
        // recording/stopping), so the confirmed mute events are intercepted here
        // rather than modelled as state-machine states. Each updates the mute
        // surface directly; we STILL call through to the machine below (these types
        // decode to the machine's `default` → no effects), so lifecycle handling is
        // unchanged.
        switch event.type {
        case "audio_muted":
            // Confirmed: capture actually STOPPED (KTD4).
            muted = true
            muteInFlight = false
        case "audio_unmuted":
            // Confirmed: capture actually STARTED/resumed (KTD4). The mic is now
            // live for the remainder — including a recording that started audio-off
            // (R2) — so reflect it in `audioEnabled`, which seeds the effective-mute
            // display so the control reads "Mic on" after unmuting a --no-audio run.
            muted = false
            audioEnabled = true
            muteInFlight = false
        case "audio_unmute_failed":
            // ADVISORY, NON-terminal (KTD4/R3): the engine could not acquire the
            // mic on unmute, so the recording keeps running MUTED. Clear the pending
            // flag, keep `muted = true`, and surface a non-terminal advisory so the
            // failure is never silent. Emitted by the engine on a denied/failed
            // unmute (recorder.py _apply_audio_mute_command -> audio_unmute_failed).
            muteInFlight = false
            muted = true
            if case .recording = state {
                captureAdvisory = Self.microphoneUnmuteFailedAdvisory
            }
        case "audio_mute_failed":
            // ADVISORY, NON-terminal (SCR-271): a raise DURING the mute toggle (a
            // faulting DB write or a stream-stop PortAudioError) — symmetric to
            // `audio_unmute_failed`. Without this terminal event the `muteInFlight`
            // guard, cleared ONLY here, would strand on "Muting…" until the
            // recording ends. A failed stop means the stream is still capturing, so
            // keep `muted = false` and surface a non-terminal advisory. Emitted by
            // recorder.py _apply_audio_mute_command -> audio_mute_failed.
            muteInFlight = false
            muted = false
            if case .recording = state {
                captureAdvisory = Self.microphoneMuteFailedAdvisory
            }
        default:
            break
        }
        apply(machine.handle(event: event))
    }

    /// Drains the state machine's effect list against `RecorderController`'s
    /// `@Published` surface and side-effecting collaborators. Mirrors
    /// `machine.state` into `state` so SwiftUI observers see the transition.
    private func apply(_ effects: [RecordingStateMachine.Effect]) {
        for effect in effects {
            switch effect {
            case .startElapsedTimer:
                startElapsedTimer()
            case .stopElapsedTimer:
                elapsedTimer?.invalidate()
                elapsedTimer = nil
            case .stopPermissionWatchdog:
                stopPermissionWatchdog()
            case .resolveAwaiting(.finalized, let success):
                stopPolicy.resolveFinalized(success)
            case .resolveAwaiting(.stopped, let success):
                stopPolicy.resolveStopped(success)
            case .surfaceError(let message):
                lastError = message
            case .clearError:
                lastError = nil
                // captureAdvisory clearing is owned by the `state.didSet` idle
                // chokepoint: it's only set during `.recording` and `.clearError`
                // is only emitted by enterStarting() (from `.idle`, where the
                // advisory was already cleared), so no clear is needed here.
            case .setMatrixDisclosure(let disclosure):
                matrixDisclosure = disclosure
            case .refreshIndex:
                Task { await self.index?.refresh() }
            case .handlePermissionLost(let permission):
                handlePermissionLost(permission: permission)
            case .handlePermissionRequired(let missing):
                handlePermissionRequired(missing: missing)
            case .handleCaptureUnhealthy(let reason, let reader):
                handleCaptureUnhealthy(reason: reason, reader: reader)
            case .handleCaptureRecovered(let reader):
                handleCaptureRecovered(reader: reader)
            case .showHUD:
                windowLifecycle.showHUD(for: self)
                // Start the ⌘⇧H hotkey + peek detector for this recording (U2/U3).
                // `.showHUD` also fires on the daemon-attach path — a recording is
                // live there too, and startMonitoring is idempotent.
                inputMonitor.startMonitoring(for: self)
            case .hideHUD:
                windowLifecycle.hideHUD()
                inputMonitor.stopMonitoring()
                // Tear down the one-time hint too, so it never outlives the
                // recording (its "Still recording" copy would otherwise linger).
                windowLifecycle.dismissHideHint()
            case .hideMainWindow:
                windowLifecycle.hideMainWindow()
                mainWindowHidden = true
            case .restoreMainWindow:
                currentRecordingName = nil
                restoreMainWindowIfHidden()
            case .endRecordingInBackground:
                // Stay-put teardown for the in-app Stop: the recording is over,
                // but we must NOT pull ScreenCap to the foreground. Clear the
                // recording title, then — only if the main window was hidden for
                // the recording — drop that bookkeeping (so the terminal
                // enterIdle's restore and this effect stay no-ops) and route the
                // still-hidden window to Library, so the new recording is there
                // when the user reopens ScreenCap on their own terms. Deliberately
                // skips `windowLifecycle.restoreMainWindow()` (its orderFront +
                // NSApp.activate is the focus steal we're avoiding). The
                // `mainWindowHidden` guard mirrors `restoreMainWindowIfHidden`, so
                // a `.starting` failure or HUD-only attach session doesn't reroute.
                currentRecordingName = nil
                if mainWindowHidden {
                    mainWindowHidden = false
                    NotificationCenter.default.post(name: .screenCapRecordingDidEnd, object: nil)
                }
            }
        }
        state = machine.state
    }

    /// Hide control (R1, R2): dismiss the floating recording HUD pill while a
    /// recording is live. No-op outside `.recording` (the pill only exists then,
    /// and the guard also blocks a menu/UI race on the `.recording → .idle` edge)
    /// or when it is already hidden — idempotent and symmetric with
    /// `showRecordingHUD()`. Capture is untouched; only the panel is ordered out.
    /// The menu-bar glyph and Stop item remain the recording indicator and stop
    /// path (R3).
    func hideRecordingHUD() {
        guard case .recording = state, !hudHidden else { return }
        hudHidden = true
        windowLifecycle.hideHUD()
        // First-ever hide: point the user at the menu bar, where the recording
        // status and Stop now live (U5, R7). The store flag is set only after the
        // hint has actually been shown (the panel calls back on dismissal), so a
        // distracted first-timer isn't silently robbed of the one guidance moment.
        if HUDHintPolicy.shouldShow(hasShownBefore: hintStore.hasShownHideHint) {
            windowLifecycle.presentHideHint { [weak self] in
                self?.hintStore.markShown()
            }
        }
    }

    /// Toggle the recording HUD pill's visibility — the ⌘⇧H entry point (R1).
    /// Hides the pill when shown, restores it when hidden. No-op outside
    /// `.recording`. Routes through `hideRecordingHUD()` / `showRecordingHUD()` so
    /// the pill button, ⌘⇧H, the peek, and the menu item all read the single
    /// `hudHidden` source of truth.
    func toggleRecordingHUD() {
        guard case .recording = state else { return }
        if hudHidden {
            showRecordingHUD()
        } else {
            hideRecordingHUD()
        }
    }

    /// Restore the HUD pill after `hideRecordingHUD()` (R4, R5), driven by the
    /// menu-bar "Show recording controls" item. No-op unless a recording is live
    /// and the pill is currently hidden. `showHUD` re-creates the panel and
    /// repositions it bottom-center (R5).
    func showRecordingHUD() {
        guard case .recording = state, hudHidden else { return }
        hudHidden = false
        windowLifecycle.showHUD(for: self)
    }

    // MARK: - Mute control (SCR-254)

    /// Toggle the recording's microphone capture (R4/R5/R7). Daemon-only (the
    /// CLI-fallback transport has no live control channel, KTD1) and only while a
    /// recording is live; a no-op while a prior toggle is still in flight so a
    /// double-tap can't dispatch two requests. Muting stops capture and needs no
    /// permission; UNMUTING may start capture, so it is gated on the app's mic TCC
    /// (U9). `muted` is NEVER flipped optimistically here — the confirming
    /// `audio_muted` / `audio_unmuted` event does that, so the UI can't show a
    /// state capture never reached (KTD4).
    func toggleMute(source: MuteToggleSource = .hud) {
        guard transport == .daemon, state.isRecording, !muteInFlight else { return }
        if MuteControlPresentation.effectivelyMuted(muted: muted, audioEnabled: audioEnabled) {
            beginUnmute(source: source)
        } else {
            dispatchMuteRequest(true)
        }
    }

    /// Unmute path (U9): unmuting can turn the mic *on* (R2), so gate on the app's
    /// own microphone TCC first. Authorized → send; undetermined → prompt, then
    /// send on grant; denied/restricted → stay muted, surface a VISIBLE denial, and
    /// do NOT send the verb (R3 — never silent).
    private func beginUnmute(source: MuteToggleSource) {
        switch micAuthorizer.authorizationStatus() {
        case .authorized:
            dispatchMuteRequest(false)
        case .notDetermined:
            // Arm the in-flight guard for the whole permission-prompt window so a
            // second tap can't re-enter beginUnmute and fire a duplicate
            // requestAccess (toggleMute gates on !muteInFlight). Cleared on
            // grant -> dispatch (which re-arms it), on denial
            // (surfaceMicUnmuteDenied clears it), or if the recording ended while
            // the prompt was up.
            muteInFlight = true
            Task { [weak self] in
                guard let self else { return }
                let granted = await self.micAuthorizer.requestAccess()
                // The recording may have ended while the prompt was up.
                guard self.state.isRecording else {
                    self.muteInFlight = false
                    return
                }
                if granted {
                    self.dispatchMuteRequest(false)
                } else {
                    self.surfaceMicUnmuteDenied(source: source)
                }
            }
        case .denied, .restricted:
            surfaceMicUnmuteDenied(source: source)
        @unknown default:
            surfaceMicUnmuteDenied(source: source)
        }
    }

    /// Arm the in-flight flag and dispatch the mute-verb. The confirming event —
    /// never the response echo — settles `muted` and clears `muteInFlight`.
    private func dispatchMuteRequest(_ target: Bool) {
        muteInFlight = true
        Task { [weak self] in await self?.sendMuteRequest(target) }
    }

    private func sendMuteRequest(_ target: Bool) async {
        do {
            // The echo is a transport ack, not confirmation — deliberately ignored
            // (KTD4). The confirming `audio_muted` / `audio_unmuted` event flips
            // `muted` and clears `muteInFlight`.
            _ = try await daemonService.setMuted(target)
        } catch {
            // A mute-verb failure must NOT end the recording (contrast
            // handleDaemonOperationFailure, which idles). Clear the pending flag,
            // keep the prior confirmed `muted`, and surface a non-terminal advisory
            // so the mic state reads as unchanged and the user can retry.
            muteInFlight = false
            if case .recording = state {
                captureAdvisory = Self.muteRequestFailedAdvisory
            }
        }
    }

    /// Surface a denied-mic unmute VISIBLY while staying muted (U9/R3). The HUD
    /// pill renders no inline error and the main window is hidden during a
    /// recording, so the modal is the one surface guaranteed visible — always
    /// present it, and auto-reveal a hidden pill (or one the user isn't looking at,
    /// having acted from the menu bar) so the denial has an on-screen anchor. The
    /// advisory is set too as a persistent trace for the menu dropdown / restored
    /// window after the modal is dismissed.
    private func surfaceMicUnmuteDenied(source: MuteToggleSource) {
        // A denial ends the toggle: clear any in-flight guard armed for the
        // permission-prompt window (harmless no-op on the already-denied path,
        // which never armed it).
        muteInFlight = false
        if case .recording = state {
            captureAdvisory = Self.microphoneAccessDeniedAdvisory
        }
        if source == .menuBar || hudHidden {
            showRecordingHUD()
        }
        alertPresenter.presentMicrophoneAccessDenied { [weak self] in
            self?.permissions?.openSystemSettings(for: .microphone)
        }
    }

    private func attachDaemonEventStream() {
        daemonEventTask?.cancel()
        daemonEventTask = Task { [weak self] in
            guard let self else { return }
            let outcome = await self.daemonService.consumeEventStream(
                callbacks: .init(
                    onEvent: { [weak self] event in self?.handleRecorderEvent(event) },
                    onTransientWarning: { [weak self] message in self?.lastError = message },
                    getPendingStartCursor: { [weak self] in self?.machine.pendingStartCursor },
                    clearPendingStartCursor: { [weak self] in self?.machine.clearPendingStartCursor() },
                    getStartedSessionID: { [weak self] in self?.machine.startedSessionID },
                    isRecording: { [weak self] in self?.state.isRecording ?? false },
                    onSnapshotConfirmedActiveRecording: { [weak self] startedAt, muted in
                        // `.starting`-only — mirrors syncDaemonSnapshot's
                        // recovery path; no-op past `.starting` so it can't
                        // regress `.recording` (clobbering elapsed) or `.stopping`.
                        guard let self else { return }
                        // SCR-254 (R6/AE4): the snapshot is CONFIRMED state, so
                        // re-hydrate `muted` on every reconnect regardless of the
                        // lifecycle promotion below — a reconnect while already
                        // `.recording` must still catch up to the daemon's mute
                        // state if the confirming event aged out of replay.
                        self.muted = muted
                        if case .starting = self.state {
                            // We started this session (`.starting`) but missed the
                            // `started` event (cursor-unknown recovery); treat it as
                            // a real start and hide the main window too, so the
                            // window-hide doesn't depend on which path wins (U7).
                            self.apply(self.machine.observeActiveDaemonSession(
                                startedAt: startedAt, hideMainWindow: true
                            ))
                        }
                    }
                )
            )
            self.applyDaemonStreamOutcome(outcome)
        }
    }

    private func applyDaemonStreamOutcome(_ outcome: DaemonSession.AttachOutcome) {
        switch outcome {
        case .shutdown:
            // Stream closed for daemon shutdown without a terminal state change.
            // Clear the started identity so a later attach can't inherit it and
            // mistake a foreign session for ours (SCR-68); the attach-path reset
            // in syncDaemonSnapshot is the primary guard, this is belt-and-suspenders.
            machine.setStartedSessionID(nil)
            return
        case .foreignClaimant:
            lastError = "Another process is recording."
            transitionToIdle()
        case .sessionEnded:
            transitionToIdle()
            lastError = "Recording ended."
        case .lostContact:
            transitionToIdle()
            lastError = "Lost contact with daemon"
        case .fatalError(let failure):
            applyDaemonFailureOutcome(failure)
        }
    }

    /// Force the state machine to `.idle` and mirror to `@Published state`.
    /// Use after transport-level rollbacks (foreign claimant, daemon failure,
    /// stream loss) that don't flow through the effect channel.
    private func transitionToIdle() {
        // U7: if a recording was active, an abnormal end (foreign claimant, daemon
        // failure, stream loss) must also close the HUD and restore the main
        // window. Gated on `wasRecording` so a defensive transitionToIdle outside a
        // recording doesn't touch the HUD; the restore itself is further gated on
        // `mainWindowHidden` so a `.starting` failure (pre-HUD) or a HUD-only
        // attach session doesn't spuriously re-activate + reroute.
        let wasRecording = machine.state.isRecording
        machine.forceState(.idle)
        state = machine.state
        if wasRecording {
            windowLifecycle.hideHUD()
            // Abnormal-end paths (foreign claimant, daemon failure, stream loss)
            // reach here imperatively, bypassing the `.hideHUD` effect arm — so the
            // input monitor must also be stopped here, or ⌘⇧H stays registered
            // after the recording ends (R3).
            inputMonitor.stopMonitoring()
            // Same for the one-time hint — dismiss it so it can't linger past the
            // recording with now-false "Still recording" copy.
            windowLifecycle.dismissHideHint()
            currentRecordingName = nil
            restoreMainWindowIfHidden()
        }
    }

    /// Restore the main window + route to Library ONLY when it was actually hidden
    /// (the live `started` path set `mainWindowHidden`). No-op otherwise, so a
    /// `.starting` failure or a HUD-only attach-session stop leaves the user's
    /// visible window and navigation untouched (U7). Idempotent.
    private func restoreMainWindowIfHidden() {
        guard mainWindowHidden else { return }
        mainWindowHidden = false
        windowLifecycle.restoreMainWindow()
        // Route the reopened window to Library (U7) — MainWindow observes.
        NotificationCenter.default.post(name: .screenCapRecordingDidEnd, object: nil)
    }

    private func handleDaemonOperationFailure(_ error: Error, fallback: (() -> Void)? = nil) {
        applyDaemonFailureOutcome(daemonService.translateFailure(error), fallback: fallback)
    }

    private func applyDaemonFailureOutcome(
        _ outcome: DaemonSession.FailureOutcome,
        fallback: (() -> Void)? = nil
    ) {
        switch outcome {
        case .schemaMismatch:
            schemaMismatchDetected = true
            transport = .cliFallback
            transitionToIdle()
            lastError = "ScreenCap daemon needs to reload."
        case .socketUnavailable:
            // The underlying error description is logged inside
            // `DaemonSessionService.translateFailure` before the typed
            // outcome strips it; we only emit the typed transition here.
            transport = .cliFallback
            // Without resetting `state`, a failed start leaves the controller
            // stuck in `.starting`; surface the failure to the user and clear
            // the in-flight state so a retry (or CLI fallback) can take over.
            transitionToIdle()
            lastError = "Daemon socket unavailable"
            fallback?()
        case .lockContended:
            lastError = "ScreenCap is already recording."
            if state.isRecording { transitionToIdle() }
        case .permissionRequired(let missing):
            // The daemon rejected the start before spawn (U6). Do NOT fall back
            // to the CLI path — route into the grant flow instead, mirroring the
            // client-side start-block (U4). No engine spawned, so no duplicate
            // permission_lost for this attempt.
            transitionToIdle()
            routeToPermissionGrant(missing: privacyPanes(fromMissing: missing))
        case .other(let description):
            lastError = description
            if state.isRecording { transitionToIdle() }
        }
    }

    private func handleProcessTerminated(exitCode: Int32) {
        // `cliService` clears `currentProcess` from inside its own onTerminated
        // hook before invoking this callback, so no nil-out needed here.
        daemonEventTask?.cancel()
        daemonEventTask = nil
        apply(machine.processTerminated(exitCode: exitCode))
    }

    /// Build the actionable "permission required before recording" message.
    static func permissionRequiredErrorMessage(for panes: [PrivacyPane]) -> String {
        let names = panes.isEmpty
            ? "a required permission"
            : panes.map(\.displayName).joined(separator: ", ")
        return "Grant \(names) to ScreenCap before recording."
    }

    /// Surface an actionable message and route the user into the grant flow
    /// (System Settings for the missing pane) WITHOUT dispatching a
    /// recording.start. Shared by the client-side daemon start-block (U4) and
    /// the server-side typed `permission_required` failure (U6) so both
    /// start-time permission failures present one consistent surface.
    private func routeToPermissionGrant(missing panes: [PrivacyPane]) {
        let primary = panes.first ?? .screenRecording
        lastError = Self.permissionRequiredErrorMessage(for: panes)
        alertPresenter.presentPermissionRequired(
            permissions: panes.map(\.displayName)
        ) { [weak self] in
            self?.permissions?.openSystemSettings(for: primary)
        }
    }

    /// Route a CLI-fallback start-time `permission_required` block (SCR-142)
    /// into the SAME precise grant flow the daemon transport's typed
    /// `permission_required` failure already uses (U6). The engine's daemon
    /// pre-spawn gate named every denied permission in `missing`; mapping each
    /// to its `PrivacyPane` lets the alert name them exactly instead of the
    /// hedged "Screen Recording, Accessibility, or Input Monitoring" fallback.
    /// No recording is in flight (the start was blocked before spawn), so this
    /// only surfaces + routes — `processTerminated` drops the state to `.idle`.
    private func handlePermissionRequired(missing: [String]) {
        routeToPermissionGrant(missing: privacyPanes(fromMissing: missing))
    }

    /// Map the raw daemon `missing` permission strings to their `PrivacyPane`s,
    /// falling back to `[.screenRecording]` when the list is empty so the
    /// grant flow always names at least the permission fatal to capture. Single
    /// owner of that empty-fallback, shared by the daemon-transport typed
    /// `permission_required` failure (U6) and the CLI-fallback stderr event
    /// (SCR-142).
    private func privacyPanes(fromMissing missing: [String]) -> [PrivacyPane] {
        missing.isEmpty
            ? [PrivacyPane.screenRecording]
            : missing.map { PrivacyPane.from(permissionString: $0) }
    }

    private func handlePermissionLost(permission: String?) {
        // Resolve the raw daemon token (e.g. "screen_recording") to its
        // user-facing display name ("Screen Recording") before it reaches the
        // modal/status copy. Keep the nil fallback so the unknown-permission
        // case doesn't falsely name a specific permission (SCR-87).
        let pane = permission.map { PrivacyPane.from(permissionString: $0) }
        let perm = pane?.displayName ?? "a required permission"
        lastError = "Recording stopped: \(perm) was revoked."

        // Initiate the stop BEFORE blocking on the modal, so the engine
        // teardown proceeds in parallel with the user reading the dialog.
        // Without this, the in-app 60s timeout in `awaitFinalizedEvent` can
        // fire while the modal is up and report a false "still finalizing"
        // message even though the recorder has cleanly shut down.
        if case .recording = state {
            stop()
        }

        alertPresenter.presentPermissionLost(permission: perm) { [weak self] in
            self?.permissions?.openSystemSettings(for: pane ?? .screenRecording)
        }
    }

    /// Surface the advisory, NON-terminal capture-health notice (SCR-76).
    ///
    /// Unlike `handlePermissionLost`, this never calls `stop()` — the engine
    /// keeps recording and the signal is purely advisory (the cause is non-TCC
    /// or could not be attributed). It is presented via the distinct
    /// `captureAdvisory` channel (no blocking modal), updated in place to match
    /// the engine's once-per-edge emission. Guarded to `.recording` so a late
    /// event during teardown (`.stopping` / `.idle`) does not present — mirrors
    /// `handlePermissionLost`'s `if case .recording = state` guard.
    private func handleCaptureUnhealthy(reason: String?, reader: String?) {
        guard case .recording = state else { return }
        // Track this reader as currently-unhealthy (recency order, dedup) and
        // recompute the hint. Multiple readers can be unhealthy at once, so the
        // tracked readers decide when the advisory clears; the message names the
        // latest one.
        let key = reader ?? "capture"
        unhealthyReaders.removeAll { $0 == key }
        unhealthyReaders.append(key)
        refreshCaptureAdvisory()
    }

    /// Clear the advisory for a reader that recovered mid-recording (SCR-100).
    ///
    /// Paired with `handleCaptureUnhealthy`: the engine emits `capture_recovered`
    /// once when a previously-unhealthy reader returns healthy. Removing it from
    /// the tracked set clears the advisory only when no reader remains unhealthy,
    /// so a transient blip on one reader no longer leaves the hint up for the
    /// rest of the recording. Guarded to `.recording` for symmetry with
    /// `handleCaptureUnhealthy` — during teardown the `.idle` chokepoint clears
    /// everything anyway.
    private func handleCaptureRecovered(reader: String?) {
        guard case .recording = state else { return }
        let key = reader ?? "capture"
        unhealthyReaders.removeAll { $0 == key }
        refreshCaptureAdvisory()
    }

    /// Recompute `captureAdvisory` from the tracked unhealthy readers: `nil`
    /// when none remain, otherwise a hint naming the most-recent reader. Updated
    /// in place (no re-pop) to match the engine's once-per-edge emission.
    private func refreshCaptureAdvisory() {
        guard let latest = unhealthyReaders.last else {
            captureAdvisory = nil
            return
        }
        let what: String
        switch latest {
        case "screen": what = "Screen capture"
        case "window": what = "Window capture"
        case "action": what = "Input capture"
        default: what = "Capture"
        }
        captureAdvisory =
            "\(what) may not be recording correctly. If this persists, check "
            + "Privacy & Security settings, or stop and restart the recording."
    }

    // MARK: - Timers

    private func startElapsedTimer() {
        elapsedTimer?.invalidate()
        // `.common` mode keeps the elapsed clock ticking while the menu bar
        // dropdown or any NSAlert is up. `Timer.scheduledTimer` defaults to
        // `.default` mode, which pauses for those event-tracking modes.
        let timer = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.tickElapsed() } }
        }
        RunLoop.main.add(timer, forMode: .common)
        elapsedTimer = timer
    }

    private func tickElapsed() {
        machine.tickElapsed()
        state = machine.state
    }

    private func startPermissionWatchdog() {
        watchdog.start { [weak self] in
            self?.checkPermissionsDuringRecording()
        }
    }

    private func stopPermissionWatchdog() {
        watchdog.stop()
    }

    private func checkPermissionsDuringRecording() {
        // Daemon-backed recordings are owned by the helper process, so the
        // Swift app's cached TCC state is not authoritative. The daemon event
        // stream reports helper-side permission failures via `permission_lost`.
        guard transport == .cliFallback else { return }

        // Only act while actively recording — once we're already `.stopping`,
        // calling `stop()` again would either be a no-op (covered by the
        // narrowed guard in `stop()`) or, before that guard existed, would
        // overwrite a Cmd+Q quit with an in-app stop.
        guard case .recording = state, let permissions else { return }
        permissions.refresh()
        if !permissions.allRequiredGranted {
            // Engine-side will also detect via the black-frame check (Unit 8).
            // Trigger a stop from our side too — belt-and-suspenders.
            lastError = "A required permission was revoked. Stopping recording."
            stop()
        }
    }
}

#if DEBUG
extension RecorderController {
    func _testSetPresentation(
        state: RecordingState = .idle,
        lastError: String? = nil,
        quitProgressSecondsRemaining: Int? = nil
    ) {
        // Drive the state machine into the requested state so subsequent
        // event processing observes a coherent view. Without this the
        // machine stays at `.idle` while the controller publishes a
        // different value, and the first `apply(...)` would clobber the
        // published state with the machine's stale `.idle`.
        machine.forceState(state)
        self.state = state
        self.lastError = lastError
        self.quitProgressSecondsRemaining = quitProgressSecondsRemaining
        self.matrixDisclosure = nil
    }

    func _testHandleStderrLine(_ line: String) {
        // Route through the same parser the CLI service uses in production
        // so this shim stays representative of the live stderr → event path.
        if let event = RecorderEventLine.parse(stderrLine: line) {
            handleRecorderEvent(event)
        }
    }

    func _testHandleProcessTerminated(exitCode: Int32) {
        handleProcessTerminated(exitCode: exitCode)
    }

    func _testSetTransport(_ transport: RecorderTransport) {
        self.transport = transport
    }

    func _testSetDefeatingStaleness(_ value: Bool) {
        isDefeatingStaleness = value
    }

    func _testCheckPermissionsDuringRecording() {
        checkPermissionsDuringRecording()
    }

    /// Cancel any in-flight daemon event stream Task so tests can tear
    /// down their fake server without waiting on a long-poll read that may
    /// currently be parked inside Network.framework.
    func _testCancelDaemonTask() async {
        let task = daemonEventTask
        daemonEventTask = nil
        transitionToIdle()
        task?.cancel()
        try? await Task.sleep(nanoseconds: 50_000_000)
    }

    /// SCR-262: enter convergence directly so loop tests can inject their own
    /// probe/clock/sleep via `startConvergenceProbeLoopIfNeeded` without going
    /// through `runLaunchDaemonCheck` (which starts the live-probe loop itself).
    /// Routes through the same `enterConvergence` entry the production paths use.
    func _testBeginUpdateConvergence(anchor: Date, defaults: UserDefaults) {
        enterConvergence(anchor: anchor, defaults: defaults)
    }

    /// SCR-262: tear down a convergence loop a test left running (e.g. after
    /// exercising `runLaunchDaemonCheck`, whose live probe no-ops under XCTest
    /// so its loop would idle-wait out the deadline).
    func _testCancelConvergenceLoop() {
        convergenceLoopTask?.cancel()
        convergenceLoopTask = nil
    }
}
#endif
