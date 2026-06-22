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
    /// Cross-window claim refusal (SCR-89/SCR-155): another controller already
    /// holds this recording's upload, so this one never spawned a child and
    /// never claimed the name. Distinct from `.failed` precisely so the review
    /// window can render it honestly (no dangling Retry that silently
    /// re-refuses) — it is *not* a genuine upload failure.
    case refused(String)
    /// Terminal busy-skip (SCR-158): the spawned `screencap upload` child exited
    /// 0 after emitting an `upload_busy` event because another *process* held
    /// the per-recording terminal-stage lock (a finalize, a daemon resume, or a
    /// concurrent upload). Distinct from `.failed` (it is not a failure — the
    /// child exited cleanly) and from `.refused` (which re-refuses on retry while
    /// the same-app claim is held): a busy-lock is transient, so this state is
    /// retry-friendly. Without it, exit-0-without-a-terminal-event would render
    /// as `.failed("upload exited with code 0")`.
    case busy(String)

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

/// Process-wide in-flight upload registry keyed by recording name (SCR-89).
///
/// R3 allows multiple concurrent review windows, and the review scene is a
/// multi-window `WindowGroup` (ScreenCapApp) — on the macOS 13 deployment
/// floor, opening the window twice for the same recording yields two windows,
/// each owning its own `UploadController`. The per-instance `.uploading`
/// guard in `start(name:)` therefore can't see an upload started by another
/// window's controller for the *same* recording, so two `screencap upload
/// <name>` children would race — wasting bandwidth and reporting all-skipped
/// / 0 uploaded. This registry is the cross-controller guard: a recording
/// name may be claimed by at most one controller at a time.
///
/// `@MainActor`-isolated, so every access is serialized on the main actor —
/// no locking required. Injected into `UploadController` (rather than read as
/// a bare static) so tests can supply a fresh instance per case or share one
/// across controllers to exercise the cross-window refusal; production uses
/// the `.shared` singleton. Mirrors the `ReviewWindowOpener.shared` pattern.
@MainActor
final class UploadRegistry {
    static let shared = UploadRegistry()

    private var activeNames: Set<String> = []

    /// Attempts to claim `name`. Returns true if the caller now owns it,
    /// false if another controller already holds it.
    func claim(_ name: String) -> Bool {
        activeNames.insert(name).inserted
    }

    /// Releases a previously claimed name. A no-op if the name isn't held,
    /// so release paths can fire idempotently.
    func release(_ name: String) {
        activeNames.remove(name)
    }
}

@MainActor
final class UploadController: ObservableObject {
    @Published private(set) var state: UploadState = .idle

