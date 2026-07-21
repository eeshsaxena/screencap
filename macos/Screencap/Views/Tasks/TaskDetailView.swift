import Foundation
import SwiftUI

// U2 — the dedicated task view. Opening a task lands HERE, scoped to just the
// task (reversing the day-first plan's R4), not the whole day: a player over the
// task's OWN footage (`DayPlaybackEngine` loaded with only the task-overlapping
// chunks, KTD-2), a strip whose axis covers only the task (`DayStripView` with a
// task-scoped `Bounds` + a base track / segment synthesized from the task, no
// `/v0/timeline.day` call, KTD-3), and a header with the task name / time /
// category plus "Open full day" (R7). Honest states for a live/in-progress task,
// a blocked/absent task, and a sealed vault (R6). The engine tears down on exit
// (KTD-7) before the day page spins up its own.
struct TaskDetailView: View {
    @EnvironmentObject private var index: RecordingsIndex
    @EnvironmentObject private var store: StoreController
    @EnvironmentObject private var recorder: RecorderController

    let task: TaskRouteKey
    /// Back to Moments. The host targets `.moments` explicitly — routing through
    /// `lastNonTimelineRoute` would self-loop (it becomes `.taskDetail` once this
    /// view is entered).
    var onBack: () -> Void
    /// R7 — "Open full day": the day page seeked to the task with its band
    /// highlighted (the prior R4 behavior, kept as the secondary path).
    var onOpenFullDay: () -> Void

    @StateObject private var engine = DayPlaybackEngine()
    @State private var phase: Phase = .loading
    /// Recomputed while live so the strip's end and the "– now" range track the
    /// growing tail.
    @State private var nowMs = Int(Date().timeIntervalSince1970 * 1000)
    /// The wall-clock "now" captured when the view opened — the reference the live
    /// check uses so `isLive` does NOT self-expire as `nowMs` advances (a live task
    /// viewed for minutes stays live while its recording is active).
    @State private var openedAtMs = Int(Date().timeIntervalSince1970 * 1000)
    /// Cleared on disappear so a reload started just before teardown doesn't
    /// re-arm the engine after `tearDown()`.
    @State private var isActive = true

    private enum Phase: Equatable { case loading, ready, empty }
    /// How recent the task's end must be (vs. a still-recording recording) to
    /// count as the live task, since `TasksQueryTask` carries no live flag (AE4).
    private let liveSlackMs = 120_000

    private var summary: RecordingSummary? {
        index.summary(recordingId: task.recordingId, name: task.recording)
    }

    /// A live/in-progress task: its recording is actively recording AND the task
    /// reaches the live tail. Judged against the OPEN time, not the advancing
    /// `nowMs`, so a live task stays live while its recording is active rather than
    /// lapsing after `liveSlackMs`. An old task in a still-recording recording
    /// ended well before open, so it reads as not-live. The task payload has no
    /// live flag, so the recording state (via `RecordingsIndex`) is the source
    /// (KTD-3/AE4).
    private var isLive: Bool {
        (summary?.isActivelyRecording ?? false) && task.endMs >= openedAtMs - liveSlackMs
    }

    /// The axis (and player-span) end: the task end normally, `now` while live so
    /// the growing tail stays in view.
    private var axisEndMs: Int { isLive ? max(task.endMs, nowMs) : task.endMs }

    private var bounds: DayStripLayout.Bounds {
        TaskSpanLayout.bounds(startMs: task.startMs, endMs: axisEndMs)
    }

    private var baseTrack: DayStripBaseTrack {
        DayStripBaseTrack(
            recording: task.recording,
            // The task name — never a recording title/name (R5). One task fills
            // the strip here, so the base track carries the task's own label.
            title: task.name,
            startMs: task.startMs,
            endMs: axisEndMs
        )
    }

