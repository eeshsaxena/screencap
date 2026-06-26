import Combine
import Foundation
import OSLog

private let inspectLogger = Logger(subsystem: "com.screencap.macos", category: "inspect-window")

/// Resolved inspect-data fields the read-only inspect window's panes consume.
///
/// The no-scrub sibling of `ReviewData`: the local navigation video plus the
/// local events and the timeline coordinate space — and deliberately NOTHING
/// from the upload world. There is no masked-screenshot set, no redaction
/// evidence, and no coverage disclosure here, because inspect is "just looking"
/// at the local recording, not reviewing an upload payload.
///
/// `eventsURLs` MAY be empty: inspect is video-first, so a recording with no
/// action events still opens (you can watch it) — the timeline simply renders
/// its empty state.
struct InspectData: Equatable {
    let videoURL: URL
    let eventsURLs: [URL]
    let startedAt: Double
    let durationSeconds: Double
    let timingStatus: ReviewTimingStatus
}

/// Read-only state machine for the inspect window. Deliberately just three
/// states — there is no `uploading` / `succeeded` / `refused` / `busy` here,
/// no `UploadController`, and no auto-close/refresh effects. That absence is the
/// whole point of the separate surface: looking carries none of the consent
/// machinery the review window does.
enum InspectState: Equatable {
    case preparing
    case ready(InspectData)
    case failed(message: String)
}

/// Test seam over the `inspect-data` CLI fetch, mirroring `ReviewDataLoader`.
@MainActor
protocol InspectDataLoader {
    func load(name: String) async throws -> ReviewDataEnvelope
}

@MainActor
final class LiveInspectDataLoader: InspectDataLoader {
    /// `inspect-data` runs NO scrub (the whole reason it exists), so the
    /// generous 600s `review-data` ceiling is unwarranted — a recording
    /// prepares in normal CLI latency. The bound is kept comfortably above
    /// worst-case in-process chunk concat for a long recording, while still
    /// surfacing a genuinely hung prepare as `.failed` rather than hanging the
    /// looking surface forever.
    static let inspectDataTimeout: TimeInterval = 120

    func load(name: String) async throws -> ReviewDataEnvelope {
        let raw = try await CLIClient.runJSONRaw(
            ["inspect-data", "--json", "--", name],
            timeout: Self.inspectDataTimeout
        )
        // Decoded with the shared envelope type — `inspect-data` emits the same
        // schema (with redaction/coverage null, screenshots empty), so the
        // tolerant `ReviewDataEnvelope` decoder applies unchanged.
        return try JSONDecoder().decode(ReviewDataEnvelope.self, from: raw)
    }
}

@MainActor
final class InspectWindowViewModel: ObservableObject {
    @Published private(set) var state: InspectState = .preparing

    let recordingName: String
    private let loader: InspectDataLoader

    init(
        recordingName: String,
        loader: InspectDataLoader = LiveInspectDataLoader()
    ) {
        self.recordingName = recordingName
        self.loader = loader
    }

    /// Drives `preparing → ready` (or `preparing → failed`). Called from the
    /// view's `.task` on first appear, and re-entrant on a Try-Again retry.
    func loadInspectData() async {
        // Re-entrancy: a retry after a failure comes back through here; reset to
        // preparing so the spinner shows while the second call runs.
        if case .failed = state { state = .preparing }
        do {
            let envelope = try await loader.load(name: recordingName)
            // Inspect is video-first: gate readiness on `ok` + the VIDEO only.
            // Unlike review (which also requires events for the upload payload),
            // a recording with no events is still worth looking at, so the
            // events path is NOT part of the guard. Null timing is likewise not
            // a failure (SCR-102) — fall back to 0, which the panes handle.
            guard envelope.ok, let videoPath = envelope.videoPath else {
                state = .failed(
                    message: envelope.error ?? "Could not load this recording."
                )
                return
            }
            let eventsURLs: [URL]
            if let paths = envelope.eventsPaths {
                eventsURLs = paths.map { URL(fileURLWithPath: $0) }
            } else if let single = envelope.eventsPath {
                eventsURLs = [URL(fileURLWithPath: single)]
            } else {
                eventsURLs = []
            }
            let data = InspectData(
                videoURL: URL(fileURLWithPath: videoPath),
                eventsURLs: eventsURLs,
                startedAt: envelope.startedAt ?? 0,
                durationSeconds: envelope.durationSeconds ?? 0,
                timingStatus: ReviewTimingStatus.resolve(
                    status: envelope.timingStatus, legacyError: envelope.timingError
                )
            )
            state = .ready(data)
        } catch {
            inspectLogger.error(
                "inspect-data load failed: \(error.localizedDescription, privacy: .public)"
            )
            state = .failed(message: error.localizedDescription)
        }
    }
}
