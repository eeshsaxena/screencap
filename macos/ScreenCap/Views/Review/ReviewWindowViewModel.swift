import Combine
import Foundation
import OSLog

private let reviewLogger = Logger(subsystem: "com.screencap.macos", category: "review-window")

/// Decoded shape of the U2 `screencap review-data --json <name>` envelope.
/// Success and error variants share the `ok` discriminator; tolerant
/// decoding mirrors the CLI's `info --json` consumer pattern.
struct ReviewDataEnvelope: Decodable, Equatable {
    let ok: Bool
    let schemaVersion: Int?
    let videoPath: String?
    let eventsPath: String?
    let startedAt: Double?
    let durationSeconds: Double?
    let videoPixfmtRemediated: Bool?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case schemaVersion = "schema_version"
        case videoPath = "video_path"
        case eventsPath = "events_path"
        case startedAt = "started_at"
        case durationSeconds = "duration_seconds"
        case videoPixfmtRemediated = "video_pixfmt_remediated"
        case error
    }
}

/// Resolved review-data fields the panes consume. Built from a successful
/// envelope; absent fields fall back to safe defaults so the panes can
/// still render something usable.
///
/// `videoPixfmtRemediated` lives on `ReviewDataEnvelope` only — it's part
/// of the U2 JSON contract — and is not promoted onto this struct until a
/// consumer (e.g. a visible "remediated for playback" indicator) needs it.
struct ReviewData: Equatable {
    let videoURL: URL
    let eventsURL: URL
    let startedAt: Double
    let durationSeconds: Double
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
    func load(name: String) async throws -> ReviewDataEnvelope {
        let raw = try await CLIClient.runJSONRaw(["review-data", "--json", "--", name], timeout: 60)
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
        if case .failed = state { state = .preparing }
        do {
            let envelope = try await loader.load(name: recordingName)
            guard envelope.ok,
                  let videoPath = envelope.videoPath,
                  let eventsPath = envelope.eventsPath,
                  let startedAt = envelope.startedAt,
                  let duration = envelope.durationSeconds
            else {
                state = .failed(message: envelope.error ?? "Failed to prepare recording.", retryData: nil)
                return
            }
            let data = ReviewData(
                videoURL: URL(fileURLWithPath: videoPath),
                eventsURL: URL(fileURLWithPath: eventsPath),
                startedAt: startedAt,
                durationSeconds: duration
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
        autoCloseHandle?.cancel()
        autoCloseHandle = nil
        uploadController.cancel()
    }

    /// Called from the view's `.onDisappear`. Cancels any pending
    /// auto-close timer and aborts an in-flight upload (safe-on-idle).
    func windowDidClose() {
        cancel()
    }

    // MARK: - Internal

    private func currentReviewData() -> ReviewData? {
        switch state {
        case .ready(let data),
             .uploading(_, let data):
            return data
        case .failed(_, let retryData):
            return retryData
        case .preparing, .succeeded:
            return nil
        }
    }

    private func handleUploadStateChange(_ uploadState: UploadState) {
        switch uploadState {
        case .idle:
            return
        case .uploading(let progress):
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
            // Carry the last-known ReviewData forward so the Retry button
            // has the panes to render against without re-running U2.
            let retry = currentReviewData()
            state = .failed(message: message, retryData: retry)
        }
    }
}
