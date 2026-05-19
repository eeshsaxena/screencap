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

    private var spawn: CLIClient.SpawnedProcess?
    private var daemonEventTask: Task<Void, Never>?
    private var elapsedTimer: Timer?
    private var daemonInstalledObserver: NSObjectProtocol?

    /// Pending awaits keyed by event type. Resolved when the matching event
    /// arrives or when the timeout fires.
    private var awaitingFinalized: [(Bool) -> Void] = []
    private var awaitingStopped: [(Bool) -> Void] = []

    func bindIndex(_ index: RecordingsIndex) {
        self.index = index
    }

    func bindPermissions(_ permissions: PermissionController) {
        self.permissions = permissions
    }

    init(
        watchdog: PermissionWatchdog = LivePermissionWatchdog(),
        alertPresenter: RecorderAlertPresenter = LiveRecorderAlertPresenter()
    ) {
        self.watchdog = watchdog
        self.alertPresenter = alertPresenter
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
        do {
            _ = try await DaemonClient.daemonInfo()
            schemaMismatchDetected = false
            transport = .daemon
            await syncDaemonSnapshot()
        } catch DaemonClientError.schemaMismatch {
            schemaMismatchDetected = true
            transport = .cliFallback
        } catch {
            recorderLogger.info("Daemon not reachable; using CLI fallback. Error: \(String(describing: error), privacy: .public)")
            transport = .cliFallback
        }
    }

    private func syncDaemonSnapshot() async {
        do {
            let snapshot = try await DaemonClient.sessionSnapshot()
            guard snapshot.isRecording == true else { return }

            if snapshot.daemonOwned {
                let start = snapshot.startedAt.map(Date.init(timeIntervalSince1970:)) ?? Date()
                apply(machine.observeActiveDaemonSession(startedAt: start))
                attachDaemonEventStream()
                // The watchdog only re-checks TCC for the app process during
                // CLI-fallback recordings (see checkPermissionsDuringRecording).
                // Skip arming it on the daemon transport so we don't wake the
                // Timer and NSWorkspace observer to immediately no-op.
                if transport == .cliFallback {
                    startPermissionWatchdog()
                }
            } else {
                lastError = "Another process is recording."
                machine.forceState(.idle)
                state = machine.state
            }
        } catch {
            recorderLogger.info("Could not sync daemon session snapshot: \(String(describing: error), privacy: .public)")
        }
    }

    func reloadDaemon() async {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["kickstart", "-kp", "gui/\(getuid())/com.screencap.daemon"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                DispatchQueue.global(qos: .userInitiated).async {
                    process.waitUntilExit()
                    continuation.resume()
                }
            }
            if process.terminationStatus == 0 {
                schemaMismatchDetected = false
                await probeDaemon()
            } else {
                lastError = "Failed to reload ScreenCap daemon."
            }
        } catch {
            lastError = "Failed to reload ScreenCap daemon: \(error.localizedDescription)"
        }
    }

    private func startViaCLI(name: String? = nil) {
        // Same shape as the outer guard in start(name:), but it covers the
        // case where `handleDaemonOperationFailure` flips transport to
        // .cliFallback and invokes us as a fallback — at that point the
        // outer guard has already passed (it gated on the prior .daemon
        // transport) so we must re-check before spawning the CLI.
        if let permissions, !permissions.allRequiredGranted {
            machine.forceState(.idle)
            state = machine.state
            lastError = Self.requiredPermissionsErrorMessage
            return
        }

        var args = ["start"]
        if let name { args.append(name) }

        do {
            let proc = try CLIClient.spawn(
                args: args,
                // Use DispatchQueue.main.async (not Task { @MainActor }) for both
                // dispatch sites: GCD's main queue is strictly FIFO, so a final
                // `recording_finalized` line dispatched from `terminationHandler`'s
                // drain step is guaranteed to land on MainActor before the
                // subsequent `onTerminated` block. Mixing `Task { @MainActor }`
                // for one side and DispatchQueue for the other gives no FIFO
                // guarantee, allowing handleProcessTerminated to resolve the
                // awaiting continuation with `false` before the in-flight event
                // ran. See /rf:review finding #4.
                onStderrLine: { [weak self] line in
                    DispatchQueue.main.async {
                        MainActor.assumeIsolated { self?.handleStderrLine(line) }
                    }
                },
                onTerminated: { [weak self] exitCode in
                    DispatchQueue.main.async {
                        MainActor.assumeIsolated { self?.handleProcessTerminated(exitCode: exitCode) }
                    }
                }
            )
            self.spawn = proc
            startPermissionWatchdog()
        } catch {
            machine.forceState(.idle)
            state = machine.state
            lastError = error.localizedDescription
        }
    }

    private func startViaDaemon(name: String? = nil) async {
        do {
            let response = try await DaemonClient.recordingStart(
                RecordingStartRequest(name: name, startedBy: "swiftui-via-daemon")
            )
            machine.pendingStartCursor = response.cursor
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

        // Re-entry while a Cmd+Q quit is already in flight: do not stack a
        // second modal or dispatch a second runStop. The in-flight task will
        // eventually call `NSApp.reply(toApplicationShouldTerminate:)` —
        // telling AppKit `.terminateLater` again is the correct hold reply.
        if case .stopping(quitting: true) = state {
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
    /// we SIGKILL the recorder and let `.upload_followup.json` surface on
    /// next launch.
    private func runStop(quitting: Bool) async {
        if transport == .daemon {
            await runStopViaDaemon(quitting: quitting)
            return
        }

        do {
            _ = try CLIClient.runDetached(["stop"])
        } catch {
            // The stop subprocess never launched — the recorder never
            // received SIGTERM, so waiting 30s/300s for stderr events would
            // surface a false "still finalizing" message. Bail out, roll
            // state back, and (for Cmd+Q) tell AppKit to abort the quit so
            // the app doesn't hang on `.terminateLater`.
            lastError = "Failed to send stop signal: \(error.localizedDescription). Try `screencap stop` in a terminal."
            if quitting {
                quitProgressSecondsRemaining = nil
                NSApp.reply(toApplicationShouldTerminate: false)
            }
            machine.restoreRecordingAfterStopFailure()
            state = machine.state
            return
        }

        let timeout: TimeInterval = quitting ? 300 : 30
        let success: Bool
        if quitting {
            success = await awaitStoppedEvent(timeout: timeout)
        } else {
            success = await awaitFinalizedEvent(timeout: timeout)
        }

        if quitting {
            quitProgressSecondsRemaining = nil
            // Only SIGKILL if the process is still alive. `processIdentifier`
            // returns the PID even after exit, and macOS recycles PIDs
            // quickly — checking `isRunning` first prevents signalling an
            // unrelated process that took the slot.
            if !success, let s = spawn, s.isRunning, s.processIdentifier > 0 {
                kill(s.processIdentifier, SIGKILL)
                lastError = "Stop timed out after 5 minutes; recorder force-killed."
            }
            machine.enterIdle()
            state = machine.state
            NSApp.reply(toApplicationShouldTerminate: true)
        } else {
            if !success {
                lastError = "Stop is still finalizing in the background."
            }
            machine.enterIdle()
            state = machine.state
        }
    }

    private func runStopViaDaemon(quitting: Bool) async {
        do {
            _ = try await DaemonClient.recordingStop(RecordingStopRequest(force: false))
        } catch {
            handleDaemonOperationFailure(error)
            if quitting {
                quitProgressSecondsRemaining = nil
                NSApp.reply(toApplicationShouldTerminate: false)
            }
            machine.restoreRecordingAfterStopFailure()
            state = machine.state
            return
        }

        let timeout: TimeInterval = quitting ? 300 : 30
        let success = quitting
            ? await awaitStoppedEvent(timeout: timeout)
            : await awaitFinalizedEvent(timeout: timeout)

        if quitting {
            quitProgressSecondsRemaining = nil
            if !success {
                lastError = "Stop timed out after 5 minutes; recorder finalization may still be running."
            }
            machine.enterIdle()
            state = machine.state
            NSApp.reply(toApplicationShouldTerminate: true)
        } else {
            if !success {
                lastError = "Stop is still finalizing in the background."
            }
            machine.enterIdle()
            state = machine.state
        }
    }

    private func awaitFinalizedEvent(timeout: TimeInterval) async -> Bool {
        await waitForOneShot(into: \.awaitingFinalized, timeout: timeout)
    }

    private func awaitStoppedEvent(timeout: TimeInterval) async -> Bool {
        await waitForOneShot(into: \.awaitingStopped, timeout: timeout)
    }

    private func waitForOneShot(
        into keyPath: ReferenceWritableKeyPath<RecorderController, [(Bool) -> Void]>,
        timeout: TimeInterval
    ) async -> Bool {
        await withCheckedContinuation { continuation in
            var resumed = false
            var timeoutTask: Task<Void, Never>?
            let resume: (Bool) -> Void = { value in
                Task { @MainActor in
                    if resumed { return }
                    resumed = true
                    // Cancel the timeout sleep so it doesn't sit for the full
                    // 30s/300s wall-clock after a successful event.
                    timeoutTask?.cancel()
                    continuation.resume(returning: value)
                }
            }
            // If the timeout path resumes the continuation first, this closure
            // stays in the array as a no-op (the `resumed` flag prevents
            // double-resume) until the next `resolveAll` drains it. A stop
            // cycle that times out without any subsequent successful event
            // would leak one closure per timeout — vanishingly rare in
            // practice and self-cleaning on the next event. Tracked as a
            // residual cleanup; deferred until usage shows it bites.
            self[keyPath: keyPath].append(resume)
            timeoutTask = Task { @MainActor in
                if quitProgressSecondsRemaining != nil {
                    await self.tickQuitProgress(totalSeconds: Int(timeout))
                } else {
                    try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                }
                if Task.isCancelled { return }
                resume(false)
            }
        }
    }

    private func tickQuitProgress(totalSeconds: Int) async {
        await QuitProgressCountdown.run(totalSeconds: totalSeconds) { remaining in
            guard quitProgressSecondsRemaining != nil else { return }
            quitProgressSecondsRemaining = remaining
        } sleep: {
            try await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }

    // MARK: - Stderr / process callbacks

    private func handleStderrLine(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.hasPrefix("{") else { return }
        guard let data = trimmed.data(using: .utf8) else { return }
        guard let event = try? JSONDecoder().decode(RecorderEventLine.self, from: data) else { return }
        handleRecorderEvent(event)
    }

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
            case .startPermissionWatchdog:
                startPermissionWatchdog()
            case .stopPermissionWatchdog:
                stopPermissionWatchdog()
            case .resolveAwaiting(.finalized, let success):
                resolveAll(pending: \.awaitingFinalized, value: success)
            case .resolveAwaiting(.stopped, let success):
                resolveAll(pending: \.awaitingStopped, value: success)
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
            await self?.consumeDaemonEvents()
        }
    }

    private func consumeDaemonEvents() async {
        // Capped exponential backoff for reconnects: a flat 100ms sleep would
        // hammer a daemon that is genuinely down, and a successful pass should
        // reset the dial. After `maxConsecutiveFailures` we give up and
        // surface the loss to the UI so the user can act.
        var consecutiveFailures = 0
        let maxConsecutiveFailures = 10
        let baseBackoff: TimeInterval = 0.1
        let cappedBackoff: TimeInterval = 30.0

        while !Task.isCancelled, state.isRecording {
            var sawProgress = false
            do {
                let snapshot = try await DaemonClient.sessionSnapshot()
                if snapshot.isRecording == true, snapshot.daemonOwned == false {
                    lastError = "Another process is recording."
                    machine.forceState(.idle)
                    state = machine.state
                    return
                }
                if snapshot.recovering {
                    lastError = "ScreenCap daemon is recovering the previous recording session."
                }
                // If the daemon snapshot says recording stopped while our local
                // state still says recording, the previous run terminated
                // outside this controller's awareness (engine crash, external
                // `screencap stop`, daemon restart that lost session). Without
                // this branch the `subscribe` below would wait forever on a
                // dead session and the UI would stay stuck in `.recording`
                // until the 10×backoff cap fires.
                if snapshot.isRecording == false, state.isRecording {
                    machine.forceState(.idle)
                    state = machine.state
                    lastError = "Recording ended."
                    return
                }

                // Until we receive `started` or `recording_failed`, keep using
                // the start boundary. If the first stream drops before the
                // boundary event is delivered, a reconnect from snapshot.cursor
                // could skip the event that moves the UI out of `.starting`.
                let sinceCursor = machine.pendingStartCursor ?? snapshot.cursor
                for try await event in DaemonClient.subscribe(sinceCursor: sinceCursor) {
                    if Task.isCancelled { return }
                    sawProgress = true
                    handleRecorderEvent(event)
                    if event.type == "_close", event.reason == "shutdown" {
                        return
                    }
                }
            } catch DaemonClientError.streamClosed(let reason) {
                if Task.isCancelled { return }
                recorderLogger.info("Daemon event stream dropped; reconnecting. Reason: \(reason, privacy: .public)")
            } catch DaemonClientError.envelopeError(let code, _) where code == "cursor_unknown" {
                if Task.isCancelled { return }
                // The requested cursor was evicted from the daemon's replay
                // window between snapshot and subscribe. Refetch the snapshot
                // and resubscribe with a fresh cursor; leave `state` intact so
                // the UI does not flicker to `.idle`.
                machine.pendingStartCursor = nil
                recorderLogger.info("Daemon event stream evicted cursor; refetching snapshot.")
                continue
            } catch {
                if Task.isCancelled { return }
                handleDaemonOperationFailure(error)
            }

            if sawProgress {
                consecutiveFailures = 0
            } else {
                consecutiveFailures += 1
                if consecutiveFailures >= maxConsecutiveFailures {
                    machine.forceState(.idle)
                    state = machine.state
                    lastError = "Lost contact with daemon"
                    return
                }
            }

            if state.isRecording {
                let attempt = max(0, consecutiveFailures - 1)
                let backoff = min(baseBackoff * pow(2.0, Double(attempt)), cappedBackoff)
                try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
            }
        }
    }

    private func handleDaemonOperationFailure(_ error: Error, fallback: (() -> Void)? = nil) {
        switch error {
        case DaemonClientError.schemaMismatch:
            schemaMismatchDetected = true
            transport = .cliFallback
            machine.forceState(.idle)
            state = machine.state
            lastError = "ScreenCap daemon needs to reload."
        case DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed:
            recorderLogger.info("Daemon transport failed; falling back to CLI. Error: \(String(describing: error), privacy: .public)")
            transport = .cliFallback
            // Without resetting `state`, a failed start leaves the controller
            // stuck in `.starting`; surface the failure to the user and clear
            // the in-flight state so a retry (or CLI fallback) can take over.
            machine.forceState(.idle)
            state = machine.state
            lastError = "Daemon socket unavailable"
            fallback?()
        case DaemonClientError.envelopeError(let code, _):
            if code == DaemonErrorCode.lockContended || code == DaemonErrorCode.notOwnedByDaemon {
                lastError = "ScreenCap is already recording."
            } else {
                lastError = error.localizedDescription
            }
            if state.isRecording {
                machine.forceState(.idle)
                state = machine.state
            }
        default:
            lastError = error.localizedDescription
            if state.isRecording {
                machine.forceState(.idle)
                state = machine.state
            }
        }
    }

    private func handleProcessTerminated(exitCode: Int32) {
        spawn = nil
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

    private func resolveAll(pending keyPath: ReferenceWritableKeyPath<RecorderController, [(Bool) -> Void]>, value: Bool) {
        let resumes = self[keyPath: keyPath]
        self[keyPath: keyPath] = []
        for resume in resumes { resume(value) }
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
        handleStderrLine(line)
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
        machine.forceState(.idle)
        state = .idle
        task?.cancel()
        try? await Task.sleep(nanoseconds: 50_000_000)
    }
}
#endif
