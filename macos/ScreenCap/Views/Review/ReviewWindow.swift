import SwiftUI

/// Stable scene id for the per-recording review `WindowGroup`. Shared by the
/// scene declaration in `ScreenCapApp` and the row-button callsite in
/// `RecordingsListView` so producer and consumers can't drift.
let ReviewWindowID = "review"

/// Per-recording review window (plan U3 scaffolded, U8 composed). A
/// `WindowGroup` scene keyed on the recording name materializes a fresh
/// window for every `openWindow(id: ReviewWindowID, value: name)` call —
/// satisfying R3 (multiple concurrent windows). The singleton `Window`
/// used by the main scene is the wrong primitive here; SCR-55 documents
/// why the two scene types are not interchangeable.
///
/// Owns a `ReviewWindowViewModel` for the entire `preparing → ready →
/// uploading → succeeded / failed` lifecycle. The video pane (U5) and the
/// timeline pane (U6) render side-by-side once the U2 envelope arrives;
/// the bottom action row swaps between Upload/Cancel, progress, success
/// confirmation, and Retry depending on the state.
struct ReviewWindow: View {
    let recordingName: String
    @EnvironmentObject private var index: RecordingsIndex
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model: ReviewWindowViewModel

    @State private var videoModel: VideoPlayerPaneModel?
    @State private var timelineEvents: [TimelineEvent] = []
    @State private var currentTime: Double = 0
    @State private var timelineLoaded = false

    init(recordingName: String) {
        self.recordingName = recordingName
        _model = StateObject(wrappedValue: ReviewWindowViewModel(
            recordingName: recordingName,
            effects: LiveReviewWindowEffects()
        ))
    }

    var body: some View {
        VStack(spacing: 0) {
            content
            Divider()
            bottomActions
        }
        .frame(minWidth: 720, minHeight: 540)
        .navigationTitle(recordingName)
        .onAppear {
            // Forward the dismiss action so the auto-close timer can fire it.
            // Set on the StateObject-preserved viewmodel rather than a
            // separate forwarder struct so SwiftUI's View-struct churn
            // doesn't replace the closure the viewmodel actually holds.
            model.dismissHandler = { dismiss() }
        }
        .task {
            await model.loadReviewData()
        }
        .onChange(of: model.state) { newState in
            if case .ready(let data) = newState, videoModel == nil {
                let engine = LiveVideoPlaybackEngine(url: data.videoURL)
                videoModel = VideoPlayerPaneModel(engine: engine)
                if !timelineLoaded {
                    timelineLoaded = true
                    Task.detached(priority: .userInitiated) {
                        let parsed = TimelineEventParser.parse(
                            url: data.eventsURL,
                            recordingStartedAt: data.startedAt
                        )
                        await MainActor.run { timelineEvents = parsed }
                    }
                }
            }
        }
        .onDisappear {
            model.windowDidClose()
        }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .preparing:
            preparingState
        case .failed(let message, let retryData) where retryData == nil:
            // Preparation failure (U2 envelope error) → no panes to render.
            // Upload-time failures fall through to the panes-with-error-row
            // layout below.
            failedPreparationState(message: message)
        default:
            panesIfAvailable
        }
    }

    private var preparingState: some View {
        VStack(spacing: 12) {
            ProgressView()
            Text("Preparing recording…")
                .font(.body)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func failedPreparationState(message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title)
                .foregroundStyle(.orange)
            Text("Couldn't prepare this recording.")
                .font(.headline)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal)
            Button("Try Again") {
                Task { await model.loadReviewData() }
            }
            .keyboardShortcut(.defaultAction)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private var panesIfAvailable: some View {
        if let videoModel {
            VStack(spacing: 0) {
                VideoPlayerPane(model: videoModel)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .onReceive(videoModel.$currentTime) { t in
                        currentTime = t
                    }
                Divider()
                TimelinePane(
                    events: timelineEvents,
                    durationSeconds: currentReviewDataDuration(),
                    currentTime: currentTime
                ) { seconds in
                    videoModel.seek(toSeconds: seconds)
                }
                .frame(height: 80)
            }
        } else {
            preparingState
        }
    }

    private func currentReviewDataDuration() -> Double {
        switch model.state {
        case .ready(let data),
             .uploading(_, let data):
            return data.durationSeconds
        case .failed(_, .some(let data)):
            return data.durationSeconds
        default:
            return 0
        }
    }

    @ViewBuilder
    private var bottomActions: some View {
        switch model.state {
        case .preparing:
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(12)
        case .ready:
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Upload") { model.startUpload() }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
            .padding(12)
        case .uploading(let progress, _):
            HStack(spacing: 12) {
                if progress.filesTotal > 0 {
                    ProgressView(value: progress.fraction) {
                        Text("Uploading \(progress.filesDone) of \(progress.filesTotal)…")
                            .font(.caption)
                    }
                    .frame(maxWidth: 320)
                } else {
                    ProgressView("Starting upload…")
                        .controlSize(.small)
                }
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(12)
        case .succeeded(let summary):
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Text("Uploaded \(summary.uploaded) file\(summary.uploaded == 1 ? "" : "s")")
                    .font(.body)
                Spacer()
                Text("Closing…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(12)
        case .failed(let message, let retryData):
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.octagon.fill")
                    .foregroundStyle(.red)
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Spacer()
                Button("Close") { dismiss() }
                if retryData != nil {
                    Button("Retry") { model.startUpload() }
                        .keyboardShortcut(.defaultAction)
                        .buttonStyle(.borderedProminent)
                }
            }
            .padding(12)
        }
    }
}

/// Production-side `ReviewWindowEffects` — bridges the viewmodel's
/// refresh / scheduling needs to live AppKit / NotificationCenter APIs.
/// Dismiss is handled directly by the viewmodel's `dismissHandler`
/// (set in `ReviewWindow.onAppear`) so this effects type stays free of
/// SwiftUI environment dependencies.
@MainActor
struct LiveReviewWindowEffects: ReviewWindowEffects {
    func refreshIndex() async {
        // The index instance lives in the SwiftUI environment of the main
        // window. The review window receives it via `.environmentObject`
        // (see ScreenCapApp). Posting a notification keeps the effects
        // surface dependency-free.
        await MainActor.run {
            NotificationCenter.default.post(name: .reviewWindowUploadSucceeded, object: nil)
        }
    }

    func scheduleAutoClose(after seconds: Double, _ action: @escaping @MainActor () -> Void) -> AutoCloseHandle {
        let task = Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
            if !Task.isCancelled {
                action()
            }
        }
        return AutoCloseHandle { task.cancel() }
    }
}

extension Notification.Name {
    /// Posted by `LiveReviewWindowEffects.refreshIndex()` so the main
    /// window's `RecordingsIndex` can refresh after a successful upload.
    /// Subscribed by the index from inside the main scene's body (added
    /// in U9).
    static let reviewWindowUploadSucceeded = Notification.Name("com.screencap.reviewWindow.uploadSucceeded")
}
