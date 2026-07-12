import Combine
import Foundation
import OSLog

private let clipLogger = Logger(subsystem: "com.screencap.macos", category: "clip-export")

/// Decoded shape of one stderr line emitted by `screencap clip` (plan U3 / KTD6).
///
/// Mirrors `UploadEventLine`'s drift-resilient contract exactly: every field is
/// optional, unknown event types are ignored by the consumer, and the parser
/// returns nil for non-JSON / blank lines so the Rich progress-bar bleed the CLI
/// can put on stdout never poisons the event stream. The `screencap clip` verb
/// emits its lifecycle on the SAME channel/shape as the upload events —
/// `clip_started`, `clip_progress{frames_done, frames_total}`, `clip_done{path}`,
/// `clip_failed{reason, retryable}` — via `screencap._stderr_events.emit_event`,
/// so every payload also carries `type` / `ts` / `schema_version`.
struct ClipEventLine: Decodable, Equatable {
    let type: String
    let schemaVersion: Int?
    let ts: Double?
    let recording: String?

    // clip_started fields
    let startMs: Int?
    let endMs: Int?

    // clip_progress fields (determinate progress, R10)
    let framesDone: Int?
    let framesTotal: Int?

    // clip_done fields
    let path: String?

    // clip_failed fields
    let reason: String?
    let retryable: Bool?

    enum CodingKeys: String, CodingKey {
        case type
        case schemaVersion = "schema_version"
        case ts
        case recording
        case startMs = "start_ms"
        case endMs = "end_ms"
        case framesDone = "frames_done"
        case framesTotal = "frames_total"
        case path
        case reason
        case retryable
    }

    /// Drift-resilient parser. Returns nil on blank lines, lines that aren't
    /// JSON objects, or lines that don't decode against the schema. `type` is
    /// required; every other field is optional.
    static func parse(stderrLine: String) -> ClipEventLine? {
        let trimmed = stderrLine.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.hasPrefix("{") else { return nil }
        guard let data = trimmed.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(ClipEventLine.self, from: data)
    }
}

/// Structured reason taxonomy the `screencap clip` verb carries on `clip_failed`
/// (KTD6). The raw strings are the cross-language contract (they match the
/// Python `_fail(reason=...)` literals); a reason the app doesn't recognize maps
/// to `.unknown` so a future Python-side addition never crashes the decoder.
enum ClipFailureReason: String, Equatable {
    /// The selected `[start, end)` held no video (empty/out-of-range window).
    case noFramesInRange = "no_frames_in_range"
    /// A flag-ON (`masked_video_upload`) recording — the source chunks are rich,
    /// so the clip fails closed and writes nothing (AE3 / KTD3).
    case maskedVideoRequired = "masked_video_required"
    /// The per-recording eviction lock stayed contended — RETRYABLE (KTD6).
    case clipBusy = "clip_busy"
    /// Not a clippable recording (missing, a stub, or no local source video).
    case notEligible = "not_eligible"
    /// Catch-all trim/encode failure.
    case trimFailed = "trim_failed"
    /// The subprocess exited without a terminal clip event, or emitted a reason
    /// this app build doesn't know — a contract-violation backstop.
    case unknown

    init(rawReason: String?) {
        self = ClipFailureReason(rawValue: rawReason ?? "") ?? .unknown
    }

    /// Only `clip_busy` is retryable (KTD6). Used as the fallback when a
    /// `clip_failed` event omits an explicit `retryable` field.
    var isRetryableByReason: Bool { self == .clipBusy }

    /// Typed, user-facing copy (R10: failures are surfaced as typed states, not
    /// a generic error). Owned here so the view renders one string per reason.
    var userMessage: String {
        switch self {
        case .noFramesInRange:
            return "There's no video in the selected range to export."
        case .maskedVideoRequired:
            return "This recording's video can't be exported as a clip. Its source "
                + "was recorded with post-hoc video masking on, so clipping fails "
                + "closed rather than exporting unmasked video."
        case .clipBusy:
            return "This recording is busy finalizing right now. Try exporting the "
                + "clip again in a moment."
        case .notEligible:
            return "This recording can't be clipped — it has no local source video."
        case .trimFailed:
            return "The clip export failed. Please try again."
        case .unknown:
            return "The clip export ended unexpectedly. Please try again."
        }
    }
}

