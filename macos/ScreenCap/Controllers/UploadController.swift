import Combine
import Foundation
import OSLog

private let uploadLogger = Logger(subsystem: "com.screencap.macos", category: "upload")

/// Plan U7: spawns `screencap upload <name>`, parses U1's stderr lifecycle
/// events, and publishes a small state machine the review window (U8)
/// renders progress / success / failure off of.
///
/// Lives behind an `UploadService` seam so the spawn + line-delivery
/// pipeline is substitutable in tests — the production wiring uses
/// `LiveUploadService` which routes through `CLIClient.spawn` and inherits
/// the four `Foundation.Process` + `Pipe` pitfall protections documented in
/// docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md.
@MainActor
protocol UploadService {
    func start(
        name: String,
        onLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle
}

@MainActor
final class LiveUploadService: UploadService {
    func start(
        name: String,
        onLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle {
        // `--` matches the RecordingsListView "view" callsite: forces Click
        // to treat the recording name as a positional argument even if it
        // begins with `--`.
        try CLIClient.spawn(
            args: ["upload", "--", name],
            onStderrLine: { line in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { onLine(line) }
                }
            },
            onTerminated: { code in
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { onTerminated(code) }
                }
            }
        )
    }
}

/// State machine surface published to the review window.
///
/// `uploading` carries derived progress (`filesDone / filesTotal`) which
/// the progress bar reads directly — the controller is the single source
/// of truth for "how far along".
enum UploadState: Equatable {
    case idle
    case uploading(progress: Progress)
    case succeeded(Summary)
    case failed(String)

    struct Progress: Equatable {
        let filesDone: Int
        let filesTotal: Int
        /// 0.0 ... 1.0 — derived from `filesDone / filesTotal`, or 0 when
        /// `filesTotal` is missing (e.g., the controller has only seen
        /// `upload_started` so far). The view renders an indeterminate
        /// spinner in that case.
        let fraction: Double
    }

    struct Summary: Equatable {
        let uploaded: Int
        let skipped: Int
        let failed: Int
    }
}

@MainActor
final class UploadController: ObservableObject {
    @Published private(set) var state: UploadState = .idle

    private let service: UploadService
    private let inactivityTimeoutSeconds: Double
    private var process: SpawnedProcessHandle?
    private var sawTerminalEvent: Bool = false
    private var filesTotal: Int = 0
    private var filesDone: Int = 0
    /// Inactivity watchdog (todo #001). Reset on every parsed upload event
    /// and on terminal exit. If no event arrives within the bound, the
    /// controller surfaces a `.failed("upload timed out")` and SIGTERMs the
    /// child so a wedged HTTP PUT can't hang the window forever.
    /// Python's `requests.put` already times out at (10s connect, 300s read)
    /// per `upload.py`, but a drip-byte middlebox would reset that read
    /// timeout indefinitely — the Swift-side watchdog is the backstop.
    private var watchdogTask: Task<Void, Never>?

    /// `inactivityTimeoutSeconds` is overridable for tests (which want a
    /// short bound). Production uses 120s — long enough that a slow but
    /// progressing chunk upload (a single per-file event arriving every
    /// minute or two on a 10MB file over a poor connection) never trips
    /// it, short enough that a wedged connection surfaces within minutes
    /// instead of hours.
    init(
        service: UploadService = LiveUploadService(),
        inactivityTimeoutSeconds: Double = 120
    ) {
        self.service = service
        self.inactivityTimeoutSeconds = inactivityTimeoutSeconds
    }

    /// Spawns `screencap upload <name>` and starts streaming events. The
    /// guard rejects only `uploading` — start is valid from idle, succeeded,
    /// or failed so the Retry button (U8) can reuse this entry point.
    func start(name: String) {
        if case .uploading = state { return }
        sawTerminalEvent = false
        filesTotal = 0
        filesDone = 0
        state = .uploading(progress: .init(filesDone: 0, filesTotal: 0, fraction: 0))
        do {
            process = try service.start(
                name: name,
                onLine: { [weak self] line in self?.handleLine(line) },
                onTerminated: { [weak self] code in self?.handleTerminated(exitCode: code) }
            )
            armWatchdog()
        } catch {
            // Spawn failure (binary not found, launch failed) maps to a
            // terminal failure state with the launch error surfaced. No
            // process to clean up.
            sawTerminalEvent = true
            state = .failed(error.localizedDescription)
        }
    }

