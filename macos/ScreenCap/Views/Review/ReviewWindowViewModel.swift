import Combine
import Foundation
import OSLog

private let reviewLogger = Logger(subsystem: "com.screencap.macos", category: "review-window")

/// Per-moment redaction marker (R8) — export-safe: a timestamp + the reason
/// category that fired, never the redacted value.
struct ReviewMarker: Decodable, Equatable {
    let t: Double
    let category: String
}

/// A risky-moment interval (R13) the scrubber acted on. `end` is nil for an
/// open-ended interval (Python serialized `inf` as JSON null).
struct ReviewBlockedInterval: Decodable, Equatable {
    let start: Double
    let end: Double?
    let action: String
    let reason: String
}

/// A fail-closed marker (R14) — content the scrubber couldn't analyze and
/// removed to be safe. Timestamp + surface only.
struct ReviewFailClosed: Decodable, Equatable {
    let t: Double
    let surface: String?
}

/// Two-level redaction evidence (R8/R13/R14). All fields optional so a
/// recording with no redactions still decodes; the panes treat absence as
/// "nothing required redaction".
struct ReviewRedaction: Decodable, Equatable {
    let summary: [String: Int]?
    let markers: [ReviewMarker]?
    let blockedIntervals: [ReviewBlockedInterval]?
    let failClosed: [ReviewFailClosed]?

    enum CodingKeys: String, CodingKey {
        case summary, markers
        case blockedIntervals = "blocked_intervals"
        case failClosed = "fail_closed"
    }
}

/// Structured R9 coverage facts the transparency UI renders honest copy from
/// (rather than hardcoding strings). All optional → "unknown" when absent.
struct ReviewCoverage: Decodable, Equatable {
    let videoLocalOnly: Bool?
    let audioLocalOnly: Bool?
    let transcriptUploadedScrubbed: Bool?
    let screenshotsUploaded: Bool?
    let allowedAppScreenshotPiiManualReview: Bool?

    enum CodingKeys: String, CodingKey {
        case videoLocalOnly = "video_local_only"
        case audioLocalOnly = "audio_local_only"
        case transcriptUploadedScrubbed = "transcript_uploaded_scrubbed"
        case screenshotsUploaded = "screenshots_uploaded"
        case allowedAppScreenshotPiiManualReview = "allowed_app_screenshot_pii_manual_review"
    }
}

/// Decoded shape of the `screencap review-data --json <name>` envelope
/// (schema v2). Success and error variants share the `ok` discriminator;
/// tolerant decoding mirrors the CLI's `info --json` consumer pattern.
///
/// The U3 enrichment fields (`eventsPaths`, `screenshots`, `redaction`,
/// `coverage`) are all Optional with safe defaults so an older/minimal
/// envelope still decodes and reaches `.ready` — readiness is never gated on
/// them (see the nullable-timing learning).
struct ReviewDataEnvelope: Decodable, Equatable {
    let ok: Bool
    let schemaVersion: Int?
    let videoPath: String?
    let eventsPath: String?
    let startedAt: Double?
    let durationSeconds: Double?
    let videoPixfmtRemediated: Bool?
    let error: String?
    // Defaulted so existing call sites (and minimal/older envelopes) need not
    // supply them; Optional → a missing JSON key decodes to nil.
    var eventsPaths: [String]? = nil
    var screenshots: [String]? = nil
    var redaction: ReviewRedaction? = nil
    var coverage: ReviewCoverage? = nil

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case videoPath = "video_path"
        case eventsPath = "events_path"
        case startedAt = "started_at"
        case durationSeconds = "duration_seconds"
        case videoPixfmtRemediated = "video_pixfmt_remediated"
        case error
        case eventsPaths = "events_paths"
        case screenshots
        case redaction
        case coverage
    }
}