/// A typed clip-export failure (R10). Carries the reason, the user-facing copy,
/// and whether a Retry should be offered.
struct ClipFailure: Equatable {
    let reason: ClipFailureReason
    let message: String
    let retryable: Bool
}

/// State machine surface published to the review window's clip-export UI.
///
/// `exporting` carries the DETERMINATE progress (`framesDone / framesTotal`, R10)
/// the progress bar reads directly; a `framesTotal` of 0 (only `clip_started`
/// seen so far) renders an indeterminate spinner, mirroring the upload progress
/// contract.
enum ClipExportState: Equatable {
    case idle
    case exporting(progress: Progress)
    case succeeded(path: String)
    case failed(ClipFailure)
    /// User aborted the encode via Cancel. U1's atomic `.tmp` / `os.replace`
    /// guarantees an aborted encode leaves NO partial or delivered file, so this
    /// is a clean terminal with nothing to reveal.
    case cancelled

    struct Progress: Equatable {
        let framesDone: Int
        let framesTotal: Int
        /// 0.0 ... 1.0 — `framesDone / framesTotal`, or 0 when `framesTotal` is
        /// still 0 (the view renders an indeterminate spinner in that case).
        let fraction: Double
    }
}

/// Test seam over the `screencap clip` subprocess spawn + stderr line delivery,
/// so the controller is substitutable in tests. Production wiring uses
/// `LiveClipExportService`, which routes through `CLIClient.spawn` and inherits
/// the four `Foundation.Process` + `Pipe` pitfall protections documented in
/// docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md —
/// crucially, `CLIClient.spawn` ALWAYS drains stdout (readabilityHandler +
/// `readToEnd()` in the termination handler) even without an `onStdoutLine`
/// caller, so the terminal JSON envelope the CLI writes to stdout can never fill
/// the 64KB pipe buffer and wedge the child (KTD6).
@MainActor
protocol ClipExportService {
    func start(
        name: String,
        range: ClipRange,
        outPath: String,
        onLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle
}

@MainActor
final class LiveClipExportService: ClipExportService {
    /// Interactive per-recording terminal-lock timeout (seconds), handed to
    /// `screencap clip --lock-timeout`. Deliberately well under the controller's
    /// `defaultInactivityTimeoutSeconds` watchdog so a contended eviction lock
    /// raises `TerminalStageBusy` fast → the CLI emits `clip_busy` (retryable) →
    /// the controller renders a retryable `.failed`, instead of the watchdog
    /// SIGTERMing the child mid-wait and rendering a hard failure. This is the
    /// same "inner lock-timeout must sit under the outer watchdog" contract the
    /// upload path pins (docs/solutions/integration-issues/inner-timeout-
    /// unreachable-behind-outer-watchdog-2026-06-24.md); the invariant is
    /// asserted by `ClipExportControllerTests`.
    static let interactiveLockTimeoutSeconds = 30

    /// The `screencap clip` argv for the interactive export path. Extracted so
    /// the argument construction is unit-testable (the `ClipExportService` seam
    /// otherwise hides argv from a fake).
    ///
    /// `range.startMs` / `range.endMs` are absolute unix-epoch ms and are passed
    /// straight through: the `screencap clip` verb (U3) is the epoch-ms consumer
    /// (the same values handed to `review-data --clip-start-ms/--clip-end-ms`).
    /// The trailing `--` forces Click to treat the recording name as a positional
    /// even if it begins with `--` (mirrors `LiveUploadService.uploadArgs`).
    static func clipArgs(name: String, range: ClipRange, outPath: String) -> [String] {
        [
            "clip",
            "--start-ms", String(range.startMs),
            "--end-ms", String(range.endMs),
            "--out", outPath,
            "--lock-timeout", String(interactiveLockTimeoutSeconds),
            "--json",
            "--", name,
        ]
    }

