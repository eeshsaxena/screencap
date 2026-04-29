import AppKit
import Combine
import Darwin
import Foundation

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
    let ts: Double?
    let schemaVersion: Int?
    let captureDir: String?
    let chunkIndex: Int?
    let forceStopped: Bool?
    let permission: String?
    let exitCode: Int?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case type
        case ts
        case schemaVersion = "schema_version"
        case captureDir = "capture_dir"
        case chunkIndex = "chunk_index"
        case forceStopped = "force_stopped"
        case permission
        case exitCode = "exit_code"
        case error
    }
}

@MainActor
final class RecorderController: ObservableObject {
    @Published private(set) var state: RecordingState = .idle
    @Published private(set) var lastError: String?
    /// Surfaced in the menu bar dropdown during a Cmd+Q stop. Counts down
    /// from 300s while we wait for the `stopped` event.
    @Published private(set) var quitProgressSecondsRemaining: Int?

    private weak var index: RecordingsIndex?
    private weak var permissions: PermissionController?

    private var spawn: CLIClient.SpawnedProcess?
    private var elapsedTimer: Timer?
    private var permissionWatchdog: Timer?
    private var permissionObserver: NSObjectProtocol?
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

    deinit {
        elapsedTimer?.invalidate()
        permissionWatchdog?.invalidate()
        if let observer = permissionObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
        }
    }

    // MARK: - Public surface

    /// Spawn `screencap start [<name>]` and start consuming stderr events.
    func start(name: String? = nil) {
        guard !state.isRecording else { return }
        if let permissions, !permissions.allRequiredGranted {
            lastError = "Grant Screen Recording and Accessibility permissions before recording."
            return
        }
        lastError = nil
        state = .starting

        var args = ["start"]
        if let name { args.append(name) }

        do {
            let proc = try CLIClient.spawn(
                args: args,
                onStderrLine: { [weak self] line in
                    guard let self else { return }
                    Task { @MainActor in self.handleStderrLine(line) }
                },
                onTerminated: { [weak self] exitCode in
                    Task { @MainActor in self?.handleProcessTerminated(exitCode: exitCode) }
                }
            )
            self.spawn = proc
            startPermissionWatchdog()
        } catch {
            state = .idle
            lastError = error.localizedDescription
        }
    }

    /// In-app Stop button path. SIGTERM via `screencap stop`, await
    /// `recording_finalized` with a 30s wall-clock fallback, then transition
    /// UI to `.idle`. Background finalization continues invisibly.
    func stop() {
        guard state.isRecording else { return }
        state = .stopping(quitting: false)
        Task { await self.runStop(quitting: false) }
    }

    /// Cmd+Q path. Shows NSAlert; on "Stop & Quit", returns `.terminateLater`
    /// and runs the long-wait stop policy (5min for `stopped` event).
    func confirmQuitWhileRecording() -> NSApplication.TerminateReply {
        guard state.isRecording else { return .terminateNow }

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

    // MARK: - Stop policy

    /// `quitting=false`: in-app Stop, 30s wait, transition UI to .idle and
    /// continue background finalization invisibly.
    /// `quitting=true`:  Cmd+Q, 300s wait, then NSApp.reply(...). On timeout
    /// we SIGKILL the recorder and let `.upload_followup.json` surface on
    /// next launch.
    private func runStop(quitting: Bool) async {
        do {
            _ = try CLIClient.runDetached(["stop"])
        } catch {
            lastError = error.localizedDescription
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
            if !success, let pid = spawn?.processIdentifier, pid > 0 {
                kill(pid, SIGKILL)
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
            let resume: (Bool) -> Void = { value in
                Task { @MainActor in
                    if resumed { return }
                    resumed = true
                    continuation.resume(returning: value)
                }
            }
            self[keyPath: keyPath].append(resume)
            Task { @MainActor in
                if quitProgressSecondsRemaining != nil {
                    await self.tickQuitProgress(totalSeconds: Int(timeout))
                } else {
                    try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                }
                resume(false)
            }
        }
    }

    private func tickQuitProgress(totalSeconds: Int) async {
        for remaining in stride(from: totalSeconds, through: 0, by: -1) {
            if quitProgressSecondsRemaining == nil { return }
            quitProgressSecondsRemaining = remaining
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }

    // MARK: - Stderr / process callbacks

    private func handleStderrLine(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.hasPrefix("{") else { return }
        guard let data = trimmed.data(using: .utf8) else { return }
        guard let event = try? JSONDecoder().decode(RecorderEventLine.self, from: data) else { return }

        switch event.type {
        case "started":
            recordingStartedAt = Date()
            state = .recording(elapsed: 0)
            startElapsedTimer()
        case "chunk_finalized":
            // Informational — no UI change needed.
            break
        case "recording_finalized":
            // Both stop policies care about this; the in-app path resolves on it.
            resolveAll(awaitingFinalized: \.awaitingFinalized, value: true)
            if event.forceStopped == true {
                lastError = "Recording stopped, but some data may not have uploaded. Run `screencap upload` to retry."
            }
            Task { await self.index?.refresh() }
        case "permission_lost":
            handlePermissionLost(event: event)
        case "disk_full":
            lastError = "Disk is full — recording stopped."
        case "stopped":
            resolveAll(awaitingFinalized: \.awaitingFinalized, value: true)
            resolveAll(awaitingFinalized: \.awaitingStopped, value: true)
        default:
            break
        }
    }

    private func handleProcessTerminated(exitCode: Int32) {
        elapsedTimer?.invalidate()
        elapsedTimer = nil
        stopPermissionWatchdog()
        spawn = nil
        recordingStartedAt = nil

        // If we never saw a `stopped` event and the process is gone, resolve
        // any in-flight awaits so the caller can transition out of stopping.
        resolveAll(awaitingFinalized: \.awaitingFinalized, value: false)
        resolveAll(awaitingFinalized: \.awaitingStopped, value: false)

        if exitCode != 0 && exitCode != 130 /* SIGINT during shutdown */ {
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
        let alert = NSAlert()
        alert.messageText = "Permission revoked"
        alert.informativeText = "ScreenCap stopped recording because \(perm) was disabled in System Settings."
        alert.addButton(withTitle: "Open System Settings")
        alert.addButton(withTitle: "Dismiss")
        if alert.runModal() == .alertFirstButtonReturn {
            let pane: PrivacyPane
            // Maps the `permission` string from `_check_permissions_now`
            // (recorder.py): one of "screen_recording" / "accessibility" /
            // "input_monitoring". Microphone is intentionally not polled
            // (audio loss should not abort a video-only capture).
            switch perm.lowercased() {
            case let s where s.contains("screen"): pane = .screenRecording
            case let s where s.contains("accessibility"): pane = .accessibility
            case let s where s.contains("input"): pane = .inputMonitoring
            default: pane = .screenRecording
            }
            permissions?.openSystemSettings(for: pane)
        }
    }

    private func resolveAll(awaitingFinalized keyPath: ReferenceWritableKeyPath<RecorderController, [(Bool) -> Void]>, value: Bool) {
        let pending = self[keyPath: keyPath]
        self[keyPath: keyPath] = []
        for resume in pending { resume(value) }
    }

    // MARK: - Timers

    private func startElapsedTimer() {
        elapsedTimer?.invalidate()
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tickElapsed() }
        }
    }

    private func tickElapsed() {
        guard case .recording = state, let start = recordingStartedAt else { return }
        state = .recording(elapsed: Date().timeIntervalSince(start))
    }

    private func startPermissionWatchdog() {
        stopPermissionWatchdog()
        permissionWatchdog = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.checkPermissionsDuringRecording() }
        }
        permissionObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.checkPermissionsDuringRecording() }
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
        guard state.isRecording, let permissions else { return }
        permissions.refresh()
        if !permissions.allRequiredGranted {
            // Engine-side will also detect via the black-frame check (Unit 8).
            // Trigger a stop from our side too — belt-and-suspenders.
            lastError = "A required permission was revoked. Stopping recording."
            stop()
        }
    }
}
