import SwiftUI

/// Stable scene id for the per-recording inspect `WindowGroup`. Shared by the
/// scene declaration in `ScreenCapApp` and the openers/callsites so producer
/// and consumers can't drift (mirrors `ReviewWindowID`).
let InspectWindowID = "inspect"

/// Whether the inspect window's "Share / Upload…" hand-off is reachable.
///
/// Extracted as pure logic so the enabled-only-when-ready rule is unit-testable
/// without a SwiftUI render (mirrors `SearchSeek` / `TimelinePaneScrub`). The
/// hand-off opens the consent window for the recording, so it must be reachable
/// only once the recording has loaded — never during preparing/failed, where a
/// rapid click could open the upload flow for a recording that just failed to
/// load locally (R9).
enum InspectShareAffordance {
    static func isEnabled(for state: InspectState) -> Bool {
        if case .ready = state { return true }
        return false
    }
}

/// Per-recording read-only inspect window — the "just looking" surface. A
/// `WindowGroup` keyed on the recording name materializes a fresh window for
/// every `openWindow(id: InspectWindowID, value: name)` call, so multiple
/// recordings can be inspected side-by-side (R7).
///
/// It reuses the playback core (video + scrub timeline + moment-anchored event
/// content) but carries NONE of the consent machinery: no Upload row, no masked
/// "what uploads" view, no redaction/coverage evidence. The only bridge to the
/// upload world is a single low-emphasis "Share / Upload…" toolbar item that
/// hands off to the untouched review/consent window (R9/R10).
struct InspectWindow: View {
    let recordingName: String
    @StateObject private var model: InspectWindowViewModel

    @State private var videoModel: VideoPlayerPaneModel?
    @State private var timelineEvents: [TimelineEvent] = []
    @State private var currentTime: Double = 0
    @State private var timelineLoaded = false

    init(recordingName: String) {
        self.recordingName = recordingName
        _model = StateObject(wrappedValue: InspectWindowViewModel(recordingName: recordingName))
    }

    var body: some View {
        content
            .frame(minWidth: 720, minHeight: 480)
            // The window title is the recording name so two inspect windows are
            // distinguishable in the title bar, Dock, and Cmd-` cycling (R7).
            .navigationTitle(recordingName)
            .toolbar { shareToolbarItem }
            .task { await model.loadInspectData() }
            .onChange(of: model.state) { newState in
                buildVideoIfReady(newState)
            }
    }

    /// Low-emphasis hand-off to the consent window (R9/R10). A secondary toolbar
    /// item — never a prominent/primary button — enabled only once the recording
    /// has loaded. Reuses the existing `ReviewWindowOpener`, exactly as the
    /// Recordings-list Upload button reaches the consent window; inspect performs
    /// no upload itself.
    private var shareToolbarItem: some ToolbarContent {
        ToolbarItem(placement: .automatic) {
            Button {
                ReviewWindowOpener.shared.open(recordingName: recordingName)
            } label: {
                Label("Share / Upload…", systemImage: "square.and.arrow.up")
            }
            .help("Open the upload review window for this recording")
            .disabled(!InspectShareAffordance.isEnabled(for: model.state))
        }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .preparing:
            preparingState
        case .failed(let message):
            failedState(message: message)
        case .ready(let data):
            readyPanes(data: data)
        }
    }

    private var preparingState: some View {
        // No scrub runs behind inspect-data, so an indeterminate spinner with a
        // plain label is the honest interstitial — no progress detail or ETA.
        VStack(spacing: 12) {
            ProgressView()
            Text("Loading…")
                .font(.body)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// Friendly failure body for a non-technical operator (A1): a plain headline
    /// with the raw error tucked into a disclosure, plus a Try-Again that
    /// re-loads in place (the window stays open rather than vanishing).
    private func failedState(message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title)
                .foregroundStyle(.orange)
            Text("Could not load this recording.")
                .font(.headline)
                .multilineTextAlignment(.center)
            DisclosureGroup("Details") {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .frame(maxWidth: 380, alignment: .leading)
            }
            .frame(maxWidth: 360)
            Button("Try Again") {
                Task { await model.loadInspectData() }
            }
            .keyboardShortcut(.defaultAction)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }

    @ViewBuilder
    private func readyPanes(data: InspectData) -> some View {
        if let videoModel {
            VStack(spacing: 0) {
                // Renders only for a transient lock / corrupt DB; a benign
                // event-free recording reads cleanly and shows nothing here.
                TimingUnavailableCallout(status: data.timingStatus)
                // The real local video is the primary surface (no masked
                // "truth view" — that is an upload concept).
                VideoPlayerPane(model: videoModel)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .onReceive(videoModel.$currentTime) { t in currentTime = t }
                Divider()
                // Moment-anchored captured content (its own empty state covers a
                // recording with no events); selecting a row seeks the video.
                EventContentPane(
                    events: timelineEvents,
                    currentTime: currentTime
                ) { seconds in
                    videoModel.seek(toSeconds: seconds)
                }
                .frame(height: 132)
                Divider()
                // Event-tick scrub timeline — no risky-band / redaction-marker
                // overlays (those are upload-review concerns; the defaults are
                // empty, so omitting them draws ticks + cursor only).
                TimelinePane(
                    events: timelineEvents,
                    durationSeconds: data.durationSeconds,
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

    /// On first `.ready`, build the AVKit-backed playback model and, if this
    /// window was opened from a search result, seek once to the hit moment
    /// (SCR-174). With no pending seek (Recordings-list open, or an unanchored
    /// search result) the video simply opens at the start, paused (R6).
    private func buildVideoIfReady(_ newState: InspectState) {
        guard case .ready(let data) = newState, videoModel == nil else { return }
        let engine = LiveVideoPlaybackEngine(url: data.videoURL)
        let vm = VideoPlayerPaneModel(engine: engine)
        videoModel = vm

        if let seekMs = InspectWindowOpener.shared.pendingSeekMs[recordingName] {
            InspectWindowOpener.shared.pendingSeekMs[recordingName] = nil
            vm.seek(toSeconds: SearchSeek.relativeSeconds(
                anchorMs: seekMs,
                startedAt: data.startedAt,
                durationSeconds: data.durationSeconds
            ))
        }

        if !timelineLoaded {
            timelineLoaded = true
            // Parse the full local event set off the main actor (a long
            // recording's events.jsonl can be large).
            Task.detached(priority: .userInitiated) {
                let parsed = TimelineEventParser.parse(
                    urls: data.eventsURLs,
                    recordingStartedAt: data.startedAt
                )
                await MainActor.run { timelineEvents = parsed }
            }
        }
    }
}