    private let service: UploadService
    private let registry: UploadRegistry
    private let inactivityTimeoutSeconds: Double
    private var process: SpawnedProcessHandle?
    private var sawTerminalEvent: Bool = false
    private var filesTotal: Int = 0
    private var filesDone: Int = 0
    /// The recording name this controller currently holds in the cross-window
    /// `registry` (SCR-89), or nil when it holds none. Set on a successful
    /// claim in `start(name:)`, cleared by `releaseClaim()` on any terminal
    /// state or cancel so the registry entry is released exactly once.
    private var claimedName: String?
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
        registry: UploadRegistry = .shared,
        inactivityTimeoutSeconds: Double = 120
    ) {
        self.service = service
        self.registry = registry
        self.inactivityTimeoutSeconds = inactivityTimeoutSeconds
    }

    deinit {
        // SCR-89: defensively release a still-held claim if this controller is
        // deallocated without cancel()/a terminal event ever firing (e.g. a
        // window torn down without onDisappear). deinit is non-isolated and
        // releaseClaim() is @MainActor, so capture the name + registry and hop
        // to the main actor. Idempotent with the other release paths.
        if let name = claimedName {
            let registry = self.registry
            Task { @MainActor in registry.release(name) }
        }
    }

    /// Spawns `screencap upload <name>` and starts streaming events. The
    /// per-instance guard rejects only `uploading` — start is valid from idle,
    /// succeeded, or failed so the Retry button (U8) can reuse this entry
    /// point. The cross-window registry guard (SCR-89) additionally refuses
    /// when another controller is already uploading the same recording.
    func start(name: String) {
        if case .uploading = state { return }
        // Cross-window guard (SCR-89): refuse if another window's controller
        // is already uploading this recording. Surface a terminal `.refused`
        // rather than a silent no-op — the review viewmodel optimistically
        // sets `.uploading` before calling `start`, so a no-op would strand
        // the window on "Starting upload…". `.refused` (SCR-155) is deliberately
        // a *distinct* state from `.failed`: the recording isn't broken, it's
        // being handled by another window, so the UI shows honest copy and no
        // Retry — rather than the old `.failed` whose enabled Retry silently
        // re-refused for as long as the owner held the claim, and whose
        // "released" promise was optimistic (eager release-before-SIGTERM,
        // SCR-154).
        guard registry.claim(name) else {
            uploadLogger.info(
                "Cross-window refusal for \(name, privacy: .public): another window already holds the upload claim."
            )
            state = .refused("This recording is already being uploaded in another window.")
            return
        }
        claimedName = name
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
            // process to clean up, but the registry claim must be released.
            sawTerminalEvent = true
            releaseClaim()
            state = .failed(error.localizedDescription)
        }
    }

    /// Window-close-as-cancel path. Safe on idle and on already-terminal
    /// states — the Python side ignores SIGTERM on an already-exited
    /// process, and the controller's guard avoids signalling stale handles.
    func cancel() {
        watchdogTask?.cancel()
        watchdogTask = nil
        // Release the cross-window claim (SCR-89) *before* the SIGTERM below.
        // This ordering is deliberate (SCR-154): SIGTERM does not kill the
        // child synchronously — on a wedged HTTP PUT the Python side only
        // reacts once the socket read returns or the syscall is interrupted —
        // so releasing here opens a brief window where the name is free while
        // the old child is still alive, during which a second window could
        // spawn a concurrent `screencap upload <name>`. We accept that on
        // purpose: releasing eagerly lets another window take over the moment
        // the user cancels rather than stranding the recording until a wedged
        // child finally dies, and the Python terminal-stage `fcntl.flock`
        // serializes the actual destructive work — worst case is wasted
        // bandwidth + one child blocking on the flock, not corruption. This
        // registry guard only prevents obviously-wasteful concurrent spawns;
        // the flock is the real overlap safety net.
        releaseClaim()
        guard let process, process.isRunning else { return }
        process.terminate()
    }

    /// Releases this controller's cross-window registry claim (SCR-89), if it
    /// holds one. Idempotent — safe to call from every terminal/cancel path;
    /// `claimedName` is nilled so a second call is a no-op.
    private func releaseClaim() {
        if let name = claimedName {
            registry.release(name)
            claimedName = nil
        }
    }

    /// Shared cleanup for a `handleLine` terminal event (`upload_finished` /
    /// `upload_failed`): cancel the inactivity watchdog and release the
    /// cross-window claim (SCR-89). The callers keep `sawTerminalEvent = true`
    /// and the `state = ...` assignment so each case still owns its outcome.
    private func cancelWatchdogAndReleaseClaim() {
        watchdogTask?.cancel()
        watchdogTask = nil
        releaseClaim()
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
        // Release the cross-window claim (SCR-89) before the SIGTERM below —
        // the same deliberate release-before-kill ordering as `cancel()`, and
        // for the same reason (SCR-154). The watchdog fires precisely because
        // the child is wedged, so SIGTERM won't reap it synchronously; rather
        // than hold the name hostage to a stuck child, release it now so the
        // recording is claimable again — including by this window's own Retry
        // button, which re-claims via `start`. The flock backstops the brief
        // concurrent-spawn window this opens (see `cancel()` for the full
        // rationale).
        releaseClaim()
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
            cancelWatchdogAndReleaseClaim()
            state = .succeeded(.init(
                uploaded: event.uploaded ?? 0,
                skipped: event.skipped ?? 0,
                failed: event.failed ?? 0
            ))
        case "upload_failed":
            guard !sawTerminalEvent else { return }
            sawTerminalEvent = true
            cancelWatchdogAndReleaseClaim()
            state = .failed(event.error ?? "upload failed")
        case "upload_busy":
            // SCR-158: a contended terminal lock is a retryable, terminal,
            // NON-failure outcome (the child exits 0). First-write-wins like the
            // other terminal events. The user-facing copy is owned here (the
            // Python event carries only `recording` + `retryable`, no message),
            // mirroring the synthesized copy for `.refused`.
            guard !sawTerminalEvent else { return }
            sawTerminalEvent = true
            cancelWatchdogAndReleaseClaim()
            state = .busy(
                "Upload already in progress — a recording is finalizing or being "
                    + "uploaded elsewhere. Try again shortly."
            )
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
        // The child has exited, so the cross-window claim (SCR-89) is always
        // released here — idempotent if a terminal event already released it.
        releaseClaim()
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