/// Resolved review-data fields the panes consume. Built from a successful
/// envelope; absent fields fall back to safe defaults so the panes can
/// still render something usable.
///
/// `videoURL` is the LOCAL navigation video (never uploaded); `eventsURLs`
/// and `screenshotURLs` point at the scrubbed copy — the bytes that actually
/// upload (R15). `redaction`/`coverage` drive the transparency UI (U8).
struct ReviewData: Equatable {
    let videoURL: URL
    /// The full scrubbed event file set (per-chunk when chunked) — what
    /// upload ships and U7 parses. Always at least one path.
    let eventsURLs: [URL]
    /// Scrubbed (masked) screenshots — the "what actually uploads" visual (U6).
    let screenshotURLs: [URL]
    let startedAt: Double
    let durationSeconds: Double
    let redaction: ReviewRedaction?
    let coverage: ReviewCoverage?
}

/// Test seam over `CLIClient.runJSONRaw` so the review-data fetch can be
/// faked. The production wiring uses `LiveReviewDataLoader` which calls
/// the bundled CLI.
@MainActor
protocol ReviewDataLoader {
    func load(name: String) async throws -> ReviewDataEnvelope
}

@MainActor
final class LiveReviewDataLoader: ReviewDataLoader {
    /// `review-data` now runs the NER scrub before returning (U2), which on a
    /// long recording can take minutes. The old 60s ceiling would SIGTERM the
    /// scrub mid-pass — R4's preparing state cannot rescue a hard kill — so it
    /// is raised to a generous fixed ceiling. A genuinely hung scrub still
    /// surfaces the failed state via this timeout rather than hanging forever.
    static let reviewDataTimeout: TimeInterval = 600

    func load(name: String) async throws -> ReviewDataEnvelope {
        let raw = try await CLIClient.runJSONRaw(
            ["review-data", "--json", "--", name],
            timeout: Self.reviewDataTimeout
        )
        return try JSONDecoder().decode(ReviewDataEnvelope.self, from: raw)
    }
}

/// Test seam over the auto-close timer + index refresh dispatch so the
/// viewmodel's wall-clock dependencies can be controlled deterministically.
@MainActor
protocol ReviewWindowEffects {
    /// Refreshes the shared `RecordingsIndex` after a successful upload so
    /// the source row's eligibility predicate flips. U9 wires this through.
    func refreshIndex() async
    /// Schedules a closure to run after `seconds`. Returns a cancel handle
    /// the viewmodel calls if the window is dismissed before the timer
    /// fires.
    func scheduleAutoClose(after seconds: Double, _ action: @escaping @MainActor () -> Void) -> AutoCloseHandle
}

/// Opaque handle the viewmodel uses to cancel a pending auto-close.
final class AutoCloseHandle {
    private let cancelClosure: () -> Void
    private var cancelled = false

    init(cancel: @escaping () -> Void) {
        self.cancelClosure = cancel
    }

    func cancel() {
        guard !cancelled else { return }
        cancelled = true
        cancelClosure()
    }
}

/// Top-level state machine for the review window (plan U8).
enum ReviewState: Equatable {
    case preparing
    case ready(ReviewData)
    case uploading(progress: UploadState.Progress, data: ReviewData)
    case succeeded(summary: UploadState.Summary)
    case failed(message: String, retryData: ReviewData?)
    /// Cross-window refusal (SCR-155): another window already owns this
    /// recording's upload. Distinct from `.failed` so the action row offers
    /// honest "another window is handling it" copy and a Close — never a Retry
    /// that would silently re-refuse. `data` is carried so the review panes
    /// still render; only the bottom action row differs.
    case refused(message: String, data: ReviewData?)
}

@MainActor
final class ReviewWindowViewModel: ObservableObject {
    @Published private(set) var state: ReviewState = .preparing

    let recordingName: String
    let uploadController: UploadController

    /// Set by the view's `.onAppear` so the auto-close timer can dismiss
    /// the window without the viewmodel depending on a SwiftUI
    /// `@Environment(\.dismiss)` reference. Lives on the viewmodel rather
    /// than a separate forwarder so it shares the StateObject's preserved
    /// identity (a separate forwarder would be reinstantiated on every
    /// View struct re-init, diverging from the one the viewmodel holds).
    var dismissHandler: (() -> Void)?

