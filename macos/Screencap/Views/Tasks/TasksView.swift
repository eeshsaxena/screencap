import SwiftUI

// U6 — the Tasks screen: a cross-day, reverse-chronological list of the named
// task segments split out of your days (R4), fed by `/v0/tasks.query` (U5) and
// paged by a widening date window. A LOCAL substring filter narrows the loaded
// tasks (browse-verb data only — free-tier Tasks search must work, so no
// 402-gated recall verb is ever hit). Rows curate through the shared `DayTasks`
// write-through (rename/delete) with the existing retryable-writeError surfacing
// (R12), and a row click lands on its day page seeked to the task span with the
// band highlighted (AE3). Honest zero/degraded states come from the per-recording
// rollup (R21) — never a blank or a false "you did nothing"; a sealed vault
// branches before the empty check (KTD-14/KTD-20).
struct TasksView: View {
    @EnvironmentObject private var store: StoreController
    @EnvironmentObject private var recorder: RecorderController
    @EnvironmentObject private var intelligence: IntelligenceController

    /// A row click opens the day page seeked to the task span with the band
    /// highlighted (AE3): (day, seekMs, highlight).
    var onOpenTimeline: (Date, Int?, DaySpanHighlight?) -> Void
    /// The honest "set up intelligence" zero state deep-links to the Intelligence
    /// pane (R21).
    var onOpenIntelligence: () -> Void

    /// The shared task-curation write-through (R12) — the SAME cache + verbs the
    /// day page uses, so a rename here and a rename there can't drift. A failed
    /// write surfaces `writeError` (retryable) and reverts optimistic state.
    @StateObject private var dayTasks = DayTasks()

    /// U8 (R5/KTD-8) — the FREE-tier history diary search behind the same field.
    /// The local `filter` covers loaded rows; this reaches the WHOLE history via
    /// `diary.search` so a weeks-old block is findable without paging.
    @StateObject private var diary = DiarySearchModel()

    @State private var phase: LoadPhase = .loading
    @State private var response: TasksQueryResponse?
    @State private var storeState: StoreState = .mounted
    @State private var query = ""
    /// The loaded window size in days back from today; "Load older" widens it.
    @State private var windowDays = 30
    @State private var loadingOlder = false
    /// The task awaiting a rename (drives the rename sheet).
    @State private var renameTarget: TasksModel.TaskRow?
    /// U8 — block rows whose topic bullets are expanded (keyed by `TaskRow.id`).
    /// Collapsed by default (mirrors ChatView's `sourcesExpanded` disclosure).
    @State private var expandedBullets: Set<String> = []
    /// U8 — debounces the history diary search behind the search field.
    @State private var diaryTask: Task<Void, Never>?

    private enum LoadPhase: Equatable {
        case loading
        case loaded
        case error(String)
    }

