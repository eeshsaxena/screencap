import AVKit
import SwiftUI

// U9 — the Day timeline (design 424–470): playback pane on top, the horizontal
// day strip below with recording segments, neutral gaps, provably-blocked
// hatched bands, search-match markers, and a playhead. Seek works across
// chunked video and multiple recordings via DayPlaybackEngine (KTD-12); the
// strip's spans + honesty-split blocked intervals come from `/v0/timeline.day`
// (U3). The search field reuses the existing SearchViewModel scoped to the day.
struct DayTimelineView: View {
    @EnvironmentObject private var index: RecordingsIndex
    @Environment(\.openWindow) private var openWindow

    let date: Date
    /// Optional wall-clock anchor to land on (a Journal card / Recall hit).
    var initialSeekMs: Int?
    var onBack: () -> Void

    private enum LoadPhase: Equatable {
        case loading
        case ready
        case daemonUnavailable
    }

    @StateObject private var engine = DayPlaybackEngine()
    @StateObject private var searchModel = SearchViewModel()
    @State private var loadPhase: LoadPhase = .loading
    @State private var reloading = false
    @State private var spans: [DaySegmentRecording] = []
    @State private var query = ""
    @State private var contentIndexEnabled = false
    @State private var searchTask: Task<Void, Never>?

    var body: some View {
        VStack(spacing: 0) {
            header
            playbackPane
            strip
        }
        .background(Color.scPaper)
        .task(id: date) { await loadDay() }
        .onDisappear {
            searchTask?.cancel()
            engine.tearDown()
        }
    }

    // MARK: - Day window

    private var dayStartMs: Int { Int(Calendar.current.startOfDay(for: date).timeIntervalSince1970 * 1000) }
    private var dayEndMs: Int { dayStartMs + 86_400_000 }

    private var axisBounds: DayStripLayout.Bounds {
        DayStripLayout.axisBounds(
            dayStartMs: dayStartMs,
            spans: spans.map { (startMs: $0.startMs, endMs: $0.endMs) }
        )
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 16) {
            Button(action: onBack) {
                Text("← Journal")
                    .font(SCTypography.sans(size: 13))
                    .foregroundStyle(Color.scInkSecondary)
            }
            .buttonStyle(.plain)
            .help("Back to Journal")
            Text(Self.headerDateFormatter.string(from: date))
                .font(SCTypography.grotesk(size: 15, weight: .semibold))
                .foregroundStyle(Color.scInk)
            Spacer()
            searchField
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .background(Color.scCanvas)
        .overlay(alignment: .bottom) { Divider().overlay(Color.scBorderWarm) }
    }