    private let loader: ReviewDataLoader
    private let effects: ReviewWindowEffects
    private let autoCloseSeconds: Double
    private var uploadObserver: AnyCancellable?
    private var autoCloseHandle: AutoCloseHandle?

    init(
        recordingName: String,
        uploadController: UploadController = UploadController(),
        loader: ReviewDataLoader = LiveReviewDataLoader(),
        effects: ReviewWindowEffects,
        autoCloseSeconds: Double = 2.0
    ) {
        self.recordingName = recordingName
        self.uploadController = uploadController
        self.loader = loader
        self.effects = effects
        self.autoCloseSeconds = autoCloseSeconds

        uploadObserver = uploadController.$state
            .sink { [weak self] uploadState in
                self?.handleUploadStateChange(uploadState)
            }
    }

    deinit {
        // Auto-close handle cleanup runs from `dismissed()` on MainActor;
        // deinit just nils the Combine subscription to break the retain
        // chain on the upload controller's publisher.
        uploadObserver?.cancel()
    }

    /// Called from the view's `.task` modifier on first appear. Drives the
    /// `preparing → ready` transition (or `preparing → failed` if the U2
    /// envelope errors).
    func loadReviewData() async {
        // Re-entrancy guard: if the user retried after a prep failure, we
        // come through here again; clear the failure surface back to
        // preparing so the spinner is visible while the second call runs.
        // Only `.failed` is reset: `.refused` (SCR-155) arises post-`ready`
        // from the upload flow, never during this `.task`-driven prep, so it
        // is never the live state when `loadReviewData` runs. A future caller
        // that re-enters from `.refused` must decide whether re-prep is wanted.
        if case .failed = state { state = .preparing }
        do {
            let envelope = try await loader.load(name: recordingName)
            // `started_at` / `duration_seconds` are legitimately null for a
            // playable recording with no action events (review.py →
            // `_read_recording_meta` returns None). They must NOT gate the
            // guard — null timing is not a preparation failure. Fall back to
            // origin 0 / unknown-duration 0, which the panes already handle
            // (TimelinePane guards `durationSeconds > 0`). `.failed` stays
            // reserved for `ok: false` / missing-path envelopes. (SCR-102)
            guard envelope.ok,
                  let videoPath = envelope.videoPath,
                  let eventsPath = envelope.eventsPath
            else {
                state = .failed(message: envelope.error ?? "Failed to prepare recording.", retryData: nil)
                return
            }
            // The full scrubbed event set drives U7; fall back to the single
            // primary path when an older envelope omits `events_paths`.
            let eventsURLs = (envelope.eventsPaths?.map { URL(fileURLWithPath: $0) })
                .flatMap { $0.isEmpty ? nil : $0 }
                ?? [URL(fileURLWithPath: eventsPath)]
            let screenshotURLs = (envelope.screenshots ?? []).map { URL(fileURLWithPath: $0) }
            let data = ReviewData(
                videoURL: URL(fileURLWithPath: videoPath),
                eventsURLs: eventsURLs,
                screenshotURLs: screenshotURLs,
                startedAt: envelope.startedAt ?? 0,
                durationSeconds: envelope.durationSeconds ?? 0,
                redaction: envelope.redaction,
                coverage: envelope.coverage
            )
            state = .ready(data)
        } catch {
            reviewLogger.error("review-data load failed: \(error.localizedDescription, privacy: .public)")
            state = .failed(message: error.localizedDescription, retryData: nil)
        }
    }

    /// Upload-button action. No-op if the viewmodel isn't in a ready /
    /// failed (with retry data) state.
    func startUpload() {
        // A new upload supersedes any pending success auto-close (SCR-90): if
        // the user re-uploads from a success confirmation, the old dismiss must
        // not fire against the new in-flight window.
        disarmAutoClose()
        let data = currentReviewData()
        guard let data else { return }
        state = .uploading(progress: .init(filesDone: 0, filesTotal: 0, fraction: 0), data: data)
        uploadController.start(name: recordingName)
    }