    private var segment: DayStripSegment {
        DayStripSegment(
            recording: task.recording,
            taskIndex: task.taskIndex,
            name: task.name,
            category: task.category,
            startMs: task.startMs,
            endMs: task.endMs
        )
    }

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.scPaper)
            .task(id: task) { await load(seekToStart: true) }
            .onDisappear {
                isActive = false
                engine.tearDown()
            }
            // A recording start/stop can change the task's footage (new chunks,
            // live→final), and a vault seal/unlock flips `storeState`; reload
            // without losing the scrub position so the strip and player stay honest.
            .onChange(of: recorder.state) { _ in Task { await load(seekToStart: false) } }
            .onChange(of: index.storeState) { _ in Task { await load(seekToStart: false) } }
            // While live, advance the "now" edge and pull newly-rotated chunks in.
            // Lifecycle-managed via `.task(id:)` — cancels on disappear / isLive
            // change — so a finished task has no perpetual timer.
            .task(id: isLive) { await liveTick() }
    }

    @ViewBuilder
    private var content: some View {
        // A sealed / absent vault is a first-class state (R6), branched before any
        // footage resolution so it never reads as an empty task.
        if !index.storeState.isMounted {
            StoreStateView(
                storeState: index.storeState,
                onUnlock: { store.unlock() },
                onRetry: { Task { await load(seekToStart: false) } },
                onSetup: { store.initializeStore() },
                isBusy: store.phase != .idle,
                errorText: store.lastError
            )
        } else {
            VStack(alignment: .leading, spacing: 0) {
                header
                    .padding(.bottom, 16)
                playbackPane
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color.scCanvas, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
                    .clipShape(RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
                strip
                    .padding(.top, 14)
            }
            .padding(.horizontal, 36)
            .padding(.vertical, 28)
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .center, spacing: 14) {
            Button(action: onBack) {
                HStack(spacing: 4) {
                    Image(systemName: "chevron.left").font(.system(size: 11, weight: .semibold))
                    Text("Moments")
                }
                .font(SCTypography.sans(size: 12.5, weight: .medium))
                .foregroundStyle(Color.scTeal)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Back to Moments")

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(task.name)
                        .font(SCTypography.serifDayHeading)
                        .foregroundStyle(Color.scInk)
                        .lineLimit(1)
                    if isLive { recordingBadge }
                }
                HStack(spacing: 8) {
                    Text(timeRangeText)
                        .font(SCTypography.mono(size: 11))
                        .foregroundStyle(Color.scInkMuted)
                    if let category = task.category, !category.isEmpty {
                        Text(category)
                            .font(SCTypography.mono(size: 10))
                            .foregroundStyle(Color.scInkMuted)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 1)
                            .overlay(
                                RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                                    .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                            )
                    }
                }
            }

            Spacer(minLength: 0)

            Button(action: onOpenFullDay) {
                Text("Open full day ▸")
                    .font(SCTypography.sans(size: 12, weight: .medium))
                    .foregroundStyle(Color.white)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Color.scTeal, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Open the full day for this task")
        }
    }

    private var recordingBadge: some View {
        HStack(spacing: 4) {
            Circle().fill(Color.scErrorFg).frame(width: 6, height: 6)
            Text("Recording")
                .font(SCTypography.mono(size: 10))
                .foregroundStyle(Color.scInkMuted)
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 1)
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
        )
        .accessibilityLabel("Still recording")
    }

    /// "HH:mm–HH:mm" over the task window (never a recording name, R5); open-ended
    /// while live.
    private var timeRangeText: String {
        let start = DayStripView.boundaryTimeString(task.startMs)
        if isLive { return "\(start) – now" }
        let end = DayStripView.boundaryTimeString(task.endMs)
        return start == end ? start : "\(start)–\(end)"
    }

    // MARK: - Player + strip

    @ViewBuilder
    private var playbackPane: some View {
        ZStack {
            switch engine.target {
            case .media:
                AVPlayerNSView(player: engine.player)
            case .placeholder:
                CardHatchPlaceholder()
                Text(placeholderCaption)
                    .font(SCTypography.mono(size: 12))
                    .foregroundStyle(Color.scInkMuted)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
            }
        }
    }

    private var placeholderCaption: String {
        switch phase {
        case .loading: return "Loading…"
        case .empty: return "No footage on file for this task."
        case .ready: return "Nothing to play at this moment."
        }
    }

    private var strip: some View {
        DayStripView(
            bounds: bounds,
            baseTracks: [baseTrack],
            segments: [segment],
            blockedBands: [],
            matchesMs: [],
            playheadMs: engine.currentDayMs,
            onSeek: { engine.seek(toDayMs: $0) },
            nowMs: nowMs,
            highlightedSpan: (startMs: task.startMs, endMs: task.endMs)
        )
    }

    // MARK: - Load

    /// While live, advance the "now" edge every second and periodically pull
    /// newly-rotated chunks into the player (position preserved). Only runs while
    /// `isLive`; cancels on disappear / isLive change — no perpetual timer for a
    /// finished task.
    private func liveTick() async {
        guard isLive else { return }
        var ticks = 0
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            if Task.isCancelled { break }
            nowMs = Int(Date().timeIntervalSince1970 * 1000)
            ticks += 1
            // Every ~5s, reload so chunks rotated during recording become
            // playable — a `recorder.state` change wouldn't fire mid-recording.
            if ticks % 5 == 0 { await load(seekToStart: false) }
        }
    }

    /// Scope the player to the task's own footage: resolve the recording (by stable
    /// id first, so a rename doesn't orphan it), build its chunks, keep only those
    /// overlapping the task span, and seek — to the task start on the initial load,
    /// or the preserved playhead on a reactive reload so a background recording
    /// start/stop doesn't yank the position (KTD-2). No `/v0/timeline.day` call. A
    /// sealed vault is handled by `content`; a task with no covering local footage
    /// lands on the honest empty state.
    private func load(seekToStart: Bool) async {
        guard isActive else { return }
        nowMs = Int(Date().timeIntervalSince1970 * 1000)
        guard index.storeState.isMounted else { return }
        if index.recordings.isEmpty { await index.refresh() }
        guard let summary = index.summary(recordingId: task.recordingId, name: task.recording) else {
            phase = .empty
            return
        }
        let root = AppPaths.recordingsRoot
        // The CURRENT directory name (survives a post-stop rename), not the task
        // snapshot's possibly-stale name.
        let recording = summary.name
        let startedAtMs = summary.startedAt.map { Int($0 * 1000) }
        let durationMs = summary.durationSeconds.map { Int($0 * 1000) }
        let chunks = await Task.detached(priority: .userInitiated) {
            DayMediaLoader.loadChunks(
                root: root, recording: recording,
                startedAtMs: startedAtMs, durationMs: durationMs
            )
        }.value
        guard isActive else { return }
        let overlapping = TaskSpanLayout.overlappingChunks(
            chunks, startMs: task.startMs, endMs: axisEndMs
        )
        // Honest empty when nothing overlaps OR every overlapping chunk's media was
        // evicted (no local file) — "no footage on file", not "nothing to play".
        guard overlapping.contains(where: { $0.fileURL != nil }) else {
            phase = .empty
            return
        }
        let seekTarget = seekToStart ? task.startMs : (engine.currentDayMs ?? task.startMs)
        engine.load(chunks: overlapping, seekToMs: seekTarget)
        phase = .ready
    }
}