    func start(
        name: String,
        range: ClipRange,
        outPath: String,
        onLine: @escaping @MainActor (String) -> Void,
        onTerminated: @escaping @MainActor (Int32) -> Void
    ) throws -> SpawnedProcessHandle {
        try CLIClient.spawn(
            args: Self.clipArgs(name: name, range: range, outPath: outPath),
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

/// Drives `screencap clip <name> --start-ms --end-ms --out --lock-timeout`,
/// parses U3's stderr lifecycle events, and publishes a small state machine the
/// review window renders determinate progress / success / typed-failure off of
/// (plan U6, KTD5/KTD6).
///
/// Deliberately auth-free and registry-free: clipping reads only local source
/// chunks (no account, no keychain), and concurrent clips of the same recording
/// serialize on the Python-side per-recording `terminal_lock` (surfacing as the
/// retryable `clip_busy`), so there is no cross-window claim to hold. This is the
/// clip sibling of `UploadController`, minus the `AccountSheetPolicy` gate and
/// the `UploadRegistry`.
///
/// The published `state` is the single decoupling seam the plan calls for
/// (docs/solutions/ui-bugs/recording-hud-frozen-on-stop-decouple-teardown-from-
/// finalization-2026-07-08.md): the progress modal observes `state` and is NEVER
/// welded to an `await` on the encode. `cancel()` SIGTERMs the child and lands a
/// terminal `.cancelled` synchronously; the long encode is not awaited anywhere.
@MainActor
final class ClipExportController: ObservableObject {
    /// Production default for the inactivity watchdog bound (seconds). Single
    /// source of truth: the `init` default references this, and the
    /// lock-timeout-under-watchdog test asserts the interactive `--lock-timeout`
    /// stays strictly under it via this same constant (no bare literal).
    static let defaultInactivityTimeoutSeconds: Double = 120

    @Published private(set) var state: ClipExportState = .idle

    private let service: ClipExportService
    private let inactivityTimeoutSeconds: Double
    private var process: SpawnedProcessHandle?
    private var sawTerminalEvent = false
    private var framesDone = 0
    private var framesTotal = 0
    /// The chosen output path, used as the `clip_done` path fallback if the event
    /// ever omits it (it shouldn't).
    private var outPathHint: String?
    /// Inactivity watchdog: reset on every parsed clip event and on terminal
    /// exit. If no event arrives within the bound, the controller surfaces a
    /// typed failure and SIGTERMs the child so a wedged encode can't hang the
    /// window forever. Reset-on-event means a healthy encode (which emits
    /// `clip_progress` per frame batch) never trips it.
    private var watchdogTask: Task<Void, Never>?

    init(
        service: ClipExportService = LiveClipExportService(),
        inactivityTimeoutSeconds: Double = ClipExportController.defaultInactivityTimeoutSeconds
    ) {
        self.service = service
        self.inactivityTimeoutSeconds = inactivityTimeoutSeconds
    }

    /// Spawns `screencap clip` for `[range.startMs, range.endMs)` into `outPath`
    /// and starts streaming events. Rejects a re-entrant start only while an
    /// export is already in flight; a fresh start from any terminal state (retry)
    /// is allowed.
    func start(name: String, range: ClipRange, outPath: String) {
        if case .exporting = state { return }
        sawTerminalEvent = false
        framesDone = 0
        framesTotal = 0
        outPathHint = outPath
        state = .exporting(progress: progressSnapshot())
        do {
            process = try service.start(
                name: name,
                range: range,
                outPath: outPath,
                onLine: { [weak self] line in self?.handleLine(line) },
                onTerminated: { [weak self] code in self?.handleTerminated(exitCode: code) }
            )
            armWatchdog()
        } catch {
            // Spawn failure (binary not found, launch failed) → terminal failure.
            sawTerminalEvent = true
            state = .failed(ClipFailure(
                reason: .trimFailed,
                message: error.localizedDescription,
                retryable: false
            ))
        }
    }

    /// Cancel (the progress modal's Cancel, or window-close-as-cancel). SIGTERMs
    /// the child; U1's atomic `.tmp` / `os.replace` guarantees the aborted encode
    /// leaves no partial or delivered file. Safe-on-idle / already-terminal: the
    /// guard avoids signalling a stale handle, and the `sawTerminalEvent` latch
    /// keeps a late `clip_done` from resurrecting success after a cancel.
    func cancel() {
        watchdogTask?.cancel()
        watchdogTask = nil
        guard let process, process.isRunning else { return }
        process.terminate()
        // Land the terminal state now (decoupled from the child's actual exit):
        // the modal dismisses off this published state, never off an await on the
        // encode. The termination handler that follows defers to this via the
        // `sawTerminalEvent` latch.
        if !sawTerminalEvent {
            sawTerminalEvent = true
            state = .cancelled
        }
    }

    // MARK: - Event handling

    private func handleLine(_ line: String) {
        guard let event = ClipEventLine.parse(stderrLine: line) else {
            // Non-JSON line (Rich progress bleed on stdout misrouted, or a blank
            // line) — drop without a state change, mirroring the upload parser.
            return
        }
        if let schemaVersion = event.schemaVersion,
           schemaVersion != SUPPORTED_API_SCHEMA_VERSION {
            clipLogger.warning(
                "Clip event schema_version=\(schemaVersion, privacy: .public) does not match SwiftUI side (\(SUPPORTED_API_SCHEMA_VERSION, privacy: .public)). Processing anyway."
            )
        }
        // Reset the inactivity watchdog on any parsed event — a progressing
        // encode keeps the timer alive.
        armWatchdog()
        switch event.type {
        case "clip_started":
            // The eligibility gate passed and the trim began. Keep an
            // (indeterminate) exporting state; `clip_progress` fills in counts.
            // Don't clobber progress that a reordered event may have set first.
            if case .exporting = state {} else {
                state = .exporting(progress: progressSnapshot())
            }
        case "clip_progress":
            if let done = event.framesDone { framesDone = done }
            if let total = event.framesTotal { framesTotal = total }
            state = .exporting(progress: progressSnapshot())
        case "clip_done":
            // First-write-wins on terminal events: a late duplicate or a
            // contradictory follow-up must not flip the settled state.
            guard !sawTerminalEvent else { return }
            sawTerminalEvent = true
            cancelWatchdog()
            state = .succeeded(path: event.path ?? outPathHint ?? "")
        case "clip_failed":
            guard !sawTerminalEvent else { return }
            sawTerminalEvent = true
            cancelWatchdog()
            let reason = ClipFailureReason(rawReason: event.reason)
            // Honor the event's explicit `retryable` when present (the Python
            // source of truth); fall back to the reason-derived default (only
            // `clip_busy` retryable) for an older CLI that omits it.
            let retryable = event.retryable ?? reason.isRetryableByReason
            state = .failed(ClipFailure(
                reason: reason,
                message: reason.userMessage,
                retryable: retryable
            ))
        default:
            // Unknown event type — silently ignore (a future additive event
            // needs no Swift bump).
            return
        }
    }

    private func handleTerminated(exitCode: Int32) {
        process = nil
        cancelWatchdog()
        // Defer to a terminal event already received (the terminationHandler may
        // run after `clip_done` / `clip_failed`, or after `cancel()`).
        guard !sawTerminalEvent else { return }
        // `screencap clip` ALWAYS exits 0 and emits a terminal event, so reaching
        // here means the contract was violated (a crash before any event). Map it
        // to a typed failure carrying the exit code rather than staying wedged on
        // `.exporting`.
        sawTerminalEvent = true
        state = .failed(ClipFailure(
            reason: .unknown,
            message: "clip export exited with code \(exitCode)",
            retryable: false
        ))
    }

    // MARK: - Watchdog

    private func armWatchdog() {
        watchdogTask?.cancel()
        let bound = inactivityTimeoutSeconds
        watchdogTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, bound) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await MainActor.run { self?.fireInactivityTimeout() }
        }
    }

    private func cancelWatchdog() {
        watchdogTask?.cancel()
        watchdogTask = nil
    }

    private func fireInactivityTimeout() {
        // Only fire if still exporting and no terminal state has landed — a late
        // timer firing during a race must not clobber a fresh terminal state.
        guard !sawTerminalEvent, case .exporting = state else { return }
        sawTerminalEvent = true
        process?.terminate()
        state = .failed(ClipFailure(
            reason: .trimFailed,
            message: "The clip export stopped responding and was cancelled.",
            retryable: false
        ))
    }

    private func progressSnapshot() -> ClipExportState.Progress {
        let fraction: Double
        if framesTotal > 0 {
            fraction = min(1, max(0, Double(framesDone) / Double(framesTotal)))
        } else {
            fraction = 0
        }
        return .init(framesDone: framesDone, framesTotal: framesTotal, fraction: fraction)
    }
}