    /// Cancel button or window close. Sends SIGTERM if an upload is in
    /// flight; the success / failure transitions land via the upload
    /// state observer. Safe-on-idle (the controller's cancel guard checks
    /// isRunning).
    func cancel() {
        disarmAutoClose()
        uploadController.cancel()
    }

    /// Called from the view's `.onDisappear`. Cancels any pending
    /// auto-close timer and aborts an in-flight upload (safe-on-idle).
    func windowDidClose() {
        cancel()
    }

    // MARK: - Internal

    /// Cancels and clears any pending success auto-close timer (SCR-90).
    /// Idempotent — `AutoCloseHandle.cancel()` is a no-op once fired/cancelled
    /// and the handle is nilled, so repeated calls are safe.
    private func disarmAutoClose() {
        autoCloseHandle?.cancel()
        autoCloseHandle = nil
    }

    private func currentReviewData() -> ReviewData? {
        switch state {
        case .ready(let data),
             .uploading(_, let data):
            return data
        case .failed(_, let retryData):
            return retryData
        case .refused(_, let data):
            return data
        case .preparing, .succeeded:
            // `.succeeded` intentionally returns nil: an upload already
            // completed, so there is no ready/retry payload to re-enter
            // `.uploading` with. See the SCR-90 note in
            // `handleUploadStateChange`'s `.uploading` branch.
            return nil
        }
    }

    private func handleUploadStateChange(_ uploadState: UploadState) {
        switch uploadState {
        case .idle:
            return
        case .uploading(let progress):
            // Disarm any pending success auto-close (SCR-90). This fires on
            // every `.uploading` event (progress ticks included), but
            // `disarmAutoClose()` is idempotent, so repeated calls during a
            // live upload are no-ops; the case that matters is the first
            // `.uploading` after `.succeeded`, which cancels the stale dismiss
            // timer before a restarted upload would render.
            disarmAutoClose()
            // Arriving here straight from `.succeeded` disarms but does NOT
            // advance the state: `currentReviewData()` returns nil for
            // `.succeeded`, so the guard below is skipped and the viewmodel
            // stays `.succeeded`. That restart-from-success path is not
            // user-reachable today (the success screen exposes no re-upload
            // affordance; the controller is first-write-wins), so this is
            // defensive-only — the disarm above is the part that matters.
            if let data = currentReviewData() {
                state = .uploading(progress: progress, data: data)
            }
        case .succeeded(let summary):
            state = .succeeded(summary: summary)
            // Fire-and-forget refresh: the source row's eligibility predicate
            // flips off `uploaded` (R2 / U4); without this, the row would
            // keep its Upload button until the next manual reload.
            Task { await effects.refreshIndex() }
            // Auto-close after a brief confirmation, per R11. The handle
            // lets a window-close-during-confirmation cancel the dismiss
            // dispatch so it doesn't fire against an already-gone window.
            autoCloseHandle = effects.scheduleAutoClose(after: autoCloseSeconds) { [weak self] in
                self?.dismissHandler?()
            }
        case .failed(let message):
            // Disarm any pending success auto-close (SCR-90): a late failure
            // after `.succeeded` must not auto-dismiss the failure window out
            // from under the user.
            disarmAutoClose()
            // Carry the last-known ReviewData forward so the Retry button
            // has the panes to render against without re-running U2.
            let retry = currentReviewData()
            state = .failed(message: message, retryData: retry)
        case .refused(let message):
            // SCR-155: a cross-window claim refusal is NOT a genuine upload
            // failure — another window already owns this recording's upload.
            // Surface it as a distinct, non-retryable state with honest copy
            // rather than a `.failed` whose enabled Retry would silently
            // re-refuse while the owner holds the claim. Carry the panes data
            // forward (the viewmodel is `.uploading` here, from the optimistic
            // `startUpload`) so the window still renders the review content.
            disarmAutoClose()
            state = .refused(message: message, data: currentReviewData())
        }
    }
}
