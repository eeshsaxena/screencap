import AppKit
import Combine
import Darwin
import Foundation
import OSLog

/// Drift-detection log for the stderr event contract with `_stderr_events.py`.
/// Tail with: `log stream --predicate 'subsystem == "com.screencap.macos"'`.
private let recorderLogger = Logger(subsystem: "com.screencap.macos", category: "recorder")
private let SUPPORTED_EVENT_SCHEMA_VERSION = 1
let SUPPORTED_API_SCHEMA_VERSION = 1

/// State machine for the recording lifecycle. Mirrors the stderr event contract
/// from `src/screencap/_stderr_events.py` (Unit 8a).
enum RecordingState: Equatable {
    case idle
    case starting
    case recording(elapsed: TimeInterval)
    case stopping(quitting: Bool)

    var isRecording: Bool {
        switch self {
        case .recording, .stopping, .starting: return true
        case .idle: return false
        }
    }

    var isStopping: Bool {
        if case .stopping = self { return true }
        return false
    }

    var elapsed: TimeInterval {
        if case .recording(let e) = self { return e }
        return 0
    }
}

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
    @Published private(set) var schemaMismatchDetected = false
    /// Surfaced in the menu bar dropdown during a Cmd+Q stop. Counts down
    /// from 300s while we wait for the `stopped` event.
    @Published private(set) var quitProgressSecondsRemaining: Int?

    private weak var index: RecordingsIndex?
    private weak var permissions: PermissionController?

    private var spawn: CLIClient.SpawnedProcess?
    private var daemonEventTask: Task<Void, Never>?
    private var elapsedTimer: Timer?
    private var permissionWatchdog: Timer?
    private var permissionObserver: NSObjectProtocol?
    private var daemonInstalledObserver: NSObjectProtocol?
    private var recordingStartedAt: Date?

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

    init() {
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
        permissionWatchdog?.invalidate()
        if let observer = permissionObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
        }
        if let daemonInstalledObserver {
            NotificationCenter.default.removeObserver(daemonInstalledObserver)
        }
    }

    // MARK: - Public surface

    /// Spawn `screencap start [<name>]` and start consuming stderr events.
    func start(name: String? = nil) {
        guard !state.isRecording else { return }
        if let permissions, !permissions.allRequiredGranted {
            lastError = Self.requiredPermissionsErrorMessage
            return
        }
        lastError = nil
        state = .starting

        switch transport {
        case .daemon:
            Task { await startViaDaemon(name: name) }
        case .cliFallback:
            startViaCLI(name: name)
        }
    }

    func probeDaemon() async {
        do {
            _ = try await DaemonClient.daemonInfo()
            schemaMismatchDetected = false
            transport = .daemon
        } catch DaemonClientError.schemaMismatch {
            schemaMismatchDetected = true
            transport = .cliFallback
        } catch {
            recorderLogger.info("Daemon not reachable; using CLI fallback. Error: \(String(describing: error), privacy: .public)")
            transport = .cliFallback
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
            state = .idle
            lastError = error.localizedDescription
        }
    }

    private func startViaDaemon(name: String? = nil) async {
        do {
            _ = try await DaemonClient.recordingStart(
                RecordingStartRequest(name: name, startedBy: "swiftui-via-daemon")
            )
            attachDaemonEventStream()
            startPermissionWatchdog()
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
        state = .stopping(quitting: false)
        Task { await self.runStop(quitting: false) }
    }

    /// Cmd+Q path. Shows NSAlert; on "Stop & Quit", returns `.terminateLater`
    /// and runs the long-wait stop policy (5min for `stopped` event).
    func confirmQuitWhileRecording() -> NSApplication.TerminateReply {
        guard state.isRecording else { return .terminateNow }

        // Re-entry while a Cmd+Q quit is already in flight: do not stack a
        // second modal or dispatch a second runStop. The in-flight task will
        // eventually call `NSApp.reply(toApplicationShouldTerminate:)` —
        // telling AppKit `.terminateLater` again is the correct hold reply.
        if case .stopping(quitting: true) = state {
            return .terminateLater
        }

        let alert = NSAlert()
        alert.messageText = "Stop recording before quitting?"
        alert.informativeText =
            "ScreenCap is still recording. Stop & Quit saves the recording — finalization can take up to 5 minutes."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Stop & Quit")
        alert.addButton(withTitle: "Keep Recording")
        alert.addButton(withTitle: "Cancel")

        let response = alert.runModal()
        switch response {
        case .alertFirstButtonReturn:
            state = .stopping(quitting: true)
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
            if state.isStopping {
                let restoredElapsed = recordingStartedAt.map { Date().timeIntervalSince($0) } ?? 0
                state = .recording(elapsed: restoredElapsed)
            }
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
            state = .idle
            NSApp.reply(toApplicationShouldTerminate: true)
        } else {
            if !success {
                lastError = "Stop is still finalizing in the background."
            }
            state = .idle
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
            if state.isStopping {
                let restoredElapsed = recordingStartedAt.map { Date().timeIntervalSince($0) } ?? 0
                state = .recording(elapsed: restoredElapsed)
            }
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
            state = .idle
            NSApp.reply(toApplicationShouldTerminate: true)
        } else {
            if !success {
                lastError = "Stop is still finalizing in the background."
            }
            state = .idle
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
        // Schema-drift guard: warn (don't fail) so we keep working under minor
        // additions while making major-version drift visible in Console.app.
        // Decision on user-facing behavior for a major bump tracked separately.
        if let v = event.schemaVersion, v != SUPPORTED_EVENT_SCHEMA_VERSION {
            recorderLogger.warning("Unexpected schema_version \(v, privacy: .public) on stderr event \(event.type, privacy: .public). Swift parser pinned to v\(SUPPORTED_EVENT_SCHEMA_VERSION, privacy: .public).")
        }

        switch event.type {
        case "started":
            // Only honour the transition when we're still in `.starting`. A
            // duplicate or out-of-order `started` arriving while we're already
            // `.recording` or `.stopping` would otherwise regress the state
            // machine and re-arm the elapsed timer.
            guard case .starting = state else { return }
            recordingStartedAt = Date()
            state = .recording(elapsed: 0)
            startElapsedTimer()
        case "chunk_finalized":
            // Informational — no UI change needed.
            break
        case "recording_finalized":
            // Both stop policies care about this; the in-app path resolves on it.
            resolveAll(pending: \.awaitingFinalized, value: true)
            if event.forceStopped == true {
                lastError = "Recording stopped, but some data may not have uploaded. Run `screencap upload` to retry."
            }
            Task { await self.index?.refresh() }
        case "permission_lost":
            handlePermissionLost(event: event)
        case "disk_full":
            lastError = "Disk is full — recording stopped."
        case "stopped":
            resolveAll(pending: \.awaitingFinalized, value: true)
            resolveAll(pending: \.awaitingStopped, value: true)
        case "matrix_disclosure_required":
            matrixDisclosure = PrivacyMatrixDisclosure(
                changes: event.changes ?? [],
                optOutCommandExamples: event.optOutCommandExamples ?? []
            )
        case "_close":
            if event.reason == "shutdown" {
                recorderLogger.info("Daemon event stream closed for shutdown.")
            } else if let reason = event.reason {
                recorderLogger.info("Daemon event stream closed: \(reason, privacy: .public)")
            }
        default:
            // Active Python events the Swift consumer doesn't model (e.g.
            // lock_contended) — log so drift is detectable; the engine handles
            // user-facing fallout via exit codes so we don't surface here.
            recorderLogger.debug("Unhandled stderr event type: \(event.type, privacy: .public)")
        }
    }

    private func attachDaemonEventStream() {
        daemonEventTask?.cancel()
        daemonEventTask = Task { [weak self] in
            await self?.consumeDaemonEvents()
        }
    }

    private func consumeDaemonEvents() async {
        while !Task.isCancelled, state.isRecording {
            do {
                let snapshot = try await DaemonClient.sessionSnapshot()
                if snapshot.isRecording == true, snapshot.daemonOwned == false {
                    lastError = "Another process is recording."
                    state = .idle
                    return
                }
                if snapshot.recovering {
                    lastError = "ScreenCap daemon is recovering the previous recording session."
                }

                for try await event in DaemonClient.subscribe(sinceCursor: snapshot.cursor) {
                    if Task.isCancelled { return }
                    handleRecorderEvent(event)
                    if event.type == "_close", event.reason == "shutdown" {
                        return
                    }
                }
            } catch DaemonClientError.streamClosed(let reason) {
                if Task.isCancelled { return }
                recorderLogger.info("Daemon event stream dropped; reconnecting. Reason: \(reason, privacy: .public)")
            } catch {
                if Task.isCancelled { return }
                handleDaemonOperationFailure(error)
            }

            if state.isRecording {
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
        }
    }

    private func handleDaemonOperationFailure(_ error: Error, fallback: (() -> Void)? = nil) {
        switch error {
        case DaemonClientError.schemaMismatch:
            schemaMismatchDetected = true
            transport = .cliFallback
            state = .idle
            lastError = "ScreenCap daemon needs to reload."
        case DaemonClientError.socketUnavailable, DaemonClientError.connectionFailed:
            recorderLogger.info("Daemon transport failed; falling back to CLI. Error: \(String(describing: error), privacy: .public)")
            transport = .cliFallback
            fallback?()
        case DaemonClientError.envelopeError(let code, _):
            if code == "lock_contended" || code == "not_owned_by_daemon" {
                lastError = "ScreenCap is already recording."
            } else {
                lastError = error.localizedDescription
            }
            if state.isRecording {
                state = .idle
            }
        default:
            lastError = error.localizedDescription
            if state.isRecording {
                state = .idle
            }
        }
    }

    private func handleProcessTerminated(exitCode: Int32) {
        elapsedTimer?.invalidate()
        elapsedTimer = nil
        stopPermissionWatchdog()
        spawn = nil
        daemonEventTask?.cancel()
        daemonEventTask = nil
        recordingStartedAt = nil

        // If we never saw a `stopped` event and the process is gone, resolve
        // any in-flight awaits so the caller can transition out of stopping.
        resolveAll(pending: \.awaitingFinalized, value: false)
        resolveAll(pending: \.awaitingStopped, value: false)

        // 0 = clean, 130 = SIGINT, 143 = SIGTERM (the engine's documented
        // graceful-shutdown signals). Treat all three as "no new terminal
        // error to surface." Intentionally preserve any warning already set
        // earlier in this session (for example the `forceStopped`
        // upload-retry note from `recording_finalized`) so the user can still
        // see it after the process exits. `start()` clears stale messages when
        // a new recording begins.
        if exitCode == 0 || exitCode == 130 || exitCode == 143 {
            // Keep any prior user-facing warning.
        } else {
            switch exitCode {
            case 2:
                lastError = "ScreenCap is already recording."
            case 3:
                lastError = "Recording stopped because a required permission was revoked."
            case 4:
                lastError = "Disk is full — recording stopped."
            default:
                lastError = "Recorder exited with code \(exitCode)."
            }
        }
        if state.isRecording {
            state = .idle
        }
    }

    private func handlePermissionLost(event: RecorderEventLine) {
        let perm = event.permission ?? "a required permission"
        lastError = "Recording stopped: \(perm) was revoked."

        // Initiate the stop BEFORE blocking on the modal, so the engine
        // teardown proceeds in parallel with the user reading the dialog.
        // Without this, the in-app 30s timeout in `awaitFinalizedEvent` can
        // fire while the modal is up and report a false "still finalizing"
        // message even though the recorder has cleanly shut down.
        if case .recording = state {
            stop()
        }

        let alert = NSAlert()
        alert.messageText = "Permission revoked"
        alert.informativeText = "ScreenCap stopped recording because \(perm) was disabled in System Settings."
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Dismiss")
        if alert.runModal() == .alertFirstButtonReturn {
            permissions?.openSystemSettings(for: PrivacyPane.from(permissionString: perm))
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
        guard case .recording = state, let start = recordingStartedAt else { return }
        state = .recording(elapsed: Date().timeIntervalSince(start))
    }

    private func startPermissionWatchdog() {
        stopPermissionWatchdog()
        // `.common` mode for the same reason as the elapsed timer: a menu bar
        // dropdown or NSAlert must not pause permission revocation detection.
        let timer = Timer(timeInterval: 5.0, repeats: true) { [weak self] _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.checkPermissionsDuringRecording() } }
        }
        RunLoop.main.add(timer, forMode: .common)
        permissionWatchdog = timer
        // `DispatchQueue.main.async` rather than `Task { @MainActor }` to keep
        // ordering FIFO with the stderr/termination dispatches in CLIClient —
        // Tasks don't preserve order against GCD blocks.
        permissionObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.checkPermissionsDuringRecording() } }
        }
    }

    private func stopPermissionWatchdog() {
        permissionWatchdog?.invalidate()
        permissionWatchdog = nil
        if let observer = permissionObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
            permissionObserver = nil
        }
    }

    private func checkPermissionsDuringRecording() {
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
}
#endif
