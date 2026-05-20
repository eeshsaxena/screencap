import AppKit
import Combine
import Darwin
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
    let changes: [String]?
    let optOutCommandExamples: [String]?
    let cursor: Int?
    let reason: String?
    let ts: Double?

    enum CodingKeys: String, CodingKey {
        case type
        case schemaVersion = "schema_version"
        case forceStopped = "force_stopped"
        case permission
        case changes
        case optOutCommandExamples = "opt_out_command_examples"
        case cursor
        case reason
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

@MainActor
final class RecorderController: ObservableObject {
    static let requiredPermissionsErrorMessage =
        "Grant Screen Recording, Accessibility, and Input Monitoring permissions before recording."

    @Published private(set) var state: RecordingState = .idle
    @Published private(set) var lastError: String?
    @Published private(set) var matrixDisclosure: PrivacyMatrixDisclosure?
    @Published private(set) var transport: RecorderTransport = .cliFallback
    @Published private(set) var daemonProbeCompleted = false
    @Published private(set) var schemaMismatchDetected = false
    /// Surfaced in the menu bar dropdown during a Cmd+Q stop. Counts down
    /// from 300s while we wait for the `stopped` event.
    @Published private(set) var quitProgressSecondsRemaining: Int?

    private weak var index: RecordingsIndex?
    private weak var permissions: PermissionController?

    /// Pure value-type state machine owning `state`, `pendingStartCursor`,
    /// and `recordingStartedAt`. All state transitions route through it; the
    /// orchestrator mirrors `machine.state` into the `@Published` surface and
    /// applies returned effects.
    private var machine = RecordingStateMachine()
    private let watchdog: PermissionWatchdog
    private let alertPresenter: RecorderAlertPresenter
    private let cliService: CLIRecorderService
    private let daemonService: DaemonSessionService
    private let stopPolicy = StopPolicyCoordinator()

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
        daemonService: DaemonSessionService = DaemonSessionService()
    ) {
        self.watchdog = watchdog
        self.alertPresenter = alertPresenter
        self.cliService = cliService
        self.daemonService = daemonService
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
        // suspended on the coordinator. The 30s / 300s timeout in
        // `StopPolicyCoordinator.waitForOneShot` is the cancellation backstop
        // for any other corner case.
        if let daemonInstalledObserver {
            NotificationCenter.default.removeObserver(daemonInstalledObserver)
        }
    }

    // MARK: - Public surface

    /// Spawn `screencap start [<name>]` and start consuming stderr events.
    func start(name: String? = nil) {
        guard !state.isRecording else { return }
        if transport == .cliFallback, let permissions, !permissions.allRequiredGranted {
            lastError = Self.requiredPermissionsErrorMessage
            return
        }
        apply(machine.enterStarting())

        switch transport {
        case .daemon:
            Task { await startViaDaemon(name: name) }
        case .cliFallback:
            startViaCLI(name: name)
        }
    }

    func probeDaemon() async {
        defer { daemonProbeCompleted = true }
        switch await daemonService.probe() {
        case .daemon:
            schemaMismatchDetected = false
            transport = .daemon
            await syncDaemonSnapshot()
        case .schemaMismatch:
            schemaMismatchDetected = true
            transport = .cliFallback
        case .unavailable:
            transport = .cliFallback
        }
    }

    private func syncDaemonSnapshot() async {
        switch await daemonService.snapshot() {
        case .noActiveSession, .unreachable:
            return
        case .daemonOwnedSession(let startedAt):
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

    private func startViaCLI(name: String? = nil) {
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

    private func startViaDaemon(name: String? = nil) async {
        do {
            let cursor = try await daemonService.startRecording(name: name)
            machine.setPendingStartCursor(cursor)
            attachDaemonEventStream()
            // Same rationale as syncDaemonSnapshot: on the daemon transport
            // the watchdog's check is a guarded no-op, so don't arm it.
            if transport == .cliFallback {
                startPermissionWatchdog()
            }
        } catch {
            handleDaemonOperationFailure(error, fallback: {
                self.startViaCLI(name: name)
            })
        }
    }

    /// In-app Stop button path. SIGTERM via `screencap stop`, await
    /// `recording_finalized` with a 30s wall-clock fallback, then transition
    /// UI to `.idle`. Background finalization continues invisibly.
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

    /// `quitting=false`: in-app Stop, 30s wait, transition UI to .idle and
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
                } else if let s = cliService.currentProcess, s.isRunning, s.processIdentifier > 0 {
                    // Only SIGKILL if the process is still alive. `processIdentifier`
                    // returns the PID even after exit, and macOS recycles PIDs
                    // quickly — checking `isRunning` first prevents signalling an
                    // unrelated process that took the slot.
                    kill(s.processIdentifier, SIGKILL)
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
        machine.enterIdle()
        state = machine.state
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
            case .setMatrixDisclosure(let disclosure):
                matrixDisclosure = disclosure
            case .refreshIndex:
                Task { await self.index?.refresh() }
            case .handlePermissionLost(let permission):
                handlePermissionLost(permission: permission)
            }
        }
        state = machine.state
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
                    isRecording: { [weak self] in self?.state.isRecording ?? false }
                )
            )
            self.applyDaemonStreamOutcome(outcome)
        }
    }

    private func applyDaemonStreamOutcome(_ outcome: DaemonSessionService.AttachOutcome) {
        switch outcome {
        case .shutdown:
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
        machine.forceState(.idle)
        state = machine.state
    }

    private func handleDaemonOperationFailure(_ error: Error, fallback: (() -> Void)? = nil) {
        applyDaemonFailureOutcome(daemonService.translateFailure(error), fallback: fallback)
    }

    private func applyDaemonFailureOutcome(
        _ outcome: DaemonSessionService.FailureOutcome,
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

    private func handlePermissionLost(permission: String?) {
        let perm = permission ?? "a required permission"
        lastError = "Recording stopped: \(perm) was revoked."

        // Initiate the stop BEFORE blocking on the modal, so the engine
        // teardown proceeds in parallel with the user reading the dialog.
        // Without this, the in-app 30s timeout in `awaitFinalizedEvent` can
        // fire while the modal is up and report a false "still finalizing"
        // message even though the recorder has cleanly shut down.
        if case .recording = state {
            stop()
        }

        alertPresenter.presentPermissionLost(permission: perm) { [weak self] in
            self?.permissions?.openSystemSettings(for: PrivacyPane.from(permissionString: perm))
        }
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
