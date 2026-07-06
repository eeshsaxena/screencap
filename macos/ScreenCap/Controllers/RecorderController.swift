import AppKit
import Combine
import Foundation
import OSLog

/// Drift-detection log for the stderr event contract with `_stderr_events.py`.
/// Tail with: `log stream --predicate 'subsystem == "com.screencap.macos"'`.
private let recorderLogger = Logger(subsystem: "com.screencap.macos", category: "recorder")
let SUPPORTED_API_SCHEMA_VERSION = 1

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
        case ts
    }
}

enum RecorderTransport: Equatable {
    case daemon
    case cliFallback
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
    /// Surfaced in the menu bar dropdown during a Cmd+Q stop. Counts down
    /// from 300s while we wait for the `stopped` event.
    @Published private(set) var quitProgressSecondsRemaining: Int?
    /// U6/U7: the EFFECTIVE audio state of the current recording — read by the
    /// HUD's mic indicator. Set at start from the daemon's `audio` echo (or the
    /// CLI's deterministic `--no-audio` choice); a stale daemon that omits the
    /// echo is treated as audio-on. Defaults to `true` between recordings.
    @Published private(set) var audioEnabled: Bool = true
    /// U7: the provisional recording name shown on the HUD title (the daemon
    /// session id / CLI name — the directory slug until post-stop auto-naming
    /// renames it; user rename is SCR-223). `nil` → the HUD shows "Recording".
    @Published private(set) var currentRecordingName: String?
    /// Hide control: true while the user has dismissed the recording HUD pill via
    /// its hide control. Distinct from the recording lifecycle — the pill's hide
    /// button and the menu-bar "Show recording controls" item both read this.
    /// Reset to `false` on every return to `.idle` (the `state.didSet` chokepoint)
    /// so each recording starts shown and no hidden state survives a recording,
    /// and defensively when `.showHUD` is applied.
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
        windowLifecycle: WindowLifecycle = WindowLifecycleFactory.makeDefault()
    ) {
        self.watchdog = watchdog
        self.alertPresenter = alertPresenter
        self.cliService = cliService
        self.daemonService = daemonService
        self.stopPolicy = stopPolicy
        self.windowLifecycle = windowLifecycle
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
            lastError = Self.requiredPermissionsErrorMessage
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
                return Self.requiredPermissionsErrorMessage
            }
        case .daemon:
            if let permissions, permissions.daemonGrants.screenRecordingDenied {
                return Self.permissionRequiredErrorMessage(for: [.screenRecording])
            }
        }
        return nil
    }

    func probeDaemon() async {
        defer { daemonProbeCompleted = true }
        switch await daemonService.probe() {
        case .daemon(let grants):
            schemaMismatchDetected = false
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
        case .daemonOwnedSession(let startedAt):
            // We are attaching to a pre-existing daemon-owned session we did not
            // start, so we have no started identity to bind. Clear any stale one
            // from a prior recording so the "nil startedSessionID means attach"
            // invariant that the cursor_unknown gate relies on is structural,
            // not just positional (SCR-68).
            machine.setStartedSessionID(nil)
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
            lastError = Self.requiredPermissionsErrorMessage
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

    /// In-app Stop button path. SIGTERM via `screencap stop`, await
    /// `recording_finalized` with a 60s wall-clock fallback (headroom over the
    /// daemon's own 30s `SCREENCAP_DAEMON_STOP_TIMEOUT` + SIGKILL fallback;
    /// see `StopPolicyCoordinator.runStop`), then transition UI to `.idle`.
    /// Background finalization continues invisibly.
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

    /// `quitting=false`: in-app Stop, 60s wait, transition UI to .idle and
    /// continue background finalization invisibly.
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
                lastError = "Failed to send stop signal: \(error.localizedDescription). Try `screencap stop` in a terminal."
            }
            if quitting {
                quitProgressSecondsRemaining = nil
                NSApp.reply(toApplicationShouldTerminate: false)
            }
            // No-op for the daemon path when `handleDaemonOperationFailure`
            // already forced state to `.idle`; only the CLI path lands in
            // `.stopping` and gets rolled back to `.recording(elapsed:)`.
            machine.restoreRecordingAfterStopFailure()
            state = machine.state
        case .completed:
            finalizeStop(quitting: quitting)
        case .timedOut:
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
            } else {
                lastError = "Stop is still finalizing in the background."
            }
            finalizeStop(quitting: quitting)
        }
    }

    /// Shared stop-completion sequence: clear the Cmd+Q countdown, transition
    /// the machine to `.idle`, and (for Cmd+Q) tell AppKit it may terminate.
    private func finalizeStop(quitting: Bool) {
        if quitting { quitProgressSecondsRemaining = nil }
        // enterIdle() emits the HUD close + main-window restore (U7); apply drains
        // them and mirrors the machine's `.idle` into `state`.
        apply(machine.enterIdle())
        if quitting { NSApp.reply(toApplicationShouldTerminate: true) }
    }

    // MARK: - Event / process callbacks

    private func handleRecorderEvent(_ event: RecorderEventLine) {
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
                // Every recording-start edge (`started`, daemon-session attach)
                // drains here; clear any hidden state so the pill shows and the
                // menu-bar restore item is hidden at the start of a recording.
                hudHidden = false
                windowLifecycle.showHUD(for: self)
            case .hideHUD:
                windowLifecycle.hideHUD()
            case .hideMainWindow:
                windowLifecycle.hideMainWindow()
                mainWindowHidden = true
            case .restoreMainWindow:
                currentRecordingName = nil
                restoreMainWindowIfHidden()
            }
        }
        state = machine.state
    }

    /// Hide control (R1, R2): dismiss the floating recording HUD pill while a
    /// recording is live. No-op outside `.recording` — the pill only exists then,
    /// and the guard keeps a menu/UI race on the `.recording → .idle` edge from
    /// acting. Capture is untouched; only the panel is ordered out. The menu-bar
    /// glyph and Stop item remain the recording indicator and stop path (R3).
    func hideRecordingHUD() {
        guard case .recording = state else { return }
        hudHidden = true
        windowLifecycle.hideHUD()
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
                    onSnapshotConfirmedActiveRecording: { [weak self] startedAt in
                        // `.starting`-only — mirrors syncDaemonSnapshot's
                        // recovery path; no-op past `.starting` so it can't
                        // regress `.recording` (clobbering elapsed) or `.stopping`.
                        guard let self else { return }
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
}
#endif
