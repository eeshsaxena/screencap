import SwiftUI

// U8 — the Journal screen (design 380–421): day-grouped recording cards with
// title, summary, duration, badge, and app tag, plus the per-day
// "Open day timeline →" link (the sole day-timeline entry point until U9's
// other affordances land). Until SCR-214, Journal deliberately shows the same
// recordings as Library — its interim value is the day-grouped reading.
struct JournalView: View {
    @EnvironmentObject private var index: RecordingsIndex

    /// Header search pill (design 386) — opens the search surface (U10's Recall
    /// palette once it lands; MainWindow owns the wiring).
    var onOpenSearch: () -> Void
    /// "Open day timeline →" (design 398) — routes to U9's day view. The second
    /// argument is an optional wall-clock seek anchor (a card click lands on the
    /// recording's start; the day link lands on the day's first media).
    var onOpenTimeline: (Date, Int?) -> Void

    // One frame resolver + thumbnail cache shared across every card (not one per
    // card), mirroring LibraryView's SCR-177 wiring.
    @State private var frameIndex = RecordingFrameIndex()
    @State private var thumbnailLoader = ThumbnailLoader()
    @StateObject private var appChips = JournalAppChips()
    // U10 — one `tasks.list` per recording, shared across every card (like the
    // frame index + app chips). Populates the day-grouped task breakdown from the
    // LOCAL tasks store with no cloud round-trip.
    @StateObject private var journalTasks = JournalTasks()

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.scPaper)
    }

    @ViewBuilder
    private var content: some View {
        if index.isLoading && index.recordings.isEmpty {
            loadingState
        } else if index.lastError != nil {
            errorState
        } else if index.recordings.isEmpty {
            emptyState
        } else {
            populated
        }
    }

    // MARK: - Populated

    private var populated: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.bottom, 26)
            ScrollView {
                VStack(alignment: .leading, spacing: 34) {
                    ForEach(JournalModel.days(index.recordings)) { day in
                        daySection(day)
                    }
                }
                .padding(.bottom, 8)
            }
            .scrollContentBackground(.hidden)
        }
        .padding(.horizontal, 36)
        .padding(.vertical, 28)
    }

    private var header: some View {
        HStack(alignment: .center) {
            Text("Journal")
                .font(SCTypography.screenHeading)
                .foregroundStyle(Color.scInk)
            Spacer()
            HStack(spacing: 12) {
                // Stub: SCR-214 — the design's "ambient recording · split by the
                // agent" caption claims agent task-splitting that doesn't exist
                // yet; until then the caption states what Journal really does.
                Text("grouped by day")
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scInkMuted)
                    .help("Coming soon — SCR-214")
                searchPill
            }
        }
    }

    private var searchPill: some View {
        Button(action: onOpenSearch) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 12))
                Text("Search any moment")
                    .font(SCTypography.sans(size: 13))
                Spacer(minLength: 0)
            }
            .foregroundStyle(Color.scInkMuted)
            .padding(.horizontal, 16)
            .padding(.vertical, 9)
            .frame(width: 220)
            .background(Color.scCanvas, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help("Search your recordings")
    }

    // MARK: - Day section

    private func daySection(_ day: JournalModel.Day) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                Text(day.label)
                    .font(SCTypography.serifDayHeading)
                    .foregroundStyle(Color.scInk)
                    .accessibilityAddTraits(.isHeader)
                Text(day.countText)
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scInkMuted)
                Spacer(minLength: 0)
                if let date = day.day {
                    Button {
                        onOpenTimeline(date, nil)
                    } label: {
                        Text("Open day timeline →")
                            .font(SCTypography.sans(size: 12.5))
                            .foregroundStyle(Color.scTeal)
                            .underline()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Open day timeline for \(day.label)")
                }
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 16) {
                    ForEach(day.items, id: \.stableID) { rec in
                        JournalCard(
                            recording: rec,
                            frameIndex: frameIndex,
                            thumbnailLoader: thumbnailLoader,
                            app: appChips.app(for: rec),
                            tasks: journalTasks.tasks(for: rec),
                            onOpen: {
                                if let date = day.day {
                                    onOpenTimeline(date, rec.startedAt.map { Int($0 * 1000) })
                                }
                            }
                        )
                        .task(id: rec.stableID) {
                            await appChips.resolve(rec)
                            await journalTasks.resolve(rec)
                        }
                    }
                }
            }
        }
    }

    // MARK: - States

    private var loadingState: some View {
        VStack(spacing: SCMetrics.space3) {
            ProgressView().controlSize(.large)
            Text("Loading recordings…")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var emptyState: some View {
        VStack(spacing: SCMetrics.space4) {
            ShellLogoMark(size: 44)
            Text("Nothing recorded yet")
                .font(SCTypography.serifDayHeading)
                .foregroundStyle(Color.scInk)
            Text("Recordings land here grouped by day — start one from the Library.")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 340)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }

    private var errorState: some View {
        VStack(spacing: SCMetrics.space4) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 34))
                .foregroundStyle(Color.scErrorFg)
            Text("Couldn't load recordings")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text(index.lastError ?? "")
                .font(SCTypography.metaMono)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Button("Retry") { Task { await index.refresh() } }
                .disabled(index.isLoading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }
}

