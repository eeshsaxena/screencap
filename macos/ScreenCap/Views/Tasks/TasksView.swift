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

    @State private var phase: LoadPhase = .loading
    @State private var response: TasksQueryResponse?
    @State private var storeState: StoreState = .mounted
    @State private var query = ""
    /// The loaded window size in days back from today; "Load older" widens it.
    @State private var windowDays = 30
    @State private var loadingOlder = false
    /// The task awaiting a rename (drives the rename sheet).
    @State private var renameTarget: TasksModel.TaskRow?

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
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    switch listState {
                    case .populated(let groups):
                        ForEach(groups) { group in
                            dayGroupView(group)
                        }
                        loadOlderButton
                    case .filterZero(let q):
                        filterZeroState(q)
                    case .systemZero(let zero):
                        systemZeroState(zero)
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
    private func dayGroupView(_ group: TasksModel.DayGroup) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(group.label)
                .font(SCTypography.serifDayHeading)
                .foregroundStyle(Color.scInk)
                .accessibilityAddTraits(.isHeader)
            VStack(spacing: 8) {
                ForEach(group.rows) { row in
                    TaskRowView(
                        row: row,
                        onOpen: { onOpenTimeline(row.day, row.startMs, row.highlight) },
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
                }
            }
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

/// One task row: the task NAME, its wall-clock range, and an optional category
/// chip — never a recording name (R5). A click opens the day page (AE3); the
/// context menu curates via the shared write-through (R12).
private struct TaskRowView: View {
    let row: TasksModel.TaskRow
    var onOpen: () -> Void
    var onRename: () -> Void
    var onDelete: () -> Void

    @State private var hovering = false

    var body: some View {
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
        .contextMenu {
            Button("Rename…", action: onRename)
            Button("Delete", role: .destructive, action: onDelete)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(row.name), \(row.timeRangeText)")
        .accessibilityHint("Opens the day seeked to this task")
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
