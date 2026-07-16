import SwiftUI

// U8 — the Journal screen (design 380–421): day-grouped recording cards with
// title, summary, duration, badge, and app tag, plus the per-day
// "Open day timeline →" link.
//
// SCR-214 U11 turns Journal into the curation surface for ambient task
// segments: cards render the day's agent/user tasks (via `JournalTasks`), a live
// "start a task" affordance opens an in-progress span, each task row can be
// renamed / split / merged / deleted through the write-through verbs, and a
// failed write surfaces a visible retry (never a silent divergence). A recording
// with no tasks reads as "unsplit — still searchable" rather than a blank.
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
    // U10/U11 — one `tasks.list` per recording, shared across every card, plus the
    // write-through curation layer. `liveTask` drives the in-progress "start a
    // task" span and persists it through the SAME `JournalTasks` instance so a
    // closed live task lands in the same cache the cards render.
    @StateObject private var journalTasks: JournalTasks
    @StateObject private var liveTask: LiveTaskController

    /// Draft name for the next live task (the header field).
    @State private var liveTaskName = ""

    init(
        onOpenSearch: @escaping () -> Void,
        onOpenTimeline: @escaping (Date, Int?) -> Void
    ) {
        self.onOpenSearch = onOpenSearch
        self.onOpenTimeline = onOpenTimeline
        let tasks = JournalTasks()
        _journalTasks = StateObject(wrappedValue: tasks)
        _liveTask = StateObject(wrappedValue: LiveTaskController(tasks: tasks))
    }

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.scPaper)
            .alert(
                "Task change didn't save",
                isPresented: writeErrorPresented,
                presenting: journalTasks.writeError
            ) { _ in
                Button("Retry") { Task { await journalTasks.retryLastWrite() } }
                Button("Dismiss", role: .cancel) { journalTasks.dismissWriteError() }
            } message: { err in
                Text(err.message)
            }
    }

    /// Bridges `JournalTasks.writeError` (private-set) to an `isPresented`
    /// binding; dismissing routes through `dismissWriteError()`.
    private var writeErrorPresented: Binding<Bool> {
        Binding(
            get: { journalTasks.writeError != nil },
            set: { if !$0 { journalTasks.dismissWriteError() } }
        )
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
                .padding(.bottom, liveTask.draft == nil ? 26 : 14)
            if let draft = liveTask.draft {
                LiveTaskBanner(draft: draft) { Task { await liveTask.stop() } }
                    .padding(.bottom, 20)
            }
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
                // SCR-214: ambient capture + on-device segmentation now exist, so
                // the design's caption states what Journal really does.
                Text("ambient recording · split by the agent")
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scInkMuted)
                startTaskControl
                searchPill
            }
        }
    }

    /// The live "start a task" affordance (manual creation, path a). Disabled
    /// with a tooltip when no ambient recording is running today — the span has
    /// nowhere to attach (surfaced gracefully rather than a silent no-op).
    @ViewBuilder
    private var startTaskControl: some View {
        if liveTask.draft == nil {
            let target = liveTaskTarget
            Button {
                guard let target else { return }
                let name = liveTaskName.trimmingCharacters(in: .whitespacesAndNewlines)
                Task { await liveTask.start(name: name.isEmpty ? "Untitled task" : name, recording: target.name) }
                liveTaskName = ""
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "record.circle")
                        .font(.system(size: 12))
                    Text("Start a task")
                        .font(SCTypography.sans(size: 12.5))
                }
                .foregroundStyle(target == nil ? Color.scInkMuted : Color.scTeal)
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .overlay(Capsule().strokeBorder(target == nil ? Color.scBorderWarm : Color.scTeal.opacity(0.5), lineWidth: 1))
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .disabled(target == nil)
            .help(target == nil
                ? "Ambient recording isn't running — a task needs a live stream to attach to"
                : "Mark a task span in today's stream")
        }
    }

    /// The ambient recording a live task attaches to: today's most recently
    /// started recording (best-effort). The authoritative "active ambient
    /// recording" comes from the session snapshot once U12 wires ambient state;
    /// until then the newest same-day recording is the pragmatic target.
    private var liveTaskTarget: RecordingSummary? {
        let today = Calendar.current.startOfDay(for: Date())
        return index.recordings
            .filter { $0.startedDay == today }
            .max { ($0.startedAt ?? 0) < ($1.startedAt ?? 0) }
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
                            journalTasks: journalTasks,
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

/// The in-progress live task indicator (SCR-214 U11): a slim banner that reads
/// as a "growing" provisional card — the task name, a live elapsed timer, and a
/// Stop control that closes + persists the span. Shown under the header while a
/// live task is open.
struct LiveTaskBanner: View {
    let draft: LiveTaskDraft
    var onStop: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Circle()
                .fill(Color.scTeal)
                .frame(width: 8, height: 8)
            Text(draft.name)
                .font(SCTypography.sans(size: 13, weight: .semibold))
                .foregroundStyle(Color.scInk)
                .lineLimit(1)
            // A live, self-updating elapsed timer — the "growing" span cue.
            TimelineView(.periodic(from: .now, by: 1)) { context in
                Text(Self.elapsedText(draft.elapsed(at: context.date)))
                    .font(SCTypography.mono(size: 11))
                    .foregroundStyle(Color.scInkMuted)
                    .monospacedDigit()
            }
            Text("recording task")
                .font(SCTypography.mono(size: 10))
                .foregroundStyle(Color.scInkMuted)
            Spacer()
            Button(action: onStop) {
                Text("Stop")
                    .font(SCTypography.sans(size: 12, weight: .semibold))
                    .foregroundStyle(Color.scCanvas)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 6)
                    .background(Color.scTeal, in: Capsule())
            }
            .buttonStyle(.plain)
            .help("Stop and save this task")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color.scTealSoft.opacity(0.12), in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .strokeBorder(Color.scTeal.opacity(0.35), lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Recording task \(draft.name)")
    }

    private static func elapsedText(_ seconds: TimeInterval) -> String {
        let total = Int(seconds)
        let m = total / 60
        let s = total % 60
        return String(format: "%d:%02d", m, s)
    }
}

