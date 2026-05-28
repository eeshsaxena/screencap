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
    private var process: SpawnedProcessHandle?
    private var sawTerminalEvent: Bool = false
    private var filesTotal: Int = 0
    private var filesDone: Int = 0

    init(service: UploadService = LiveUploadService()) {
        self.service = service
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
        guard let process, process.isRunning else { return }
        process.terminate()
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
            sawTerminalEvent = true
            state = .succeeded(.init(
                uploaded: event.uploaded ?? 0,
                skipped: event.skipped ?? 0,
                failed: event.failed ?? 0
            ))
        case "upload_failed":
            sawTerminalEvent = true
            state = .failed(event.error ?? "upload failed")
        default:
            // Unknown event type — silently ignore. A future addition
            // (e.g. `upload_progress`) does not need a Swift bump.
            return
        }
    }

    private func handleTerminated(exitCode: Int32) {
        process = nil
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