    /// Window-close-as-cancel path. Safe on idle and on already-terminal
    /// states — the Python side ignores SIGTERM on an already-exited
    /// process, and the controller's guard avoids signalling stale handles.
    func cancel() {
        watchdogTask?.cancel()
        watchdogTask = nil
        guard let process, process.isRunning else { return }
        process.terminate()
    }

    /// (Re-)arm the inactivity watchdog. Called at start and on every
    /// parsed upload event. Cancels any prior task so resets are cheap.
    private func armWatchdog() {
        watchdogTask?.cancel()
        let bound = inactivityTimeoutSeconds
        watchdogTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, bound) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await MainActor.run { self?.fireInactivityTimeout() }
        }
    }

    private func fireInactivityTimeout() {
        // Only fire if we're still mid-upload and haven't already landed
        // on a terminal state. `handleTerminated` / `handleLine` clear the
        // watchdog on terminal transitions, but a late timer firing during
        // a race shouldn't clobber a fresh .succeeded / .failed.
        guard !sawTerminalEvent, case .uploading = state else { return }
        sawTerminalEvent = true
        // SIGTERM the child so the OS reaps it and the user isn't left
        // with a zombie upload process after the UI gives up.
        process?.terminate()
        state = .failed("upload timed out")
    }

    // MARK: - Event handling

    private func handleLine(_ line: String) {
        guard let event = UploadEventLine.parse(stderrLine: line) else {
            // Rich progress bar bleed on stdout (or any non-JSON line)
            // arrives here when the production drain misroutes — silently
            // drop without state change. Mirrors the recorder-event parser
            // contract.
            return
        }
        if let schemaVersion = event.schemaVersion,
           schemaVersion != SUPPORTED_API_SCHEMA_VERSION {
            uploadLogger.warning(
                "Upload event schema_version=\(schemaVersion, privacy: .public) does not match SwiftUI side (\(SUPPORTED_API_SCHEMA_VERSION, privacy: .public)). Processing anyway."
            )
        }
        // Reset the inactivity watchdog on any parsed event — a progressing
        // upload, even slowly, keeps the timer alive.
        armWatchdog()
        switch event.type {
        case "upload_started":
            if let total = event.fileCount {
                filesTotal = total
            }
            state = .uploading(progress: progressSnapshot())
        case "upload_file_done":
            if let done = event.filesDone {
                filesDone = done
            } else {
                filesDone += 1
            }
            if let total = event.filesTotal {
                filesTotal = total
            }
            state = .uploading(progress: progressSnapshot())
        case "upload_finished":
            // First-write-wins on terminal events (todo #010). The child
            // may emit a duplicate or a contradictory follow-up (e.g. a
            // second `upload_finished` from a retry path, or an
            // `upload_failed` arriving after the success branch has
            // already triggered the auto-close + index refresh). Whichever
            // terminal event lands first owns the final state.
            guard !sawTerminalEvent else { return }
            sawTerminalEvent = true
            watchdogTask?.cancel()
            watchdogTask = nil
            state = .succeeded(.init(
                uploaded: event.uploaded ?? 0,
                skipped: event.skipped ?? 0,
                failed: event.failed ?? 0
            ))
        case "upload_failed":
            guard !sawTerminalEvent else { return }
            sawTerminalEvent = true
            watchdogTask?.cancel()
            watchdogTask = nil
            state = .failed(event.error ?? "upload failed")
        default:
            // Unknown event type — silently ignore. A future addition
            // (e.g. `upload_progress`) does not need a Swift bump.
            return
        }
    }

    private func handleTerminated(exitCode: Int32) {
        process = nil
        watchdogTask?.cancel()
        watchdogTask = nil
        // If we already received a terminal event, defer to it — the
        // terminationHandler may run after `upload_finished` / `upload_failed`.
        guard !sawTerminalEvent else { return }
        // Exit-without-terminal-event is a contract violation worth surfacing
        // to the user; map it to a failure with the exit code so a future bug
        // in the Python side is observable rather than silent.
        state = .failed("upload exited with code \(exitCode)")
    }

    private func progressSnapshot() -> UploadState.Progress {
        let fraction: Double
        if filesTotal > 0 {
            fraction = min(1, max(0, Double(filesDone) / Double(filesTotal)))
        } else {
            fraction = 0
        }
        return .init(filesDone: filesDone, filesTotal: filesTotal, fraction: fraction)
    }
}