    var body: some View {
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.scPaper)
            .task { await intelligence.refresh() }
            // The initial load shows the full loader; a widen ("Load older")
            // reloads quietly so the already-loaded list never flashes away.
            .task(id: windowDays) { await load(showLoading: response == nil) }
            // Light live refresh: a recording start/stop can add named tasks; keep
            // the cross-day list current without a manual reload (KTD-12 spirit).
            .onChange(of: recorder.state) { _ in
                Task { await load(showLoading: false) }
            }
            // U8 — drive the FREE-tier history diary search off the same field,
            // debounced so each keystroke doesn't fire a verb call.
            .onChange(of: query) { _ in scheduleDiarySearch() }
            .onDisappear { diaryTask?.cancel() }
            // R12 — the shared write-through's failure surfaces here with the same
            // retryable alert the day page uses, never a silent no-op.
            .alert(
                "Couldn't update the task",
                isPresented: writeErrorPresented,
                presenting: dayTasks.writeError
            ) { _ in
                Button("Retry") { Task { await dayTasks.retryLastWrite(); await load(showLoading: false) } }
                Button("Dismiss", role: .cancel) { dayTasks.dismissWriteError() }
            } message: { err in
                Text(err.message)
            }
            .sheet(item: $renameTarget) { row in
                RenameTaskSheet(currentName: row.name) { newName in
                    renameTarget = nil
                    Task {
                        let ok = await dayTasks.rename(
                            recording: row.recording, taskIndex: row.taskIndex, to: newName
                        )
                        if ok { await load(showLoading: false) }
                    }
                } onCancel: {
                    renameTarget = nil
                }
            }
    }

    @ViewBuilder
    private var content: some View {
        switch phase {
        case .loading:
            loadingState
        case .error(let message):
            errorState(message)
        case .loaded:
            // KTD-14/KTD-20: a sealed / absent / key-missing vault is a FIRST-CLASS
            // state, branched BEFORE the empty check so it never falls through to a
            // "no tasks" zero state that would misread as data loss.
            if !storeState.isMounted {
                StoreStateView(
                    storeState: storeState,
                    onUnlock: { store.unlock() },
                    onRetry: { Task { await load(showLoading: true) } },
                    onSetup: { store.initializeStore() },
                    isBusy: store.phase != .idle,
                    errorText: store.lastError
                )
            } else {
                populated
            }
        }
    }

    // MARK: - Populated

    private var verdict: IntelligenceVerdict? {
        IntelligenceVerdict.compose(probe: OnDeviceModelStatus.probe(), settings: intelligence.settings)
    }

    private var listState: TasksModel.ListState {
        TasksModel.listState(
            days: response?.days ?? [],
            recordings: response?.recordings ?? [],
            query: query,
            verdict: verdict
        )
    }

    private var populated: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.bottom, 18)
            filterField
                .padding(.bottom, 18)
            // A ScrollViewReader so a thread chip tap can scroll to a sibling
            // sitting on the SAME day page (R7 — same-day scope, no cross-day jump).
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 22) {
                        switch listState {
                        case .populated(let groups):
                            ForEach(groups) { group in
                                dayGroupView(group, proxy: proxy)
                            }
                            loadOlderButton
                        case .filterZero(let q):
                            filterZeroState(q)
                        case .systemZero(let zero):
                            systemZeroState(zero)
                        }
                        // U8 — the FREE-tier history diary results (R5/KTD-8):
                        // matches beyond the loaded window. Shown whenever a query
                        // is active and history returned hits — vital in the
                        // filter-zero case (nothing loaded matched, but a weeks-old
                        // block did).
                        diaryHistorySection
                    }
                    .padding(.bottom, 8)
                }
                .scrollContentBackground(.hidden)
            }
        }
        .padding(.horizontal, 36)
        .padding(.vertical, 28)
    }

    /// U8 — the "found across your history" section: cross-day diary-search hits
    /// (block snippet, day label, matched time) that jump to the day + block on
    /// select (deep-linked by span, KTD-2). Rendered only when a query is active
    /// and `diary.search` returned hits; a blank query or no hits hides it.
    @ViewBuilder
    private var diaryHistorySection: some View {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !needle.isEmpty, !diary.results.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text("Found across your history")
                    .font(SCTypography.serifDayHeading)
                    .foregroundStyle(Color.scInk)
                    .accessibilityAddTraits(.isHeader)
                VStack(spacing: 8) {
                    ForEach(diary.results) { result in
                        DiaryResultRowView(result: result) {
                            onOpenTimeline(result.day, result.startMs, result.highlight)
                        }
                    }
                }
            }
            .padding(.top, 4)
        }
    }

    private var header: some View {
        HStack(alignment: .center) {
            Text("Tasks")
                .font(SCTypography.screenHeading)
                .foregroundStyle(Color.scInk)
            Spacer()
        }
    }

    private var filterField: some View {
        HStack(spacing: 8) {
            Image(systemName: "line.3.horizontal.decrease.circle")
                .font(.system(size: 12))
                .foregroundStyle(Color.scInkMuted)
            TextField("Filter tasks by name", text: $query)
                .textFieldStyle(.plain)
                .font(SCTypography.sans(size: 13))
                .foregroundStyle(Color.scInk)
            if !query.isEmpty {
                Button {
                    query = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 12))
                        .foregroundStyle(Color.scInkFaint)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear filter")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .frame(maxWidth: 360, alignment: .leading)
        .background(Color.scCanvas, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.scBorderWarm, lineWidth: 1))
    }

    @ViewBuilder
    private func dayGroupView(_ group: TasksModel.DayGroup, proxy: ScrollViewProxy) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(group.label)
                .font(SCTypography.serifDayHeading)
                .foregroundStyle(Color.scInk)
                .accessibilityAddTraits(.isHeader)
            VStack(spacing: 8) {
                ForEach(group.rows) { row in
                    TaskRowView(
                        row: row,
                        bulletsExpanded: bulletsExpandedBinding(row),
                        onOpen: { onOpenTimeline(row.day, row.startMs, row.highlight) },
                        onTapThread: {
                            // R7 — same-day scope: scroll to the next sitting of
                            // this thread within the loaded day group.
                            if let sibling = TasksModel.threadSiblingId(in: group.rows, from: row) {
                                withAnimation { proxy.scrollTo(sibling, anchor: .center) }
                            }
                        },
                        onRename: { renameTarget = row },
                        onDelete: {
                            Task {
                                let ok = await dayTasks.delete(
                                    recording: row.recording, taskIndex: row.taskIndex
                                )
                                if ok { await load(showLoading: false) }
                            }
                        }
                    )
                    // The scroll target id for a thread chip tap (row-stable).
                    .id(row.id)
                }
            }
        }
    }

    /// A per-row binding into the expanded-bullets set (collapsed by default).
    private func bulletsExpandedBinding(_ row: TasksModel.TaskRow) -> Binding<Bool> {
        Binding(
            get: { expandedBullets.contains(row.id) },
            set: { expanded in
                if expanded { expandedBullets.insert(row.id) }
                else { expandedBullets.remove(row.id) }
            }
        )
    }

    /// U8 — debounce the history diary search a short beat behind typing so each
    /// keystroke doesn't fire a verb call; a cleared field clears results at once.
    private func scheduleDiarySearch() {
        diaryTask?.cancel()
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            diary.clear()
            return
        }
        diaryTask = Task {
            try? await Task.sleep(nanoseconds: 250_000_000)
            if Task.isCancelled { return }
            await diary.search(text)
        }
    }

    /// Widen the loaded window to reach older days (paging). Disabled while a
    /// widen is in flight so a double-tap can't stack reloads.
    @ViewBuilder
    private var loadOlderButton: some View {
        HStack {
            Spacer()
            Button {
                loadingOlder = true
                windowDays += 30
            } label: {
                Text(loadingOlder ? "Loading…" : "Load older days")
                    .font(SCTypography.sans(size: 12.5, weight: .medium))
                    .foregroundStyle(Color.scTeal)
            }
            .buttonStyle(.plain)
            .disabled(loadingOlder)
            Spacer()
        }
        .padding(.top, 4)
    }

    // MARK: - Zero / degraded states (R21)

    /// A filter that matched nothing among existing tasks — DISTINCT from the
    /// system-wide zero copy (R21), so it never reads as data loss.
    private func filterZeroState(_ q: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("No tasks match “\(q)”")
                .font(SCTypography.sans(size: 13.5, weight: .semibold))
                .foregroundStyle(Color.scInkSecondary)
            Text("Try a different word, or clear the filter to see every task.")
                .font(SCTypography.sans(size: 12.5))
                .foregroundStyle(Color.scInkMuted)
            Button("Clear filter") { query = "" }
                .buttonStyle(.plain)
                .font(SCTypography.sans(size: 12.5, weight: .medium))
                .foregroundStyle(Color.scTeal)
                .padding(.top, 2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 6)
    }

    /// The honest system-wide zero/degraded state from the rollup (R21).
    private func systemZeroState(_ zero: TasksModel.SystemZero) -> some View {
        VStack(spacing: SCMetrics.space4) {
            Image(systemName: zero.offersSetup ? "sparkles" : "tray")
                .font(.system(size: 30))
                .foregroundStyle(Color.scInkMuted)
            Text(zero.title)
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
                .multilineTextAlignment(.center)
            Text(zero.message)
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            if zero.offersSetup {
                Button("Set up intelligence") { onOpenIntelligence() }
                    .buttonStyle(.borderedProminent)
                    .tint(Color.scTeal)
                    .padding(.top, 2)
            } else if zero.isRetryable {
                Button("Refresh") { Task { await load(showLoading: true) } }
                    .buttonStyle(.bordered)
                    .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .center)
        .padding(.top, 24)
        .padding(SCMetrics.space4)
    }

    private var loadingState: some View {
        VStack(spacing: SCMetrics.space3) {
            ProgressView().controlSize(.large)
            Text("Loading tasks…")
                .font(SCTypography.bodyText)
                .foregroundStyle(Color.scInkSecondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ message: String) -> some View {
        VStack(spacing: SCMetrics.space4) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 34))
                .foregroundStyle(Color.scErrorFg)
            Text("Couldn't load tasks")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            Text(message)
                .font(SCTypography.metaMono)
                .foregroundStyle(Color.scInkSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Button("Retry") { Task { await load(showLoading: true) } }
                .padding(.top, SCMetrics.space1)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(SCMetrics.space8)
    }

    private var writeErrorPresented: Binding<Bool> {
        Binding(
            get: { dayTasks.writeError != nil },
            set: { if !$0 { dayTasks.dismissWriteError() } }
        )
    }

    // MARK: - Load

    /// Query the current window and store the response. Fail-open: a daemon hiccup
    /// surfaces a retryable error state rather than a blank. The vault-degraded
    /// case is NOT an error — it lands on `.loaded` with a non-mounted `storeState`
    /// so `StoreStateView` renders (KTD-14).
    private func load(showLoading: Bool) async {
        if showLoading { phase = .loading }
        let now = Date()
        let calendar = Calendar.current
        let start = calendar.date(byAdding: .day, value: -windowDays, to: calendar.startOfDay(for: now)) ?? now
        let tz = TimeZone.current.secondsFromGMT(for: now)
        do {
            let resp = try await DaemonClient.tasksQuery(
                startDate: TasksModel.dateKey(start, calendar: calendar),
                endDate: TasksModel.dateKey(now, calendar: calendar),
                tzOffsetSeconds: tz
            )
            response = resp
            storeState = resp.resolvedStoreState
            phase = .loaded
        } catch {
            phase = .error(error.localizedDescription)
        }
        loadingOlder = false
    }
}

// MARK: - Task row

/// One diary BLOCK row: the block NAME, its wall-clock range, an optional category
/// chip, a thread chip (rollup, tappable → sibling sitting), and its topic bullets
/// (expandable) — never a recording name (R5). A click on the header opens the day
/// page (AE3); the context menu curates via the shared write-through (R12). The
/// live trailing block (`isOpen`) renders provisional (mirrors `LiveTaskDraft`).
///
/// The header is a `Button(onOpen)`; the thread chip and bullets disclosure are
/// SIBLING controls (not nested Buttons) so each tap resolves unambiguously.
private struct TaskRowView: View {
    let row: TasksModel.TaskRow
    /// Collapsed-by-default bullets disclosure (mirrors ChatView's
    /// `sourcesExpanded`), owned by the parent so state survives row re-diffing.
    @Binding var bulletsExpanded: Bool
    var onOpen: () -> Void
    var onTapThread: () -> Void
    var onRename: () -> Void
    var onDelete: () -> Void

    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            headerButton
            if row.threadChipText != nil || !row.bullets.isEmpty {
                HStack(spacing: 8) {
                    if let chip = row.threadChipText { threadChip(chip) }
                    if !row.bullets.isEmpty { bulletsDisclosure }
                    Spacer(minLength: 0)
                }
            }
            if bulletsExpanded, !row.bullets.isEmpty { bulletsList }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        .overlay(
            RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                .strokeBorder(rowBorderColor, lineWidth: 1)
        )
        .onHover { hovering = $0 }
        .contextMenu {
            Button("Rename…", action: onRename)
            Button("Delete", role: .destructive, action: onDelete)
        }
    }

    /// The primary tap target: name + time + category + open indicator. A live
    /// (`isOpen`) block shows a provisional "live" marker instead of the
    /// open-day affordance (it is still being written).
    private var headerButton: some View {
        Button(action: onOpen) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(row.name)
                        .font(SCTypography.sans(size: 13.5, weight: .medium))
                        .foregroundStyle(Color.scInk)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    HStack(spacing: 8) {
                        Text(row.timeRangeText)
                            .font(SCTypography.mono(size: 11))
                            .foregroundStyle(Color.scInkMuted)
                        if let category = row.category, !category.isEmpty {
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
                if row.isOpen {
                    livePill
                } else {
                    Text("Open day →")
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scTeal)
                        .opacity(hovering ? 1 : 0.6)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(row.name), \(row.timeRangeText)")
        .accessibilityHint("Opens the day seeked to this task")
    }

    /// The provisional "live" marker for the open trailing block (mirrors the
    /// `LiveTaskDraft` provisional presentation): a small pulsing-tone dot + label.
    private var livePill: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(Color.scTeal)
                .frame(width: 6, height: 6)
            Text("live")
                .font(SCTypography.mono(size: 10))
                .foregroundStyle(Color.scTeal)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 2)
        .background(Color.scTealSoft.opacity(0.4), in: Capsule())
        .accessibilityLabel("Live — still recording")
    }

    /// The thread chip ("2 of 2 · 2h43 today", R7) — tappable to scroll to a
    /// sibling sitting on the same day. Rendered only for threads of >=2 sittings.
    private func threadChip(_ text: String) -> some View {
        Button(action: onTapThread) {
            HStack(spacing: 4) {
                Image(systemName: "link")
                    .font(.system(size: 9, weight: .semibold))
                Text(text)
                    .font(SCTypography.mono(size: 10))
            }
            .foregroundStyle(Color.scTeal)
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(Color.scTealSoft.opacity(0.4), in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Thread, \(text). Tap to go to the linked sitting.")
    }

    /// The bullets disclosure toggle — chevron + count, mirroring ChatView's
    /// `sourcesStrip` disclosure. Collapsed by default (R4 — bullets confirm the
    /// block without crowding the scan).
    private var bulletsDisclosure: some View {
        Button {
            bulletsExpanded.toggle()
        } label: {
            HStack(spacing: 5) {
                Image(systemName: bulletsExpanded ? "chevron.down" : "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
                Text(row.bullets.count == 1 ? "1 note" : "\(row.bullets.count) notes")
                    .font(SCTypography.mono(size: 10))
            }
            .foregroundStyle(Color.scInkMuted)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(bulletsExpanded ? "Hide topic notes" : "Show \(row.bullets.count) topic notes")
    }

    /// The expanded topic bullets (R4) — one line per bullet, evidence-bound prose.
    private var bulletsList: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(row.bullets.enumerated()), id: \.offset) { _, bullet in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("•")
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scInkMuted)
                    Text(bullet)
                        .font(SCTypography.sans(size: 12))
                        .foregroundStyle(Color.scInkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.leading, 2)
        .accessibilityElement(children: .combine)
    }

    /// A live block gets the teal accent border; hover keeps the teal affordance;
    /// otherwise the warm resting border.
    private var rowBorderColor: Color {
        if row.isOpen { return Color.scTeal }
        return hovering ? Color.scTeal : Color.scBorderWarm
    }
}

/// One FREE-tier history diary-search result (U8, R5/KTD-8): the matched block
/// snippet, its day label + time, and a jump into the day + block on select
/// (deep-linked by span). POINTER ONLY — never a recording name (R5).
private struct DiaryResultRowView: View {
    let result: TasksModel.DiaryResultRow
    var onOpen: () -> Void

    @State private var hovering = false

    var body: some View {
        Button(action: onOpen) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(result.snippet)
                        .font(SCTypography.sans(size: 13, weight: .medium))
                        .foregroundStyle(Color.scInk)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    Text("\(result.dayLabel) · \(result.timeText)")
                        .font(SCTypography.mono(size: 11))
                        .foregroundStyle(Color.scInkMuted)
                }
                Spacer(minLength: 0)
                Text("Open day →")
                    .font(SCTypography.sans(size: 12))
                    .foregroundStyle(Color.scTeal)
                    .opacity(hovering ? 1 : 0.6)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(Color.scSurface, in: RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
            .overlay(
                RoundedRectangle(cornerRadius: SCMetrics.radiusMd)
                    .strokeBorder(hovering ? Color.scTeal : Color.scBorderWarm, lineWidth: 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: SCMetrics.radiusMd))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(result.snippet), \(result.dayLabel)")
        .accessibilityHint("Opens the day seeked to this block")
    }
}

// MARK: - Rename sheet

/// A minimal rename sheet for a task (R12) — the flat Tasks list's inline
/// curation. Split / merge stay on the day page, where the timeline gives the
/// split point and multi-select their context.
private struct RenameTaskSheet: View {
    let currentName: String
    var onSave: (String) -> Void
    var onCancel: () -> Void

    @State private var name: String

    init(currentName: String, onSave: @escaping (String) -> Void, onCancel: @escaping () -> Void) {
        self.currentName = currentName
        self.onSave = onSave
        self.onCancel = onCancel
        _name = State(initialValue: currentName)
    }

    private var trimmed: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Rename task")
                .font(SCTypography.sectionTitle)
                .foregroundStyle(Color.scInk)
            TextField("Task name", text: $name)
                .textFieldStyle(.roundedBorder)
                .font(SCTypography.sans(size: 13))
                .onSubmit { if !trimmed.isEmpty { onSave(trimmed) } }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel, action: onCancel)
                    .keyboardShortcut(.cancelAction)
                Button("Save") { onSave(trimmed) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(trimmed.isEmpty)
            }
        }
        .padding(20)
        .frame(width: 360)
    }
}