/// A Journal card (design 403–416): 290pt wide, 16/9 thumbnail with duration
/// chip, title, summary (hidden when the recording has none), and the
/// badge + app chip row. The thumbnail/badge pieces are shared with U5's
/// Library card; the summary line and app chip are Journal-only.
struct JournalCard: View {
    let recording: RecordingSummary
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    /// Dominant app for the recording's span (JournalAppChips) — chip omitted
    /// while unresolved or when the lookup failed (nullable contract).
    let app: String?
    /// The recording's locally-named task segments (U10, JournalTasks) — empty
    /// while unresolved, on a daemon miss, or when the recording has no tasks
    /// store. Feeds the day-grouped task breakdown + title/summary fallback.
    var tasks: [RecordingTask] = []
    var onOpen: () -> Void

    @State private var hovering = false

    private var badge: LibraryBadge { LibraryBadge.forRecording(recording) }

    /// Title prefers the recording's own, falling back to the first local task name when
    /// the recording is otherwise un-named (U10).
    private var title: String { JournalModel.displayTitle(recording, tasks: tasks) }

    /// The card's task breakdown — the named tasks under the summary (U10). The
    /// prototype had no per-recording task list; this reuses the card body rather
    /// than adding a new surface. Empty (section omitted) when the recording has
    /// no tasks store. Capped so a long session doesn't blow out the card.
    private var breakdown: [RecordingTask] { Array(tasks.prefix(4)) }

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 10) {
                RecordingCardThumbnail(
                    recording: recording,
                    frameIndex: frameIndex,
                    thumbnailLoader: thumbnailLoader,
                    aspectRatio: 16.0 / 9.0,
                    borderColor: .scFillSubtle
                )
                info
            }
            .padding(11)
            .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .strokeBorder(hovering ? Color.scTeal : Color.scBorderWarm, lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .frame(width: 290)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private var info: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title)
                .font(SCTypography.sans(size: 13.5, weight: .semibold))
                .foregroundStyle(Color.scInk)
                .lineLimit(1)
            if let summary = JournalModel.summaryLine(recording, tasks: tasks) {
                Text(summary)
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scInkSecondary)
                    .lineLimit(2)
            }
            taskBreakdown
            HStack(spacing: 6) {
                LibraryBadgeChip(badge: badge)
                if let app {
                    Text(app)
                        .font(SCTypography.mono(size: 10))
                        .foregroundStyle(Color.scInkMuted)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 2)
                        .overlay(
                            RoundedRectangle(cornerRadius: SCMetrics.radiusPill)
                                .strokeBorder(Color.scBorderWarm, lineWidth: 1)
                        )
                }
            }
            .padding(.top, 9)
        }
        .padding(.horizontal, 3)
        .padding(.bottom, 3)
    }

    /// The day-grouped task breakdown (U10): the recording's locally-named tasks
    /// as a compact bulleted list. Omitted entirely when there are no tasks, so a
    /// recording with no tasks store renders exactly as before.
    @ViewBuilder
    private var taskBreakdown: some View {
        if !breakdown.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(breakdown) { task in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•")
                            .font(SCTypography.sans(size: 11))
                            .foregroundStyle(Color.scInkMuted)
                        Text(task.name)
                            .font(SCTypography.sans(size: 11.5))
                            .foregroundStyle(Color.scInkSecondary)
                            .lineLimit(1)
                    }
                }
                if tasks.count > breakdown.count {
                    Text("+\(tasks.count - breakdown.count) more")
                        .font(SCTypography.mono(size: 10))
                        .foregroundStyle(Color.scInkMuted)
                }
            }
            .padding(.top, 5)
        }
    }

    private var accessibilityText: String {
        var parts = [title, recording.duration, badge.text]
        if let summary = JournalModel.summaryLine(recording, tasks: tasks) { parts.insert(summary, at: 1) }
        if let app { parts.append(app) }
        if !breakdown.isEmpty {
            parts.append("tasks: " + breakdown.map(\.name).joined(separator: ", "))
        }
        return parts.joined(separator: ", ")
    }
}
