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

    // SCR-219 (U5) — clip-bounds mode. `clipMode` swaps the playback pane's
    // action buttons for the `ClipBoundsView` overlay; the rest are the working
    // selection state. `clipCenterMs` pins the playhead the clip was started
    // from so a span-picker change re-centers the fixed window on the SAME
    // instant rather than the (possibly moved) live playhead.
    @State private var clipMode = false
    @State private var clipRange: ClipRange?
    @State private var clipSpan: ClipSpan = .thirtySeconds
    @State private var clipSnappedToTask = false
    @State private var clipCenterMs = 0
    @State private var longClipNudgeDismissed = false

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
        .overlay(alignment: .bottomTrailing) { if !clipMode { actionButtons } }
        .overlay(alignment: .bottom) { clipBoundsOverlay }
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
            clipButton
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

    /// SCR-219 (U5) — "Clip this moment", enabled for a clippable recording
    /// (`isClippable`: local, non-stub, video present — R8), disabled otherwise
    /// so the design's affordance stays present. Tapping enters clip-bounds
    /// mode. Replaces the shipped disabled stub (R9).
    @ViewBuilder
    private var clipButton: some View {
        let clippable = clippableRecordingName != nil
        Button("Clip this moment") { if clippable { beginClip() } }
            .buttonStyle(.plain)
            .font(SCTypography.sans(size: 12, weight: clippable ? .semibold : .regular))
            .foregroundStyle(clippable ? Color.scCanvas : Color.scCanvas.opacity(0.6))
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .background(Color.scInk.opacity(0.85), in: Capsule())
            .disabled(!clippable)
            .help(clippable
                ? "Clip this moment to a local video file"
                : "Clipping isn't available for this recording")
    }

    /// The clip-bounds selection panel, shown at the bottom of the playback
    /// pane while `clipMode` is active. Nil-guards fall back to nothing so an
    /// edge case (recording changed out from under clip mode) can't crash.
    @ViewBuilder
    private var clipBoundsOverlay: some View {
        if clipMode,
           let range = clipRange,
           let footage = currentFootageBounds {
            ClipBoundsView(
                range: Binding(get: { clipRange ?? range }, set: { clipRange = $0 }),
                footageStartMs: footage.start,
                footageEndMs: footage.end,
                snappedToTask: clipSnappedToTask,
                span: clipSpan,
                showNudge: ClipBoundsResolver.isLongClip(range) && !longClipNudgeDismissed,
                onSelectSpan: { selectClipSpan($0) },
                onDismissNudge: { longClipNudgeDismissed = true },
                onReview: { confirmClip() },
                onCancel: { exitClipMode() }
            )
        }
    }

    /// The recording under the playhead, when it can still go through the
    /// Review-before-upload flow.
    private var shareableRecording: String? {
        guard let name = engine.currentRecording,
              let summary = index.recordings.first(where: { $0.name == name }),
              summary.isUploadEligible else { return nil }
        return name
    }

    /// The recording under the playhead, when it is clippable (R8). Distinct
    /// from `shareableRecording`: an already-uploaded (but still local)
    /// recording is clippable even though it is not upload-eligible.
    private var clippableRecordingName: String? {
        guard let name = engine.currentRecording,
              let summary = index.recordings.first(where: { $0.name == name }),
              summary.isClippable else { return nil }
        return name
    }

    /// Available footage bounds (absolute unix ms) for the recording under the
    /// playhead — the clamp window for clip bounds. A recording's day spans are
    /// unioned (a recording could surface as more than one strip segment).
    private var currentFootageBounds: (start: Int, end: Int)? {
        guard let name = engine.currentRecording else { return nil }
        let recSpans = spans.filter { $0.name == name }
        guard let start = recSpans.map(\.startMs).min(),
              let end = recSpans.map(\.endMs).max(),
              end > start else { return nil }
        return (start, end)
    }

    // MARK: - Clip bounds mode

    /// Enter clip-bounds mode: resolve the default range (snap to the labeled
    /// moment under the playhead via `tasks.list`, else a centered fixed
    /// window), then show the `ClipBoundsView` overlay (R1/R3).
    private func beginClip() {
        guard let playheadMs = engine.currentDayMs,
              let footage = currentFootageBounds else { return }
        clipCenterMs = playheadMs
        longClipNudgeDismissed = false
        Task {
            await engine.refreshTasksForCurrentRecording()
            if let task = ClipBoundsResolver.containingTask(
                tasks: engine.currentRecordingTasks, playheadMs: playheadMs
            ) {
                clipSnappedToTask = true
                clipRange = ClipBoundsResolver.momentBounds(
                    task: task, footageStartMs: footage.start, footageEndMs: footage.end
                )
            } else {
                clipSnappedToTask = false
                clipRange = ClipBoundsResolver.fixedWindow(
                    centerMs: playheadMs, span: clipSpan,
                    footageStartMs: footage.start, footageEndMs: footage.end
                )
            }
            clipMode = true
        }
    }

    /// Change the fixed-window span (fallback only) — recompute the window
    /// centered on the ORIGINAL clip center, clamped to footage (R3).
    private func selectClipSpan(_ span: ClipSpan) {
        clipSpan = span
        guard !clipSnappedToTask, let footage = currentFootageBounds else { return }
        clipRange = ClipBoundsResolver.fixedWindow(
            centerMs: clipCenterMs, span: span,
            footageStartMs: footage.start, footageEndMs: footage.end
        )
    }

    /// Confirm the bounds: carry the range out-of-band via `pendingClipRange`
    /// and open the Review window keyed on the recording name (KTD5), then exit
    /// bounds mode. The Review window (U6) consumes the range on `.ready`.
    private func confirmClip() {
        guard let name = clippableRecordingName, let range = clipRange else {
            exitClipMode()
            return
        }
        ReviewWindowOpener.shared.pendingClipRange[name] = range
        openWindow(id: ReviewWindowID, value: name)
        exitClipMode()
    }

    /// Leave clip-bounds mode without opening Review (the named Cancel, R9).
    private func exitClipMode() {
        clipMode = false
        clipRange = nil
        clipSnappedToTask = false
        longClipNudgeDismissed = false
    }

    // MARK: - Strip

    private var strip: some View {
        VStack(alignment: .leading, spacing: 0) {
            DayStripView(
                bounds: axisBounds,
                baseTracks: stripBaseTracks,
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

    /// The "unsplit — still searchable" base layer: one full-span rectangle per
    /// recording, titled from the local index (falling back to the directory
    /// name). Task bands overlay these.
    private var stripBaseTracks: [DayStripBaseTrack] {
        spans.map { span in
            DayStripBaseTrack(
                recording: span.name,
                title: index.recordings.first { $0.name == span.name }?.title ?? span.name,
                startMs: span.startMs,
                endMs: span.endMs
            )
        }
    }

    /// N agent/user task bands per recording, from the additive `tasks` field on
    /// `timeline.day` (U9) — see `DayStripSegment.bands(from:)`. A recording with
    /// no tasks contributes nothing here and renders as a plain unsplit base band.
    private var stripSegments: [DayStripSegment] {
        DayStripSegment.bands(from: spans)
    }

    // MARK: - Search (day-scoped)

    /// Anchored search hits inside this day — the amber markers + the header's
    /// "N moments" count.
    private var dayMatchesMs: [Int] {
        // U12: only `.loaded` yields markers. A lapsed user's day-scoped search
        // resolves to `.subscriptionRequired` (recall gated) → no markers, while
        // the day timeline itself (browse verb `timeline.day`) stays fully
        // available. Graceful degradation of the in-day search overlay; the
        // primary upgrade CTA lives on the Library + Recall-palette surfaces.
        // TODO(build-verify): confirm the day-scoped search field's placeholder
        // reads acceptably with zero markers when gated (no explicit CTA here).
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