/// A Journal card (design 403–416): 290pt wide, 16/9 thumbnail with duration
/// chip, title, summary (hidden when the recording has none), the task
/// breakdown, and the badge + app chip row.
///
/// SCR-214 U11: the task breakdown is now editable — each task row carries a
/// context menu (rename / split / merge / delete) routed through
/// `JournalTasks`' write-through layer, and a recording with no tasks renders an
/// "unsplit — still searchable" placeholder rather than an empty gap.
struct JournalCard: View {
    let recording: RecordingSummary
    let frameIndex: RecordingFrameIndex
    let thumbnailLoader: ThumbnailLoader
    /// Dominant app for the recording's span (JournalAppChips) — chip omitted
    /// while unresolved or when the lookup failed (nullable contract).
    let app: String?
    /// The shared task store — the card reads this recording's tasks from it and
    /// routes edits back through its write-through verbs.
    @ObservedObject var journalTasks: JournalTasks
    var onOpen: () -> Void

    @State private var hovering = false
    @State private var renamingTask: RecordingTask?
    /// App-wide intelligence settings — combined with the fresh on-device probe to
    /// compose the honest verdict (U5) the empty-state resolves against (U6).
    @EnvironmentObject private var intelligence: IntelligenceController

    private var badge: LibraryBadge { LibraryBadge.forRecording(recording) }

    /// The live "usable" verdict (nil while settings are still loading → "unknown").
    private var verdict: IntelligenceVerdict? {
        IntelligenceVerdict.compose(probe: OnDeviceModelStatus.probe(), settings: intelligence.settings)
    }
    /// The honest state for this recording's task display (R7 / KTD3 / KTD6).
    private var honestState: RecordingHonestState {
        RecordingHonestState.resolve(reason: journalTasks.reason(for: recording), verdict: verdict)
    }

    /// The recording's locally-named task segments, ordered by task index.
    private var tasks: [RecordingTask] { journalTasks.tasks(for: recording) }
    /// Whether the tasks have been resolved at least once — gates the empty-state
    /// placeholder so it doesn't flash before agent tasks land.
    private var resolved: Bool { journalTasks.hasResolved(recording) }

    /// Title prefers the recording's own, falling back to the first local task
    /// name when the recording is otherwise un-named (U10).
    private var title: String { JournalModel.displayTitle(recording, tasks: tasks) }

    /// The card's task breakdown — capped so a long session doesn't blow out the
    /// card.
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
        .sheet(item: $renamingTask) { task in
            TaskRenameSheet(initial: task.name) { newName in
                Task { await journalTasks.rename(recording: recording.name, taskIndex: task.taskIndex, to: newName) }
            }
        }
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