    private var searchField: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 12))
                .foregroundStyle(Color.scTeal)
            TextField("Search this day", text: $query)
                .textFieldStyle(.plain)
                .font(SCTypography.sans(size: 13))
                .onSubmit { runDayScopedSearch() }
            if !dayMatchesMs.isEmpty {
                Text("\(dayMatchesMs.count) moment\(dayMatchesMs.count == 1 ? "" : "s")")
                    .font(SCTypography.mono(size: 10))
                    .foregroundStyle(Color.scTeal)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .frame(width: 300)
        .background(Color.scSurface, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.scTeal.opacity(0.4), lineWidth: 1))
    }

    // MARK: - Playback pane

    @ViewBuilder
    private var playbackPane: some View {
        ZStack {
            switch engine.target {
            case .media:
                AVPlayerNSView(player: engine.player)
            case .placeholder(let reason):
                LibraryHatchPlaceholder()
                VStack(spacing: 12) {
                    Text(placeholderCaption(reason))
                        .font(SCTypography.mono(size: 12))
                        .foregroundStyle(Color.scInkMuted)
                        .multilineTextAlignment(.center)
                    // The daemon-down caption tells the user to retry, so give
                    // them the control to do it — a stale-daemon restart (a
                    // no-op if the daemon isn't stale) followed by a reload,
                    // mirroring the Library's staleDaemonError affordance.
                    // Without this the message is an instruction with no button.
                    if loadPhase == .daemonUnavailable {
                        Button(reloading ? "Retrying…" : "Retry") { retryDay() }
                            .disabled(reloading)
                    }
                }
                .padding(.horizontal, 24)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .bottomLeading) { timestampChip }
        .overlay(alignment: .bottomTrailing) { actionButtons }
    }

    private func placeholderCaption(_ reason: DayPlaceholderReason) -> String {
        switch loadPhase {
        case .loading: return "loading day…"
        case .daemonUnavailable: return "day view needs the background helper — start ScreenCap's helper and retry"
        case .ready:
            switch reason {
            case .nothingCaptured:
                // R7 both ways: inside a recording span, activity *was*
                // captured — only the video frame is missing (e.g. the moment
                // precedes the first written frame), so "nothing captured"
                // would over-claim in the other direction.
                return playheadInsideRecordingSpan
                    ? "no video frames for this moment"
                    : "nothing captured at this time"
            // R7: the moment existed but its local media was evicted after
            // upload — "nothing captured" would be a false claim.
            case .mediaUnavailable: return "local media removed after upload"
            }
        }
    }

    private var playheadInsideRecordingSpan: Bool {
        guard let ms = engine.currentDayMs else { return false }
        return spans.contains { $0.startMs <= ms && ms < $0.endMs }
    }

    private var timestampChip: some View {
        Group {
            if let ms = engine.currentDayMs {
                Text(Self.clockFormatter.string(from: Date(timeIntervalSince1970: Double(ms) / 1000)))
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scCanvas)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Color.scInk.opacity(0.85), in: RoundedRectangle(cornerRadius: 5))
                    .padding(14)
                    .accessibilityLabel(DayStripAccessibility.playheadLabel(ms: ms))
            }
        }
    }

    private var actionButtons: some View {
        HStack(spacing: 8) {
            // Stub: SCR-219 clip-and-share a moment — rendered per the design,
            // disabled until the capability exists (KTD-8).
            Button("Clip this moment") {}
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12))
                .foregroundStyle(Color.scCanvas.opacity(0.6))
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Color.scInk.opacity(0.85), in: Capsule())
                .disabled(true)
                .help("Coming soon — SCR-219")
            if let recording = shareableRecording {
                Button("Share from here") {
                    // Upload consent stays load-bearing: sharing routes through
                    // the existing Review window flow (KTD-4).
                    openWindow(id: ReviewWindowID, value: recording)
                }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12, weight: .semibold))
                .foregroundStyle(Color.scCanvas)
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .background(Color.scTeal, in: Capsule())
                .help("Review and upload this recording")
            }
        }
        .padding(14)
    }

    /// The recording under the playhead, when it can still go through the
    /// Review-before-upload flow.
    private var shareableRecording: String? {
        guard let name = engine.currentRecording,
              let summary = index.recordings.first(where: { $0.name == name }),
              summary.isUploadEligible else { return nil }
        return name
    }

    // MARK: - Strip

    private var strip: some View {
        VStack(alignment: .leading, spacing: 0) {
            DayStripView(
                bounds: axisBounds,
                segments: stripSegments,
                blockedBands: DayStripBlockedBand.provenBands(from: spans),
                matchesMs: dayMatchesMs,
                playheadMs: engine.currentDayMs,
                onSeek: { engine.seek(toDayMs: $0) }
            )
        }
        .padding(.horizontal, 24)
        .padding(.top, 18)
        .padding(.bottom, 20)
        .background(Color.scPaper)
        .overlay(alignment: .top) { Divider().overlay(Color.scBorderWarm) }
    }

    private var stripSegments: [DayStripSegment] {
        spans.map { span in
            DayStripSegment(
                recording: span.name,
                title: index.recordings.first { $0.name == span.name }?.title ?? span.name,
                startMs: span.startMs,
                endMs: span.endMs
            )
        }
    }

    // MARK: - Search (day-scoped)

    /// Anchored search hits inside this day — the amber markers + the header's
    /// "N moments" count.
    private var dayMatchesMs: [Int] {
        guard case .loaded(let results) = searchModel.phase else { return [] }
        return results.items.compactMap { item in
            guard let ms = item.anchorMs, ms >= dayStartMs, ms < dayEndMs else { return nil }
            return ms
        }
    }

    /// Manual retry for the daemon-unavailable placeholder: attempt a
    /// stale-daemon restart (fail-safe no-op when the daemon isn't stale), then
    /// reload the day. Guarded against a double-tap racing two loads.
    private func retryDay() {
        guard !reloading else { return }
        reloading = true
        Task {
            await DaemonInstallController.restartStaleDaemonIfNeeded()
            await loadDay()
            reloading = false
        }
    }

    private func runDayScopedSearch() {
        searchTask?.cancel()
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        searchTask = Task {
            await searchModel.search(text, contentIndexEnabled: contentIndexEnabled)
        }
    }

    // MARK: - Loading

    private func loadDay() async {
        loadPhase = .loading
        if let data = try? await CLIClient.runJSONRaw(["settings", "--json"]),
           let env = try? JSONDecoder().decode(SettingsEnvelope.self, from: data) {
            contentIndexEnabled = env.settings.contentIndexEnabled ?? false
            if let dur = env.settings.chunkDuration { searchModel.chunkDurationSeconds = dur }
        }

        let dayStart = Calendar.current.startOfDay(for: date)
        let request = TimelineDayRequest(
            date: Self.wireDateFormatter.string(from: dayStart),
            tzOffsetSeconds: TimeZone.current.secondsFromGMT(for: dayStart)
        )
        do {
            let response = try await DaemonClient.timelineDay(request)
            spans = response.recordings
            loadPhase = .ready
        } catch {
            spans = []
            loadPhase = .daemonUnavailable
            return
        }

        // Build the seek map off the main actor: manifests + frame lists +
        // legacy recording.db anchors, per recording (KTD-12 direct-read).
        let inputs = spans.map { span -> (name: String, startedAtMs: Int?, durationMs: Int?) in
            let summary = index.recordings.first { $0.name == span.name }
            return (
                name: span.name,
                startedAtMs: summary?.startedAt.map { Int($0 * 1000) },
                durationMs: summary?.durationSeconds.map { Int($0 * 1000) }
            )
        }
        let chunks = await Task.detached(priority: .userInitiated) { () -> [DayPlayableChunk] in
            let root = AppPaths.recordingsRoot
            return inputs.flatMap { input in
                DayMediaLoader.loadChunks(
                    root: root,
                    recording: input.name,
                    startedAtMs: input.startedAtMs,
                    durationMs: input.durationMs
                )
            }
        }.value
        engine.load(chunks: chunks, seekToMs: initialSeekMs)
    }

    // MARK: - Formatters

    private static let headerDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        f.dateFormat = "EEEE, d MMMM"
        return f
    }()

    private static let wireDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()
}