    /// The day-grouped task breakdown (U10/U11): the recording's locally-named
    /// tasks as a compact editable list, or the "unsplit — still searchable"
    /// placeholder once resolution confirms the recording has no tasks (never a
    /// blank — R10). Each row's context menu curates the task.
    @ViewBuilder
    private var taskBreakdown: some View {
        if !breakdown.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                // U6: `mechanicalOnly` HAS tasks (heuristic-named) — a banner ABOVE the
                // populated list, not an empty-state string, so it reads distinctly from
                // AI-named `produced` tasks (R7d).
                if honestState == .mechanicalOnly {
                    honestLabel("Mechanical names — set up intelligence for real task names", tone: .attention)
                }
                ForEach(breakdown) { task in
                    taskRow(task)
                }
                if tasks.count > breakdown.count {
                    Text("+\(tasks.count - breakdown.count) more")
                        .font(SCTypography.mono(size: 10))
                        .foregroundStyle(Color.scInkMuted)
                }
            }
            .padding(.top, 5)
        } else if resolved {
            honestEmptyState
                .padding(.top, 5)
        }
    }

    /// The honest empty-state message for a recording with no AI-named tasks (R7).
    /// Distinct copy per state so no empty recording is ambiguous (KTD3 / KTD6).
    @ViewBuilder
    private var honestEmptyState: some View {
        switch honestState {
        case .notSetUp:
            honestLabel("No tasks — intelligence isn't set up", tone: .attention)
        case .couldntRun:
            honestLabel("Couldn't name this recording", tone: .attention)
        case .inProgress:
            honestLabel("Still processing…", tone: .quiet)
        case .nothingToName:
            honestLabel("Nothing to name in this recording", tone: .quiet)
        case .producedTasks, .mechanicalOnly, .unknown:
            // producedTasks/mechanicalOnly never reach here (tasks are present); unknown
            // = legacy recording / older daemon → the neutral, searchable-footage copy.
            Text("unsplit — still searchable")
                .font(SCTypography.mono(size: 10.5))
                .foregroundStyle(Color.scInkMuted)
                .accessibilityLabel("Unsplit, still searchable")
        }
    }

    private enum HonestTone { case attention, quiet }
    private func honestLabel(_ text: String, tone: HonestTone) -> some View {
        Text(text)
            .font(SCTypography.mono(size: 10.5))
            .foregroundStyle(tone == .attention ? Color.scInkSecondary : Color.scInkMuted)
            .accessibilityLabel(text)
    }

    private func taskRow(_ task: RecordingTask) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("•")
                .font(SCTypography.sans(size: 11))
                .foregroundStyle(Color.scInkMuted)
            Text(task.name)
                .font(SCTypography.sans(size: 11.5))
                .foregroundStyle(Color.scInkSecondary)
                .lineLimit(1)
        }
        .contentShape(Rectangle())
        .contextMenu { taskMenu(task) }
    }

    /// Per-task curation menu (SCR-214 U11) routed through the write-through
    /// verbs. "Split in half" splits at the span midpoint (always strictly inside
    /// a non-zero span); "Merge with next" combines this task with the
    /// chronologically-following one.
    @ViewBuilder
    private func taskMenu(_ task: RecordingTask) -> some View {
        Button("Rename…") { renamingTask = task }
        Button("Split in half") {
            let mid = (task.startTs + task.endTs) / 2
            Task { await journalTasks.split(recording: recording.name, taskIndex: task.taskIndex, splitTs: mid) }
        }
        .disabled(task.endTs - task.startTs < 2)
        if let next = nextTask(after: task) {
            Button("Merge with next") {
                Task {
                    await journalTasks.merge(
                        recording: recording.name,
                        taskIndices: [task.taskIndex, next.taskIndex],
                        name: task.name
                    )
                }
            }
        }
        Divider()
        Button("Delete", role: .destructive) {
            Task { await journalTasks.delete(recording: recording.name, taskIndex: task.taskIndex) }
        }
    }

    /// The chronologically-next task after `task` (by start time), for "Merge
    /// with next". Nil when `task` is the last span.
    private func nextTask(after task: RecordingTask) -> RecordingTask? {
        tasks
            .filter { $0.taskIndex != task.taskIndex && $0.startTs >= task.startTs }
            .min { $0.startTs < $1.startTs }
    }

    private var accessibilityText: String {
        var parts = [title, recording.duration, badge.text]
        if let summary = JournalModel.summaryLine(recording, tasks: tasks) { parts.insert(summary, at: 1) }
        if let app { parts.append(app) }
        if !breakdown.isEmpty {
            parts.append("tasks: " + breakdown.map(\.name).joined(separator: ", "))
        } else if resolved {
            parts.append("unsplit, still searchable")
        }
        return parts.joined(separator: ", ")
    }
}

/// A minimal rename sheet for a task (SCR-214 U11), mirroring the recording
/// `RenameSheet` pattern. Reports the new (non-empty) name to the caller, which
/// routes it through `JournalTasks.rename`.
private struct TaskRenameSheet: View {
    let initial: String
    var onSave: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var draft: String
    @FocusState private var fieldFocused: Bool

    init(initial: String, onSave: @escaping (String) -> Void) {
        self.initial = initial
        self.onSave = onSave
        _draft = State(initialValue: initial)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Rename task")
                .font(.title2.weight(.semibold))
            TextField("Task name", text: $draft)
                .textFieldStyle(.roundedBorder)
                .focused($fieldFocused)
                .onSubmit(submit)
            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Save", action: submit)
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(24)
        .frame(minWidth: 380)
        .onAppear { fieldFocused = true }
    }

    private func submit() {
        let name = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name != initial else { dismiss(); return }
        onSave(name)
        dismiss()
    }
}
